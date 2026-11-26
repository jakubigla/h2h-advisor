import re
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from h2h_advisor.browser.guard import GuardRules
from h2h_advisor.browser.session import PageResult
from h2h_advisor.identity import PlayerIndex
from h2h_advisor.sources.bbm import urls
from h2h_advisor.sources.bbm.parsers import ParseError
from h2h_advisor.sources.bbm.source import BbmSource, BbmStateError

FIXTURES = Path(__file__).parents[2] / "fixtures" / "bbm"
DAY_URL = urls.PROJECTIONS_PAGE["day"]
ROS_URL = urls.PROJECTIONS_PAGE["ros"]


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def with_state(
    html: str,
    *,
    league: str | None = None,
    filter_value: str | None = None,
    game_date: str | None = None,
) -> str:
    """The fixture with other options selected (what BBM would render)."""

    def select(html: str, name: str, value: str) -> str:
        head, sep, rest = html.partition(f'<select name="{name}"')
        assert sep, name
        body, end, tail = rest.partition("</select>")
        body = body.replace(' selected="selected"', "")
        body, n = re.subn(
            rf'<option value="{re.escape(value)}"', r'\g<0> selected="selected"', body
        )
        assert n == 1, (name, value)
        return head + sep + body + end + tail

    if league is not None:
        html = select(html, "pageTitleRowLeague", league)
    if filter_value is not None:
        html = select(html, "PlayerFilterControl", filter_value)
    if game_date is not None:
        html, n = re.subn(
            r'(name="GAMEDATE"[^>]*value=")[^"]*', rf"\g<1>{game_date}", html
        )
        assert n == 1
    return html


class FakeSession:
    """Serves ``fetch`` from a url -> html map and ``postback`` from a queue."""

    def __init__(self, pages: dict[str, str], replies: list[str] | None = None):
        self.pages = pages
        self.replies = list(replies or [])
        self.fetches: list[tuple[str, str]] = []
        self.postbacks: list[tuple[str, str, str, dict[str, str] | None]] = []
        self.page = SimpleNamespace(url=None)  # the browser's current page

    def fetch(self, site: str, url: str) -> PageResult:
        self.fetches.append((site, url))
        self.page.url = url
        return PageResult(url, url, self.pages[url], datetime.now(UTC))

    def postback(
        self,
        site: str,
        url: str,
        event_target: str,
        select: dict[str, str] | None = None,
    ) -> PageResult:
        # Like BrowserSession.postback, refuse what the guard rules do not allow.
        bbm = GuardRules.load().bbm
        assert event_target in bbm.post_event_targets
        assert all(k.casefold() in bbm.post_settable_fields for k in select or {})
        self.postbacks.append((site, url, event_target, select))
        self.page.url = url
        return PageResult(url, url, self.replies.pop(0), datetime.now(UTC))


def make(pages, replies=None, aliases=None):
    session = FakeSession(pages, replies)
    return session, BbmSource(session, PlayerIndex(aliases or {}))


DAILY = load("daily_2026-11-25.html")
ROS = load("ros.html")  # league 30008, "Top Free Agents+My"
LEAGUE_VIEW = load("ros_league.html")  # league 602, "Top Free Agents+My"
ALL_AND_MINE = urls.FILTER_ALL_AVAILABLE_AND_MINE


# --- day ----------------------------------------------------------------------


def test_day_needs_one_postback_when_the_filter_differs():
    shown = with_state(DAILY, filter_value=urls.FILTER_ALL)
    session, source = make({DAY_URL: DAILY}, [shown])
    page = source.day(date(2026, 11, 25))
    assert session.fetches == [("bbm", DAY_URL)]
    assert session.postbacks == [
        (
            "bbm",
            DAY_URL,
            urls.FILTER_CONTROL,
            {urls.DATE_FIELD: "2026-11-25", urls.FILTER_CONTROL: urls.FILTER_ALL},
        )
    ]
    assert page.filter_value == urls.FILTER_ALL
    assert page.game_date == date(2026, 11, 25)
    assert len(page.rows) == 189


def test_day_for_another_date_is_one_postback():
    shown = with_state(DAILY, filter_value=urls.FILTER_ALL, game_date="2026-11-27")
    session, source = make({DAY_URL: DAILY}, [shown])
    page = source.day(date(2026, 11, 27))
    assert len(session.fetches) == 1
    assert len(session.postbacks) == 1
    assert session.postbacks[0][3][urls.DATE_FIELD] == "2026-11-27"
    assert page.game_date == date(2026, 11, 27)


def test_day_needs_no_postback_when_the_page_already_shows_it():
    ready = with_state(DAILY, filter_value=urls.FILTER_ALL)
    session, source = make({DAY_URL: ready})
    page = source.day(date(2026, 11, 25))
    assert session.postbacks == []
    assert len(session.fetches) == 1
    assert len(page.rows) == 189


def test_day_wrong_date_after_postback_is_an_error():
    stale = with_state(DAILY, filter_value=urls.FILTER_ALL)  # still 2026-11-25
    _, source = make({DAY_URL: DAILY}, [stale])
    with pytest.raises(BbmStateError, match="2026-11-27"):
        source.day(date(2026, 11, 27))


def test_day_never_switches_league():
    shown = with_state(DAILY, filter_value=urls.FILTER_ALL)
    session, source = make({DAY_URL: DAILY}, [shown])
    source.day(date(2026, 11, 25))
    assert all(p[2] != urls.LEAGUE_CONTROL for p in session.postbacks)
    assert all(urls.LEAGUE_CONTROL not in p[3] for p in session.postbacks)


def test_rows_are_keyed_through_the_player_index_aliases():
    ready = with_state(DAILY, filter_value=urls.FILTER_ALL)
    _, source = make({DAY_URL: ready}, aliases={"Victor Wembanyama": "Victor Wemby"})
    page = source.day(date(2026, 11, 25))
    keys = {r.projection.name: r.projection.player_key for r in page.rows}
    assert keys["Victor Wembanyama"] == "victor wemby|SAS"


# --- ros ----------------------------------------------------------------------


def test_ros_posts_back_to_all_players_when_the_filter_differs():
    shown = with_state(ROS, filter_value=urls.FILTER_ALL)
    session, source = make({ROS_URL: ROS}, [shown])
    page = source.ros()
    assert session.postbacks == [
        (
            "bbm",
            ROS_URL,
            urls.FILTER_CONTROL,
            {urls.FILTER_CONTROL: urls.FILTER_ALL},
        )
    ]
    assert page.filter_value == urls.FILTER_ALL


def test_ros_needs_no_postback_when_already_all_players():
    ready = with_state(ROS, filter_value=urls.FILTER_ALL)
    session, source = make({ROS_URL: ready})
    source.ros()
    assert session.postbacks == []


def test_ros_filter_not_applied_is_an_error():
    _, source = make({ROS_URL: ROS}, [ROS])
    with pytest.raises(BbmStateError, match=urls.FILTER_ALL):
        source.ros()


# --- league view --------------------------------------------------------------


def test_league_view_already_on_that_league_and_filter_needs_no_postback():
    ready = with_state(LEAGUE_VIEW, filter_value=ALL_AND_MINE)
    session, source = make({ROS_URL: ready})
    page = source.league_view("602")
    assert session.postbacks == []
    assert session.fetches == [("bbm", ROS_URL)]
    assert page.league_value == "602"


def test_league_view_switches_league_with_one_postback_when_filter_sticks():
    reply = with_state(LEAGUE_VIEW, filter_value=ALL_AND_MINE)
    session, source = make({ROS_URL: ROS}, [reply])  # page shows 30008
    page = source.league_view("602")
    assert session.postbacks == [
        (
            "bbm",
            ROS_URL,
            urls.LEAGUE_CONTROL,
            {urls.LEAGUE_CONTROL: "602", urls.FILTER_CONTROL: ALL_AND_MINE},
        )
    ]
    assert page.league_value == "602"
    assert page.filter_value == ALL_AND_MINE


def test_league_view_resets_the_filter_when_it_did_not_stick():
    league_only = LEAGUE_VIEW  # league 602 but filter "Top Free Agents+My"
    final = with_state(LEAGUE_VIEW, filter_value=ALL_AND_MINE)
    session, source = make({ROS_URL: ROS}, [league_only, final])
    page = source.league_view("602")
    assert [p[2] for p in session.postbacks] == [
        urls.LEAGUE_CONTROL,
        urls.FILTER_CONTROL,
    ]
    assert session.postbacks[1][3] == {urls.FILTER_CONTROL: ALL_AND_MINE}
    assert page.league_value == "602"
    assert page.filter_value == ALL_AND_MINE


def test_league_view_on_the_right_league_only_fixes_the_filter():
    session, source = make(
        {ROS_URL: LEAGUE_VIEW}, [with_state(LEAGUE_VIEW, filter_value=ALL_AND_MINE)]
    )
    source.league_view("602")
    assert [p[2] for p in session.postbacks] == [urls.FILTER_CONTROL]


def test_league_view_never_returns_another_leagues_data():
    still_other = with_state(ROS, filter_value=ALL_AND_MINE)  # still 30008
    session, source = make({ROS_URL: ROS}, [still_other])
    with pytest.raises(BbmStateError, match="602"):
        source.league_view("602")
    assert len(session.postbacks) == 1  # no retry loop


def test_league_view_filter_still_wrong_after_second_postback_is_an_error():
    session, source = make({ROS_URL: ROS}, [LEAGUE_VIEW, LEAGUE_VIEW])
    with pytest.raises(BbmStateError, match=ALL_AND_MINE):
        source.league_view("602")
    assert len(session.postbacks) == 2


def test_league_view_unknown_league_posts_nothing():
    session, source = make({ROS_URL: ROS})
    with pytest.raises(BbmStateError, match="9999"):
        source.league_view("9999")
    assert session.postbacks == []


# --- league options -------------------------------------------------------------


def test_league_options_reuse_the_last_loaded_page():
    ready = with_state(ROS, filter_value=urls.FILTER_ALL)
    session, source = make({ROS_URL: ready})
    source.ros()
    options = source.league_options()
    assert len(session.fetches) == 1
    assert options["602"].startswith("_1. ")


def test_league_options_load_the_ros_page_when_nothing_is_cached():
    session, source = make({ROS_URL: ROS})
    options = source.league_options()
    assert session.fetches == [("bbm", ROS_URL)]
    assert {"602", "30008", "28681", "26949", "31648"} <= set(options)


# --- known state: no redundant GETs ----------------------------------------------


def test_second_day_posts_back_from_the_known_page_without_another_get():
    d1 = with_state(DAILY, filter_value=urls.FILTER_ALL)
    d2 = with_state(DAILY, filter_value=urls.FILTER_ALL, game_date="2026-11-26")
    session, source = make({DAY_URL: DAILY}, [d1, d2])
    source.day(date(2026, 11, 25))
    page = source.day(date(2026, 11, 26))
    assert session.fetches == [("bbm", DAY_URL)]
    assert len(session.postbacks) == 2
    assert session.postbacks[1][3][urls.DATE_FIELD] == "2026-11-26"
    assert page.game_date == date(2026, 11, 26)


def test_league_view_after_ros_posts_back_without_another_get():
    ros_all = with_state(ROS, filter_value=urls.FILTER_ALL)
    league_view = with_state(LEAGUE_VIEW, filter_value=ALL_AND_MINE)
    session, source = make({ROS_URL: ROS}, [ros_all, league_view])
    source.ros()
    page = source.league_view("602")
    assert session.fetches == [("bbm", ROS_URL)]
    assert [p[2] for p in session.postbacks] == [
        urls.FILTER_CONTROL,
        urls.LEAGUE_CONTROL,
    ]
    assert page.league_value == "602"


def test_page_left_since_is_loaded_again():
    ready = with_state(DAILY, filter_value=urls.FILTER_ALL)
    session, source = make({DAY_URL: ready})
    source.day(date(2026, 11, 25))
    session.page.url = "https://basketball.fantasysports.yahoo.com/nba"  # Yahoo load
    source.day(date(2026, 11, 25))
    assert len(session.fetches) == 2


def test_parse_errors_propagate():
    _, source = make({ROS_URL: "<html></html>"})
    with pytest.raises(ParseError):
        source.ros()
