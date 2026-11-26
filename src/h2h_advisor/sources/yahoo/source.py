"""Yahoo Fantasy NBA source: fetch pages through the browser session and parse them."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from datetime import date

from h2h_advisor.browser.session import BrowserSession
from h2h_advisor.identity import PlayerIndex
from h2h_advisor.models import FreeAgent, LeagueSettings, Matchup, Player, RosterEntry
from h2h_advisor.sources.yahoo import parsers, urls
from h2h_advisor.sources.yahoo.parsers import LeagueRef, ParseError

SITE = "yahoo"
WAIVERS_LIMIT = 2 * urls.PAGE_SIZE  # players read from the waivers list
TAKEN_PAGES_MAX = 24  # 600 rostered players: more than any league holds


class YahooSource:
    """Read-only access to my Yahoo leagues. One page load per fetch, sequential."""

    def __init__(
        self,
        session: BrowserSession,
        index: PlayerIndex,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._session = session
        self._index = index
        self._today = today

    def _get(self, url: str) -> str:
        return self._session.fetch(SITE, url).html

    def _rekey(self, player: Player) -> Player:
        """Apply the name aliases; the parsers only normalise."""
        return replace(player, key=self._index.key(player.name, player.nba_team))

    def leagues(self) -> list[LeagueRef]:
        return parsers.parse_league_list(self._get(urls.home()))

    def settings(self, league_id: str | int) -> LeagueSettings:
        """Settings page plus the current matchup page (which carries the week)."""
        league = int(league_id)
        settings_html = self._get(urls.league_settings(league))
        week = parsers.parse_week(self._get(urls.matchup(league)))
        return parsers.parse_settings(settings_html, str(league), week)

    def week(self, league_id: str | int) -> tuple[int, date, date]:
        """Current week number, first and last day, from the current matchup page."""
        return parsers.parse_week(self._get(urls.matchup(int(league_id))))

    def matchup(self, league_id: str | int, week: int | None = None) -> Matchup:
        """The current week's matchup (``week=None``) or an explicit week."""
        return self._matchups(int(league_id), [week])[0]

    def history(self, league_id: str | int, weeks: list[int]) -> list[Matchup]:
        """One matchup page per week, plus one team page to tell which is current.

        Only the current week has ``moves_used`` (from my team page); past weeks
        carry 0.
        """
        return self._matchups(int(league_id), list(weeks))

    def _matchups(self, league: int, weeks: list[int | None]) -> list[Matchup]:
        pages: list[tuple[str, int]] = []
        for week in weeks:
            html = self._get(urls.matchup(league, week))
            shown = parsers.parse_matchup_week(html)
            if week is not None and shown != week:
                raise ParseError(
                    "matchup", f"asked for week {week}, the page shows week {shown}"
                )
            pages.append((html, shown))
        if not pages:
            return []
        team_id = parsers.parse_my_team_id(pages[0][0], str(league))
        team_html = self._get(urls.team(league, int(team_id)))
        current_week = parsers.parse_team_week(team_html)
        for week, (_, shown) in zip(weeks, pages, strict=True):
            if week is None and shown != current_week:
                raise ParseError(
                    "matchup",
                    f"current matchup shows week {shown}, team page says {current_week}",
                )
        used, _cap = parsers.parse_adds_used(team_html)
        return [
            parsers.parse_matchup(
                html,
                str(league),
                moves_used=used if shown == current_week else 0,
                current=shown == current_week,
            )
            for html, shown in pages
        ]

    def roster(self, league_id: str | int, team_id: str | int) -> list[RosterEntry]:
        html = self._get(urls.team(int(league_id), int(team_id)))
        return [
            replace(entry, player=self._rekey(entry.player))
            for entry in parsers.parse_roster(html)
        ]

    def free_agents(
        self, league_id: str | int, limit: int = 50, position: str | None = None
    ) -> list[FreeAgent]:
        """Available players, best-ranked first, at most ``ceil(limit/25)`` pages."""
        return self._players(int(league_id), limit, position, "A")

    def waivers(self, league_id: str | int) -> list[FreeAgent]:
        """Players on waivers with their clear dates, at most WAIVERS_LIMIT (2 pages).

        Fewer than WAIVERS_LIMIT means the list is complete."""
        return self._players(int(league_id), WAIVERS_LIMIT, None, "W")

    def taken(self, league_id: str | int) -> list[Player]:
        """Every player on a fantasy team's roster in the league (IL included), one
        page load per 25. The whole list or an error: a list cut short would show
        rostered players as free."""
        league = int(league_id)
        found: list[Player] = []
        for page in range(TAKEN_PAGES_MAX):
            html = self._get(
                urls.free_agents(league, page * urls.PAGE_SIZE, status="T")
            )
            found.extend(self._rekey(p) for p in parsers.parse_taken_players(html))
            if not parsers.has_next_page(html):
                return found
        raise ParseError(
            "players", f"taken list runs past {TAKEN_PAGES_MAX} pages; not read whole"
        )

    def _players(
        self, league: int, limit: int, position: str | None, status: str
    ) -> list[FreeAgent]:
        if limit <= 0:
            raise ValueError(f"limit must be positive, got {limit!r}")
        found: list[FreeAgent] = []
        for page in range(math.ceil(limit / urls.PAGE_SIZE)):
            html = self._get(
                urls.free_agents(
                    league, page * urls.PAGE_SIZE, position=position, status=status
                )
            )
            listed = parsers.parse_free_agents(
                html, self._today(), allow_empty=status == "W"
            )
            found.extend(replace(fa, player=self._rekey(fa.player)) for fa in listed)
            if not listed or len(found) >= limit or not parsers.has_next_page(html):
                break
        return found[:limit]
