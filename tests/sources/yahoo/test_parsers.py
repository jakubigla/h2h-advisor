import re
from datetime import date
from pathlib import Path

import pytest

from h2h_advisor.models import Category, StatLine
from h2h_advisor.sources.yahoo import parsers
from h2h_advisor.sources.yahoo.parsers import ParseError

FIXTURES = Path(__file__).parents[2] / "fixtures" / "yahoo"
TODAY = date(2026, 11, 25)
WEEK6 = (6, date(2026, 11, 23), date(2026, 11, 29))


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_league_list():
    leagues = parsers.parse_league_list(load("home.html"))
    assert [(x.league_id, x.name) for x in leagues] == [
        ("51347", "League 1"),
        ("57243", "League 2"),
        ("59422", "League 3"),
        ("211727", "League 4"),
        ("259619", "League 5"),
    ]
    assert {x.league_id: x.team_id for x in leagues} == {
        "51347": "9",
        "57243": "6",
        "59422": "7",
        "211727": "9",
        "259619": "9",
    }
    assert {x.team_name for x in leagues} == {"Team 1"}


def test_parse_week():
    assert parsers.parse_week(load("matchup_51347.html")) == WEEK6


@pytest.mark.parametrize(
    ("league_id", "max_adds", "scoring_type", "faab"),
    [
        ("51347", 5, "one_win", True),
        ("211727", 5, "categories", False),
        ("259619", 5, "categories", False),
        ("57243", 7, "categories", True),
        ("59422", 5, "categories", False),
    ],
)
def test_parse_settings_per_league(league_id, max_adds, scoring_type, faab):
    s = parsers.parse_settings(load(f"settings_{league_id}.html"), league_id, WEEK6)
    assert s.league_id == league_id
    assert s.max_adds_per_week == max_adds
    assert s.scoring_type == scoring_type
    assert s.faab is faab
    assert s.lineup_effective == "today"
    assert s.categories == (
        Category.FG_PCT,
        Category.FT_PCT,
        Category.TPM,
        Category.PTS,
        Category.REB,
        Category.AST,
        Category.STL,
        Category.BLK,
        Category.TO,
    )
    assert (s.current_week, s.week_start, s.week_end) == (
        6,
        date(2026, 11, 23),
        date(2026, 11, 29),
    )


def test_parse_settings_details():
    s = parsers.parse_settings(load("settings_51347.html"), "51347", WEEK6)
    assert s.name == "League 1"
    assert s.waiver_type == "FAB w/ Continual rolling list tiebreak"
    assert s.roster_slots == {
        "PG": 1, "SG": 1, "G": 1, "SF": 1, "PF": 1, "F": 1, "C": 1,
        "Util": 3, "BN": 3, "IL": 1,
    }  # fmt: skip
    snake = parsers.parse_settings(load("settings_59422.html"), "59422", WEEK6)
    assert snake.roster_slots["Util"] == 1
    assert snake.roster_slots["BN"] == 5
    assert snake.waiver_type is not None
    assert "FAB" not in snake.waiver_type


def test_parse_settings_rejects_other_scoring_type():
    html = load("settings_51347.html").replace(
        "Head-to-Head - One Win", "Season Points"
    )
    with pytest.raises(ParseError, match="Scoring Type"):
        parsers.parse_settings(html, "51347", WEEK6)


def test_parse_settings_lineup_effective_tomorrow():
    html = load("settings_51347.html").replace("Daily - Today", "Daily - Tomorrow")
    s = parsers.parse_settings(html, "51347", WEEK6)
    assert s.lineup_effective == "tomorrow"


def test_parse_settings_max_adds_not_a_number_is_an_error():
    original = load("settings_51347.html")
    head, label, tail = original.partition("Max Acquisitions per Week:")
    html = head + label + tail.replace("<b>5</b>", "<b>No maximum</b>", 1)
    assert html != original  # the replacement hit its target
    with pytest.raises(ParseError, match="Max Acquisitions per Week"):
        parsers.parse_settings(html, "51347", WEEK6)


def test_parse_matchup():
    m = parsers.parse_matchup(load("matchup_51347.html"), "51347", moves_used=2)
    assert m.league_id == "51347"
    assert m.week == 6
    assert m.opponent_team_id == "13"
    assert m.opponent_name == "Team 2"
    assert m.moves_used == 2
    assert m.my_totals == StatLine(
        fgm=48,
        fga=120,
        ftm=22,
        fta=28,
        tpm=24,
        pts=142,
        reb=43,
        ast=35,
        stl=12,
        blk=8,
        to=17,
    )
    assert m.opp_totals == StatLine(
        fgm=42,
        fga=81,
        ftm=14,
        fta=17,
        tpm=16,
        pts=114,
        reb=41,
        ast=23,
        stl=4,
        blk=6,
        to=11,
    )
    assert (m.cats_won, m.cats_lost) == (6, 3)


def test_parse_matchup_games_remaining():
    m = parsers.parse_matchup(load("matchup_51347.html"), "51347", moves_used=0)
    assert (m.my_games_played, m.my_games_total, m.my_games_remaining) == (11, 38, 27)
    assert (m.opp_games_played, m.opp_games_total, m.opp_games_remaining) == (
        10,
        34,
        24,
    )


def _without_remaining_games(html: str) -> str:
    start = html.index('<div id="remaining-games"')
    # Drop the whole remaining-games block; the rest of the page is intact.
    end = html.index("</div>\n</div>", start) + len("</div>\n</div>")
    return html[:start] + html[end:]


def _without_score_row(html: str) -> str:
    # "vs" is the row header of the score row only; rename it out of the way.
    return html.replace(">vs</div></div></th>", ">v</div></div></th>")


def test_parse_matchup_past_week_without_extras_leaves_them_unset():
    html = _without_score_row(_without_remaining_games(load("matchup_51347.html")))
    m = parsers.parse_matchup(html, "51347", moves_used=0, current=False)
    assert m.my_games_remaining is None
    assert m.opp_games_total is None
    assert (m.cats_won, m.cats_lost) == (None, None)
    assert m.my_totals.pts == 142


def test_parse_matchup_current_week_requires_remaining_games():
    html = _without_remaining_games(load("matchup_51347.html"))
    with pytest.raises(ParseError, match="remaining-games"):
        parsers.parse_matchup(html, "51347", moves_used=0)


def test_parse_matchup_current_week_requires_score_row():
    html = _without_score_row(load("matchup_51347.html"))
    assert html != load("matchup_51347.html")
    with pytest.raises(ParseError, match="score"):
        parsers.parse_matchup(html, "51347", moves_used=0)


def test_parse_matchup_week():
    assert parsers.parse_matchup_week(load("matchup_51347.html")) == 6


@pytest.mark.parametrize("name", ["51347", "57243"])
def test_parse_team_week(name):
    assert parsers.parse_team_week(load(f"team_{name}.html")) == 6


def test_parse_settings_empty_league_name_is_an_error():
    original = load("settings_51347.html")
    head, label, tail = original.partition("League Name:")
    html = head + label + tail.replace("League 1", "&nbsp;", 1)
    assert html != original
    with pytest.raises(ParseError, match="League Name"):
        parsers.parse_settings(html, "51347", WEEK6)


@pytest.mark.parametrize(("name", "used", "cap"), [("51347", 2, 5), ("57243", 3, 7)])
def test_parse_adds_used(name, used, cap):
    assert parsers.parse_adds_used(load(f"team_{name}.html")) == (used, cap)


def test_parse_my_team_id():
    assert parsers.parse_my_team_id(load("matchup_51347.html"), "51347") == "9"


def test_parse_roster():
    roster = parsers.parse_roster(load("team_51347.html"))
    assert len(roster) == 14
    assert [e.slot for e in roster] == [
        "PG", "SG", "G", "SF", "PF", "F", "C", "Util", "Util", "Util",
        "BN", "BN", "BN", "IL",
    ]  # fmt: skip
    first = roster[0].player
    assert first.name == "De'Anthony Melton"
    assert first.yahoo_id == "6057"
    assert first.nba_team == "GSW"
    assert first.positions == ("PG", "SG")
    assert first.key == "deanthony melton|GSW"
    assert first.status is None
    il = roster[-1]
    assert il.slot == "IL"
    assert il.player.name == "Dejounte Murray"
    assert il.player.status == "O"
    assert il.player.nba_team == "NOP"
    assert roster[6].player.status == "D"
    assert roster[9].player.name == "Y. Konan Niederhäuser"


def test_parse_roster_skips_an_empty_slot():
    # Yahoo labels an unfilled slot "(Empty)" in a div.emptyplayer, with no name link.
    roster = parsers.parse_roster(load("team_57243_empty_slot.html"))
    assert len(roster) == 14
    assert "G" not in [e.slot for e in roster]
    assert roster[0].player.name == "Darius Acuff Jr."
    assert roster[-1].slot == "IL"


def test_parse_free_agents():
    fas = parsers.parse_free_agents(load("players_51347.html"), TODAY)
    assert len(fas) == 25
    first = fas[0]
    assert first.player.name == "Justin Champagnie"
    assert first.player.yahoo_id == "6632"
    assert first.player.nba_team == "WAS"
    assert first.player.status == "O"
    assert (first.on_waivers, first.waiver_clear) == (False, None)
    martin = next(f for f in fas if f.player.name == "Caleb Martin")
    assert martin.on_waivers
    assert martin.waiver_clear == date(2026, 11, 26)
    assert martin.player.positions == ("SF", "PF")
    waivers = {f.player.name: f.waiver_clear for f in fas if f.on_waivers}
    assert waivers["Bennett Stirtz"] == date(2026, 11, 28)
    assert len(waivers) == 6


def test_parse_free_agents_waiver_date_rolls_into_next_year():
    html = load("players_51347.html").replace("W (Nov 26)", "W (Jan 2)")
    fas = parsers.parse_free_agents(html, date(2026, 12, 30))
    clears = {f.waiver_clear for f in fas if f.on_waivers}
    assert date(2027, 1, 2) in clears


def test_parse_taken_players():
    players = parsers.parse_taken_players(load("players_taken_57243.html"))
    assert len(players) == 25
    first = players[0]
    assert (first.name, first.nba_team, first.positions) == ("Victor Wembanyama", "SAS", ("C",))
    assert first.yahoo_id == "10094"
    assert parsers.has_next_page(load("players_taken_57243.html"))


def test_parse_taken_players_needs_rows_and_the_roster_status_column():
    html = load("players_taken_57243.html")
    with pytest.raises(ParseError, match="Roster Status"):
        parsers.parse_taken_players(html.replace("Roster Status", "Owner"))
    with pytest.raises(ParseError, match="no rows"):
        parsers.parse_taken_players(_without_player_rows(html))


def test_has_next_page():
    html = load("players_51347.html")
    assert parsers.has_next_page(html)
    last = re.sub(r'<a href="[^"]*">(Next 25)</a>', r"\1", html)
    assert last != html
    assert not parsers.has_next_page(last)


@pytest.mark.parametrize(
    "call",
    [
        lambda h: parsers.parse_league_list(h),
        lambda h: parsers.parse_week(h),
        lambda h: parsers.parse_settings(h, "51347", WEEK6),
        lambda h: parsers.parse_matchup(h, "51347", moves_used=0),
        lambda h: parsers.parse_adds_used(h),
        lambda h: parsers.parse_my_team_id(h, "51347"),
        lambda h: parsers.parse_matchup_week(h),
        lambda h: parsers.parse_team_week(h),
        lambda h: parsers.parse_roster(h),
        lambda h: parsers.parse_free_agents(h, TODAY),
    ],
)
def test_parse_error_on_layout_change(call):
    with pytest.raises(ParseError):
        call("<html></html>")


def test_parse_error_names_page_and_detail():
    with pytest.raises(ParseError) as exc:
        parsers.parse_roster("<html></html>")
    assert exc.value.page == "team"
    assert "statTable0" in exc.value.detail


def _without_player_rows(html: str) -> str:
    head, sep, rest = html.partition("<table")
    table, end, tail = rest.partition("</table>")
    table = re.sub(r"<tbody>.*</tbody>", "<tbody></tbody>", table, flags=re.DOTALL)
    return head + sep + table + end + tail


def test_empty_waivers_page_is_an_empty_list():
    html = _without_player_rows(load("players_51347.html"))
    assert parsers.parse_free_agents(html, TODAY, allow_empty=True) == []
    with pytest.raises(ParseError, match="no rows"):
        parsers.parse_free_agents(html, TODAY)


def test_empty_table_without_filter_form_is_still_a_parse_error():
    html = _without_player_rows(load("players_51347.html"))
    html = html.replace('name="status"', 'name="other"')
    with pytest.raises(ParseError):
        parsers.parse_free_agents(html, TODAY, allow_empty=True)


def test_live_empty_waivers_page_is_an_empty_list():
    html = load("waivers_empty_211727.html")
    assert parsers.parse_free_agents(html, TODAY, allow_empty=True) == []
    with pytest.raises(ParseError):
        parsers.parse_free_agents(html, TODAY)


def test_no_table_and_no_empty_state_text_is_a_parse_error():
    html = load("waivers_empty_211727.html").replace("No players found", "Oops")
    with pytest.raises(ParseError):
        parsers.parse_free_agents(html, TODAY, allow_empty=True)


def test_empty_state_text_outside_the_players_table_container_is_not_enough():
    html = load("waivers_empty_211727.html").replace(
        'id="players-table"', 'id="other-table"'
    )
    with pytest.raises(ParseError):
        parsers.parse_free_agents(html, TODAY, allow_empty=True)
