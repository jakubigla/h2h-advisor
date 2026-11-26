"""Advisor service: fetch-or-cache over Yahoo and BBM, then analyse.

Every public method returns an ``Envelope`` (data, per-source ages, notices); the MCP
tools wrap them one-to-one. One public call is one "call": it opens at most one
browser session, lazily, the first time something stale must be fetched, and loads
each Yahoo URL at most once (pages are memoised for the call). Failures degrade to
cached data plus a notice; nothing is retried within a call.
"""

from __future__ import annotations

import dataclasses
import re
import threading
import types
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints
from zoneinfo import ZoneInfo

from h2h_advisor.analysis.build import (
    Build,
    at_risk_strong,
    build_from_config,
    category_weights,
    infer_build,
    matchup_result,
    one_win_targets,
    resolve_build,
)
from h2h_advisor.analysis.outlook import (
    WeekAnalysis,
    WeekContext,
    remaining_days,
)
from h2h_advisor.analysis.outlook import analyze_week as analyze_week_ctx
from h2h_advisor.analysis.projection import (
    INACTIVE_SLOTS,
    OUT_STATUSES,
    lineup_by_day,
    projected_rest,
)
from h2h_advisor.analysis.streamers import (
    StreamerOption,
    activate_il,
    drop_pool,
    effective_start,
)
from h2h_advisor.analysis.streamers import long_term_upgrades as long_term_upgrades_ctx
from h2h_advisor.analysis.streamers import plan_adds as plan_adds_ctx
from h2h_advisor.analysis.streamers import rank_streamers as rank_streamers_ctx
from h2h_advisor.browser.ratelimit import DailyCapReached
from h2h_advisor.browser.session import (
    BrowserBusy,
    BrowserSession,
    FetchFailed,
    NeedsLogin,
)
from h2h_advisor.cache import Cache
from h2h_advisor.config import Config
from h2h_advisor.identity import PlayerIndex, normalize_name, reconcile_keys
from h2h_advisor.models import (
    NINE_CAT,
    Category,
    FreeAgent,
    LeagueSettings,
    Matchup,
    Player,
    RosterEntry,
    StatLine,
)
from h2h_advisor.sources.bbm.parsers import BbmPage, BbmRow
from h2h_advisor.sources.bbm.source import BbmSource, BbmStateError
from h2h_advisor.sources.errors import ParseError
from h2h_advisor.sources.espn import EspnError, EspnSource, RecentMinutes
from h2h_advisor.sources.schedule import Schedule
from h2h_advisor.sources.yahoo.parsers import LeagueRef
from h2h_advisor.sources.yahoo.source import YahooSource

# Fantasy days follow the NBA calendar (US Eastern), like BBM's day pages.
NBA_TZ = ZoneInfo("America/New_York")
REFRESH_FLOOR_S = 120
BUSY_NOTICE = "browser busy in another session; showing cached data"
# The server cannot know where the plugin is installed, so the path stays a placeholder.
LOGIN_HINT = (
    "run /h2h-advisor:setup, or in a terminal: "
    "uv run --directory <path to the h2h-advisor plugin> h2h-advisor login"
)
BROWSER_SITES = frozenset({"yahoo", "bbm"})
MAX_SCHEDULE_DAYS = 14  # a schedule query loads one BBM day page per day
BRIEF_STREAMERS = 3
ROTATION_CHECKED = 8  # weakest players looked up for recent minutes
ROTATION_MIN_GAMES = 3  # games a minutes sample needs to count
CLOSE_CATEGORY = 0.1  # |win_prob - 0.5| below this is "within a hair"
# Spec 7.4 "back off and stop": a 403, 429 or 5xx (after the session's one retry)
# stops the site for the rest of the call and, for this long, for later calls.
BACKOFF_COOLDOWN_S = 15 * 60
HISTORY_RETRY_S = 24 * 3600  # a past week that failed is not retried sooner
# BBM injury labels that mean "not playing", as the Yahoo codes the engine knows.
_BBM_STATUS = {"OUT": "O", "INJ": "INJ"}

Horizon = Literal["today", "rest_of_week"]
Outcome = Literal["ok", "parse", "failed", "blocked"]


@dataclass
class SourceAge:
    source: str
    fetched_at: datetime | None
    stale: bool


@dataclass
class Envelope:
    data: Any
    ages: list[SourceAge]
    notices: list[str]


# -- cached JSON -> models ---------------------------------------------------------


def _decode(tp: Any, value: Any) -> Any:
    """Rebuild a value of type ``tp`` from the JSON the cache stored for it."""
    if value is None:
        return None
    origin, args = get_origin(tp), get_args(tp)
    if origin in (Union, types.UnionType):
        return _decode(next(a for a in args if a is not type(None)), value)
    if origin is Literal:
        return value
    if origin is list:
        return [_decode(args[0], v) for v in value]
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(args[0], v) for v in value)
        return tuple(_decode(a, v) for a, v in zip(args, value, strict=True))
    if origin in (set, frozenset):
        return frozenset(_decode(args[0], v) for v in value)
    if origin is dict:
        return {_decode(args[0], k): _decode(args[1], v) for k, v in value.items()}
    if tp is datetime:
        return datetime.fromisoformat(value)
    if tp is date:
        return date.fromisoformat(value)
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(value)
    if dataclasses.is_dataclass(tp):
        hints = get_type_hints(tp)
        return tp(
            **{
                f.name: _decode(hints[f.name], value[f.name])
                for f in dataclasses.fields(tp)
                if f.name in value
            }
        )
    if tp is float:
        return float(value)
    return value


def _as(tp: Any) -> Callable[[Any], Any]:
    return lambda value: _decode(tp, value)


# -- one call ----------------------------------------------------------------------


class _CallSession:
    """What the sources see: opens the browser lazily, memoises Yahoo pages by URL.

    BBM pages are not memoised by URL: the projections pages are stateful
    (postbacks change league, filter and date on the same URL); their parsed
    results are memoised per cache key instead.
    """

    def __init__(
        self,
        open_session: Callable[[], BrowserSession],
        current: Callable[[], BrowserSession | None],
    ) -> None:
        self._open = open_session
        self._current = current
        self._pages: dict[str, Any] = {}

    @property
    def page(self) -> Any:
        """The browser's current page (None before the session opens)."""
        return getattr(self._current(), "page", None)

    def fetch(self, site: str, url: str) -> Any:
        if site == "yahoo" and url in self._pages:
            return self._pages[url]
        result = self._open().fetch(site, url)
        if site == "yahoo":
            self._pages[url] = result
        return result

    def postback(
        self,
        site: str,
        url: str,
        event_target: str,
        select: dict[str, str] | None = None,
    ) -> Any:
        return self._open().postback(
            site=site, url=url, event_target=event_target, select=select
        )


@dataclass
class _Call:
    stack: ExitStack
    proxy: _CallSession | None = None
    session: Any = None
    yahoo: Any = None
    bbm: Any = None
    busy: bool = False
    needs_login: dict[str, str] = field(default_factory=dict)  # site -> reason
    capped: set[str] = field(default_factory=set)
    backed_off: set[str] = field(default_factory=set)
    ok_sites: set[str] = field(default_factory=set)
    values: dict[str, Any] = field(default_factory=dict)
    ages: dict[str, SourceAge] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)

    def notice(self, text: str) -> None:
        if text not in self.notices:
            self.notices.append(text)

    def keep(self, key: str, value: Any, age: SourceAge) -> Any:
        self.values[key] = value
        self.ages[age.source] = age
        return value

    def blocked(self, site: str) -> bool:
        if site in BROWSER_SITES and self.busy:
            return True
        return (
            site in self.needs_login or site in self.capped or site in self.backed_off
        )


@dataclass(frozen=True)
class _Week:
    settings: LeagueSettings
    matchup: Matchup
    ctx: WeekContext
    ros_cats: dict[str, dict[Category, float]]
    ros_games: dict[str, float]  # BBM ROS games remaining
    il_moves: list[tuple[Player, Player]] = field(default_factory=list)  # (activated, dropped), mine
    il_skipped: list[tuple[Player, Player]] = field(default_factory=list)  # not worth the drop
    rotation: dict[str, RecentMinutes] = field(default_factory=dict)  # rotation players by ESPN minutes
    proj_minutes: dict[str, float] = field(default_factory=dict)  # BBM projected minutes per game


class Advisor:
    def __init__(
        self,
        config: Config,
        cache: Cache,
        session_factory: Callable[[], AbstractContextManager[BrowserSession]],
        clock: Callable[[], datetime],
        *,
        yahoo_source: Callable[..., YahooSource] = YahooSource,
        bbm_source: Callable[..., BbmSource] = BbmSource,
        espn_source: EspnSource | None = None,
    ) -> None:
        self._config = config
        self._cache = cache
        self._session_factory = session_factory
        self._clock = clock
        self._yahoo_factory = yahoo_source
        self._bbm_factory = bbm_source
        self._espn = espn_source  # None: recent minutes are not checked
        self._index = PlayerIndex(config.aliases)
        self._lock = threading.RLock()
        self._call: _Call | None = None

    # -- public: plain data ----------------------------------------------------------

    def list_leagues(self, refresh: bool = False) -> Envelope:
        with self._calling() as call:
            return self._envelope(call, self._leagues(call, refresh))

    def league_settings(self, league: str, refresh: bool = False) -> Envelope:
        with self._calling() as call:
            lid = self._resolve(call, league)
            return self._envelope(call, self._settings(call, lid, refresh))

    def matchup(
        self, league: str, week: int | None = None, refresh: bool = False
    ) -> Envelope:
        with self._calling() as call:
            lid = self._resolve(call, league)
            return self._envelope(call, self._matchup(call, lid, week, refresh))

    def roster(self, league: str, team: str = "me", refresh: bool = False) -> Envelope:
        with self._calling() as call:
            lid = self._resolve(call, league)
            return self._envelope(call, self._roster(call, lid, team, refresh))

    def free_agents(
        self,
        league: str,
        limit: int = 50,
        position: str | None = None,
        refresh: bool = False,
    ) -> Envelope:
        with self._calling() as call:
            lid = self._resolve(call, league)
            settings = self._settings(call, lid, refresh)
            fas = (
                None
                if settings is None
                else self._free_agents(call, lid, settings, refresh)
            )
            if settings is None or fas is None:
                return self._envelope(call, None)
            if position is not None:
                fas = [
                    f
                    for f in fas
                    if position == "Util" or position in _eligible(f.player)
                ]
            fas = fas[:limit]
            data = {
                "free_agents": fas,
                "claims": _claims(fas, [f.player.key for f in fas], settings),
                "faab": settings.faab,
            }
            return self._envelope(call, data)

    def projections(
        self, scope: str, day: date | None = None, refresh: bool = False
    ) -> Envelope:
        if scope not in ("day", "ros"):
            raise ValueError(f"scope must be 'day' or 'ros', got {scope!r}")
        with self._calling() as call:
            if scope == "ros":
                return self._envelope(call, self._ros(call, refresh))
            return self._envelope(call, self._day(call, day or self._today(), refresh))

    def schedule(self, start: date, end: date, refresh: bool = False) -> Envelope:
        with self._calling() as call:
            span = (end - start).days + 1
            if span < 1:
                raise ValueError(
                    f"end {end.isoformat()} is before start {start.isoformat()}"
                )
            if span > MAX_SCHEDULE_DAYS:
                raise ValueError(
                    f"schedule range is capped at {MAX_SCHEDULE_DAYS} days "
                    f"(one BBM page load per day); asked for {span}"
                )
            days = [start + timedelta(days=i) for i in range(span)]
            schedule = self._schedule(call, days, refresh)
            data = {d: sorted(schedule[d]) for d in days if d in schedule} or None
            return self._envelope(call, data)

    def history(self, league: str) -> Envelope:
        with self._calling() as call:
            lid = self._resolve(call, league)
            current = self._matchup(call, lid, None, False)
            if current is None:
                return self._envelope(call, None)
            settings = self._settings(call, lid, False)
            cats = settings.categories if settings is not None else NINE_CAT
            data = [
                {
                    "week": m.week,
                    "opponent_team_id": m.opponent_team_id,
                    "opponent_name": m.opponent_name,
                    "results": matchup_result(m, cats),
                    "matchup": m,
                }
                for m in self._history(call, lid, current.week)
            ]
            return self._envelope(call, data)

    def status(self) -> Envelope:
        limits = self._config.limits
        today = self._clock().astimezone().date()  # the limiter counts local days
        sites: dict[str, dict[str, Any]] = {}
        for site, cap in (
            ("yahoo", limits.yahoo_daily_pages),
            ("bbm", limits.bbm_daily_pages),
        ):
            item = self._cache.get(f"login:{site}")
            sites[site] = {
                "logged_in": None if item is None else not item.value["needs_login"],
                "login_reason": None if item is None else item.value["reason"],
                "login_checked_at": None if item is None else item.fetched_at,
                "loads_today": self._cache.page_loads(site, today),
                "daily_cap": cap,
                "backed_off_until": self._backoff_until(site),
            }
        by_type: dict[str, dict[str, Any]] = {}
        for key, fetched_at in self._cache.item_ages():
            kind = _item_type(key)
            if kind in ("login", "backoff"):
                continue
            entry = by_type.setdefault(
                kind, {"items": 0, "newest": fetched_at, "oldest": fetched_at}
            )
            entry["items"] += 1
            entry["newest"] = max(entry["newest"], fetched_at)
            entry["oldest"] = min(entry["oldest"], fetched_at)
        data = {
            "sites": sites,
            "cache": by_type,
            "recent_blocked": self._cache.recent_blocked(10),
        }
        return Envelope(data, [], [])

    # -- public: analysis -------------------------------------------------------------

    def analyze_week(self, league: str, refresh: bool = False) -> Envelope:
        with self._calling() as call:
            lid = self._resolve(call, league)
            week = self._week(call, lid, "rest_of_week", refresh)
            if week is None:
                return self._envelope(call, None)
            build = self._build_for(call, lid, week)
            return self._envelope(
                call, _analysis_data(analyze_week_ctx(week.ctx), build)
            )

    def rank_streamers(
        self,
        league: str,
        horizon: Horizon = "rest_of_week",
        max_results: int = 10,
        refresh: bool = False,
    ) -> Envelope:
        _check_horizon(horizon)
        with self._calling() as call:
            lid = self._resolve(call, league)
            week = self._week(call, lid, horizon, refresh)
            fas = (
                None
                if week is None
                else self._free_agents(call, lid, week.settings, refresh)
            )
            if week is None or fas is None:
                return self._envelope(call, None)
            build = self._build_for(call, lid, week)
            options, reason = self._streamers(
                lid, week, fas, horizon, build, max_results
            )
            return self._envelope(
                call, _streamer_data(options, reason, fas, week.settings, build)
            )

    def plan_adds(
        self, league: str, refresh: bool = False, target_cats: int | None = None
    ) -> Envelope:
        """Dated add/drop plan for the rest of the week (moves dated today are "do now")."""
        with self._calling() as call:
            lid = self._resolve(call, league)
            week = self._week(call, lid, "rest_of_week", refresh)
            fas = (
                None
                if week is None
                else self._free_agents(call, lid, week.settings, refresh)
            )
            if week is None or fas is None:
                return self._envelope(call, None)
            plan = plan_adds_ctx(
                week.ctx, fas, self._pool(lid, week, fas), self._config.drop_margin,
                target_cats=target_cats,
            )
            claims = _claims(fas, [m.add.key for m in plan.moves], week.settings)
            return self._envelope(call, {"plan": plan, "claims": claims})

    def build(self, league: str, refresh: bool = False) -> Envelope:
        with self._calling() as call:
            lid = self._resolve(call, league)
            settings = self._settings(call, lid, refresh)
            matchup = self._matchup(call, lid, None, refresh)
            mine = self._roster(call, lid, "me", refresh)
            ros = self._ros(call, refresh)
            if settings is None or matchup is None or mine is None or ros is None:
                return self._envelope(call, None)
            ros_cats = _ros_cats(
                ros,
                _bbm_to_yahoo_keys(
                    (e.player.key for e in mine),
                    (r.projection.player_key for r in ros.rows),
                ),
            )
            return self._envelope(
                call, self._build(call, lid, settings, matchup, mine, ros_cats)
            )

    def long_term_upgrades(
        self, league: str, max_results: int = 5, refresh: bool = False
    ) -> Envelope:
        with self._calling() as call:
            lid = self._resolve(call, league)
            week = self._week(call, lid, "rest_of_week", refresh)
            fas = (
                None
                if week is None
                else self._free_agents(call, lid, week.settings, refresh)
            )
            if week is None or fas is None:
                return self._envelope(call, None)
            build = self._build_for(call, lid, week)
            return self._envelope(
                call, self._upgrades(lid, week, fas, build, max_results)
            )

    def daily_brief(
        self,
        leagues: list[str] | Literal["all"] = "all",
        horizon: Horizon = "today",
        refresh: bool = False,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> Envelope:
        """One browser session for every league, processed one after another.

        ``progress(done, total, league)`` is called after each league.
        """
        _check_horizon(horizon)
        with self._calling() as call:
            if leagues == "all":
                refs = self._leagues(call, refresh)
                if refs is None:
                    return self._envelope(call, None)
                wanted = [r.league_id for r in refs]
            else:
                wanted = list(leagues)
            out: dict[str, Any] = {}
            for done, league in enumerate(wanted, start=1):
                try:
                    lid = self._resolve(call, league)
                    out[lid] = self._brief(call, lid, horizon, refresh)
                except Exception as exc:  # noqa: BLE001 - one league must not sink the brief
                    call.notice(f"{league}: {exc}")
                    out[league] = None
                if progress is not None:
                    progress(done, len(wanted), league)
            return self._envelope(call, {"leagues": out})

    # -- call plumbing ---------------------------------------------------------------

    @contextmanager
    def _calling(self) -> Iterator[_Call]:
        with self._lock:
            if self._call is not None:
                yield self._call
                return
            with ExitStack() as stack:
                call = _Call(stack)
                call.proxy = _CallSession(
                    lambda: self._open(call), lambda: call.session
                )
                self._call = call
                try:
                    yield call
                finally:
                    self._call = None
                    self._record_login(call)

    def _open(self, call: _Call) -> BrowserSession:
        if call.session is None:
            if call.busy:
                raise BrowserBusy()
            try:
                call.session = call.stack.enter_context(self._session_factory())
            except BrowserBusy:
                call.busy = True
                raise
        return call.session

    def _yahoo(self, call: _Call) -> YahooSource:
        if call.yahoo is None:
            call.yahoo = self._yahoo_factory(call.proxy, self._index, self._today)
        return call.yahoo

    def _bbm(self, call: _Call) -> BbmSource:
        if call.bbm is None:
            call.bbm = self._bbm_factory(call.proxy, self._index)
        return call.bbm

    def _record_login(self, call: _Call) -> None:
        now = self._clock()
        for site, reason in call.needs_login.items():
            self._cache.put(
                f"login:{site}", {"needs_login": True, "reason": reason}, now=now
            )
        for site in call.ok_sites - set(call.needs_login):
            self._cache.put(
                f"login:{site}", {"needs_login": False, "reason": None}, now=now
            )

    @staticmethod
    def _envelope(call: _Call, data: Any) -> Envelope:
        return Envelope(data, list(call.ages.values()), list(call.notices))

    def _today(self) -> date:
        return self._clock().astimezone(NBA_TZ).date()

    def _attempt(
        self, call: _Call, site: str, label: str, fetch: Callable[[], Any]
    ) -> tuple[Outcome, Any]:
        """Run ``fetch`` with the fallbacks.

        Outcome: "ok"; "parse" (the page changed layout); "failed" (this request
        failed: fetch error, BBM state, a source's ValueError);
        "blocked" (the site cannot be used for the rest of the call: busy browser,
        login needed, daily cap).
        """
        if site in BROWSER_SITES and site not in call.backed_off:
            until = self._backoff_until(site)
            if until is not None:
                call.backed_off.add(site)
                call.notice(
                    f"{site} backed off until {until:%H:%M} UTC after a blocking "
                    "answer; showing cached data"
                )
        if call.blocked(site):
            if site in BROWSER_SITES and call.busy:
                call.notice(BUSY_NOTICE)
            return "blocked", None
        try:
            value = fetch()
        except BrowserBusy:
            call.busy = True
            call.notice(BUSY_NOTICE)
        except NeedsLogin as exc:
            call.needs_login[exc.site] = exc.reason
            call.notice(f"{exc.site} needs login: {LOGIN_HINT}")
        except DailyCapReached as exc:
            call.capped.add(exc.site)
            call.notice(
                f"daily page-load cap reached for {exc.site} ({exc.cap}); showing cached data"
            )
        except ParseError as exc:
            call.notice(
                f"could not read the {exc.page} page ({exc.detail}); {label} unavailable"
            )
            return "parse", None
        except FetchFailed as exc:
            if not _backs_off(exc):
                call.notice(f"{label}: {exc}; showing cached data")
                return "failed", None
            call.backed_off.add(exc.site)
            self._cache.put(
                f"backoff:{exc.site}",
                {"status": exc.status_or_reason, "seconds": BACKOFF_COOLDOWN_S},
                now=self._clock(),
            )
            call.notice(
                f"{exc.site} answered {exc.status_or_reason}: backing off for "
                f"{BACKOFF_COOLDOWN_S // 60} min; showing cached data"
            )
        except (BbmStateError, ValueError) as exc:
            call.notice(f"{label}: {exc}; showing cached data")
            return "failed", None
        else:
            if site in BROWSER_SITES:
                call.ok_sites.add(site)
            return "ok", value
        return "blocked", None

    def _backoff_until(self, site: str) -> datetime | None:
        """End of the site's cooldown after a blocking answer, if still running."""
        item = self._cache.get(f"backoff:{site}")
        if item is None:
            return None
        until = item.fetched_at + timedelta(seconds=item.value["seconds"])
        return until if until > self._clock() else None

    def _get(
        self,
        call: _Call,
        key: str,
        ttl: str | None,
        site: str,
        label: str,
        fetch: Callable[[], Any],
        decode: Callable[[Any], Any],
        refresh: bool = False,
        force: bool = False,
    ) -> Any:
        """Fresh cache, else fetch, else cached data with a notice, else None.

        ``ttl`` names an entry of ``config.ttl_s``; None means cached forever.
        ``refresh`` is ignored for an item younger than REFRESH_FLOOR_S. ``force``
        fetches whatever the cache and this call's memo hold (consistency repair).
        A cached fallback is stale when it is past its TTL or the site needs login.
        """
        if key in call.values and not force:
            return call.values[key]
        now = self._clock()
        item = self._cache.get(key)
        fresh = item is not None and (
            ttl is None or Cache.is_fresh(item, self._config.ttl_s[ttl], now)
        )
        if item is not None and not force:
            young = Cache.is_fresh(item, REFRESH_FLOOR_S, now)
            if (fresh and not refresh) or young:
                return call.keep(
                    key, decode(item.value), SourceAge(label, item.fetched_at, False)
                )
        outcome, value = self._attempt(call, site, label, fetch)
        if outcome == "ok":
            self._cache.put(key, value, now=now)
            return call.keep(key, value, SourceAge(label, now, False))
        if outcome == "parse":
            return call.keep(
                key, None, SourceAge(label, item.fetched_at if item else None, True)
            )
        if item is None:
            call.notice(f"no cached data for {label}")
            return call.keep(key, None, SourceAge(label, None, True))
        stale = not fresh or site in call.needs_login
        return call.keep(
            key, decode(item.value), SourceAge(label, item.fetched_at, stale)
        )

    # -- pieces ----------------------------------------------------------------------

    def _leagues(self, call: _Call, refresh: bool = False) -> list[LeagueRef] | None:
        return self._get(
            call, "leagues", "settings", "yahoo", "yahoo:leagues",
            lambda: self._yahoo(call).leagues(), _as(list[LeagueRef]), refresh,
        )  # fmt: skip

    def _resolve(self, call: _Call, league: str) -> str:
        """League id, or a case-insensitive substring of exactly one league's name."""
        wanted = str(league).strip()
        refs = self._leagues(call)
        if refs is None:
            if wanted.isdigit():
                return wanted
            raise ValueError(
                f"cannot look up league {wanted!r}: the league list is unavailable"
            )
        if any(r.league_id == wanted for r in refs):
            return wanted
        needle = wanted.casefold()
        matches = [r for r in refs if needle and needle in r.name.casefold()]
        if len(matches) == 1:
            return matches[0].league_id
        listing = "; ".join(f"{r.league_id}: {r.name}" for r in refs)
        problem = "matches several leagues" if matches else "matches no league"
        raise ValueError(f"{wanted!r} {problem}; leagues: {listing}")

    def _league_ref(self, call: _Call, lid: str) -> LeagueRef | None:
        refs = self._leagues(call) or []
        return next((r for r in refs if r.league_id == lid), None)

    def _settings(
        self, call: _Call, lid: str, refresh: bool, force_week: bool = False
    ) -> LeagueSettings | None:
        """Static settings (7 d) with week info from the matchup page (matchup TTL).

        ``force_week`` re-fetches the week info whatever its age (consistency repair).
        """
        static = self._get(
            call, f"settings:{lid}", "settings", "yahoo", f"yahoo:settings:{lid}",
            lambda: self._yahoo(call).settings(lid), _as(LeagueSettings), refresh,
        )  # fmt: skip
        week = self._get(
            call, f"week:{lid}", "matchup", "yahoo", f"yahoo:week:{lid}",
            lambda: self._yahoo(call).week(lid), _as(tuple[int, date, date]), refresh,
            force=force_week,
        )  # fmt: skip
        if static is None or week is None:
            return None
        current, start, end = week
        settings = replace(static, current_week=current, week_start=start, week_end=end)
        league_cfg = self._config.leagues.get(lid)
        if (
            league_cfg
            and league_cfg.max_adds is not None
            and settings.max_adds_per_week <= 0
        ):
            settings = replace(settings, max_adds_per_week=league_cfg.max_adds)
        return settings

    def _matchup(
        self,
        call: _Call,
        lid: str,
        week: int | None,
        refresh: bool,
        force: bool = False,
    ) -> Matchup | None:
        suffix = "" if week is None else f":{week}"
        return self._get(
            call, f"matchup:{lid}{suffix}", "matchup", "yahoo", f"yahoo:matchup:{lid}{suffix}",
            lambda: self._yahoo(call).matchup(lid, week), _as(Matchup), refresh,
            force=force,
        )  # fmt: skip

    def _roster(
        self, call: _Call, lid: str, team: str, refresh: bool
    ) -> list[RosterEntry] | None:
        if team == "me":
            ref = self._league_ref(call, lid)
            if ref is None:
                call.notice(
                    f"my team in league {lid} is unknown: the league list is unavailable"
                )
                return None
            team_id = ref.team_id
        elif team == "opponent":
            current = self._matchup(call, lid, None, refresh)
            if current is None:
                return None
            team_id = current.opponent_team_id
        else:
            team_id = str(team)
        return self._get(
            call, f"roster:{lid}:{team_id}", "roster", "yahoo", f"yahoo:roster:{lid}:{team_id}",
            lambda: self._yahoo(call).roster(lid, team_id), _as(list[RosterEntry]), refresh,
        )  # fmt: skip

    def _ros(self, call: _Call, refresh: bool) -> BbmPage | None:
        return self._get(
            call, "bbm:ros", "bbm_ros", "bbm", "bbm:ros",
            lambda: self._bbm(call).ros(), _as(BbmPage), refresh,
        )  # fmt: skip

    def _day(self, call: _Call, day: date, refresh: bool) -> BbmPage | None:
        iso = day.isoformat()
        # ":games": pages cached before BBM's games table was parsed lack it and
        # would read as a day without games, so they are never reused.
        return self._get(
            call, f"bbm:day:{iso}:games", "bbm_day", "bbm", f"bbm:day:{iso}",
            lambda: self._checked_day(call, day), _as(BbmPage), refresh,
        )  # fmt: skip

    def _checked_day(self, call: _Call, day: date) -> BbmPage:
        """Load a day page; a future page whose games table lacks a team that has
        projected games is unreadable. Today's (and earlier) may legitimately lack
        games already started, so it is kept and the schedule is completed from
        its player rows (``_day_pages``)."""
        page = self._bbm(call).day(day)
        if page.uncovered_teams and day > self._today():
            raise ParseError(
                "bbm",
                f"games table lacks games for {', '.join(page.uncovered_teams)}, "
                "which have projected games",
            )
        return page

    def _day_pages(
        self, call: _Call, days: list[date], refresh: bool
    ) -> dict[date, BbmPage]:
        """The BBM day page of each day that has one (others get a notice)."""
        pages: dict[date, BbmPage] = {}
        for d in days:
            page = self._day(call, d, refresh)
            if page is None:
                call.notice(
                    f"BBM day page for {d.isoformat()} unavailable: no games or "
                    "projections known for that day"
                )
            else:
                pages[d] = page
                if page.uncovered_teams:
                    call.notice(
                        "BBM games table may omit games already started "
                        f"({', '.join(page.uncovered_teams)})"
                    )
        return pages

    def _schedule(self, call: _Call, days: list[date], refresh: bool) -> Schedule:
        """Teams playing on each of ``days`` that has a BBM day page, from its games."""
        return _schedule_of(self._day_pages(call, days, refresh))

    def _waivers(self, call: _Call, lid: str, refresh: bool) -> list[FreeAgent] | None:
        return self._get(
            call, f"waivers:{lid}", "free_agents", "yahoo", f"yahoo:waivers:{lid}",
            lambda: self._yahoo(call).waivers(lid), _as(list[FreeAgent]), refresh,
        )  # fmt: skip

    def _bbm_league_id(
        self, call: _Call, lid: str, settings: LeagueSettings, refresh: bool
    ) -> str | None:
        """Configured BBM league id, else the one-to-one name match (with a notice)."""
        league_cfg = self._config.leagues.get(lid)
        if league_cfg and league_cfg.bbm_league_id is not None:
            return league_cfg.bbm_league_id
        ros = self._ros(call, refresh)
        options = ros.league_options if ros is not None else {}
        names = {r.league_id: r.name for r in self._leagues(call) or []}
        names.setdefault(lid, settings.name)
        taken = {
            c.bbm_league_id
            for other, c in self._config.leagues.items()
            if other != lid and c.bbm_league_id is not None
        }
        free_options = {v: t for v, t in options.items() if v not in taken}
        bbm_id = match_bbm_leagues(names, free_options).get(lid)
        if bbm_id is None:
            listing = ", ".join(sorted(options.values())) or "none loaded"
            call.notice(
                f"set bbm_league_id for {settings.name} in config (BBM leagues: {listing})"
            )
        else:
            call.notice(f"matched {settings.name} ↔ BBM {options[bbm_id]}")
        return bbm_id

    def _free_agents(
        self, call: _Call, lid: str, settings: LeagueSettings, refresh: bool
    ) -> list[FreeAgent] | None:
        """BBM league view cross-checked with Yahoo waivers; Yahoo players pages when
        the BBM league is unknown or its view unavailable."""
        bbm_id = self._bbm_league_id(call, lid, settings, refresh)
        if bbm_id is not None:
            page = self._get(
                call, f"bbm:league:{bbm_id}", "free_agents", "bbm", f"bbm:league:{bbm_id}",
                lambda: self._bbm(call).league_view(bbm_id), _as(BbmPage), refresh,
            )  # fmt: skip
            if page is not None:
                return self._merge_bbm_free_agents(call, lid, settings, page, refresh)
        fas = self._get(
            call, f"free_agents:{lid}", "free_agents", "yahoo", f"yahoo:free_agents:{lid}",
            lambda: self._yahoo(call).free_agents(lid, limit=50), _as(list[FreeAgent]), refresh,
        )  # fmt: skip
        if fas is None:
            return None
        merged = {f.player.key: f for f in fas}
        for w in self._waivers(call, lid, refresh) or []:
            merged.setdefault(w.player.key, w)
        return list(merged.values())

    def _merge_bbm_free_agents(
        self,
        call: _Call,
        lid: str,
        settings: LeagueSettings,
        page: BbmPage,
        refresh: bool,
    ) -> list[FreeAgent]:
        """BBM free agents and waiver players, with Yahoo's waivers list as authority.

        Every player on Yahoo's waivers page is on waivers with Yahoo's date, whatever
        BBM's Own column says; BBM waiver players Yahoo does not list keep no date.
        Yahoo's player (positions, injury status) wins; the key stays BBM's so
        projections line up. Otherwise the injury status comes from BBM.
        """
        rows = [r for r in page.rows if r.own in ("free_agent", "waivers")]
        waivers = self._waivers(call, lid, refresh)
        yahoo_waivers = {f.player.key: f for f in waivers or []}
        # Yahoo's shortened names ("N. Alexander-Walker") against BBM's full ones
        yahoo_by_bbm_key = {
            bbm: yahoo_waivers[yahoo]
            for yahoo, bbm in reconcile_keys(
                yahoo_waivers, (r.projection.player_key for r in rows)
            ).items()
        }
        reserved = {f.player.key for f in yahoo_by_bbm_key.values()}
        matched: set[str] = set()
        out: list[FreeAgent] = []
        undated: list[str] = []
        for row in rows:
            p = row.projection
            player = Player(
                p.player_key, p.name, p.nba_team, row.positions, _status(row.status)
            )
            found = yahoo_by_bbm_key.get(p.player_key)
            if found is None:
                # name-only fallback: never a waiver player another row already took
                # or that the reconciled map assigns to another BBM row
                found = self._index.match(
                    p.name,
                    p.nba_team,
                    {
                        k: f
                        for k, f in yahoo_waivers.items()
                        if k not in matched and k not in reserved
                    },
                )
            if found is not None:
                matched.add(found.player.key)
                out.append(
                    FreeAgent(
                        replace(found.player, key=player.key),
                        on_waivers=True,
                        waiver_clear=found.waiver_clear,
                    )
                )
            elif row.own == "waivers":
                undated.append(p.name)
                out.append(FreeAgent(player, on_waivers=True, waiver_clear=None))
            else:
                out.append(FreeAgent(player, on_waivers=False))
        out += [f for key, f in yahoo_waivers.items() if key not in matched]
        if undated and waivers is not None:
            call.notice(
                f"{settings.name}: no Yahoo waiver date for {', '.join(undated)}; "
                "left out of adds until it is known"
            )
        return out

    def _history(self, call: _Call, lid: str, current_week: int) -> list[Matchup]:
        """Weeks 1..current-1, loaded week by week and cached forever once fetched.

        A week that fails to load or parse is negatively cached for
        HISTORY_RETRY_S (not retried each call); the age is stale while any week
        is missing.
        """
        memo = f"history:{lid}"
        if memo in call.values:
            return call.values[memo]
        label = f"yahoo:history:{lid}"
        found: dict[int, Matchup] = {}
        newest: datetime | None = None
        missing = False
        for week in range(1, current_week):
            item = self._cache.get(f"history:{lid}:{week}")
            if item is not None:
                found[week] = _decode(Matchup, item.value)
                newest = (
                    item.fetched_at if newest is None else max(newest, item.fetched_at)
                )
                continue
            now = self._clock()
            failed = self._cache.get(f"history_failed:{lid}:{week}")
            if failed is not None and Cache.is_fresh(failed, HISTORY_RETRY_S, now):
                missing = True
                continue
            outcome, fetched = self._attempt(
                call, "yahoo", f"{label}:{week}",
                lambda week=week: self._yahoo(call).history(lid, [week]),
            )  # fmt: skip
            if outcome == "ok":
                (m,) = fetched
                self._cache.put(f"history:{lid}:{week}", m, now=now)
                found[week] = m
                newest = now
                continue
            missing = True
            if outcome in ("parse", "failed"):
                self._cache.put(f"history_failed:{lid}:{week}", {"week": week}, now=now)
                call.notice(f"week {week} history unavailable; retried after 24 h")
        history = [found[w] for w in sorted(found)]
        return call.keep(memo, history, SourceAge(label, newest, missing))

    # -- analysis inputs -------------------------------------------------------------

    def _week(
        self, call: _Call, lid: str, horizon: Horizon, refresh: bool
    ) -> _Week | None:
        """WeekContext from settings, matchup, rosters, BBM day pages and ROS.

        The day pages of all remaining days give the schedule (the teams in each
        page's games); a day whose page is unavailable has no schedule, with a
        notice. Projections come from the pages from the effective start (only the
        first of them for horizon "today"). A player gets an active day when his day
        row has g >= 1; on a day without projections the schedule decides, unless
        every loaded row of his says g = 0 and every day from the effective start
        has a page: then he keeps only the days before the effective start (never
        loaded, already locked in), where the schedule decides. Per-game
        stats are the mean of his playing days' rows, else his ROS per-game line.

        The week info and the matchup are cached separately: if their weeks differ
        (a Sunday boundary), both are re-fetched once whatever their age (the matchup
        page is memoised per call, so this costs at most the team page); still
        different, the week cannot be analysed.
        """
        settings = self._settings(call, lid, refresh)
        matchup = self._matchup(call, lid, None, refresh)
        if settings is None or matchup is None:
            return None
        if settings.current_week != matchup.week:
            settings = self._settings(call, lid, refresh, force_week=True)
            matchup = self._matchup(call, lid, None, refresh, force=True)
            if (
                settings is None
                or matchup is None
                or settings.current_week != matchup.week
            ):
                week_info = settings.current_week if settings else "unknown"
                shown = matchup.week if matchup else "unknown"
                call.notice(
                    f"league {lid}: week info (week {week_info}) and matchup "
                    f"(week {shown}) disagree; week analysis unavailable"
                )
                return None
        mine = self._roster(call, lid, "me", refresh)
        opp = self._roster(call, lid, matchup.opponent_team_id, refresh)
        ros = self._ros(call, refresh)
        if mine is None or opp is None or ros is None:
            return None

        today = self._today()
        probe = WeekContext(settings, matchup, mine, opp, {}, {}, {}, {}, today)
        days = remaining_days(probe)
        start = effective_start(settings, today)
        # Every remaining day's page gives the schedule (today's too, even when
        # lineups take effect tomorrow); only some also give projections.
        pages = self._day_pages(call, days, refresh)
        if days and not pages:
            call.notice(
                f"league {lid}: no BBM day page available; week analysis unavailable"
            )
            return None
        schedule = _schedule_of(pages)
        probe = replace(probe, schedule=schedule)
        to_load = [d for d in days if d >= start]
        if horizon == "today":
            to_load = to_load[:1]
        # BBM rows are keyed in Yahoo's key space for the rostered players, so the
        # rest of the build (per game, active days, values, ROS cats) stays keyed by
        # Yahoo's player.key even when Yahoo shortens a first name.
        bbm_keys = {r.projection.player_key for r in ros.rows}
        for d in to_load:
            if d in pages:
                bbm_keys.update(r.projection.player_key for r in pages[d].rows)
        to_yahoo = _bbm_to_yahoo_keys([e.player.key for e in [*mine, *opp]], bbm_keys)
        rows_by_day: dict[date, dict[str, Any]] = {
            d: _rows_by_key(pages[d].rows, to_yahoo) for d in to_load if d in pages
        }

        # Days from the effective start without a page (horizon "today", failures):
        # there the schedule still decides, even for a player g = 0 on loaded days.
        unloaded_ahead = [d for d in days if d >= start and d not in rows_by_day]
        ros_rows = _rows_by_key(ros.rows, to_yahoo)
        keys = set(ros_rows).union(*(rows.keys() for rows in rows_by_day.values()))
        per_game: dict[str, StatLine] = {}
        values: dict[str, float] = {}
        active: dict[str, set[date]] = {}
        for key in keys:
            playing = [
                rows[key]
                for rows in rows_by_day.values()
                if key in rows and rows[key].games >= 1
            ]
            if playing:
                total = StatLine()
                for row in playing:
                    total = total + row.projection.per_game
                per_game[key] = total.scale(1 / len(playing))
            elif key in ros_rows:
                per_game[key] = ros_rows[key].projection.per_game
            listed = [rows[key] for rows in rows_by_day.values() if key in rows]
            if listed and not playing and not unloaded_ahead:
                # projected not to play on any loaded day: only the locked days
                # before the effective start (never loaded) stay to the schedule
                active[key] = {d for d in days if d < start}
            else:
                active[key] = {
                    d for d in days
                    if d not in rows_by_day
                    or (key in rows_by_day[d] and rows_by_day[d][key].games >= 1)
                }  # fmt: skip
            ros_row = ros_rows.get(key)
            if ros_row is not None and ros_row.projection.value is not None:
                values[key] = ros_row.projection.value

        unknown = [e.player.name for e in [*mine, *opp] if e.player.key not in per_game]
        if unknown:
            call.notice(f"{settings.name}: no BBM projection for {', '.join(unknown)}")
        ctx = replace(
            probe,
            per_game=per_game,
            values=values,
            ros_values=dict(values),
            active_days=active,
        )
        ros_games = {key: row.games for key, row in ros_rows.items()}
        # A questionable player in an IL slot is coming back: count him (and the drop).
        never_drop = self._config.never_drop_for(lid)
        proj_minutes = {
            key: row.projection.minutes
            for key, row in ros_rows.items()
            if row.projection.minutes is not None
        }
        rotation = self._rotation(call, ctx, mine, never_drop, proj_minutes, refresh)
        keep = frozenset(rotation)
        with_il, il_moves = activate_il(ctx, "mine", never_drop, keep)
        il_skipped: list[tuple[Player, Player]] = []
        if il_moves and (
            analyze_week_ctx(with_il).matchup_win_prob < analyze_week_ctx(ctx).matchup_win_prob
        ):
            # the dropped player is worth more this week than the returning one adds
            il_skipped, il_moves, with_il = il_moves, [], ctx
        ctx = with_il
        ctx, _ = activate_il(ctx, "opponent")
        ros_cats = _ros_cats(ros, to_yahoo)
        # Who starts when more players have a game than there are slots follows the
        # build: a punted category (or a strong one already safe) must not decide it.
        build = self._build(call, lid, settings, matchup, ctx.my_roster, ros_cats)
        weights = category_weights(build, analyze_week_ctx(ctx).categories)
        ctx = replace(ctx, my_values=_build_values(ctx.values, ros_cats, weights))
        return _Week(
            settings, matchup, ctx, ros_cats, ros_games, il_moves, il_skipped, rotation, proj_minutes
        )

    def _rotation(
        self,
        call: _Call,
        ctx: WeekContext,
        roster: list[RosterEntry],
        never_drop: set[str],
        proj_minutes: dict[str, float],
        refresh: bool,
    ) -> dict[str, RecentMinutes]:
        """Players among my weakest by ROS value playing well above their projected minutes.

        Basketball Monster's projection lags a role change: a player it rates a throwaway
        can be playing 25 minutes a night. A player counts as a rotation player when his
        last games average at least `rotation_mpg` and exceed BBM's projected minutes by
        `rotation_gain_mpg` (projection unknown: minutes alone decide, the cautious way).
        Only the few players that could be offered as a drop are looked up; results are
        cached per player. Fail-soft: an unreachable ESPN leaves the check out with a
        notice and never stops the brief.
        """
        if self._espn is None:
            return {}
        weakest = sorted(
            (
                e.player for e in roster
                if e.slot not in INACTIVE_SLOTS
                and e.player.status not in OUT_STATUSES
                and normalize_name(e.player.name) not in never_drop
            ),
            key=lambda p: (ctx.ros_values.get(p.key, float("-inf")), p.key),
        )[:ROTATION_CHECKED]
        now = self._clock()
        ttl = self._config.ttl_s["espn_minutes"]
        found: dict[str, RecentMinutes] = {}
        fetched_at: list[datetime] = []
        todo: list[Player] = []
        for player in weakest:
            item = self._cache.get(f"espn_min:{player.key}")
            if item is not None and (
                (Cache.is_fresh(item, ttl, now) and not refresh)
                or Cache.is_fresh(item, REFRESH_FLOOR_S, now)
            ):
                found[player.key] = _decode(RecentMinutes, item.value)
                fetched_at.append(item.fetched_at)
            else:
                todo.append(player)
        failed: list[str] = []
        if todo:
            with ThreadPoolExecutor(max_workers=4) as pool:
                outcomes = list(pool.map(lambda p: self._minutes_of(p), todo))
            for player, outcome in zip(todo, outcomes, strict=True):
                if isinstance(outcome, EspnError):
                    failed.append(player.name)
                    old = self._cache.get(f"espn_min:{player.key}")
                    if old is not None:  # stale beats nothing
                        found[player.key] = _decode(RecentMinutes, old.value)
                        fetched_at.append(old.fetched_at)
                    continue
                self._cache.put(f"espn_min:{player.key}", outcome, now=now)
                found[player.key] = outcome
                fetched_at.append(now)
        if failed:
            call.notice(
                "ESPN game logs unavailable for " + ", ".join(failed)
                + ": recent minutes not checked for them"
            )
        if fetched_at:
            call.ages["espn:minutes"] = SourceAge("espn:minutes", min(fetched_at), bool(failed))
        floor, gain = self._config.rotation_mpg, self._config.rotation_gain_mpg
        return {
            key: m for key, m in found.items()
            if m.games >= ROTATION_MIN_GAMES
            and m.mpg >= floor
            and (key not in proj_minutes or m.mpg - proj_minutes[key] >= gain)
        }

    def _minutes_of(self, player: Player) -> RecentMinutes | EspnError:
        """Recent minutes of one player (athlete ids cached for good); never raises."""
        assert self._espn is not None
        try:
            id_key = f"espn_id:{player.key}"
            cached = self._cache.get(id_key)
            athlete = cached.value.get("id") if cached is not None else None
            if athlete is None:
                athlete = self._espn.athlete_id(player.name, player.nba_team)
                if athlete is None:
                    return RecentMinutes(0, 0.0, [], None)  # not found: no rotation claim
                self._cache.put(id_key, {"id": athlete}, now=self._clock())
            return self._espn.recent_minutes(athlete) or RecentMinutes(0, 0.0, [], None)
        except EspnError as exc:
            return exc

    def _build(
        self,
        call: _Call,
        lid: str,
        settings: LeagueSettings,
        matchup: Matchup,
        mine: list[RosterEntry],
        ros_cats: dict[str, dict[Category, float]],
    ) -> Build:
        history = self._history(call, lid, matchup.week)
        roster_cats = {
            c: sum(ros_cats.get(e.player.key, {}).get(c, 0.0) for e in mine)
            for c in NINE_CAT
        }
        inferred = infer_build(
            roster_cats, [matchup_result(m, settings.categories) for m in history]
        )
        league_cfg = self._config.leagues.get(lid)
        return resolve_build(
            build_from_config(league_cfg.build if league_cfg else None), inferred
        )

    def _build_for(self, call: _Call, lid: str, week: _Week) -> Build:
        return self._build(
            call, lid, week.settings, week.matchup, week.ctx.my_roster, week.ros_cats
        )

    def _pool(self, lid: str, week: _Week, fas: list[FreeAgent]) -> list[RosterEntry]:
        league_cfg = self._config.leagues.get(lid)
        return drop_pool(
            week.ctx.my_roster,
            week.ctx.ros_values,
            fas,
            never_drop=self._config.never_drop_for(lid),
            margin=self._config.drop_margin,
            stream_slots=league_cfg.stream_slots if league_cfg else None,
            keep=frozenset(week.rotation),
        )

    def _streamers(
        self,
        lid: str,
        week: _Week,
        fas: list[FreeAgent],
        horizon: Horizon,
        build: Build,
        max_results: int,
    ) -> tuple[list[StreamerOption], str | None]:
        return rank_streamers_ctx(
            week.ctx,
            fas,
            self._pool(lid, week, fas),
            horizon,
            build,
            light_day_max_games=self._config.light_day_max_games,
            max_results=max_results,
            margin=self._config.drop_margin,
        )

    def _upgrades(
        self,
        lid: str,
        week: _Week,
        fas: list[FreeAgent],
        build: Build,
        max_results: int,
    ) -> list:
        return long_term_upgrades_ctx(
            week.ctx,
            fas,
            self._pool(lid, week, fas),
            week.ros_cats,
            build,
            margin=self._config.drop_margin,
            max_results=max_results,
            ros_games=week.ros_games,
        )

    def _brief(
        self, call: _Call, lid: str, horizon: Horizon, refresh: bool
    ) -> dict[str, Any]:
        empty = {"analysis": None, "streamers": [], "streamer_reason": None,
                 "upgrades": [], "claims": {}, "facts": [], "potential": None}  # fmt: skip
        # The status block always uses the whole week's day pages (shared by every
        # league, one load per date per call); the horizon only shapes streamers.
        week = self._week(call, lid, "rest_of_week", refresh)
        if week is None:
            return empty
        build = self._build_for(call, lid, week)
        analysis = analyze_week_ctx(week.ctx)
        brief = {
            **empty,
            "name": week.settings.name,
            "analysis": _analysis_data(analysis, build),
            "facts": self._facts(call, lid, week, analysis),
        }
        fas = self._free_agents(call, lid, week.settings, refresh)
        if fas is not None:
            options, reason = self._streamers(
                lid, week, fas, horizon, build, BRIEF_STREAMERS
            )
            upgrades = self._upgrades(lid, week, fas, build, 5)
            brief["potential"] = plan_adds_ctx(
                week.ctx, fas, self._pool(lid, week, fas), self._config.drop_margin
            )
            adds = [*_option_adds(options), *(u.add.key for u in upgrades)]
            brief.update(
                streamers=options,
                streamer_reason=reason,
                upgrades=upgrades,
                claims=_claims(fas, adds, week.settings),
            )
        return brief

    def _facts(
        self, call: _Call, lid: str, week: _Week, analysis: WeekAnalysis
    ) -> list[str]:
        ctx, settings, matchup = week.ctx, week.settings, week.matchup
        facts: list[str] = []
        start = effective_start(settings, ctx.today)
        # a day absent from the schedule has no BBM page: unknown, not "0 games"
        games = [
            (d, len(ctx.schedule[d]) // 2)
            for d in analysis.days_left
            if d >= start and d in ctx.schedule
        ]
        light = [(d, n) for d, n in games if 0 < n <= self._config.light_day_max_games]
        if light:
            facts.append(
                "Light days left: "
                + ", ".join(
                    f"{d.isoformat()} ({n} game{'' if n == 1 else 's'})"
                    for d, n in light
                )
            )
        idle = [d for d, n in games if n == 0]
        if idle:
            facts.append("No NBA games: " + ", ".join(d.isoformat() for d in idle))
        close = [
            o for o in analysis.categories if abs(o.win_prob - 0.5) < CLOSE_CATEGORY
        ]
        if close:
            facts.append(
                "Close categories: "
                + ", ".join(f"{o.category.value} ({o.win_prob:.0%})" for o in close)
            )
        for label, roster in (("me", ctx.my_roster), ("opponent", ctx.opp_roster)):
            sitting = projected_rest(
                roster, ctx.schedule, [d for d in analysis.days_left if d >= start], ctx.active_days
            )
            if sitting:
                facts.append(
                    f"Projected to sit though their team plays (BBM), {label}: "
                    + ", ".join(
                        f"{p.name} ({', '.join(d.strftime('%a') for d in days)})"
                        for p, days in sitting
                    )
                )
        if week.rotation:
            by_key = {e.player.key: e.player for e in ctx.my_roster}
            games = [d for d in analysis.days_left if d >= start]
            parts = []
            for key, m in week.rotation.items():
                player = by_key[key]
                days = [
                    d.strftime("%a") for d in games
                    if player.nba_team in ctx.schedule.get(d, frozenset())
                    and d in (ctx.active_days or {}).get(key, set(games))
                ]
                projected = week.proj_minutes.get(key)
                against = f" vs {projected:.0f} projected" if projected is not None else ""
                plays = f", plays {', '.join(days)}" if days else ", no game left this week"
                parts.append(f"{player.name} {m.mpg:.0f} mpg{against}{plays}")
            facts.append(
                "Kept off the drop list, playing more than Basketball Monster projects "
                "(ESPN, last games): " + "; ".join(parts)
            )
        for activated, dropped in week.il_skipped:
            facts.append(
                f"{activated.name} ({activated.status or 'healthy'}) could leave IL, but dropping "
                f"{dropped.name} costs more this week than he adds: kept in IL, not counted"
            )
        for activated, dropped in week.il_moves:
            facts.append(
                f"Assumes {activated.name} ({activated.status or 'healthy'}) is moved out of IL "
                f"and {dropped.name} dropped (weakest ROS): he counts in every projection here; "
                "if he does not play, the outlook is lower"
            )
        mine = _usable_games(ctx, ctx.my_roster, ctx.active_days)
        theirs = _usable_games(ctx, ctx.opp_roster, ctx.active_days)
        facts.append(
            f"Usable games left: me {mine}, opponent {theirs} ({mine - theirs:+d})"
        )
        met = [m for m in self._history(call, lid, matchup.week)
               if m.opponent_team_id == matchup.opponent_team_id]  # fmt: skip
        if met:
            won = [
                c for c in settings.categories
                if 2 * sum(1 for m in met if matchup_result(m, (c,))[c] < 0) > len(met)
            ]  # fmt: skip
            if won:
                facts.append(
                    f"{matchup.opponent_name} won {', '.join(c.value for c in won)} "
                    f"in most of your {len(met)} past meeting(s)"
                )
        return facts


# -- helpers -------------------------------------------------------------------------


def _check_horizon(horizon: str) -> None:
    if horizon not in ("today", "rest_of_week"):
        raise ValueError(f"horizon must be 'today' or 'rest_of_week', got {horizon!r}")


def _backs_off(exc: FetchFailed) -> bool:
    status = exc.status_or_reason
    return isinstance(status, int) and (status in (403, 429) or status >= 500)


def _item_type(key: str) -> str:
    parts = key.split(":")
    return ":".join(parts[:2]) if parts[0] == "bbm" else parts[0]


def _eligible(player: Player) -> set[str]:
    positions = set(player.positions)
    if positions & {"PG", "SG"}:
        positions.add("G")
    if positions & {"SF", "PF"}:
        positions.add("F")
    return positions


_ORDINAL = re.compile(r"^_?\d+\.\s*")
_SITE_SUFFIX = re.compile(r"\s*\(yahoo!?\)$", re.IGNORECASE)
_ELLIPSIS = ("…", "...")


def _bbm_league_text(text: str) -> tuple[str, bool]:
    """A BBM league option as (casefolded name, truncated?).

    Options read ``_<n>. <name> (Yahoo!)``. In the fixtures no name is cut short
    (the longest, 29 characters, is shown whole), so the only sign of truncation
    we trust is a trailing ellipsis.
    """
    name = _SITE_SUFFIX.sub("", _ORDINAL.sub("", " ".join(text.split()), count=1))
    truncated = name.endswith(_ELLIPSIS)
    for mark in _ELLIPSIS:
        name = name.removesuffix(mark)
    return " ".join(name.split()).casefold(), truncated


def match_bbm_leagues(names: dict[str, str], options: dict[str, str]) -> dict[str, str]:
    """Yahoo league id -> BBM option value, one-to-one.

    An option matches a Yahoo league on equal names (casefold, whitespace
    normalised), or as a prefix only when the option text is truncated. A league
    with several candidate options, or an option claimed by several leagues, is
    left unmatched.
    """
    candidates: dict[str, list[str]] = {}
    for lid, name in names.items():
        target = " ".join(name.split()).casefold()
        found = []
        for value, text in options.items():
            short, truncated = _bbm_league_text(text)
            if short and (short == target or (truncated and target.startswith(short))):
                found.append(value)
        candidates[lid] = found
    claims: dict[str, int] = {}
    for found in candidates.values():
        for value in found:
            claims[value] = claims.get(value, 0) + 1
    return {
        lid: found[0]
        for lid, found in candidates.items()
        if len(found) == 1 and claims[found[0]] == 1
    }


def _status(bbm_status: str | None) -> str | None:
    """A BBM injury label as Player.status (OUT -> O, INJ -> INJ, others as shown)."""
    if bbm_status is None:
        return None
    return _BBM_STATUS.get(bbm_status, bbm_status)


def _bbm_to_yahoo_keys(
    yahoo_keys: Iterable[str], bbm_keys: Iterable[str]
) -> dict[str, str]:
    """BBM key -> Yahoo key for players Yahoo names differently (exact matches omitted)."""
    return {
        bbm: yahoo
        for yahoo, bbm in reconcile_keys(yahoo_keys, bbm_keys).items()
        if yahoo != bbm
    }


def _rows_by_key(rows: Iterable[BbmRow], to_yahoo: dict[str, str]) -> dict[str, BbmRow]:
    return {
        to_yahoo.get(r.projection.player_key, r.projection.player_key): r for r in rows
    }


def _ros_cats(
    ros: BbmPage, to_yahoo: dict[str, str] | None = None
) -> dict[str, dict[Category, float]]:
    return {
        key: dict(row.projection.cat_values)
        for key, row in _rows_by_key(ros.rows, to_yahoo or {}).items()
        if row.projection.cat_values
    }


def _build_values(
    values: dict[str, float],
    ros_cats: dict[str, dict[Category, float]],
    weights: dict[Category, float],
) -> dict[str, float]:
    """Lineup value per player: his BBM category values weighted by the build.

    A player without category values keeps his plain overall value.
    """
    return {
        key: sum(weights.get(c, 0.0) * v for c, v in ros_cats[key].items())
        if key in ros_cats
        else value
        for key, value in values.items()
    }


def _schedule_of(pages: dict[date, BbmPage]) -> Schedule:
    """Teams in each day page's games, plus any team the page's players show with a
    game that its games table omits; a day without games maps to no teams."""
    return {
        d: frozenset(team for game in page.games for team in game)
        | frozenset(page.uncovered_teams)
        for d, page in pages.items()
    }


def _claims(
    fas: list[FreeAgent], keys: list[str], settings: LeagueSettings
) -> dict[str, dict[str, Any]]:
    """For each add that is on waivers: when he clears and whether it is a FAB bid."""
    by_key = {f.player.key: f for f in fas}
    claims = {}
    for key in keys:
        fa = by_key.get(key)
        if fa is not None and fa.on_waivers:
            claims[key] = {
                "waiver_clear": fa.waiver_clear.isoformat()
                if fa.waiver_clear
                else None,
                "faab": settings.faab,
            }
    return claims


def _option_adds(options: list[StreamerOption]) -> list[str]:
    adds = []
    for option in options:
        adds.append(option.add.key)
        if option.follow_up is not None:
            adds.append(option.follow_up.add.key)
    return adds


def _analysis_data(analysis: WeekAnalysis, build: Build) -> dict[str, Any]:
    data: dict[str, Any] = {
        "analysis": analysis,
        "objective": analysis.objective,
        "expected_cats_won": analysis.expected_cats_won,
        "matchup_win_prob": analysis.matchup_win_prob,
        "build": build,
        "at_risk_strong": at_risk_strong(build, analysis.categories),
    }
    if analysis.objective == "one_win":
        data["targets"] = one_win_targets(build, analysis.categories)
    return data


def _streamer_data(
    options: list[StreamerOption],
    reason: str | None,
    fas: list[FreeAgent],
    settings: LeagueSettings,
    build: Build,
) -> dict[str, Any]:
    return {
        "options": options,
        "reason": reason,
        "claims": _claims(fas, _option_adds(options), settings),
        "faab": settings.faab,
        "build": build,
    }


def _usable_games(
    ctx: WeekContext,
    roster: list[RosterEntry],
    active_days: dict[str, set[date]] | None,
) -> int:
    lineups = lineup_by_day(
        roster, ctx.per_game, ctx.my_lineup_values if roster is ctx.my_roster else ctx.values,
        ctx.schedule, remaining_days(ctx),
        ctx.settings.roster_slots, active_days=active_days,
    )  # fmt: skip
    return sum(len(day) for day in lineups.values())
