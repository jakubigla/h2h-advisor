import re
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from h2h_advisor.models import NINE_CAT, Category
from h2h_advisor.sources.bbm import parsers
from h2h_advisor.sources.bbm.parsers import ParseError

FIXTURES = Path(__file__).parents[2] / "fixtures" / "bbm"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def daily() -> parsers.BbmPage:
    return parsers.parse_page(load("daily_2026-11-25.html"), "day")


@pytest.fixture(scope="module")
def ros() -> parsers.BbmPage:
    return parsers.parse_page(load("ros.html"), "ros")


@pytest.fixture(scope="module")
def league_view() -> parsers.BbmPage:
    return parsers.parse_page(load("ros_league.html"), "ros")


def by_name(page: parsers.BbmPage, name: str) -> parsers.BbmRow:
    return next(r for r in page.rows if r.projection.name == name)


# --- daily ----------------------------------------------------------------


def test_daily_row_count_skips_repeated_header_rows(daily):
    assert len(daily.rows) == 189


def test_daily_own_status_counts(daily):
    assert Counter(r.own for r in daily.rows) == {
        "free_agent": 168,
        "waivers": 12,
        "mine": 8,
        "mine_il": 1,
    }


def test_daily_page_state(daily):
    assert daily.game_date == date(2026, 11, 25)
    assert daily.filter_value == "AvailableAndMyPlayers"
    assert daily.league_value == "30008"


def test_daily_player_line_derives_makes(daily):
    row = by_name(daily, "Victor Wembanyama")
    p = row.projection
    assert p.player_key == "victor wembanyama|SAS"
    assert p.nba_team == "SAS"
    assert row.own == "mine_il"
    assert row.bbm_id == "6728"
    assert row.games == 1
    assert p.value == 1.42
    line = p.per_game
    assert (line.pts, line.tpm, line.reb, line.ast) == (32.5, 2.4, 9.5, 3.7)
    assert (line.stl, line.blk, line.to) == (1.3, 3.2, 2.6)
    assert (line.fga, line.fta) == (23.4, 7.4)
    assert line.fgm == pytest.approx(0.513 * 23.4)
    assert line.ftm == pytest.approx(0.821 * 7.4)


def test_daily_cat_values_has_all_nine(daily):
    p = by_name(daily, "Victor Wembanyama").projection
    assert set(p.cat_values) == set(NINE_CAT)
    assert p.cat_values == {
        Category.PTS: 2.87,
        Category.TPM: 0.80,
        Category.REB: 1.81,
        Category.AST: 0.14,
        Category.STL: 0.89,
        Category.BLK: 5.31,
        Category.FG_PCT: 1.23,
        Category.FT_PCT: 0.69,
        Category.TO: -0.99,
    }


def test_every_row_has_value_and_cat_values(daily, ros, league_view):
    for page in (daily, ros, league_view):
        for row in page.rows:
            assert row.projection.value is not None
            assert set(row.projection.cat_values) == set(NINE_CAT)


def test_team_codes_are_canonicalised(daily, ros):
    daily_teams = {r.projection.nba_team for r in daily.rows}
    ros_teams = {r.projection.nba_team for r in ros.rows}
    assert "PHX" in daily_teams
    assert {"PHX", "NOP"} <= ros_teams
    assert not ({"PHO", "NOR"} & (daily_teams | ros_teams))
    assert all(
        r.projection.player_key.endswith("|" + r.projection.nba_team) for r in ros.rows
    )


def test_positions_split_on_known_tokens():
    split = parsers.split_positions
    assert split("SFPF") == ("SF", "PF")
    assert split("SF PF") == ("SF", "PF")
    assert split("PGSG") == ("PG", "SG")
    assert split("PGSGSF") == ("PG", "SG", "SF")
    assert split("SFPFC") == ("SF", "PF", "C")
    assert split("C") == ("C",)
    assert split("GF") == ("G", "F")
    with pytest.raises(ParseError):
        split("XX")


def test_daily_rows_carry_player_positions(daily):
    row = by_name(daily, "Victor Wembanyama")
    assert row.positions == ("C",)


# --- games table (the schedule) ------------------------------------------

GAMES_2026_11_25 = (
    ("MIA", "PHI"),
    ("CHI", "CHA"),
    ("CLE", "NYK"),
    ("MIN", "OKC"),
    ("LAC", "MEM"),
    ("BKN", "MIL"),
    ("PHX", "HOU"),
    ("SAC", "GSW"),
    ("SAS", "POR"),
)


def _games_page(
    body_rows: str, *, header: str = "<th>#</th><th></th><th>Game</th><th>Time</th>"
) -> str:
    """The daily fixture with its games table swapped for synthetic markup."""
    html = load("daily_2026-11-25.html")
    start = html.index('<table class="table-bordered')
    end = html.index("</table>", start) + len("</table>")
    table = f"<table><thead><tr>{header}</tr></thead><tbody>{body_rows}</tbody></table>"
    return html[:start] + table + html[end:]


def _game_row(game: str) -> str:
    return f"<tr><td>1</td><td></td><td>{game}</td></tr>"


ALL_GAME_ROWS = "".join(_game_row(f"{a} @ {h}") for a, h in (
    ("MIA", "PHI"), ("CHI", "CHA"), ("CLE", "NYK"), ("MIN", "OKC"), ("LAC", "MEM"),
    ("BKN", "MIL"), ("PHO", "HOU"), ("SAC", "GSW"), ("SAS", "POR"),
))  # fmt: skip


def _day_page_without_players(games_section: str) -> str:
    """A minimal daily page with no player rows, so no team needs a game."""
    heads = "".join(
        f"<th>{h}</th>"
        for h in [
            "Own",
            "Value",
            "Name",
            "Team",
            "Pos",
            "g",
            "pts",
            "3",
            "reb",
            "ast",
            "stl",
            "blk",
            "fg%",
            "fga",
            "ft%",
            "fta",
            "to",
            "pV",
            "3V",
            "rV",
            "aV",
            "sV",
            "bV",
            "fg%V",
            "ft%V",
            "toV",
        ]
    )
    return f"""
    <input type="date" name="GAMEDATE" value="2026-12-25">
    <select name="PlayerFilterControl"><option value="AllPlayers" selected>x</option></select>
    <div><h3>Games for Friday<small><input type="submit"></small></h3>{games_section}</div>
    <table class="modern-stats-table"><thead><tr>{heads}</tr></thead><tbody></tbody></table>
    <p>Player news: no games for 4 days.</p>"""


def _game_table(rows: str) -> str:
    return f"<table><thead><tr><th>#</th><th></th><th>Game</th><th>Time</th></tr></thead><tbody>{rows}</tbody></table>"


def test_daily_games_are_away_home_pairs_in_canonical_codes(daily):
    # BBM writes PHO; the schedule uses the canonical PHX
    assert daily.games == GAMES_2026_11_25


def test_ros_page_has_no_games(ros, league_view):
    assert ros.games == () and league_view.games == ()


def test_games_table_is_found_by_its_game_header_not_by_position():
    html = _games_page(ALL_GAME_ROWS + _game_row("NOR @ UTAH"))
    page = parsers.parse_page(html, "day")
    assert page.games[-1] == ("NOP", "UTA")
    assert len(page.rows) == 189  # the player table is still the one parsed


def test_empty_games_table_means_no_games_that_day():
    page = parsers.parse_page(_day_page_without_players(_game_table("")), "day")
    assert page.games == ()


def test_placeholder_row_without_a_game_cell_means_no_games():
    for row in (
        "<tr><td colspan='5'>No games scheduled</td></tr>",
        "<tr><td>No games</td></tr>",
    ):
        page = parsers.parse_page(_day_page_without_players(_game_table(row)), "day")
        assert page.games == ()


def test_no_games_text_in_the_games_section_without_a_table_means_no_games():
    page = parsers.parse_page(
        _day_page_without_players("<p>No games today.</p>"), "day"
    )
    assert page.games == ()


def test_no_games_text_outside_the_games_section_is_not_an_off_day():
    # player news elsewhere on the page says "no games for 4 days"
    html = _day_page_without_players("")
    assert "no games for 4 days" in html
    with pytest.raises(ParseError, match="games table"):
        parsers.parse_page(html, "day")


def test_missing_games_table_on_a_real_game_day_is_a_parse_error():
    html = load("daily_2026-11-25.html")
    start = html.index('<table class="table-bordered')
    end = html.index("</table>", start) + len("</table>")
    html = html[:start] + html[end:].replace(
        "</body>", "<p>no games for 4 days</p></body>"
    )
    with pytest.raises(ParseError, match="games table"):
        parsers.parse_page(html, "day")


def test_games_table_without_a_game_header_is_a_parse_error():
    with pytest.raises(ParseError, match="games table"):
        parsers.parse_page(_games_page("", header="<th>#</th><th>Match</th>"), "day")


def test_empty_games_but_players_with_games_leaves_every_playing_team_uncovered(
    daily,
):
    page = parsers.parse_page(_games_page(""), "day")
    assert page.games == ()
    assert len(page.uncovered_teams) == 18  # every team in the nine games
    assert daily.uncovered_teams == ()


def test_annotated_game_cell_still_gives_its_teams():
    rows = ALL_GAME_ROWS.replace("MIA @ PHI", "MIA @ PHI (PPD)")
    assert parsers.parse_page(_games_page(rows), "day").games == GAMES_2026_11_25


def test_unparseable_game_row_is_skipped_when_its_teams_are_covered_elsewhere():
    rows = ALL_GAME_ROWS + _game_row("TBD") + "<tr><td>note</td></tr>"
    assert parsers.parse_page(_games_page(rows), "day").games == GAMES_2026_11_25


@pytest.mark.parametrize("game", ["MIA vs PHI", "MIA", "@ PHI", "Miami @ Philadelphia"])
def test_unparseable_game_row_with_uncovered_teams_is_reported_uncovered(game):
    rows = ALL_GAME_ROWS.replace("MIA @ PHI", game)  # MIA and PHI now have no game
    page = parsers.parse_page(_games_page(rows), "day")
    assert page.uncovered_teams == ("MIA", "PHI")
    assert len(page.games) == 8


def test_games_table_with_the_player_table_class_is_still_the_games_table():
    # BBM once gave both tables class modern-stats-table
    html = _games_page(ALL_GAME_ROWS).replace(
        "<table><thead><tr><th>#", '<table class="modern-stats-table"><thead><tr><th>#'
    )
    page = parsers.parse_page(html, "day")
    assert page.games == GAMES_2026_11_25 and len(page.rows) == 189


def test_ambiguous_tables_are_parse_errors():
    html = load("daily_2026-11-25.html")
    games = _game_table(ALL_GAME_ROWS)
    with pytest.raises(ParseError, match="several games tables"):
        parsers.parse_page(html.replace("</body>", games + "</body>"), "day")
    start = html.index('<table class="modern-stats-table"')
    end = html.index("</table>", start) + len("</table>")
    with pytest.raises(ParseError, match="several player tables"):
        parsers.parse_page(html.replace("</body>", html[start:end] + "</body>"), "day")


# --- live markup of 2026-11-25 (both tables are table.modern-stats-table) ----


@pytest.fixture(scope="module")
def live2() -> parsers.BbmPage:
    return parsers.parse_page(load("daily_2026-11-25_live2.html"), "day")


def test_live2_games_and_rows(live2):
    assert live2.games == GAMES_2026_11_25
    assert live2.uncovered_teams == ()
    assert live2.game_date == date(2026, 11, 25)
    assert len(live2.rows) == 190  # no "$" column on this page
    assert all(r.projection.value is not None for r in live2.rows)


def test_live2_player_table_is_not_the_games_table(live2):
    assert {r.projection.nba_team for r in live2.rows} >= {"PHI", "PHX", "POR"}
    assert Counter(r.own for r in live2.rows)  # own statuses read from the player table


# --- per-game stat headers after a postback (live 2026-11-27) ----------------

GAMES_2026_11_27 = (
    ("MIA", "NYK"), ("MIL", "DET"), ("BKN", "TOR"), ("PHI", "IND"), ("ATL", "BOS"),
    ("MEM", "MIN"), ("DAL", "PHX"), ("DEN", "UTA"), ("LAL", "SAS"), ("NOP", "LAC"),
    ("GSW", "POR"),
)  # fmt: skip


@pytest.fixture(scope="module")
def pergame() -> parsers.BbmPage:
    # trimmed to the games table and the first 120 player rows (AllPlayers)
    return parsers.parse_page(load("daily_2026-11-27_pergame.html"), "day")


def test_postback_day_page_with_per_game_headers_parses(pergame):
    assert pergame.game_date == date(2026, 11, 27)
    assert pergame.games == GAMES_2026_11_27
    assert pergame.uncovered_teams == ()
    assert len(pergame.rows) == 120


def test_postback_day_page_stats_come_from_the_per_game_columns(pergame):
    wemby = by_name(pergame, "Victor Wembanyama").projection.per_game
    assert (wemby.pts, wemby.reb, wemby.ast, wemby.stl, wemby.blk, wemby.to) == (
        27.2, 11.6, 3.2, 1.3, 3.5, 2.2,
    )  # fmt: skip
    assert (wemby.tpm, wemby.fga, wemby.fta) == (1.9, 18.6, 7.1)
    assert wemby.fgm == pytest.approx(9.7278)  # pct * attempts


def test_day_page_with_both_name_styles_for_a_stat_is_ambiguous():
    html = load("daily_2026-11-27_pergame.html")  # has p/g; give another column "pts"
    both, n = re.subn(r">Ease</(a|th)>", r">pts</\1>", html)
    assert n >= 1
    with pytest.raises(ParseError, match="ambiguous columns: pts, p/g"):
        parsers.parse_page(both, "day")


def test_day_page_with_neither_name_style_for_a_stat_is_missing_it():
    html = load("daily_2026-11-27_pergame.html")
    broken, n = re.subn(r">p/g</(a|th)>", r">points</\1>", html)
    assert n >= 1
    with pytest.raises(ParseError, match="missing columns: pts"):
        parsers.parse_page(broken, "day")


# --- a day without games has no player table (Thanksgiving) -------------------
# No live fixture: assumed shape is the "Games for ..." section and nothing else.


def _without_player_table(games_section: str) -> str:
    html = _day_page_without_players(games_section)
    start = html.index('<table class="modern-stats-table"')
    end = html.index("</table>", start) + len("</table>")
    return html[:start] + html[end:]


@pytest.mark.parametrize(
    "section",
    [
        _game_table(""),
        _game_table("<tr><td colspan='5'>No games scheduled</td></tr>"),
        "<p>No games today.</p>",
    ],
)
def test_day_without_games_and_without_player_table_is_a_valid_empty_day(section):
    page = parsers.parse_page(_without_player_table(section), "day")
    assert page.rows == [] and page.games == () and page.uncovered_teams == ()
    assert page.game_date == date(2026, 12, 25)
    assert page.filter_value == "AllPlayers"


def test_missing_player_table_while_games_exist_is_a_parse_error():
    html = _without_player_table(_game_table(_game_row("MIA @ PHI")))
    with pytest.raises(ParseError, match="player table"):
        parsers.parse_page(html, "day")


def test_missing_player_table_without_a_games_section_is_a_parse_error():
    html = _without_player_table("")  # the section is there but says nothing
    with pytest.raises(ParseError, match="player table"):
        parsers.parse_page(html, "day")


def test_ros_page_without_player_table_is_still_a_parse_error():
    html = _without_player_table(_game_table(""))
    with pytest.raises(ParseError, match="player table"):
        parsers.parse_page(html, "ros")


# --- rest of season --------------------------------------------------------


def test_ros_row_count_and_own_counts(ros):
    assert len(ros.rows) == 110
    assert Counter(r.own for r in ros.rows) == {
        "free_agent": 75,
        "waivers": 21,
        "mine": 13,
        "mine_il": 1,
    }


def test_ros_page_state(ros):
    assert ros.game_date is None
    assert ros.filter_value == "AvailableAndMyPlayers"
    assert ros.league_value == "30008"


def test_ros_player_line_uses_per_game_columns(ros):
    row = by_name(ros, "Victor Wembanyama")
    p = row.projection
    assert row.games == 46
    assert p.value == 1.51
    line = p.per_game
    assert (line.pts, line.tpm, line.reb, line.ast) == (30.6, 2.1, 10.7, 3.4)
    assert (line.stl, line.blk, line.to) == (1.2, 3.6, 2.4)
    assert (line.fga, line.fta) == (21.3, 7.5)
    assert line.fgm == pytest.approx(0.524 * 21.3)
    assert line.ftm == pytest.approx(0.821 * 7.5)
    assert p.cat_values[Category.BLK] == 6.15


def test_ros_league_view_has_owner_column_and_no_dollar_column(league_view):
    assert len(league_view.rows) == 107
    assert league_view.league_value == "602"
    assert league_view.filter_value == "AvailableAndMyPlayers"
    assert Counter(r.own for r in league_view.rows) == {
        "free_agent": 82,
        "waivers": 11,
        "mine": 13,
        "mine_il": 1,
    }
    tatum = by_name(league_view, "Jayson Tatum").projection
    assert tatum.value == 0.49
    assert tatum.per_game.pts == 28.2
    assert tatum.per_game.fga == 20.5


def test_ros_rows_positions_from_spaced_text(league_view):
    assert by_name(league_view, "Jayson Tatum").positions == ("SF", "PF")


# --- league options --------------------------------------------------------


def test_league_options(daily, league_view):
    for page in (daily, league_view):
        assert len(page.league_options) == 13
        for value in ("602", "30008", "28681", "26949", "31648"):
            assert value in page.league_options
    assert daily.league_options["602"] == "_1. League 1 (Yahoo!)"
    assert daily.league_options["30008"] == "_2. League 2 (Yahoo!)"


def test_league_name_strips_numeric_prefix():
    assert (
        parsers.league_name("_1. H2H Quokka/Wombat Cup (Yahoo!)")
        == "H2H Quokka/Wombat Cup (Yahoo!)"
    )
    assert parsers.league_name("3. Emu Superliga (Yahoo!)") == "Emu Superliga (Yahoo!)"
    assert parsers.league_name("No prefix") == "No prefix"


# --- own status rules on synthetic markup ---------------------------------


def _page(own_cell: str, *, filter_value: str, owner: str | None = None) -> str:
    owner_th = "<th>Owner</th>" if owner is not None else ""
    owner_td = f"<td>{owner}</td>" if owner is not None else ""
    return f"""
    <select name="PlayerFilterControl">
      <option value="TopPlayers">t</option>
      <option value="{filter_value}" selected="selected">x</option>
    </select>
    <table class="modern-stats-table"><thead><tr>
      <th>Own</th>{owner_th}<th>Value</th><th>Name</th><th>Team</th><th>Pos</th>
      <th>g</th><th>p/g</th><th>3/g</th><th>r/g</th><th>a/g</th><th>s/g</th>
      <th>b/g</th><th>fg%</th><th>fga/g</th><th>ft%</th><th>fta/g</th><th>to/g</th>
      <th>pV</th><th>3V</th><th>rV</th><th>aV</th><th>sV</th><th>bV</th>
      <th>fg%V</th><th>ft%V</th><th>toV</th>
    </tr></thead><tbody><tr>
      <td>{own_cell}</td>{owner_td}<td></td><td><a href="playerinfo.aspx?i=1">A. Guy Jr.</a></td>
      <td>PHO</td><td>PG</td><td>50</td><td>10</td><td>1</td><td>2</td><td>3</td>
      <td>1</td><td>0</td><td>48.8</td><td>5</td><td>.8</td><td>2</td><td>1</td>
      <td>1</td><td>1</td><td>1</td><td>1</td><td>1</td><td>1</td><td>1</td><td>1</td>
      <td>1</td>
    </tr></tbody></table>"""


def test_percent_shown_as_whole_number_is_handled():
    page = parsers.parse_page(_page("", filter_value="AvailableAndMyPlayers"), "ros")
    line = page.rows[0].projection.per_game
    assert line.fgm == pytest.approx(0.488 * 5)
    assert line.ftm == pytest.approx(0.8 * 2)
    assert page.rows[0].projection.value is None  # blank Value cell
    assert page.rows[0].projection.player_key == "a guy|PHX"


def test_blank_own_is_free_agent_when_filter_lists_only_available_and_mine():
    page = parsers.parse_page(_page("", filter_value="AvailablePlayers"), "ros")
    assert page.rows[0].own == "free_agent"


@pytest.mark.parametrize("filter_value", ["AllPlayers", "TopPlayers", "Rookies"])
def test_blank_own_on_all_teams_filter_is_unknown(filter_value):
    page = parsers.parse_page(_page("", filter_value=filter_value), "ros")
    assert page.rows[0].own is None


def test_blank_own_on_all_teams_filter_with_empty_owner_is_free_agent():
    page = parsers.parse_page(_page("", filter_value="AllPlayers", owner=""), "ros")
    assert page.rows[0].own == "free_agent"


def test_blank_own_with_other_team_owner_is_none():
    page = parsers.parse_page(
        _page("", filter_value="AllPlayers", owner="Other Team"), "ros"
    )
    assert page.rows[0].own is None


def test_waiver_flag_wins_on_all_teams_filter():
    cell = '<small title="waivers">WW</small>'
    page = parsers.parse_page(_page(cell, filter_value="AllPlayers"), "ros")
    assert page.rows[0].own == "waivers"


def test_row_without_name_link_is_skipped():
    html = _page("", filter_value="AllPlayers").replace(
        '<a href="playerinfo.aspx?i=1">A. Guy Jr.</a>', "A. Guy Jr."
    )
    assert parsers.parse_page(html, "ros").rows == []


# --- errors ------------------------------------------------------------------


def test_missing_columns_parse_error():
    html = load("daily_2026-11-25.html")
    # Rename the fga header (a sort link in the real header row, plain text in
    # the copies repeated inside tbody).
    broken, n = re.subn(r">fga</(a|th)>", r">attempts</\1>", html)
    assert n >= 1
    with pytest.raises(ParseError, match="fga"):
        parsers.parse_page(broken, "day")


def test_ros_page_is_not_a_daily_page():
    # the stat names match after a postback, but the ROS page has no game date
    with pytest.raises(ParseError, match="GAMEDATE"):
        parsers.parse_page(load("ros.html"), "day")


@pytest.mark.parametrize("html", ["", "<html><body>nothing</body></html>"])
def test_missing_table_parse_error(html):
    with pytest.raises(ParseError, match="table"):
        parsers.parse_page(html, "day")


def test_non_numeric_stat_parse_error():
    html = _page("", filter_value="AllPlayers").replace("<td>48.8</td>", "<td>n/a</td>")
    with pytest.raises(ParseError, match="fg%"):
        parsers.parse_page(html, "ros")


def test_daily_page_without_date_parse_error():
    html = load("daily_2026-11-25.html").replace('name="GAMEDATE"', 'name="OTHER"')
    with pytest.raises(ParseError, match="GAMEDATE"):
        parsers.parse_page(html, "day")


def test_status_column_gives_the_injury_label(ros):
    statuses = Counter(r.status for r in ros.rows if r.status)
    assert statuses == Counter(
        {"INJ": 17, "Q": 7, "OUT": 2, "D": 1, "IN": 1, "Off Inj": 1}
    )
    assert sum(1 for r in ros.rows if r.status is None) == len(ros.rows) - 29


def test_ros_rows_carry_projected_minutes():
    page = parsers.parse_page(load("ros.html"), "ros")
    minutes = [r.projection.minutes for r in page.rows]
    assert all(m is not None and 0 <= m <= 48 for m in minutes)
    assert any(m >= 20 for m in minutes)
