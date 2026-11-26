"""MCP server: the Advisor service as thirteen read-only tools over stdio.

A thin layer. Each tool forwards its parameters to one ``Advisor`` method and
returns the resulting ``Envelope`` as JSON (dates ISO, enums by value). Nothing here
can navigate, click, type or fetch an arbitrary URL. The Advisor, and with it the
cache and browser plumbing, is built on the first tool call; starting the server
opens no browser and touches no network.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable
from typing import Any, Literal

import anyio.from_thread
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from h2h_advisor import paths
from h2h_advisor.browser.guard import GuardRules
from h2h_advisor.browser.ratelimit import RateLimiter
from h2h_advisor.browser.session import BrowserSession
from h2h_advisor.cache import Cache, jsonable
from h2h_advisor.config import load_config
from h2h_advisor.service import Advisor, Envelope
from h2h_advisor.sources.espn import EspnSource

INSTRUCTIONS = (
    "Read-only fantasy basketball advisor over Yahoo Fantasy NBA and Basketball "
    "Monster. No tool changes anything on Yahoo or Basketball Monster: adds, drops, "
    "lineups and trades are made by the user, by hand. For 'how am I doing' or "
    "'what should I do', call daily_brief first (one call, all leagues). Every "
    "response carries per-source fetched_at and stale flags: report how old the "
    "data is and pass refresh=true only when the user wants fresher numbers. "
    "Relay every notice. 'browser busy in another session' means another Claude "
    "session holds the browser, so cached data is shown. '<site> needs login' "
    "means the user must log in themselves: run /h2h-advisor:setup, or in a "
    "terminal: uv run --directory <path to the h2h-advisor plugin> h2h-advisor "
    "login. No tool can log in. 'set bbm_league_id for <league>' "
    "means the Basketball Monster league could not be matched by name: offer "
    "setup to pin it in config. "
    "Never claim a move was made; only recommend it."
)

Horizon = Literal["today", "rest_of_week"]
READ_ONLY = (
    " It is read-only and never changes anything on Yahoo or Basketball Monster."
)

mcp = MCPServer("h2h-advisor", instructions=INSTRUCTIONS)

_advisor: Advisor | None = None
_advisor_lock = threading.Lock()


def _build_advisor() -> Advisor:
    config = load_config()
    cache = Cache(paths.cache_db())
    limiter = RateLimiter(cache, config.limits)
    rules = GuardRules.load()
    return Advisor(
        config,
        cache,
        lambda: BrowserSession(cache, limiter, rules, headless=True),
        lambda: dt.datetime.now(dt.UTC),
        espn_source=EspnSource(),
    )


def get_advisor() -> Advisor:
    """The process-wide Advisor, built on first use."""
    global _advisor
    with _advisor_lock:
        if _advisor is None:
            _advisor = _build_advisor()
        return _advisor


def _parse_date(text: str, name: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise ToolError(
            f"invalid {name} {text!r}: use an ISO date, YYYY-MM-DD"
        ) from None


def _at_least_one(value: int, name: str) -> int:
    if value < 1:
        raise ToolError(f"{name} must be at least 1, got {value}")
    return value


def _respond(method: str, **kwargs: Any) -> dict[str, Any]:
    """Call one Advisor method; its Envelope becomes plain JSON data."""
    try:
        envelope: Envelope = getattr(get_advisor(), method)(**kwargs)
    except ValueError as exc:  # unknown or ambiguous league, bad horizon, ...
        raise ToolError(str(exc)) from exc
    return jsonable(envelope)


def _tool(title: str, description: str, *, open_world: bool = True) -> Any:
    return mcp.tool(
        title=title,
        description=description + READ_ONLY,
        annotations=ToolAnnotations(
            title=title,
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=open_world,
        ),
    )


@_tool("List leagues", "Lists the user's Yahoo NBA fantasy leagues (id and name).")
def list_leagues(refresh: bool = False) -> dict[str, Any]:
    return _respond("list_leagues", refresh=refresh)


@_tool(
    "League settings",
    "Scoring categories, roster slots, max adds per week and the current week for "
    "one league (name or id).",
)
def get_league_settings(league: str, refresh: bool = False) -> dict[str, Any]:
    return _respond("league_settings", league=league, refresh=refresh)


@_tool(
    "Matchup",
    "The head-to-head matchup (both teams' category totals so far) for this week or "
    "a given week of one league.",
)
def get_matchup(
    league: str, week: int | None = None, refresh: bool = False
) -> dict[str, Any]:
    return _respond("matchup", league=league, week=week, refresh=refresh)


@_tool(
    "Roster",
    "A team's roster with slots and injury status; team is 'me' (default), "
    "'opponent' (this week's opponent) or a Yahoo team id.",
)
def get_roster(league: str, team: str = "me", refresh: bool = False) -> dict[str, Any]:
    return _respond("roster", league=league, team=team, refresh=refresh)


@_tool(
    "Free agents",
    "Best available free agents and waiver claims for one league, optionally "
    "filtered to one position.",
)
def get_free_agents(
    league: str,
    limit: int = 50,
    position: str | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    return _respond(
        "free_agents",
        league=league,
        limit=_at_least_one(limit, "limit"),
        position=position,
        refresh=refresh,
    )


@_tool(
    "Projections",
    "Basketball Monster projections for one day (scope='day'; date is ISO and "
    "defaults to today) or for the rest of the season (scope='ros'; date is "
    "ignored).",
)
def get_projections(
    scope: Literal["day", "ros"], date: str | None = None, refresh: bool = False
) -> dict[str, Any]:
    day = None if date is None else _parse_date(date, "date")
    return _respond("projections", scope=scope, day=day, refresh=refresh)


@_tool(
    "NBA schedule",
    "Which NBA teams play on each day between two ISO dates, inclusive (at most 14 "
    "days), read from Basketball Monster's daily pages: ranges beyond the current "
    "week cost one Basketball Monster page load per day.",
)
def get_schedule(start: str, end: str) -> dict[str, Any]:
    first, last = _parse_date(start, "start"), _parse_date(end, "end")
    if last < first:
        raise ToolError(f"end {end} is before start {start}")
    return _respond("schedule", start=first, end=last)


@_tool(
    "Analyze week",
    "Projected category outcome for the rest of the matchup week, with the swing "
    "categories and the build and objective in use.",
)
def analyze_week(league: str, refresh: bool = False) -> dict[str, Any]:
    return _respond("analyze_week", league=league, refresh=refresh)


@_tool(
    "Rank streamers",
    "Ranks add/drop streaming moves for one league over the horizon ('today' or "
    "'rest_of_week'); use for 'who should I pick up'.",
)
def rank_streamers(
    league: str,
    horizon: Horizon = "today",
    max_results: int = 10,
    refresh: bool = False,
) -> dict[str, Any]:
    return _respond(
        "rank_streamers",
        league=league,
        horizon=horizon,
        max_results=_at_least_one(max_results, "max_results"),
        refresh=refresh,
    )


@_tool(
    "Plan adds",
    "Dated add/drop plan for the rest of the week in one league: which day to make "
    "each move so the dropped player plays until then and the add counts from his "
    "first useful game. Moves dated today are 'do now'; the rest are for planning.",
)
def plan_adds(league: str, refresh: bool = False) -> dict[str, Any]:
    return _respond("plan_adds", league=league, refresh=refresh)


@_tool(
    "Long-term upgrades",
    "Free agents whose rest-of-season value beats someone on the roster; use for "
    "permanent swaps rather than streaming.",
)
def long_term_upgrades(
    league: str, max_results: int = 5, refresh: bool = False
) -> dict[str, Any]:
    return _respond(
        "long_term_upgrades",
        league=league,
        max_results=_at_least_one(max_results, "max_results"),
        refresh=refresh,
    )


@_tool(
    "Matchup history",
    "The results of this season's earlier matchup weeks for one league.",
)
def get_history(league: str) -> dict[str, Any]:
    return _respond("history", league=league)


@_tool(
    "Daily brief",
    "Status or moves for one or all leagues in one call; prefer this for 'how am I "
    "doing' / 'what should I do'. leagues is 'all' or a list of names or ids.",
)
def daily_brief(
    leagues: str | list[str] = "all",
    horizon: Horizon = "today",
    refresh: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    wanted = [leagues] if isinstance(leagues, str) and leagues != "all" else leagues
    return _respond(
        "daily_brief",
        leagues=wanted,
        horizon=horizon,
        refresh=refresh,
        progress=league_progress(ctx),
    )


def league_progress(ctx: Context | None) -> Callable[[int, int, str], None]:
    """A progress notification per league of a brief, when the client asked for them.

    Sync tools run in a worker thread, so the notification is sent on the event
    loop through ``anyio.from_thread``. Best effort: progress never fails a brief.
    """

    def report(done: int, total: int, league: str) -> None:
        if ctx is None:
            return
        try:
            anyio.from_thread.run(
                ctx.report_progress, done, total, f"league {league} done"
            )
        except Exception:  # noqa: BLE001 - no request, no worker thread, client gone
            return

    return report


@_tool(
    "Status",
    "Login state, page loads against the daily caps, cache ages and recently "
    "blocked requests; loads no pages.",
    open_world=False,
)
def status() -> dict[str, Any]:
    return _respond("status")


def run() -> None:
    """Serve over stdio until the client disconnects."""
    mcp.run("stdio")
