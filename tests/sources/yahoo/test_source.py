import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from h2h_advisor.browser.session import PageResult
from h2h_advisor.identity import PlayerIndex
from h2h_advisor.models import Category
from h2h_advisor.sources.yahoo import urls
from h2h_advisor.sources.yahoo.parsers import ParseError
from h2h_advisor.sources.yahoo.source import YahooSource

FIXTURES = Path(__file__).parents[2] / "fixtures" / "yahoo"
TODAY = date(2026, 11, 25)


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeSession:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, str]] = []

    def fetch(self, site: str, url: str) -> PageResult:
        self.calls.append((site, url))
        return PageResult(url, url, self.pages[url], datetime.now(UTC))

    @property
    def urls(self) -> list[str]:
        return [u for _, u in self.calls]


def make(pages: dict[str, str], aliases: dict[str, str] | None = None):
    session = FakeSession(pages)
    return session, YahooSource(
        session, PlayerIndex(aliases or {}), today=lambda: TODAY
    )


def test_leagues():
    session, source = make({urls.home(): load("home.html")})
    leagues = source.leagues()
    assert [x.league_id for x in leagues] == [
        "51347",
        "57243",
        "59422",
        "211727",
        "259619",
    ]
    assert session.calls == [("yahoo", urls.home())]


def test_settings_combines_settings_and_matchup_pages():
    session, source = make(
        {
            urls.league_settings(51347): load("settings_51347.html"),
            urls.matchup(51347): load("matchup_51347.html"),
        }
    )
    s = source.settings("51347")
    assert s.max_adds_per_week == 5
    assert s.categories[0] is Category.FG_PCT
    assert (s.current_week, s.week_start, s.week_end) == (
        6,
        date(2026, 11, 23),
        date(2026, 11, 29),
    )
    assert sorted(session.urls) == sorted(
        [urls.league_settings(51347), urls.matchup(51347)]
    )


def test_matchup_current_week_reads_adds_from_my_team_page():
    session, source = make(
        {
            urls.matchup(51347): load("matchup_51347.html"),
            urls.team(51347, 9): load("team_51347.html"),
        }
    )
    m = source.matchup("51347")
    assert (m.week, m.opponent_team_id, m.moves_used) == (6, "13", 2)
    assert session.urls == [urls.matchup(51347), urls.team(51347, 9)]


def matchup_page(week: int) -> str:
    """The week-6 fixture relabelled as another week."""
    html = load("matchup_51347.html")
    relabelled = html.replace('title="Week 6"', f'title="Week {week}"', 1)
    return relabelled.replace("Week 6\n", f"Week {week}\n", 1)


def test_history_fetches_one_matchup_page_per_week_plus_one_team_page():
    session, source = make(
        {
            urls.matchup(51347, 4): matchup_page(4),
            urls.matchup(51347, 5): matchup_page(5),
            urls.team(51347, 9): load("team_51347.html"),
        }
    )
    matchups = source.history("51347", [4, 5])
    assert [m.week for m in matchups] == [4, 5]
    assert {m.moves_used for m in matchups} == {0}
    assert session.urls == [
        urls.matchup(51347, 4),
        urls.matchup(51347, 5),
        urls.team(51347, 9),
    ]


def test_matchup_for_the_current_week_by_number_reads_adds_used():
    _, source = make(
        {
            urls.matchup(51347, 6): load("matchup_51347.html"),
            urls.team(51347, 9): load("team_51347.html"),
        }
    )
    assert source.matchup("51347", week=6).moves_used == 2


def test_matchup_for_a_past_week_has_no_moves_used():
    _, source = make(
        {
            urls.matchup(51347, 5): matchup_page(5),
            urls.team(51347, 9): load("team_51347.html"),
        }
    )
    m = source.matchup("51347", week=5)
    assert (m.week, m.moves_used) == (5, 0)


def test_matchup_raises_when_the_page_shows_another_week():
    session, source = make({urls.matchup(51347, 5): load("matchup_51347.html")})
    with pytest.raises(ParseError, match="asked for week 5.*week 6"):
        source.matchup("51347", week=5)
    assert session.urls == [urls.matchup(51347, 5)]


def test_history_raises_when_yahoo_ignores_the_week_param():
    _, source = make(
        {
            urls.matchup(51347, 4): load("matchup_51347.html"),
            urls.matchup(51347, 5): load("matchup_51347.html"),
        }
    )
    with pytest.raises(ParseError):
        source.history("51347", [4, 5])


def test_history_of_no_weeks_loads_nothing():
    session, source = make({})
    assert source.history("51347", []) == []
    assert session.calls == []


def test_roster_rekeys_players_through_the_index():
    _, source = make(
        {urls.team(51347, 9): load("team_51347.html")},
        aliases={"Y. Konan Niederhäuser": "Konan Niederhauser"},
    )
    roster = source.roster("51347", "9")
    assert len(roster) == 14
    assert roster[9].player.key == "konan niederhauser|LAC"
    assert roster[0].player.key == "deanthony melton|GSW"


def test_free_agents_page_cap():
    session, source = make(
        {
            urls.free_agents(51347, 0): load("players_51347.html"),
            urls.free_agents(51347, 25): load("players_51347.html"),
            urls.free_agents(51347, 50): load("players_51347.html"),
        }
    )
    fas = source.free_agents("51347", limit=50)
    assert len(fas) == 50
    assert session.urls == [urls.free_agents(51347, 0), urls.free_agents(51347, 25)]


def test_free_agents_trims_to_limit_and_fetches_only_needed_pages():
    session, source = make({urls.free_agents(51347, 0): load("players_51347.html")})
    assert len(source.free_agents("51347", limit=10)) == 10
    assert len(session.calls) == 1


def test_free_agents_uneven_limit_uses_ceil_pages():
    session, source = make(
        {
            urls.free_agents(51347, 0): load("players_51347.html"),
            urls.free_agents(51347, 25): load("players_51347.html"),
        }
    )
    assert len(source.free_agents("51347", limit=30)) == 30
    assert len(session.calls) == 2


def test_free_agents_stops_when_there_is_no_next_page():
    page = load("players_51347.html")
    last = re.sub(r'<a href="[^"]*">(Next 25)</a>', r"\1", page)
    assert last != page
    session, source = make({urls.free_agents(51347, 0): last})
    assert len(source.free_agents("51347", limit=100)) == 25
    assert len(session.calls) == 1


def test_free_agents_position_filter_is_passed_to_the_url():
    session, source = make(
        {urls.free_agents(51347, 0, position="PG"): load("players_51347.html")}
    )
    source.free_agents("51347", limit=25, position="PG")
    assert session.urls == [urls.free_agents(51347, 0, position="PG")]


def test_free_agents_rejects_non_positive_limit():
    _, source = make({})
    with pytest.raises(ValueError):
        source.free_agents("51347", limit=0)


def test_week_reads_the_current_matchup_page():
    session, source = make({urls.matchup(51347): load("matchup_51347.html")})
    assert source.week("51347") == (6, date(2026, 11, 23), date(2026, 11, 29))
    assert session.urls == [urls.matchup(51347)]


def test_waivers_use_the_waiver_filter_and_at_most_two_pages():
    page = load("players_51347.html")
    session, source = make(
        {
            urls.free_agents(51347, 0, status="W"): page,
            urls.free_agents(51347, 25, status="W"): page,
        }
    )
    assert len(source.waivers("51347")) == 50
    assert session.urls == [
        urls.free_agents(51347, 0, status="W"),
        urls.free_agents(51347, 25, status="W"),
    ]


def test_empty_waivers_page_gives_no_waivers_and_one_load():
    page = load("players_51347.html")
    head, sep, rest = page.partition("<table")
    table, end, tail = rest.partition("</table>")
    table = re.sub(r"<tbody>.*</tbody>", "<tbody></tbody>", table, flags=re.DOTALL)
    session, source = make(
        {urls.free_agents(51347, 0, status="W"): head + sep + table + end + tail}
    )
    assert source.waivers("51347") == []
    assert len(session.calls) == 1


def test_taken_reads_every_page_until_there_is_no_next():
    page = load("players_taken_57243.html")
    last = re.sub(r'<a href="[^"]*">(Next 25)</a>', r"\1", page)
    session, source = make(
        {
            urls.free_agents(57243, 0, status="T"): page,
            urls.free_agents(57243, 25, status="T"): last,
        }
    )
    taken = source.taken(57243)
    assert len(taken) == 50
    assert taken[0].key == "victor wembanyama|SAS"
    assert session.urls == [
        urls.free_agents(57243, 0, status="T"),
        urls.free_agents(57243, 25, status="T"),
    ]


def test_taken_list_that_never_ends_is_an_error_not_a_short_list(monkeypatch):
    monkeypatch.setattr("h2h_advisor.sources.yahoo.source.TAKEN_PAGES_MAX", 2)
    page = load("players_taken_57243.html")
    _, source = make(
        {urls.free_agents(57243, n, status="T"): page for n in (0, 25, 50)}
    )
    with pytest.raises(ParseError, match="not read whole"):
        source.taken(57243)
