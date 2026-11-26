import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "sanitize_fixture.py"
spec = importlib.util.spec_from_file_location("sanitize_fixture", SCRIPT)
sanitize_fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sanitize_fixture)
sanitize = sanitize_fixture.sanitize

PLAYER = (
    '<table><tr><td class="player"><a class="name" '
    'href="https://sports.yahoo.com/nba/players/1">Jaylen Brown</a></td>'
    "<td>C</td><td>td stuff</td></tr></table>"
)


def page(body: str) -> str:
    return f"<html><head><title>L</title></head><body>{body}</body></html>"


def test_manager_named_like_a_player_keeps_the_player_name(capsys):
    out = sanitize(
        page(f'<span class="user-id">Brown</span>{PLAYER}<p>Brown said hi</p>'),
        "yahoo",
    )
    assert ">Jaylen Brown</a>" in out
    assert '<span class="user-id">Manager 1</span>' in out
    # The name also occurs in player text, so free text keeps it and a warning says so.
    assert "<p>Brown said hi</p>" in out
    assert "Brown" in capsys.readouterr().err


def test_short_nicknames_leave_markup_and_position_cells_alone(capsys):
    out = sanitize(
        page(
            '<span class="user-id">C</span><span class="user-id">td</span>'
            f"{PLAYER}<p>c td C</p>"
        ),
        "yahoo",
    )
    assert "<td>C</td>" in out
    assert "<td>td stuff</td>" in out
    assert '<td class="player">' in out
    assert "<p>c td C</p>" in out
    assert '<span class="user-id">Manager 1</span>' in out
    assert '<span class="user-id">Manager 2</span>' in out
    assert "shorter than" in capsys.readouterr().err


def test_similar_names_are_different_managers():
    out = sanitize(
        page(
            '<span class="user-id">Al</span><span class="user-id">Alex</span>'
            '<a href="/profile/alex9">Alex</a><a href="/profile/al2">Al</a>'
        ),
        "yahoo",
    )
    assert (
        '<span class="user-id">Manager 1</span><span class="user-id">Manager 2</span>'
        in out
    )
    assert '<a href="/profile/manager-2">Manager 2</a>' in out
    assert '<a href="/profile/manager-1">Manager 1</a>' in out


def test_slugs_change_only_in_profile_urls():
    body = (
        '<span class="user-id">C</span><a href="/profile/c">View Profile</a>'
        '<span class="user-id">Brown</span><a href="/profile/brown">View Profile</a>'
        '<a href="/nba/x/brown">team brown</a><a href="https://sports.yahoo.com/nba">c</a>'
        "<b>c</b>"
    )
    out = sanitize(page(body), "yahoo")
    assert 'href="/profile/manager-1"' in out
    assert 'href="/profile/manager-2"' in out
    assert 'href="/nba/x/brown"' in out
    assert 'class="user-id"' in out
    assert "<b>c</b>" in out
    assert 'href="https://sports.yahoo.com/nba"' in out
    assert "/profile/c" not in out
    assert "/profile/brown" not in out


def test_slug_does_not_match_a_longer_slug():
    out = sanitize(
        page(
            '<a href="/profile/ab">Anna</a><a href="/profile/abc">Bella</a>'
            '<span class="user-id">Anna</span><span class="user-id">Bella</span>'
        ),
        "yahoo",
    )
    assert out.count("/profile/manager-1") == 1
    assert out.count("/profile/manager-2") == 1


def test_viewstate_blanked_but_field_count_kept():
    out = sanitize(
        page(
            '<input name="__VIEWSTATEFIELDCOUNT" value="17">'
            '<input name="__VIEWSTATE" value="secret0">'
            '<input name="__VIEWSTATE1" value="secret1">'
            '<input name="__EVENTVALIDATION" value="secret2">'
        ),
        "bbm",
    )
    assert 'name="__VIEWSTATEFIELDCOUNT" value="17"' in out
    assert 'name="__VIEWSTATE" value="X"' in out
    assert 'name="__VIEWSTATE1" value="X"' in out
    assert 'name="__EVENTVALIDATION" value="X"' in out
    assert "secret" not in out


def test_email_replaced_but_image_names_kept():
    out = sanitize(page('<p>mail me@secret.org</p><img src="chat@6x.png">'), "yahoo")
    assert "user@example.com" in out
    assert "secret.org" not in out
    assert "chat@6x.png" in out


@pytest.mark.parametrize(
    "markup",
    [
        '<a href="/x?crumb=abc123&amp;y=1">l</a>',
        '<a href="/x?y=1&amp;crumb=abc123">l</a>',
    ],
)
def test_crumb_value_replaced(markup):
    out = sanitize(page(markup), "yahoo")
    assert "crumb=X" in out
    assert "abc123" not in out
    assert "y=1" in out


def test_crumb_input_blanked_and_scripts_removed():
    out = sanitize(
        page('<input name="crumb" value="tok"><script>var a=1</script>'), "yahoo"
    )
    assert 'name="crumb" value="X"' in out
    assert "<script" not in out


def bbm_table(
    own_titles: list[str], *, header: str = "<th>Own</th><th>Rank</th>"
) -> str:
    rows = "".join(
        f'<tr><td><span data-toggle="tooltip" title="{t}">&nbsp;</span></td><td>1</td></tr>'
        for t in own_titles
    )
    return f"<table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table>"


def test_bbm_own_column_team_names_become_stable_generic_labels():
    out = sanitize(
        page(
            bbm_table(["Chyzio's Choice Team", "Quokka Five", "Chyzio's Choice Team"])
        ),
        "bbm",
    )
    assert out.count('title="Team 1"') == 2  # the same team keeps its label
    assert out.count('title="Team 2"') == 1
    assert "Chyzio" not in out and "Quokka Five" not in out


def test_bbm_own_team_is_kept_and_does_not_use_up_a_label():
    out = sanitize(
        page(bbm_table(["Wombat City", "Manule", "wombat city"])),
        "bbm",
        keep_teams=["Wombat City"],
    )
    assert out.count('title="Wombat City"') == 1
    assert out.count('title="wombat city"') == 1  # kept, any case
    assert 'title="Team 1"' in out and "Manule" not in out


def test_bbm_other_titles_and_other_columns_are_left_alone():
    other = (
        "<table><thead><tr><th>Name</th></tr></thead><tbody><tr><td>"
        '<span title="New Contract">x</span></td></tr></tbody></table>'
    )
    own_with_tip = (
        "<table><thead><tr><th>Rank</th><th>Own</th></tr></thead><tbody><tr>"
        '<td><span title="Rank tip">1</span></td>'
        '<td><span title="Some Team">&nbsp;</span></td></tr></tbody></table>'
    )
    out = sanitize(page(other + own_with_tip), "bbm")
    assert 'title="New Contract"' in out  # not an Own table
    assert 'title="Rank tip"' in out  # not the Own column
    assert 'title="Team 1"' in out and "Some Team" not in out


def test_yahoo_pages_do_not_get_own_team_relabelling():
    out = sanitize(page(bbm_table(["Some Team"])), "yahoo")
    assert 'title="Some Team"' in out


def test_yahoo_players_filter_team_names_become_generic_labels():
    select = (
        '<select name="fteam" id="fteamselect">'
        '<option value="NONE" selected>No Team Selected</option>'
        '<option value="6">Action Hank</option>'
        '<option value="9">Wombat City</option>'
        "</select>"
    )
    out = sanitize(page(select), "yahoo")
    assert "No Team Selected" in out
    assert ">Team 1<" in out and ">Team 2<" in out
    assert "Action Hank" not in out and "Wombat" not in out
    assert ">Action Hank<" in sanitize(page(select), "bbm")  # yahoo only


YAHOO_LEAGUE_PAGE = (
    "<html><head><title>Quokka Cup - Wombat City | Fantasy Basketball</title></head>"
    "<body>"
    '<h3><a href="/nba/123">Quokka Cup</a></h3>'
    '<a href="https://basketball.fantasysports.yahoo.com/nba/123/9">Wombat City</a>'
    '<a href="/nba/123/4/team?date=2026-11-25&amp;week=6">Emu Gang</a>'
    '<a href="/nba/123/9?stat1=S&amp;stat2=D">Today</a>'
    '<a href="/nba/123/9/team">\ue003</a>'
    '<span class="Nowrap">Wombat City</span>'
    "<caption>Wombat City's Players roster</caption>"
    '<img alt="Emu Gang" src="x.png">'
    '<select name="fteam"><option value="NONE">No Team Selected</option>'
    '<option value="4">Emu Gang</option><option value="7">Dingo Dogs</option></select>'
    "</body></html>"
)


def test_yahoo_league_and_team_names_become_league_n_and_team_n():
    out = sanitize(YAHOO_LEAGUE_PAGE, "yahoo")
    for name in ("Quokka", "Wombat", "Emu", "Dingo"):
        assert name not in out, name
    assert "<title>League 1 - Team 1 | Fantasy Basketball</title>" in out
    assert '<a href="/nba/123">League 1</a>' in out
    assert ">Team 1</a>" in out and ">Team 2</a>" in out  # own team, opponent
    assert ">Today</a>" in out  # a stat tab of the team page, not a team
    assert "Team 1's Players roster" in out
    assert 'alt="Team 2"' in out
    assert '<option value="4">Team 2</option>' in out  # same team, same label
    assert '<option value="7">Team 3</option>' in out
    assert "No Team Selected" in out


def test_yahoo_settings_league_name_and_custom_url():
    rows = (
        '<table id="settings-table">'
        "<tr><td>League Name:</td><td><b>Quokka Cup</b></td></tr>"
        "<tr><td>Custom League URL:</td><td><b>"
        "https://basketball.fantasysports.yahoo.com/league/quokkacup</b></td></tr>"
        "<tr><td>Draft Type:</td><td><b>Live Standard Draft</b></td></tr>"
        "</table>"
    )
    out = sanitize(page(rows), "yahoo")
    assert "<b>League 1</b>" in out
    assert "quokka" not in out.lower()
    assert "fantasysports.yahoo.com/league/example" in out
    assert "<b>Live Standard Draft</b>" in out


def test_team_name_never_touches_player_names():
    body = f'<a href="/nba/123/4">Jaylen Brown</a>{PLAYER}<p>Jaylen Brown</p>'
    out = sanitize(page(body), "yahoo")
    assert '<a href="/nba/123/4">Team 1</a>' in out
    assert ">Jaylen Brown</a></td>" in out  # the player link
    assert "<p>Team 1</p>" in out


def test_sanitize_is_idempotent_and_keeps_existing_placeholders():
    once = sanitize(YAHOO_LEAGUE_PAGE, "yahoo")
    assert sanitize(once, "yahoo") == once
    mixed = page(
        '<select name="fteam"><option value="NONE">No Team Selected</option>'
        '<option value="1">Team 2</option><option value="2">Emu Gang</option>'
        '<option value="3">Team 1</option></select>'
    )
    out = sanitize(mixed, "yahoo")
    assert '<option value="1">Team 2</option>' in out
    assert '<option value="2">Team 3</option>' in out
    assert '<option value="3">Team 1</option>' in out


def test_name_map_fixes_labels_across_pages():
    out = sanitize(
        YAHOO_LEAGUE_PAGE,
        "yahoo",
        name_map={"Quokka Cup": "League 4", "Emu Gang": "Team 1"},
    )
    assert '<a href="/nba/123">League 4</a>' in out
    assert ">Team 1</a>" in out and "<title>League 4 - Team 2 |" in out
    assert "Emu" not in out


BBM_LEAGUE_SELECT = (
    '<span class="bm-custom-select-value">_1. Quokka Cup  (Yahoo!)</span>'
    '<div data-value="602" class="bm-custom-select-option selected">'
    "_1. Quokka Cup  (Yahoo!)</div>"
    '<select name="pageTitleRowLeague">'
    '<option value="602" selected="selected">_1. Quokka Cup  (Yahoo!)</option>'
    '<option value="30008">_2. I Liga Emu$  (Yahoo!)</option>'
    '<option value="5344">1. dingoroto  (Yahoo!)</option>'
    "</select>"
)


def test_bbm_league_options_keep_values_and_ordinals():
    out = sanitize(page(BBM_LEAGUE_SELECT), "bbm")
    for name in ("Quokka", "Emu", "dingo"):
        assert name not in out, name
    assert (
        '<option value="602" selected="selected">_1. League 1  (Yahoo!)</option>' in out
    )
    assert '<option value="30008">_2. League 2  (Yahoo!)</option>' in out
    assert '<option value="5344">1. League 3  (Yahoo!)</option>' in out
    assert '<span class="bm-custom-select-value">_1. League 1  (Yahoo!)</span>' in out
    assert sanitize(out, "bbm") == out


def test_bbm_owner_column_uses_the_own_column_labels():
    table = (
        "<table><thead><tr><th>Own</th><th>Owner</th><th>Name</th></tr></thead><tbody>"
        '<tr><td><span title="Wombat City">&nbsp;</span></td><td>Wombat City</td>'
        "<td>A</td></tr>"
        "<tr><td></td><td>Emu Gang</td><td>B</td></tr>"
        "<tr><td></td><td></td><td>C</td></tr>"
        "</tbody></table>"
    )
    out = sanitize(page(table), "bbm")
    assert 'title="Team 1"' in out
    assert "<td>Team 1</td>" in out and "<td>Team 2</td>" in out
    assert "Wombat" not in out and "Emu" not in out


def test_cli_map_option(tmp_path):
    src, dst = tmp_path / "in.html", tmp_path / "out.html"
    src.write_text(YAHOO_LEAGUE_PAGE, encoding="utf-8")
    sanitize_fixture.main(["yahoo", str(src), str(dst), "--map", "Quokka Cup=League 5"])
    assert '<a href="/nba/123">League 5</a>' in dst.read_text(encoding="utf-8")


def test_yahoo_team_page_title_names_the_league_and_own_team():
    html = (
        "<html><head><title>Quokka Cup - Wombat City | Fantasy Basketball | Yahoo!"
        "</title></head><body>"
        '<span class="F-reset Nowrap">Wombat City</span>'
        '<a href="/nba/123/4/team?date=2026-11-25">Emu Gang</a></body></html>'
    )
    out = sanitize(html, "yahoo")
    assert "<title>League 1 - Team 1 | Fantasy Basketball | Yahoo!</title>" in out
    assert '<span class="F-reset Nowrap">Team 1</span>' in out
    assert ">Team 2</a>" in out


def test_mapped_names_are_replaced_even_where_no_rule_finds_them():
    out = sanitize(
        page("<p>Go Wombat City!</p>"), "bbm", name_map={"Wombat City": "Team 7"}
    )
    assert "<p>Go Team 7!</p>" in out
