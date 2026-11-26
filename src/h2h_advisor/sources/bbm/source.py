"""Basketball Monster source: GET a projections page, then drive approved postbacks.

BBM keeps the selected league on the account and the page remembers its last
filter, so nothing is assumed about the state a load starts in: every page is
parsed, compared with what was asked for, and posted back only when it differs.
The wanted state is verified again on the response; a page that still shows the
wrong league, filter or date is an error, never returned.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Literal

from h2h_advisor.browser.session import BrowserSession
from h2h_advisor.identity import PlayerIndex
from h2h_advisor.sources.bbm import parsers, urls
from h2h_advisor.sources.bbm.parsers import BbmPage

SITE = "bbm"


class BbmStateError(Exception):
    """BBM would not show the league, filter or date that was asked for."""


@dataclass(frozen=True)
class _Want:
    filter_value: str
    league: str | None = None  # None: league-independent, never switched
    game_date: date | None = None  # daily page only

    def league_matches(self, page: BbmPage) -> bool:
        return self.league is None or page.league_value == self.league

    def filter_matches(self, page: BbmPage) -> bool:
        return page.filter_value == self.filter_value

    def problems(self, page: BbmPage) -> list[str]:
        found: list[str] = []
        if self.league is not None and page.league_value != self.league:
            found.append(f"league {page.league_value!r}, wanted {self.league!r}")
        if page.filter_value != self.filter_value:
            found.append(f"filter {page.filter_value!r}, wanted {self.filter_value!r}")
        if self.game_date is not None and page.game_date != self.game_date:
            shown = page.game_date.isoformat() if page.game_date else None
            found.append(f"date {shown}, wanted {self.game_date.isoformat()}")
        return found


class BbmSource:
    """Read-only access to BBM projections. Sequential, fewest page loads."""

    def __init__(self, session: BrowserSession, index: PlayerIndex) -> None:
        self._session = session
        self._index = index
        self._league_options: dict[str, str] = {}
        # Last page parsed per URL: when the browser is still on that URL, its state
        # is known and a postback can start from it without another GET.
        self._known: dict[str, BbmPage] = {}

    # -- public --------------------------------------------------------------

    def day(self, d: date) -> BbmPage:
        """Every player's projection for one date (league-independent)."""
        want = _Want(urls.FILTER_ALL, game_date=d)
        return self._reach("day", want)

    def ros(self) -> BbmPage:
        """Every player's per-game rest-of-season projection (league-independent)."""
        return self._reach("ros", _Want(urls.FILTER_ALL))

    def league_view(self, bbm_league_id: str) -> BbmPage:
        """My players, free agents and waiver players of one league, with ownership."""
        want = _Want(urls.FILTER_ALL_AVAILABLE_AND_MINE, league=bbm_league_id)
        return self._reach("ros", want)

    def league_options(self) -> dict[str, str]:
        """League option value -> visible text; loads the ROS page if none is cached."""
        if not self._league_options:
            url = self._url("ros")
            self._load("ros", url, self._session.fetch(SITE, url).html)
        return dict(self._league_options)

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _url(scope: Literal["day", "ros"]) -> str:
        return urls.PROJECTIONS_PAGE[scope]

    def _load(self, scope: Literal["day", "ros"], url: str, html: str) -> BbmPage:
        page = parsers.parse_page(html, scope)
        if page.league_options:
            self._league_options = page.league_options
        self._known[url] = page
        return page

    def _still_showing(self, url: str) -> BbmPage | None:
        """The page last parsed for ``url`` if the browser has not left it since."""
        browser_page = getattr(self._session, "page", None)
        if getattr(browser_page, "url", None) != url:
            return None
        return self._known.get(url)

    def _reach(self, scope: Literal["day", "ros"], want: _Want) -> BbmPage:
        url = self._url(scope)
        page = self._still_showing(url)
        if page is None:
            page = self._load(scope, url, self._session.fetch(SITE, url).html)
        if want.league is not None and want.league not in page.league_options:
            raise BbmStateError(f"unknown BBM league {want.league!r}")
        if not want.problems(page):
            return self._rekey(page)

        switching_league = want.league is not None and page.league_value != want.league
        if switching_league:
            event_target = urls.LEAGUE_CONTROL
            select = {
                urls.LEAGUE_CONTROL: want.league,
                urls.FILTER_CONTROL: want.filter_value,
            }
        else:
            event_target = urls.FILTER_CONTROL
            select = {urls.FILTER_CONTROL: want.filter_value}
            if want.game_date is not None:
                select[urls.DATE_FIELD] = want.game_date.isoformat()
        page = self._post(scope, url, event_target, select)

        if (
            switching_league
            and want.league_matches(page)
            and not want.filter_matches(page)
        ):
            # The league switched but the filter did not stick: set it once more.
            page = self._post(
                scope,
                url,
                urls.FILTER_CONTROL,
                {urls.FILTER_CONTROL: want.filter_value},
            )
        problems = want.problems(page)
        if problems:
            raise BbmStateError("BBM page shows " + "; ".join(problems))
        return self._rekey(page)

    def _post(
        self,
        scope: Literal["day", "ros"],
        url: str,
        event_target: str,
        select: dict[str, str],
    ) -> BbmPage:
        result = self._session.postback(
            site=SITE, url=url, event_target=event_target, select=select
        )
        return self._load(scope, url, result.html)

    def _rekey(self, page: BbmPage) -> BbmPage:
        """Apply the name aliases; the parsers only normalise."""
        rows = [
            replace(
                row,
                projection=replace(
                    row.projection,
                    player_key=self._index.key(
                        row.projection.name, row.projection.nba_team
                    ),
                ),
            )
            for row in page.rows
        ]
        return replace(page, rows=rows)
