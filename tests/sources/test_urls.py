from urllib.parse import urlsplit

import pytest

from h2h_advisor.browser.guard import GuardRules, RequestInfo, decide
from h2h_advisor.sources.bbm import urls as bbm
from h2h_advisor.sources.yahoo import urls as yahoo

YAHOO = "https://basketball.fantasysports.yahoo.com"


def test_yahoo_home():
    assert yahoo.home() == f"{YAHOO}/nba"


def test_yahoo_league_settings():
    assert yahoo.league_settings(51347) == f"{YAHOO}/nba/51347/settings"


def test_yahoo_matchup_current_week():
    assert yahoo.matchup(51347) == f"{YAHOO}/nba/51347/matchup"


def test_yahoo_matchup_given_week():
    assert yahoo.matchup(51347, 6) == f"{YAHOO}/nba/51347/matchup?week=6"
    assert yahoo.matchup(51347, week=12) == f"{YAHOO}/nba/51347/matchup?week=12"


def test_yahoo_team():
    assert yahoo.team(51347, 9) == f"{YAHOO}/nba/51347/9"


def test_yahoo_free_agents_first_page_is_all_available():
    assert yahoo.free_agents(51347) == f"{YAHOO}/nba/51347/players?status=A"


def test_yahoo_free_agents_paging_uses_count_offset():
    assert (
        yahoo.free_agents(51347, start=25)
        == f"{YAHOO}/nba/51347/players?status=A&count=25"
    )


def test_yahoo_free_agents_position_filter():
    assert (
        yahoo.free_agents(51347, start=50, position="PG")
        == f"{YAHOO}/nba/51347/players?status=A&pos=PG&count=50"
    )
    assert (
        yahoo.free_agents(51347, position="Util")
        == f"{YAHOO}/nba/51347/players?status=A&pos=Util"
    )


def test_yahoo_free_agents_status_filter():
    assert yahoo.free_agents(51347, status="W") == f"{YAHOO}/nba/51347/players?status=W"
    assert yahoo.free_agents(51347, status="T") == f"{YAHOO}/nba/51347/players?status=T"
    assert (
        yahoo.free_agents(51347, start=25, status="W")
        == f"{YAHOO}/nba/51347/players?status=W&count=25"
    )


@pytest.mark.parametrize("bad", ["", "w", "t", "K", "ALL", None])
def test_yahoo_free_agents_invalid_status_raises(bad):
    with pytest.raises(ValueError):
        yahoo.free_agents(51347, status=bad)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "51347", None])
def test_yahoo_invalid_league_id_raises(bad):
    for build in (
        yahoo.league_settings,
        yahoo.matchup,
        yahoo.free_agents,
        lambda lg: yahoo.team(lg, 9),
    ):
        with pytest.raises(ValueError):
            build(bad)


@pytest.mark.parametrize("bad", [0, -3, False, 2.0, "9", None])
def test_yahoo_invalid_team_id_raises(bad):
    with pytest.raises(ValueError):
        yahoo.team(51347, bad)


@pytest.mark.parametrize("bad", [0, -1, 1.0, "6"])
def test_yahoo_invalid_week_raises(bad):
    with pytest.raises(ValueError):
        yahoo.matchup(51347, bad)


@pytest.mark.parametrize("bad", [-25, 10, True, 25.0, "25"])
def test_yahoo_invalid_start_raises(bad):
    with pytest.raises(ValueError):
        yahoo.free_agents(51347, start=bad)


@pytest.mark.parametrize("bad", ["pg", "P", "X", ""])
def test_yahoo_invalid_position_raises(bad):
    with pytest.raises(ValueError):
        yahoo.free_agents(51347, position=bad)


def test_yahoo_urls_are_allowed_by_the_guard():
    rules = GuardRules.load()
    for url in (
        yahoo.home(),
        yahoo.league_settings(51347),
        yahoo.matchup(51347, 6),
        yahoo.team(51347, 9),
        yahoo.free_agents(51347, start=25, position="C"),
    ):
        verdict = decide(RequestInfo("GET", url, "document", True), rules)
        assert verdict.allowed, (url, verdict.reason)


def test_bbm_projection_pages():
    assert bbm.PROJECTIONS_PAGE == {
        "day": "https://basketballmonster.com/dailyprojections.aspx",
        "ros": "https://basketballmonster.com/Projections.aspx",
    }


def test_bbm_control_names_and_values():
    assert bbm.DATE_FIELD == "GAMEDATE"
    assert bbm.FILTER_CONTROL == "PlayerFilterControl"
    assert bbm.LEAGUE_CONTROL == "pageTitleRowLeague"
    assert bbm.FILTER_ALL == "AllPlayers"
    assert bbm.FILTER_AVAILABLE_AND_MINE == "AvailableAndMyPlayers"
    assert bbm.FILTER_ALL_AVAILABLE_AND_MINE == "AllAvailableAndMyPlayers"


def test_bbm_controls_match_guard_rules():
    rules = GuardRules.load().bbm
    assert {bbm.FILTER_CONTROL, bbm.LEAGUE_CONTROL} == rules.post_event_targets
    assert {
        bbm.DATE_FIELD.casefold(),
        bbm.FILTER_CONTROL.casefold(),
        bbm.LEAGUE_CONTROL.casefold(),
    } == rules.post_settable_fields


def test_bbm_pages_are_known_to_the_guard():
    rules = GuardRules.load().bbm
    for url in bbm.PROJECTIONS_PAGE.values():
        path = urlsplit(url).path.lower()
        assert path in rules.get_pages
        assert path in rules.post_pages
