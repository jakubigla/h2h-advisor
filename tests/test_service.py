"""Advisor service: fetch-or-cache, one browser session per call, graceful fallbacks.

Fake sources stand in for Yahoo and BBM; every source method "loads" a page through
the session the Advisor hands it, so page loads and session openings are counted
exactly as the real sources would cause them. No live site is touched.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from h2h_advisor.browser.ratelimit import DailyCapReached
from h2h_advisor.browser.session import BrowserBusy, FetchFailed, NeedsLogin, PageResult
from h2h_advisor.cache import Cache
from h2h_advisor.config import Config, LeagueConfig
from h2h_advisor.identity import player_key
from h2h_advisor.models import (
    NINE_CAT,
    Category,
    FreeAgent,
    LeagueSettings,
    Matchup,
    Player,
    Projection,
    RosterEntry,
    StatLine,
)
from h2h_advisor.service import BACKOFF_COOLDOWN_S, Advisor, match_bbm_leagues
from h2h_advisor.sources.bbm.parsers import BbmPage, BbmRow, parse_page
from h2h_advisor.sources.errors import ParseError
from h2h_advisor.sources.espn import EspnError, RecentMinutes
from h2h_advisor.sources.yahoo import urls as yahoo_urls
from h2h_advisor.sources.yahoo.parsers import LeagueRef, parse_league_list

FIXTURES = Path(__file__).parent / "fixtures" / "yahoo"
BBM_FIXTURES = Path(__file__).parent / "fixtures" / "bbm"
NOW = datetime(2026, 11, 25, 17, 0, tzinfo=UTC)  # noon US Eastern, Wed 25 Nov
WEEK_START, WEEK_END = date(2026, 11, 23), date(2026, 11, 29)
A, B = "51347", "57243"
SLOTS = {"G": 1, "F": 1, "C": 1, "Util": 1, "BN": 2, "IL": 1}
SCHEDULE = {
    date(2026, 11, 25): frozenset({"LAL", "BOS", "MIA", "NYK", "DEN", "PHX"}),
    date(2026, 11, 26): frozenset({"LAL", "MIA", "DEN", "GSW"}),
    date(2026, 11, 27): frozenset({"BOS", "NYK", "PHX", "GSW", "CHI", "ATL"}),
    date(2026, 11, 28): frozenset({"LAL", "BOS", "DEN", "CHI"}),
    date(2026, 11, 29): frozenset({"MIA", "NYK", "PHX", "GSW", "ATL", "CHI"}),
}


def line(pts: float, reb: float = 4, ast: float = 3, tpm: float = 1.5) -> StatLine:
    return StatLine(
        fgm=pts * 0.18, fga=pts * 0.4, ftm=pts * 0.1, fta=pts * 0.13, tpm=tpm,
        pts=pts, reb=reb, ast=ast, stl=1.0, blk=0.6, to=1.8,
    )  # fmt: skip


@dataclass(frozen=True)
class P:
    name: str
    team: str
    positions: tuple[str, ...]
    value: float
    pts: float

    @property
    def key(self) -> str:
        return player_key(self.name, self.team)

    def player(self) -> Player:
        return Player(self.key, self.name, self.team, self.positions)


STAR = P("Star Guard", "LAL", ("PG", "SG"), 8.0, 28)
MID = P("Nickeil Alexander-Walker", "BOS", ("SF", "PF"), 3.0, 15)
BIG = P("Big Center", "DEN", ("C",), 5.0, 20)
WING = P("Bench Wing", "MIA", ("SF",), -1.0, 6)
SPARE = P("Spare Guard", "NYK", ("SG",), -0.5, 7)
OPP = [
    P("Opp One", "PHX", ("PG",), 6.0, 26),
    P("Opp Two", "BOS", ("PF",), 4.0, 18),
    P("Opp Three", "GSW", ("C",), 3.0, 16),
    P("Opp Four", "CHI", ("SF",), 2.0, 14),
]
HOT = P("Hot Pickup", "GSW", ("SF", "PF"), 2.0, 22)
WAIVER = P("Waiver Guy", "CHI", ("PG",), 4.0, 24)
MEH = P("Meh Center", "ATL", ("C",), -2.0, 5)
MINE = [STAR, MID, BIG, WING, SPARE]
MY_SLOTS = ["G", "F", "C", "Util", "BN"]
EVERYONE = [*MINE, *OPP, HOT, WAIVER, MEH]


def settings(league: str, name: str, **kw) -> LeagueSettings:
    base = {
        "league_id": league, "name": name, "categories": NINE_CAT, "roster_slots": SLOTS,
        "max_adds_per_week": 4, "week_start": WEEK_START, "week_end": WEEK_END,
        "current_week": 6, "lineup_effective": "today",
    }  # fmt: skip
    return LeagueSettings(**{**base, **kw})


def matchup(league: str, week: int = 6, moves_used: int = 1) -> Matchup:
    return Matchup(
        league_id=league, week=week, opponent_team_id="4", opponent_name="Rivals",
        my_totals=line(300, 120, 70, 30), opp_totals=line(320, 125, 72, 33),
        moves_used=moves_used,
    )  # fmt: skip


def bbm_row(
    p: P, games: float = 1.0, own=None, status: str | None = None, minutes: float | None = None
) -> BbmRow:
    cats = {c: p.value / 9 for c in NINE_CAT}
    return BbmRow(
        Projection(p.key, p.name, p.team, line(p.pts), p.value, cats, minutes),
        own=own, bbm_id=p.name, games=games, positions=p.positions, status=status,
    )  # fmt: skip


@dataclass
class World:
    leagues: list[LeagueRef] = field(
        default_factory=lambda: [
            LeagueRef(A, "Hoops League", "9", "My Team"),
            LeagueRef(B, "Big Men League", "9", "My Other Team"),
        ]
    )
    settings: dict[str, LeagueSettings] = field(
        default_factory=lambda: {
            A: settings(A, "Hoops League"),
            B: settings(B, "Big Men League", faab=True),
        }
    )
    matchups: dict[str, Matchup] = field(
        default_factory=lambda: {A: matchup(A), B: matchup(B)}
    )
    league_options: dict[str, str] = field(
        default_factory=lambda: {
            "1001": "_1. Hoops League (Yahoo!)",
            "1002": "_2. Big Men Le… (Yahoo!)",
        }
    )
    # day -> teams whose games the games table omits (their players still have g = 1)
    omitted_games: dict[date, tuple[str, ...]] = field(default_factory=dict)
    day_games: dict[tuple[str, date], float] = field(default_factory=dict)  # override g
    ros_games: dict[str, float] = field(default_factory=dict)  # name -> ROS g
    proj_minutes: dict[str, float] = field(default_factory=dict)  # name -> BBM projected m/g
    bbm_status: dict[str, str] = field(default_factory=dict)  # name -> BBM injury label
    week_seq: list[int] = field(
        default_factory=list
    )  # week() answers, then the matchup's
    raise_on: dict[str, Exception] = field(
        default_factory=dict
    )  # site -> raised on fetch
    attempts: list[str] = field(default_factory=list)  # every fetch, even failed ones
    yahoo_waivers: list[FreeAgent] = field(
        default_factory=lambda: [
            FreeAgent(
                Player(WAIVER.key, WAIVER.name, WAIVER.team, ("PG", "SG")),
                on_waivers=True,
                waiver_clear=date(2026, 11, 27),
            )
        ]
    )
    # BBM name -> the shortened name Yahoo shows for the same player
    yahoo_names: dict[str, str] = field(default_factory=dict)
    # BBM free agents listed in the league view before the waiver player
    bbm_extra_fas: list[P] = field(default_factory=list)
    errors: dict[tuple[str, str], Exception] = field(default_factory=dict)
    needs_login: set[str] = field(default_factory=set)
    loads: list[str] = field(default_factory=list)
    history_calls: list[tuple[str, list[int]]] = field(default_factory=list)

    def count(self, url: str) -> int:
        return self.loads.count(url)

    def yahoo_player(self, p: P) -> Player:
        name = self.yahoo_names.get(p.name, p.name)
        return Player(player_key(name, p.team), name, p.team, p.positions)


class FakeSession:
    def __init__(self, world: World) -> None:
        self.world = world

    def fetch(self, site: str, url: str) -> PageResult:
        self.world.attempts.append(url)
        if site in self.world.raise_on:
            raise self.world.raise_on[site]
        if site in self.world.needs_login:
            raise NeedsLogin(site, "redirected to login")
        self.world.loads.append(url)
        return PageResult(url, url, "", NOW)


class FakeYahoo:
    def __init__(self, session, world: World) -> None:
        self.s, self.w = session, world

    def _load(self, method: str, league: str, *urls: str) -> None:
        for url in urls:
            self.s.fetch("yahoo", url)
        if (method, league) in self.w.errors:
            raise self.w.errors[(method, league)]

    def leagues(self):
        self._load("leagues", "", "y/home")
        return self.w.leagues

    def settings(self, league):
        self._load("settings", league, f"y/settings/{league}", f"y/matchup/{league}")
        return self.w.settings[league]

    def week(self, league):
        self._load("week", league, f"y/matchup/{league}")
        s = self.w.settings[league]
        week = (
            self.w.week_seq.pop(0) if self.w.week_seq else self.w.matchups[league].week
        )
        return (week, s.week_start, s.week_end)

    def matchup(self, league, week=None):
        self._load("matchup", league, f"y/matchup/{league}", f"y/team/{league}/9")
        return self.w.matchups[league]

    def history(self, league, weeks):
        self.w.history_calls.append((league, list(weeks)))
        self._load("history", league, *[f"y/matchup/{league}?week={n}" for n in weeks])
        return [replace(matchup(league, week=n, moves_used=0)) for n in weeks]

    def roster(self, league, team_id):
        self._load("roster", league, f"y/team/{league}/{team_id}")
        if str(team_id) == "9":
            return [
                RosterEntry(self.w.yahoo_player(p), s)
                for p, s in zip(MINE, MY_SLOTS, strict=True)
            ]
        return [
            RosterEntry(self.w.yahoo_player(p), s)
            for p, s in zip(OPP, ["G", "F", "C", "Util"], strict=True)
        ]

    def free_agents(self, league, limit=50, position=None):
        self._load("free_agents", league, f"y/players/{league}?status=A")
        return [
            FreeAgent(HOT.player(), False),
            FreeAgent(MEH.player(), False),
            *self.w.yahoo_waivers,
        ]

    def waivers(self, league):
        self._load("waivers", league, f"y/players/{league}?status=W")
        return list(self.w.yahoo_waivers)


class FakeBbm:
    def __init__(self, session, world: World) -> None:
        self.s, self.w = session, world

    def _page(
        self, rows, game_date=None, league=None, games=(), uncovered=()
    ) -> BbmPage:
        return BbmPage(
            rows,
            game_date,
            "All",
            league,
            dict(self.w.league_options),
            games,
            uncovered,
        )

    def day(self, d):
        self.s.fetch("bbm", f"b/day/{d.isoformat()}")
        if ("day", d.isoformat()) in self.w.errors:
            raise self.w.errors[("day", d.isoformat())]
        teams = SCHEDULE.get(d, frozenset())
        ordered = sorted(teams)
        games = tuple(zip(ordered[::2], ordered[1::2], strict=True))
        omitted = self.w.omitted_games.get(d, ())
        games = tuple(g for g in games if not set(g) & set(omitted))
        listed = {t for g in games for t in g}
        uncovered = tuple(sorted(teams - listed)) if omitted else ()
        rows = [
            bbm_row(
                p,
                games=self.w.day_games.get((p.name, d), 1.0),
                status=self.w.bbm_status.get(p.name),
            )
            for p in EVERYONE
            if p.team in teams
        ]
        return self._page(rows, game_date=d, games=games, uncovered=uncovered)

    def ros(self):
        self.s.fetch("bbm", "b/ros")
        rows = [
            bbm_row(
                p,
                games=self.w.ros_games.get(p.name, 40),
                status=self.w.bbm_status.get(p.name),
                minutes=self.w.proj_minutes.get(p.name),
            )
            for p in EVERYONE
        ]
        return self._page(rows)

    def league_view(self, bbm_league_id):
        self.s.fetch("bbm", f"b/league/{bbm_league_id}")
        if ("league_view", bbm_league_id) in self.w.errors:
            raise self.w.errors[("league_view", bbm_league_id)]
        st = self.w.bbm_status.get
        rows = [bbm_row(p, own="mine") for p in MINE]
        rows += [bbm_row(p, own="free_agent", status=st(p.name)) for p in (HOT, MEH)]
        rows += [bbm_row(p, own="free_agent") for p in self.w.bbm_extra_fas]
        rows += [bbm_row(WAIVER, own="waivers", status=st(WAIVER.name))]
        return self._page(rows, league=bbm_league_id)


class Factory:
    def __init__(self, world: World) -> None:
        self.world = world
        self.calls = 0
        self.busy = False

    def __call__(self):
        self.calls += 1
        return self._open()

    @contextmanager
    def _open(self):
        if self.busy:
            raise BrowserBusy()
        yield FakeSession(self.world)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


@dataclass
class Rig:
    advisor: Advisor
    world: World
    factory: Factory
    clock: Clock
    cache: Cache


@pytest.fixture
def make(tmp_path):
    built = 0

    def build(world: World | None = None, config: Config | None = None, espn=None) -> Rig:
        nonlocal built
        built += 1
        world = world or World()
        factory, clock = Factory(world), Clock()
        cache = Cache(tmp_path / f"cache{built}.db")
        advisor = Advisor(
            config or Config(),
            cache,
            factory,
            clock,
            yahoo_source=lambda session, index, today: FakeYahoo(session, world),
            bbm_source=lambda session, index: FakeBbm(session, world),
            espn_source=espn,
        )
        return Rig(advisor, world, factory, clock, cache)

    return build


def ages(env) -> dict[str, object]:
    return {a.source: a for a in env.ages}


# -- fetch-or-cache --------------------------------------------------------------


def test_fresh_cache_does_not_open_browser(make):
    rig = make()
    first = rig.advisor.matchup(A)
    assert rig.factory.calls == 1
    rig.clock.advance(minutes=5)
    again = rig.advisor.matchup(A)
    assert rig.factory.calls == 1
    assert again.data == first.data
    assert again.notices == []
    assert all(not a.stale for a in again.ages)


def test_stale_triggers_single_session_for_whole_call(make):
    rig = make()
    env = rig.advisor.analyze_week("hoops")
    assert env.data is not None
    assert rig.factory.calls == 1
    # settings, week and matchup share one load of the matchup page (memoised per call)
    assert rig.world.count(f"y/matchup/{A}") == 1
    assert rig.world.count(f"y/team/{A}/9") == 1


def test_needs_login_returns_cached_stale_with_notice(make):
    rig = make()
    before = rig.advisor.matchup(A).data
    rig.clock.advance(minutes=30)
    rig.world.needs_login.add("yahoo")
    calls_before, attempts_before = rig.factory.calls, len(rig.world.attempts)

    env = rig.advisor.matchup(A)

    assert env.data == before
    assert ages(env)[f"yahoo:matchup:{A}"].stale is True
    login = [n for n in env.notices if n.startswith("yahoo needs login")]
    assert login and "/h2h-advisor:setup" in login[0]
    assert (
        "uv run --directory <path to the h2h-advisor plugin> h2h-advisor login"
        in login[0]
    )
    assert rig.factory.calls == calls_before + 1
    assert len(rig.world.attempts) == attempts_before + 1  # one attempt, no retry


def test_needs_login_stops_every_later_yahoo_fetch_in_the_call(make):
    rig = make()
    rig.advisor.list_leagues()
    rig.world.needs_login.add("yahoo")
    attempts_before = len(rig.world.attempts)
    env = rig.advisor.analyze_week(A)  # settings, week, matchup, rosters all missing
    assert env.data is None
    assert len(rig.world.attempts) == attempts_before + 1


def test_daily_cap_returns_cached_with_notice(make):
    rig = make()
    before = rig.advisor.matchup(A).data
    rig.clock.advance(minutes=30)
    rig.world.raise_on["yahoo"] = DailyCapReached("yahoo", 250)
    env = rig.advisor.matchup(A)
    assert env.data == before
    assert ages(env)[f"yahoo:matchup:{A}"].stale is True
    assert any("daily page-load cap reached for yahoo (250)" in n for n in env.notices)


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_blocking_status_backs_off_the_site_for_the_rest_of_the_call(make, status):
    rig = make()
    rig.advisor.list_leagues()
    rig.clock.advance(minutes=30)
    rig.world.raise_on["yahoo"] = FetchFailed("yahoo", status)
    attempts_before = len(rig.world.attempts)
    env = rig.advisor.analyze_week(A)  # settings, week, matchup, rosters all missing
    assert len(rig.world.attempts) == attempts_before + 1  # no further Yahoo URL
    assert any(f"yahoo answered {status}: backing off" in n for n in env.notices)


def test_back_off_persists_across_calls_then_expires(make):
    rig = make()
    before = rig.advisor.matchup(A).data
    rig.clock.advance(minutes=30)
    rig.world.raise_on["yahoo"] = FetchFailed("yahoo", 429)
    rig.advisor.matchup(A)
    del rig.world.raise_on["yahoo"]
    attempts_before = len(rig.world.attempts)

    rig.clock.advance(minutes=5)
    env = rig.advisor.matchup(A)  # still cooling down: cached, no load
    assert env.data == before
    assert len(rig.world.attempts) == attempts_before
    assert ages(env)[f"yahoo:matchup:{A}"].stale is True
    assert any("yahoo backed off until" in n for n in env.notices)
    assert rig.advisor.status().data["sites"]["yahoo"]["backed_off_until"] is not None

    rig.clock.advance(minutes=BACKOFF_COOLDOWN_S // 60)
    env = rig.advisor.matchup(A)
    assert len(rig.world.attempts) > attempts_before  # loads again
    assert not any("back" in n for n in env.notices)
    assert rig.advisor.status().data["sites"]["yahoo"]["backed_off_until"] is None


def test_other_fetch_failures_do_not_back_off(make):
    rig = make()
    rig.advisor.list_leagues()
    rig.clock.advance(minutes=30)
    rig.world.raise_on["yahoo"] = FetchFailed("yahoo", 404)
    attempts_before = len(rig.world.attempts)
    rig.advisor.analyze_week(A)
    assert len(rig.world.attempts) > attempts_before + 1  # each URL is still tried
    assert rig.cache.get("backoff:yahoo") is None


def test_busy_refresh_within_ttl_is_not_stale(make):
    rig = make()
    rig.advisor.matchup(A)
    rig.clock.advance(minutes=5)
    rig.factory.busy = True
    env = rig.advisor.matchup(A, refresh=True)
    assert "browser busy in another session; showing cached data" in env.notices
    assert ages(env)[f"yahoo:matchup:{A}"].stale is False


def test_value_error_from_a_source_is_a_notice(make):
    world = World(errors={("matchup", A): ValueError("odd page")})
    env = make(world).advisor.matchup(A)
    assert env.data is None
    assert any("odd page" in n for n in env.notices)


def test_needs_login_is_remembered_by_status(make):
    rig = make()
    rig.advisor.matchup(A)
    rig.clock.advance(minutes=30)
    rig.world.needs_login.add("yahoo")
    rig.advisor.matchup(A)
    status = rig.advisor.status().data
    assert status["sites"]["yahoo"]["logged_in"] is False
    assert status["sites"]["yahoo"]["daily_cap"] == 250
    assert "matchup" in status["cache"]


def test_busy_returns_cached_with_notice(make):
    rig = make()
    before = rig.advisor.matchup(A).data
    rig.clock.advance(minutes=30)
    rig.factory.busy = True
    env = rig.advisor.matchup(A)
    assert env.data == before
    assert "browser busy in another session; showing cached data" in env.notices
    assert ages(env)[f"yahoo:matchup:{A}"].stale is True


def test_no_cache_and_busy_gives_none(make):
    rig = make()
    rig.advisor.list_leagues()
    rig.factory.busy = True
    env = rig.advisor.matchup(A)
    assert env.data is None
    assert "browser busy in another session; showing cached data" in env.notices


def test_refresh_floor_120s(make):
    rig = make()
    rig.advisor.matchup(A)
    rig.clock.advance(seconds=60)
    rig.advisor.matchup(A, refresh=True)
    assert rig.factory.calls == 1
    rig.clock.advance(seconds=90)
    rig.advisor.matchup(A, refresh=True)
    assert rig.factory.calls == 2


def test_parse_error_names_page_and_gives_none(make):
    world = World(errors={("matchup", A): ParseError("matchup", "no stat table")})
    rig = make(world)
    env = rig.advisor.matchup(A)
    assert env.data is None
    assert any("matchup" in n and "no stat table" in n for n in env.notices)


def test_league_name_lookup_ambiguous_raises(make):
    world = World()
    world.leagues = [
        LeagueRef(A, "Hoops League", "9", "x"),
        LeagueRef(B, "Hoops Cup", "9", "y"),
    ]
    rig = make(world)
    with pytest.raises(ValueError, match="Hoops League") as exc:
        rig.advisor.matchup("hoops")
    assert "Hoops Cup" in str(exc.value)
    assert rig.advisor.matchup("CUP").data.league_id == B
    assert rig.advisor.matchup(A).data.league_id == A


def test_settings_week_comes_from_matchup_ttl_data(make):
    rig = make()
    rig.advisor.league_settings(A)
    rig.world.matchups[A] = matchup(A, week=7)
    rig.clock.advance(hours=1)  # settings (7 d) still fresh, week info (20 min) stale
    env = rig.advisor.league_settings(A)
    assert env.data.current_week == 7
    assert rig.world.count(f"y/settings/{A}") == 1


def test_settings_and_week_share_matchup_page_with_real_yahoo_source(tmp_path):
    pages = {
        yahoo_urls.home(): "home.html",
        yahoo_urls.league_settings(51347): "settings_51347.html",
        yahoo_urls.matchup(51347): "matchup_51347.html",
    }
    loads: list[str] = []

    class Session:
        def fetch(self, site, url):
            loads.append(url)
            return PageResult(url, url, (FIXTURES / pages[url]).read_text("utf-8"), NOW)

    @contextmanager
    def factory():
        yield Session()

    advisor = Advisor(Config(), Cache(tmp_path / "c.db"), factory, Clock())
    env = advisor.league_settings("51347")
    assert env.data.current_week == 6
    assert loads.count(yahoo_urls.matchup(51347)) == 1


# -- week projections from day pages -----------------------------------------------


def test_week_projections_use_day_pages(make):
    healthy = make().advisor.analyze_week(A).data["analysis"]
    world = World(day_games={(BIG.name, date(2026, 11, 26)): 0.0})
    rig = make(world)
    hurt = rig.advisor.analyze_week(A).data["analysis"]
    # Big Center is projected not to play on 26 Nov: that day loses his points.
    assert healthy.my_proj.pts - hurt.my_proj.pts == pytest.approx(BIG.pts)
    days = [f"b/day/{d.isoformat()}" for d in sorted(SCHEDULE)]
    assert [rig.world.count(u) for u in days] == [1, 1, 1, 1, 1]


def test_short_yahoo_name_gets_its_bbm_projection(make):
    full = make().advisor.analyze_week(A)
    world = World(yahoo_names={MID.name: "N. Alexander-Walker"})
    short = make(world).advisor.analyze_week(A)
    assert not any("no BBM projection" in n for n in short.notices)
    assert short.data["analysis"].my_proj.pts == pytest.approx(
        full.data["analysis"].my_proj.pts
    )
    assert short.data["analysis"].my_proj.pts > 0


def test_short_opponent_name_gets_its_bbm_projection(make):
    full = make().advisor.analyze_week(A)
    world = World(yahoo_names={OPP[0].name: "O. One"})
    short = make(world).advisor.analyze_week(A)
    assert not any("no BBM projection" in n for n in short.notices)
    assert short.data["analysis"].opp_proj.pts == pytest.approx(
        full.data["analysis"].opp_proj.pts
    )


def test_unmatched_yahoo_name_is_still_reported_without_a_projection(make):
    world = World(yahoo_names={MID.name: "Z. Unknown-Person"})
    env = make(world).advisor.analyze_week(A)
    assert any("no BBM projection for Z. Unknown-Person" in n for n in env.notices)


def test_unavailable_day_page_leaves_that_day_out_with_notice(make):
    world = World(errors={("day", "2026-11-26"): ParseError("bbm", "no table")})
    healthy = make().advisor.analyze_week(A).data["analysis"]
    env = make(world).advisor.analyze_week(A)
    # 26 Nov has neither schedule nor projections: Star, Big and Wing (LAL, DEN, MIA)
    # play that day in the healthy week, and nothing is invented in their place.
    lost = STAR.pts + BIG.pts + WING.pts
    assert healthy.my_proj.pts - env.data["analysis"].my_proj.pts == pytest.approx(lost)
    assert any("2026-11-26" in n and "unavailable" in n for n in env.notices)


def test_no_day_page_at_all_means_no_week_analysis(make):
    world = World(
        errors={("day", d.isoformat()): ParseError("bbm", "no table") for d in SCHEDULE}
    )
    env = make(world).advisor.analyze_week(A)
    assert env.data is None
    assert any("no BBM day page available" in n for n in env.notices)


def test_schedule_comes_from_day_pages_and_has_no_separate_source(make):
    rig = make()
    env = rig.advisor.analyze_week(A)
    sources = {a.source for a in env.ages}
    assert not any(s.startswith("nba") or s.endswith("schedule") for s in sources)
    assert {f"bbm:day:{d.isoformat()}" for d in SCHEDULE} <= sources


def test_today_page_is_loaded_for_the_schedule_when_lineups_take_effect_tomorrow(
    make,
):
    world = World()
    world.settings[A] = settings(A, "Hoops League", lineup_effective="tomorrow")
    rig = make(world)
    rig.advisor.analyze_week(A)
    assert rig.world.count("b/day/2026-11-25") == 1


def test_day_without_games_is_an_empty_schedule_day(make):
    rig = make()
    day = date(2026, 11, 24)  # not in SCHEDULE: the fake page lists no games
    env = rig.advisor.schedule(day, day)
    assert env.data == {day: []}


def test_analyze_week_envelope_has_objective_build_and_targets(make):
    world = World()
    world.settings[A] = settings(A, "Hoops League", scoring_type="one_win")
    data = make(world).advisor.analyze_week(A).data
    assert data["objective"] == "one_win"
    assert data["matchup_win_prob"] == data["analysis"].matchup_win_prob
    assert data["expected_cats_won"] == data["analysis"].expected_cats_won
    assert len(data["targets"]) == 5
    assert "build" in data and "at_risk_strong" in data


# -- free agents, BBM league match, waivers ------------------------------------------


def test_bbm_league_auto_match_unique(make):
    rig = make()
    env = rig.advisor.free_agents("Big Men")
    assert rig.world.count("b/league/1002") == 1
    assert rig.world.count(f"y/players/{B}?status=A") == 0
    keys = {f.player.key for f in env.data["free_agents"]}
    assert keys == {HOT.key, MEH.key, WAIVER.key}


def test_bbm_league_ambiguous_falls_back_to_yahoo_with_notice(make):
    world = World(
        league_options={"1001": "_1. Hoops… (Yahoo!)", "1003": "_3. Hoops Lea…"}
    )
    rig = make(world)
    env = rig.advisor.free_agents(A)
    assert rig.world.count(f"y/players/{A}?status=A") == 1
    assert not any(u.startswith("b/league/") for u in rig.world.loads)
    notice = next(n for n in env.notices if "set bbm_league_id for Hoops League" in n)
    assert "_3. Hoops Lea…" in notice
    assert {f.player.key for f in env.data["free_agents"]} == {
        HOT.key,
        MEH.key,
        WAIVER.key,
    }


def test_configured_bbm_league_id_wins(make):
    config = Config(leagues={A: LeagueConfig(bbm_league_id="1002")})
    rig = make(config=config)
    rig.advisor.free_agents(A)
    assert rig.world.count("b/league/1002") == 1


def test_waiver_merge_takes_date_and_positions_from_yahoo(make):
    rig = make()
    env = rig.advisor.free_agents(B)
    waiver = next(f for f in env.data["free_agents"] if f.player.key == WAIVER.key)
    assert waiver.on_waivers and waiver.waiver_clear == date(2026, 11, 27)
    assert waiver.player.positions == ("PG", "SG")
    assert env.data["claims"] == {
        WAIVER.key: {"waiver_clear": "2026-11-27", "faab": True}
    }


def test_short_yahoo_waiver_name_matches_the_bbm_waiver_player(make):
    short = Player(player_key("W. Guy", "CHI"), "W. Guy", "CHI", ("PG", "SG"))
    world = World(
        yahoo_waivers=[FreeAgent(short, True, waiver_clear=date(2026, 11, 27))]
    )
    env = make(world).advisor.free_agents(B)
    waivers = [f for f in env.data["free_agents"] if f.on_waivers]
    assert [f.player.key for f in waivers] == [WAIVER.key]  # one player, BBM's key
    assert waivers[0].waiver_clear == date(2026, 11, 27)
    assert not any("no Yahoo waiver date" in n for n in env.notices)


def test_waiver_player_reconciled_to_one_row_is_not_merged_into_another(make):
    short = Player(player_key("W. Guy", "CHI"), "W. Guy", "CHI", ("PG", "SG"))
    # a BBM free agent on another team whose name alone equals the Yahoo waiver's
    namesake = P("W. Guy", "ATL", ("PG",), 0.0, 8)
    world = World(
        yahoo_waivers=[FreeAgent(short, True, waiver_clear=date(2026, 11, 27))],
        bbm_extra_fas=[namesake],
    )
    env = make(world).advisor.free_agents(B)
    fas = env.data["free_agents"]
    waivers = [f for f in fas if f.on_waivers]
    assert [f.player.key for f in waivers] == [WAIVER.key]
    assert next(f for f in fas if f.player.key == namesake.key).on_waivers is False
    assert len(fas) == len({f.player.key for f in fas})


def test_waiver_without_yahoo_date_keeps_none_with_notice(make):
    world = World(yahoo_waivers=[])
    env = make(world).advisor.free_agents(B)
    waiver = next(f for f in env.data["free_agents"] if f.player.key == WAIVER.key)
    assert waiver.on_waivers and waiver.waiver_clear is None
    assert any("Waiver Guy" in n for n in env.notices)


# -- streamers, history, brief ---------------------------------------------------------


def test_rank_streamers_never_suggests_never_drop(make, monkeypatch):
    # Spare Guard's swaps gain ~0.04 expected categories: below the negligible cut.
    monkeypatch.setattr("h2h_advisor.analysis.streamers.MIN_GAIN", 1e-9)
    config = Config(
        never_drop=[WING.name]
    )  # Bench Wing is the lowest ROS value on my roster
    env = make(config=config).advisor.rank_streamers(A, "rest_of_week")
    options = env.data["options"]
    assert options, env.data["reason"]
    assert all(o.drop.key != WING.key for o in options)
    assert all(o.follow_up is None or o.follow_up.drop.key != WING.key for o in options)


def test_rank_streamers_without_never_drop_may_drop_lowest(make):
    env = make().advisor.rank_streamers(A, "rest_of_week")
    assert any(o.drop.key == WING.key for o in env.data["options"])


def test_rank_streamers_reports_waiver_claims(make):
    env = make().advisor.rank_streamers(B, "rest_of_week")
    options = env.data["options"]
    adds = {o.add.key for o in options} | {
        o.follow_up.add.key for o in options if o.follow_up
    }
    claims = env.data["claims"]
    assert set(claims) == adds & {WAIVER.key}
    for claim in claims.values():
        assert claim == {"waiver_clear": "2026-11-27", "faab": True}


def test_history_cached_permanently_fetches_only_new_weeks(make):
    world = World()
    world.matchups[A] = matchup(A, week=5)
    rig = make(world)
    for week in (1, 2, 3):
        rig.cache.put(
            f"history:{A}:{week}",
            matchup(A, week=week, moves_used=0),
            now=NOW - timedelta(days=60),
        )
    env = rig.advisor.history(A)
    assert rig.world.history_calls == [(A, [4])]
    assert [h["week"] for h in env.data] == [1, 2, 3, 4]
    assert all(set(h["results"]) == set(NINE_CAT) for h in env.data)
    rig.clock.advance(days=30)
    rig.advisor.history(A)
    assert rig.world.history_calls == [(A, [4])]


def test_build_uses_pinned_config(make):
    config = Config(
        leagues={A: LeagueConfig(build={"punt": ["TO"], "strong": ["PTS"]})}
    )
    build = make(config=config).advisor.build(A).data
    assert build.source == "pinned" and build.punt == frozenset({Category.TO})


def test_daily_brief_one_session_all_leagues(make):
    world = World(errors={("matchup", B): ParseError("matchup", "no stat table")})
    rig = make(world)
    env = rig.advisor.daily_brief()
    assert rig.factory.calls == 1
    a, b = env.data["leagues"][A], env.data["leagues"][B]
    assert a["analysis"] is not None and len(a["streamers"]) <= 3
    assert isinstance(a["facts"], list) and a["facts"]
    assert b["analysis"] is None
    assert any("matchup" in n and "no stat table" in n for n in env.notices)
    # one day-page load per date, shared by both leagues
    for d in sorted(SCHEDULE):
        assert rig.world.count(f"b/day/{d.isoformat()}") <= 1
    assert rig.world.count("b/ros") == 1


def test_daily_brief_reports_progress_after_each_league(make):
    seen = []
    env = make().advisor.daily_brief(
        "all", progress=lambda done, total, league: seen.append((done, total, league))
    )
    assert set(env.data["leagues"]) == {A, B}
    assert seen == [(1, 2, A), (2, 2, B)]


def test_daily_brief_shares_day_pages_when_a_league_fails_after_them(make):
    world = World(errors={("league_view", "1002"): RuntimeError("boom")})
    rig = make(world)
    env = rig.advisor.daily_brief()
    assert rig.factory.calls == 1
    assert env.data["leagues"][A]["analysis"] is not None
    assert env.data["leagues"][B] is None
    assert f"{B}: boom" in env.notices
    # both leagues analysed the whole week; each date's page loaded once
    assert [rig.world.count(f"b/day/{d.isoformat()}") for d in sorted(SCHEDULE)] == [
        1
    ] * 5
    assert rig.world.count("b/league/1002") == 1


def test_daily_brief_today_horizon_still_analyses_the_week_from_day_pages(make):
    world = World(day_games={(BIG.name, date(2026, 11, 28)): 0.0})
    rig = make(world)
    brief = rig.advisor.daily_brief([A], horizon="today").data["leagues"][A]
    assert rig.world.count("b/day/2026-11-28") == 1
    week = make(World(day_games=dict(world.day_games))).advisor.analyze_week(A).data
    assert brief["analysis"]["analysis"].my_proj == week["analysis"].my_proj


def test_daily_brief_facts_are_deterministic(make):
    first = make().advisor.daily_brief([A]).data["leagues"][A]["facts"]
    second = make().advisor.daily_brief([A]).data["leagues"][A]["facts"]
    assert first == second
    assert any(f.startswith("Light days left") for f in first)
    assert any(f.startswith("Usable games left") for f in first)
    week = make().advisor.daily_brief([A], horizon="rest_of_week").data["leagues"][A]
    assert 1 <= len(week["streamers"]) <= 3


def test_projections_and_schedule(make):
    rig = make()
    day = rig.advisor.projections("day").data
    assert day.game_date == date(2026, 11, 25)
    assert rig.advisor.projections("ros").data.rows
    with pytest.raises(ValueError):
        rig.advisor.projections("week")
    sched = rig.advisor.schedule(date(2026, 11, 25), date(2026, 11, 26)).data
    assert sched == {
        date(2026, 11, 25): sorted(SCHEDULE[date(2026, 11, 25)]),
        date(2026, 11, 26): sorted(SCHEDULE[date(2026, 11, 26)]),
    }


def test_todays_games_table_missing_started_games_is_completed_from_player_rows(make):
    today = date(2026, 11, 25)
    world = World(omitted_games={today: ("LAL", "BOS")})
    env = make(world).advisor.schedule(today, today)
    assert env.data == {today: sorted(SCHEDULE[today])}  # nothing lost
    notice = next(n for n in env.notices if "may omit games already started" in n)
    assert "LAL" in notice and "BOS" in notice


def test_future_games_table_missing_games_of_playing_teams_is_unreadable(make):
    tomorrow = date(2026, 11, 26)
    world = World(omitted_games={tomorrow: ("LAL",)})
    env = make(world).advisor.schedule(date(2026, 11, 25), tomorrow)
    assert list(env.data) == [date(2026, 11, 25)]  # strict: no schedule invented
    assert any("games table lacks games for" in n and "LAL" in n for n in env.notices)


def test_zero_game_day_is_not_light_but_has_its_own_fact(make, monkeypatch):
    schedule = {**SCHEDULE, date(2026, 11, 26): frozenset()}
    monkeypatch.setattr(f"{__name__}.SCHEDULE", schedule)
    facts = make().advisor.daily_brief([A]).data["leagues"][A]["facts"]
    light = next(f for f in facts if f.startswith("Light days left"))
    assert "2026-11-26" not in light and "(0 games)" not in light
    assert "No NBA games: 2026-11-26" in facts


def test_no_game_days_are_comma_separated_and_absent_days_unreported(make, monkeypatch):
    schedule = {**SCHEDULE, date(2026, 11, 26): frozenset(),
                date(2026, 11, 28): frozenset()}  # fmt: skip
    monkeypatch.setattr(f"{__name__}.SCHEDULE", schedule)
    world = World(errors={("day", "2026-11-29"): ParseError("bbm", "no table")})
    facts = make(world).advisor.daily_brief([A]).data["leagues"][A]["facts"]
    assert "No NBA games: 2026-11-26, 2026-11-28" in facts
    assert not any(
        "2026-11-29" in f for f in facts if f.startswith(("No NBA", "Light"))
    )


def test_one_game_light_day_says_game_not_games(make, monkeypatch):
    schedule = {**SCHEDULE, date(2026, 11, 26): frozenset({"LAL", "MIA"})}
    monkeypatch.setattr(f"{__name__}.SCHEDULE", schedule)
    facts = make().advisor.daily_brief([A]).data["leagues"][A]["facts"]
    assert any("2026-11-26 (1 game)" in f for f in facts if f.startswith("Light"))
    assert not any("(1 games)" in f for f in facts)


def test_no_game_fact_absent_when_every_day_has_games(make):
    facts = make().advisor.daily_brief([A]).data["leagues"][A]["facts"]
    assert not any(f.startswith("No NBA games") for f in facts)


def test_day_without_a_page_is_not_reported_as_a_light_day(make):
    healthy = make().advisor.daily_brief([A]).data["leagues"][A]["facts"]
    assert any("2026-11-26 (2 games)" in f for f in healthy if f.startswith("Light"))
    world = World(errors={("day", "2026-11-26"): ParseError("bbm", "no table")})
    env = make(world).advisor.daily_brief([A])
    facts = env.data["leagues"][A]["facts"]
    assert not any("2026-11-26" in f for f in facts if f.startswith("Light"))
    assert any("2026-11-26" in n and "unavailable" in n for n in env.notices)


def test_day_page_cached_before_games_were_parsed_is_not_reused(make):
    rig = make()
    day = date(2026, 11, 25)
    old = {
        "rows": [],
        "game_date": day.isoformat(),
        "filter_value": "All",
        "league_value": None,
        "league_options": {},
    }  # no "games": the old layout
    rig.cache.put("bbm:day:2026-11-25", old, now=rig.clock())
    env = rig.advisor.schedule(day, day)
    assert env.data == {day: sorted(SCHEDULE[day])}
    assert rig.world.count("b/day/2026-11-25") == 1


def test_schedule_range_is_capped_at_fourteen_days(make):
    rig = make()
    start = date(2026, 11, 25)
    with pytest.raises(ValueError, match="14"):
        rig.advisor.schedule(start, start + timedelta(days=14))  # 15 days
    assert rig.world.loads == []
    env = rig.advisor.schedule(start, start + timedelta(days=13))  # 14 days
    assert len(env.data) == 14


def test_schedule_end_before_start_is_a_value_error(make):
    with pytest.raises(ValueError, match="before"):
        make().advisor.schedule(date(2026, 11, 26), date(2026, 11, 25))


def test_schedule_skips_days_without_a_page_and_says_so(make):
    world = World(errors={("day", "2026-11-26"): ParseError("bbm", "no table")})
    env = make(world).advisor.schedule(date(2026, 11, 25), date(2026, 11, 26))
    assert list(env.data) == [date(2026, 11, 25)]
    assert any("2026-11-26" in n for n in env.notices)


def test_schedule_with_no_page_at_all_is_none(make):
    world = World(errors={("day", "2026-11-25"): ParseError("bbm", "no table")})
    assert (
        make(world).advisor.schedule(date(2026, 11, 25), date(2026, 11, 25)).data
        is None
    )


# -- fix round 1 -----------------------------------------------------------------------


def test_free_agent_projected_idle_every_day_is_not_recommended(make):
    world = World(day_games={(HOT.name, d): 0.0 for d in SCHEDULE})
    env = make(world).advisor.rank_streamers(A, "rest_of_week")
    adds = {o.add.key for o in env.data["options"] or []}
    assert HOT.key not in adds


def test_free_agent_out_per_bbm_status_is_not_recommended(make):
    world = World(bbm_status={HOT.name: "OUT"})
    rig = make(world)
    fas = rig.advisor.free_agents(A).data["free_agents"]
    assert next(f for f in fas if f.player.key == HOT.key).player.status == "O"
    assert HOT.key not in {
        o.add.key for o in rig.advisor.rank_streamers(A).data["options"]
    }
    assert HOT.key not in {u.add.key for u in rig.advisor.long_term_upgrades(A).data}


def test_long_term_upgrades_skip_players_with_no_ros_games(make):
    upgrades = make().advisor.long_term_upgrades(A).data
    hot = next(u for u in upgrades if u.add.key == HOT.key)
    assert hot.ros_games == 40
    world = World(ros_games={HOT.name: 0})
    assert HOT.key not in {
        u.add.key for u in make(world).advisor.long_term_upgrades(A).data
    }


def test_yahoo_waivers_override_bbm_free_agent_status(make):
    world = World()
    world.yahoo_waivers.append(
        FreeAgent(HOT.player(), on_waivers=True, waiver_clear=date(2026, 11, 28))
    )
    rig = make(world)
    fas = {f.player.key: f for f in rig.advisor.free_agents(A).data["free_agents"]}
    assert fas[HOT.key].on_waivers and fas[HOT.key].waiver_clear == date(2026, 11, 28)
    assert rig.world.count(f"y/players/{A}?status=W") == 1


def test_yahoo_waivers_load_even_when_bbm_shows_none(make):
    rig = make()
    rig.advisor.free_agents(A)
    assert rig.world.count(f"y/players/{A}?status=W") == 1


def test_auto_match_success_adds_notice(make):
    env = make().advisor.free_agents("Big Men")
    assert "matched Big Men League ↔ BBM _2. Big Men Le… (Yahoo!)" in env.notices


@pytest.mark.parametrize(
    ("names", "options", "expected"),
    [
        ({"1": "Hoops Legends"}, {"9": "_1. Hoops (Yahoo!)"}, {}),  # not truncated
        ({"1": "Dynasty A", "2": "Dynasty B"}, {"9": "_1. Dynasty… (Yahoo!)"}, {}),
        ({"1": "Dynasty A", "2": "Dynasty B"}, {"8": "_1. Dynasty A", "9": "_2. Dynasty B"},
         {"1": "8", "2": "9"}),
        ({"1": "St. Louis"}, {"9": "_1. St. Louis (Yahoo!)"}, {"1": "9"}),
        ({"1": "Big  Men League"}, {"9": "_2. big men le... (Yahoo!)"}, {"1": "9"}),
        # BBM may spell a name differently ("I Liga" for Yahoo's "1 Liga"): no guess.
        ({"1": "1 Liga Hoops"}, {"9": "_2. I Liga Hoops (Yahoo!)"}, {}),
    ],
)  # fmt: skip
def test_match_bbm_leagues_is_exact_or_truncated_and_one_to_one(
    names, options, expected
):
    assert match_bbm_leagues(names, options) == expected


def test_match_bbm_leagues_on_the_real_fixture_names():
    bbm = parse_page((BBM_FIXTURES / "ros.html").read_text("utf-8"), "ros")
    yahoo = {
        r.league_id: r.name
        for r in parse_league_list((FIXTURES / "home.html").read_text("utf-8"))
    }
    assert match_bbm_leagues(yahoo, bbm.league_options) == {
        "51347": "602", "57243": "30008", "59422": "28681", "211727": "26949",
        "259619": "31648",
    }  # fmt: skip


def test_week_and_matchup_mismatch_refetches_week_info_once(make):
    world = World()
    world.matchups[A] = matchup(A, week=7)
    world.week_seq = [6]  # first week answer is stale, the forced one agrees
    rig = make(world)
    env = rig.advisor.analyze_week(A)
    assert env.data is not None
    assert not any("disagree" in n for n in env.notices)


def test_week_and_matchup_still_mismatched_gives_no_analysis(make):
    world = World()
    world.matchups[A] = matchup(A, week=7)
    world.week_seq = [6, 6]
    env = make(world).advisor.analyze_week(A)
    assert env.data is None
    assert any("disagree" in n for n in env.notices)


def test_history_failed_week_is_negatively_cached_for_a_day(make):
    world = World(errors={("history", A): ParseError("matchup", "no stat table")})
    world.matchups[A] = matchup(A, week=3)
    rig = make(world)
    rig.cache.put(f"history:{A}:1", matchup(A, week=1, moves_used=0), now=NOW)
    env = rig.advisor.history(A)
    assert [h["week"] for h in env.data] == [1]
    assert ages(env)[f"yahoo:history:{A}"].stale is True
    assert any("week 2 history unavailable" in n for n in env.notices)
    assert rig.world.history_calls == [(A, [2])]
    rig.clock.advance(hours=1)
    rig.advisor.history(A)
    assert rig.world.history_calls == [(A, [2])]  # not retried within 24 h
    rig.clock.advance(hours=24)
    del world.errors[("history", A)]
    env = rig.advisor.history(A)
    assert rig.world.history_calls == [(A, [2]), (A, [2])]
    assert [h["week"] for h in env.data] == [1, 2]
    assert ages(env)[f"yahoo:history:{A}"].stale is False


def test_today_horizon_keeps_schedule_days_for_a_player_resting_today(make):
    world = World(day_games={(BIG.name, date(2026, 11, 25)): 0.0})
    rig = make(world)
    with rig.advisor._calling() as call:
        week = rig.advisor._week(call, A, "today", False)
    # every day's page is loaded for the schedule, but only today's rows are used
    assert rig.world.count("b/day/2026-11-26") == 1
    # not today (g = 0); later days are left to the schedule
    assert week.ctx.active_days[BIG.key] == {
        date(2026, 11, d) for d in (26, 27, 28, 29)
    }
    full = make(world)
    with full.advisor._calling() as call:
        whole = full.advisor._week(call, A, "rest_of_week", False)
    assert whole.ctx.active_days[BIG.key] == {date(2026, 11, 26), date(2026, 11, 28)}


def test_player_idle_on_every_loaded_day_keeps_the_locked_days_before_effective_start(
    make,
):
    # Tomorrow lineup: today (25th) is locked, so its page gives the schedule but no
    # projections. DEN and BOS play today.
    world = World(
        day_games={
            (BIG.name, date(2026, 11, 26)): 0.0,
            (BIG.name, date(2026, 11, 28)): 0.0,
            (OPP[1].name, date(2026, 11, 27)): 0.0,
            (OPP[1].name, date(2026, 11, 28)): 0.0,
        }
    )
    world.settings[A] = settings(A, "Hoops League", lineup_effective="tomorrow")
    rig = make(world)
    with rig.advisor._calling() as call:
        week = rig.advisor._week(call, A, "rest_of_week", False)
    today = {date(2026, 11, 25)}
    assert week.ctx.active_days[BIG.key] == today
    assert week.ctx.active_days[OPP[1].key] == today
    analysis = rig.advisor.analyze_week(A).data["analysis"]
    healthy = World()
    healthy.settings[A] = settings(A, "Hoops League", lineup_effective="tomorrow")
    full = make(healthy).advisor.analyze_week(A).data["analysis"]
    # Big Center keeps today's game and loses only the 26th and 28th
    assert full.my_proj.pts - analysis.my_proj.pts == pytest.approx(2 * BIG.pts)


def test_stale_cached_matchup_from_last_week_is_corrected_in_one_call(make):
    rig = make()
    rig.advisor.analyze_week(A)  # caches week 6 matchup and week info
    rig.clock.advance(minutes=10)  # both still within the matchup TTL
    rig.world.matchups[A] = matchup(A, week=7)  # Sunday boundary passed on Yahoo
    rig.cache.put(f"week:{A}", (7, WEEK_START, WEEK_END), now=rig.clock())
    calls_before = rig.factory.calls
    env = rig.advisor.analyze_week(A)
    assert env.data is not None
    assert not any("disagree" in n for n in env.notices)
    assert rig.advisor.matchup(A).data.week == 7  # corrected in the cache too
    assert rig.factory.calls == calls_before + 1


# -- recent minutes (ESPN) -------------------------------------------------------


class FakeEspn:
    def __init__(self, mpg=None, fail=False):
        self.mpg, self.fail, self.calls = mpg, fail, 0

    def athlete_id(self, name, team):
        self.calls += 1
        if self.fail:
            raise EspnError("boom")
        return "id-" + name

    def recent_minutes(self, athlete_id):
        self.calls += 1
        if self.fail:
            raise EspnError("boom")
        mpg = self.mpg(athlete_id) if callable(self.mpg) else self.mpg
        return RecentMinutes(5, mpg, [mpg] * 5, date(2026, 11, 24))


def test_rotation_players_are_kept_off_the_drop_list(make):
    espn = FakeEspn(mpg=28.0)
    rig = make(espn=espn)
    brief = rig.advisor.daily_brief([A], horizon="rest_of_week")
    league = brief.data["leagues"][A]
    kept = next(f for f in league["facts"] if f.startswith("Kept off the drop list"))
    assert "28 mpg" in kept and "plays " in kept  # the reason: minutes and the days he plays
    assert league["streamers"] == [] and league["streamer_reason"] == "drop pool is empty"
    assert "espn:minutes" in ages(brief)


def test_minutes_in_line_with_the_projection_do_not_protect(make):
    world = World(proj_minutes={p.name: 27.0 for p in EVERYONE})  # BBM already expects 27
    league = make(world, espn=FakeEspn(mpg=28.0)).advisor.daily_brief(
        [A], horizon="rest_of_week"
    ).data["leagues"][A]
    assert not any(f.startswith("Kept off the drop list") for f in league["facts"])


def test_minutes_above_the_projection_protect(make):
    world = World(proj_minutes={p.name: 21.0 for p in EVERYONE})  # 28 vs 21: a role BBM missed
    league = make(world, espn=FakeEspn(mpg=28.0)).advisor.daily_brief(
        [A], horizon="rest_of_week"
    ).data["leagues"][A]
    assert any(f.startswith("Kept off the drop list") for f in league["facts"])


def test_low_minutes_do_not_protect_anyone(make):
    rig = make(espn=FakeEspn(mpg=12.0))
    league = rig.advisor.daily_brief([A], horizon="rest_of_week").data["leagues"][A]
    assert not any(f.startswith("Kept off the drop list") for f in league["facts"])
    assert league["streamer_reason"] != "drop pool is empty"


def test_espn_failure_is_a_notice_not_an_error(make):
    rig = make(espn=FakeEspn(fail=True))
    brief = rig.advisor.daily_brief([A], horizon="rest_of_week")
    assert any("ESPN game logs unavailable" in n for n in brief.notices)
    assert brief.data["leagues"][A]["analysis"] is not None


def test_recent_minutes_are_cached_per_player(make):
    espn = FakeEspn(mpg=28.0)
    rig = make(espn=espn)
    rig.advisor.daily_brief([A], horizon="rest_of_week")
    first = espn.calls
    assert first > 0
    rig.clock.advance(minutes=30)
    rig.advisor.daily_brief([A], horizon="rest_of_week")
    assert espn.calls == first  # within the 6 h TTL: no new requests


def test_without_an_espn_source_nothing_changes(make):
    league = make().advisor.daily_brief([A], horizon="rest_of_week").data["leagues"][A]
    assert not any(f.startswith("Kept off the drop list") for f in league["facts"])


# -- notes: the user's short-lived guidance ------------------------------------------


def test_keep_note_protects_like_never_drop(make, monkeypatch):
    monkeypatch.setattr("h2h_advisor.analysis.streamers.MIN_GAIN", 1e-9)
    rig = make()
    env = rig.advisor.add_note("keep", WING.name, "Hoops", "more minutes with the starter out")
    assert env.data["note"].league == A  # a name part resolves to the league id
    options = rig.advisor.rank_streamers(A, "rest_of_week").data["options"]
    assert options
    assert all(o.drop.key != WING.key for o in options)
    # ...in that league only
    other = rig.advisor.rank_streamers(B, "rest_of_week").data["options"]
    assert any(o.drop.key == WING.key for o in other)


def test_drop_first_note_beats_the_rotation_check(make, monkeypatch):
    # Everyone plays 28 mpg: the pool is empty until the user names his drop.
    monkeypatch.setattr("h2h_advisor.analysis.streamers.MIN_GAIN", 1e-9)  # Spare's swaps are small
    rig = make(espn=FakeEspn(mpg=28.0))
    league = rig.advisor.daily_brief([A], horizon="rest_of_week").data["leagues"][A]
    assert league["streamer_reason"] == "drop pool is empty"
    rig.advisor.add_note("drop_first", SPARE.name, A, "useless")
    league = rig.advisor.daily_brief([A], horizon="rest_of_week").data["leagues"][A]
    assert league["streamer_reason"] != "drop pool is empty"
    assert {o.drop.key for o in league["streamers"]} == {SPARE.key}, league["streamer_reason"]
    noted = next(f for f in league["facts"] if f.startswith("Your notes"))
    assert "drop first Spare Guard" in noted and "useless" in noted


def test_drop_first_wins_over_keep_and_never_drop(make, monkeypatch):
    monkeypatch.setattr("h2h_advisor.analysis.streamers.MIN_GAIN", 1e-9)
    rig = make(config=Config(never_drop=[SPARE.name]), espn=FakeEspn(mpg=28.0))
    rig.advisor.add_note("drop_first", SPARE.name, A)
    league = rig.advisor.daily_brief([A], horizon="rest_of_week").data["leagues"][A]
    assert {o.drop.key for o in league["streamers"]} == {SPARE.key}, league["streamer_reason"]


def test_notes_expire_and_can_be_removed(make):
    rig = make()
    keep = rig.advisor.add_note("keep", WING.name, "all", until=date(2026, 11, 29)).data["note"]
    drop = rig.advisor.add_note("drop_first", SPARE.name, A).data["note"]
    assert {n.id for n in rig.advisor.list_notes().data["active"]} == {keep.id, drop.id}
    assert {n.id for n in rig.advisor.list_notes(B).data["active"]} == {keep.id}  # "all" applies
    rig.advisor.remove_note(drop.id)
    assert [n.id for n in rig.advisor.list_notes().data["active"]] == [keep.id]
    rig.clock.advance(days=5)  # past Sun 29 Nov
    assert rig.advisor.list_notes().data["active"] == []
    with pytest.raises(ValueError, match="no note"):
        rig.advisor.remove_note(drop.id)


def test_unreadable_notes_are_a_notice_not_a_failed_brief(make, tmp_path):
    from h2h_advisor import paths

    paths.notes_file().parent.mkdir(parents=True, exist_ok=True)
    paths.notes_file().write_text("{broken", encoding="utf-8")
    brief = make().advisor.daily_brief([A], horizon="rest_of_week")
    assert brief.data["leagues"][A]["analysis"] is not None
    assert any(n.startswith("notes ignored") for n in brief.notices)


def test_build_ros_leaves_punted_categories_out():
    from h2h_advisor.service import _build_ros

    blocker = {Category.BLK: 3.0, Category.REB: 1.5, Category.STL: 1.5, Category.PTS: -2.0,
               Category.AST: -2.0, Category.FT_PCT: -2.0, Category.TPM: 0.0,
               Category.FG_PCT: 0.0, Category.TO: 0.0}  # fmt: skip
    values = {"blocker": sum(blocker.values()) / 9, "nocats": 0.4}
    punt = frozenset({Category.PTS, Category.AST, Category.FT_PCT})
    out = _build_ros(values, {"blocker": blocker}, punt)
    assert values["blocker"] == pytest.approx(0.0)  # BBM's Value: a throwaway
    assert out["blocker"] == pytest.approx(1.0)  # for a punt-PTS/AST/FT build: a keeper
    assert out["nocats"] == 0.4
    assert _build_ros(values, {"blocker": blocker}, frozenset())["blocker"] == pytest.approx(0.0)
