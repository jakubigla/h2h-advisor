"""Recent minutes from ESPN's public game logs (no login, JSON).

Used for one signal only: is a player in a real rotation right now? Basketball
Monster's projections lag a role change, so a player it rates as a throwaway can be
playing 25 minutes a night. The source is unofficial and may change without notice,
so every failure is an `EspnError` the caller turns into a notice; nothing depends on
it. Read-only: GET requests to one host, no cookies, nothing about the user.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from h2h_advisor.identity import normalize_name

HOST = "site.web.api.espn.com"
SEARCH_URL = f"https://{HOST}/apis/search/v2"
GAMELOG_URL = f"https://{HOST}/apis/common/v3/sports/basketball/nba/athletes/{{id}}/gamelog"
TIMEOUT_S = 8.0
USER_AGENT = "h2h-advisor"  # a generic client name, nothing about the user
RECENT_GAMES = 5

# Team (Basketball Monster abbreviation) -> a word of ESPN's team name, to tell apart
# players who share a name. Only used when the name alone is ambiguous.
TEAM_WORD: dict[str, str] = {
    "ATL": "hawks", "BKN": "nets", "BOS": "celtics", "CHA": "hornets", "CHI": "bulls",
    "CLE": "cavaliers", "DAL": "mavericks", "DEN": "nuggets", "DET": "pistons",
    "GSW": "warriors", "HOU": "rockets", "IND": "pacers", "LAC": "clippers",
    "LAL": "lakers", "MEM": "grizzlies", "MIA": "heat", "MIL": "bucks",
    "MIN": "timberwolves", "NOR": "pelicans", "NOP": "pelicans", "NYK": "knicks",
    "OKC": "thunder", "ORL": "magic", "PHI": "76ers", "PHO": "suns", "PHX": "suns",
    "POR": "trail blazers", "SAC": "kings", "SAS": "spurs", "TOR": "raptors",
    "UTA": "jazz", "WAS": "wizards",
}


class EspnError(Exception):
    """ESPN did not answer, or answered something that is not a game log."""


@dataclass(frozen=True)
class RecentMinutes:
    games: int  # games in the sample (at most RECENT_GAMES)
    mpg: float  # mean minutes over them
    last: list[float]  # minutes, newest first
    last_game: date | None


def fetch_json(url: str) -> Any:
    """GET `url` and decode its JSON; the host is fixed to ESPN's API host."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != HOST:
        raise EspnError(f"refusing to fetch {url!r}: only https://{HOST} is allowed")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise EspnError(f"ESPN request failed: {exc}") from exc


def search_candidates(data: Any) -> list[tuple[str, str, str]]:
    """(athlete id, display name, subtitle) of every player result of a search response."""
    found = []
    for group in data.get("results", []) if isinstance(data, dict) else []:
        if group.get("type") != "player":
            continue
        for item in group.get("contents", []):
            uid = str(item.get("uid", ""))
            athlete = next((part[2:] for part in uid.split("~") if part.startswith("a:")), "")
            if athlete and item.get("displayName"):
                found.append((athlete, item["displayName"], str(item.get("subtitle", ""))))
    return found


def pick_athlete(candidates: list[tuple[str, str, str]], name: str, team: str) -> str | None:
    """The athlete id for `name`: the one exact normalised-name match, else the one on `team`."""
    wanted = normalize_name(name)
    same = [c for c in candidates if normalize_name(c[1]) == wanted]
    if len(same) == 1:
        return same[0][0]
    word = TEAM_WORD.get(team.upper())
    if len(same) > 1 and word:
        on_team = [c for c in same if word in c[2].lower()]
        if len(on_team) == 1:
            return on_team[0][0]
    return None


def parse_gamelog(data: Any, games: int = RECENT_GAMES) -> RecentMinutes | None:
    """Minutes of the latest `games` regular-season games of the current season.

    None when the log has no played game yet. Anything that is not the documented
    shape is an `EspnError`: the caller must not read silence as "does not play".
    """
    try:
        names = data["names"]
        minutes_at = names.index("minutes")
        season = next(
            t for t in data["seasonTypes"] if str(t.get("displayName", "")).endswith("Regular Season")
        )
        dates = {eid: ev["gameDate"] for eid, ev in data.get("events", {}).items()}
        played: list[tuple[date, float]] = []
        for category in season.get("categories", []):
            for event in category.get("events", []):
                try:
                    minutes = float(event["stats"][minutes_at])
                except (ValueError, IndexError):
                    continue  # "--": did not play
                if minutes <= 0:
                    continue
                played.append((datetime.fromisoformat(dates[event["eventId"]]).date(), minutes))
    except (KeyError, StopIteration, ValueError, TypeError, AttributeError) as exc:
        raise EspnError(f"unexpected game-log shape: {exc!r}") from exc
    if not played:
        return None
    recent = sorted(played, key=lambda g: g[0], reverse=True)[:games]
    last = [m for _, m in recent]
    return RecentMinutes(len(last), sum(last) / len(last), last, recent[0][0])


class EspnSource:
    """Athlete lookup and recent minutes; `fetch` is injectable for tests."""

    def __init__(self, fetch: Callable[[str], Any] = fetch_json) -> None:
        self._fetch = fetch

    def athlete_id(self, name: str, team: str) -> str | None:
        query = urllib.parse.urlencode({"query": name, "limit": 6, "type": "player"})
        return pick_athlete(search_candidates(self._fetch(f"{SEARCH_URL}?{query}")), name, team)

    def recent_minutes(self, athlete_id: str) -> RecentMinutes | None:
        return parse_gamelog(self._fetch(GAMELOG_URL.format(id=athlete_id)))
