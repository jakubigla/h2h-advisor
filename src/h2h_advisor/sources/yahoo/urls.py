"""Yahoo Fantasy NBA page URLs (absolute), as found in docs/discovery.md."""

from urllib.parse import urlencode

BASE = "https://basketball.fantasysports.yahoo.com"
# Eligibility filter radios on the players page (`pos=`); "P" (all) is the default.
POSITIONS = frozenset({"PG", "SG", "G", "SF", "PF", "F", "C", "Util"})
# The players page lists 25 per page and pages by offset in the `count` param.
PAGE_SIZE = 25
# Availability filter on the players page: all available, free agents only, waivers
# only, and every player on a fantasy team's roster (taken).
STATUSES = frozenset({"A", "FA", "W", "T"})


def _positive_int(value: int, what: str) -> int:
    # bool is an int subclass; True is not a league id.
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{what} must be a positive int, got {value!r}")
    return value


def home() -> str:
    return f"{BASE}/nba"


def league_settings(league_id: int) -> str:
    return f"{BASE}/nba/{_positive_int(league_id, 'league_id')}/settings"


def matchup(league_id: int, week: int | None = None) -> str:
    """The matchup page; `week=<n>` selects a week, no param means the current one."""
    url = f"{BASE}/nba/{_positive_int(league_id, 'league_id')}/matchup"
    if week is None:
        return url
    return f"{url}?week={_positive_int(week, 'week')}"


def team(league_id: int, team_id: int) -> str:
    league = _positive_int(league_id, "league_id")
    return f"{BASE}/nba/{league}/{_positive_int(team_id, 'team_id')}"


def free_agents(
    league_id: int, start: int = 0, position: str | None = None, status: str = "A"
) -> str:
    """Players page filtered by availability: `A` (free agents and waivers, the
    default), `FA` (free agents only), `W` (waivers only) or `T` (taken: on a roster).

    `start` is a zero-based offset into the list, a multiple of PAGE_SIZE; it is
    sent as Yahoo's `count` param and omitted for the first page.
    """
    url = f"{BASE}/nba/{_positive_int(league_id, 'league_id')}/players"
    if status not in STATUSES:
        raise ValueError(f"status must be one of {sorted(STATUSES)}, got {status!r}")
    params = {"status": status}
    if position is not None:
        if position not in POSITIONS:
            raise ValueError(
                f"position must be one of {sorted(POSITIONS)}, got {position!r}"
            )
        params["pos"] = position
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or start < 0
        or start % PAGE_SIZE
    ):
        raise ValueError(
            f"start must be a non-negative multiple of {PAGE_SIZE}, got {start!r}"
        )
    if start:
        params["count"] = str(start)
    return f"{url}?{urlencode(params)}"
