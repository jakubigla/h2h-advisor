import json
from datetime import date
from pathlib import Path

import pytest

from h2h_advisor.sources import espn
from h2h_advisor.sources.espn import (
    EspnError,
    EspnSource,
    parse_gamelog,
    pick_athlete,
    search_candidates,
)

FIXTURES = Path(__file__).parent / "fixtures" / "espn"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_gamelog_gives_latest_five_regular_season_games():
    recent = parse_gamelog(load("gamelog_reed.json"))
    assert recent.games == 5 and len(recent.last) == 5
    assert recent.last_game == date(2026, 11, 25)  # ESPN's game date, newest first
    assert recent.mpg == pytest.approx(sum(recent.last) / 5)
    assert 20 <= recent.mpg <= 30  # a rotation player


def test_gamelog_ignores_preseason_and_dnp():
    data = load("gamelog_reed.json")
    data["seasonTypes"].insert(0, {"displayName": "2026-27 Preseason", "categories": [
        {"displayName": "Preseason", "events": [{"eventId": "x", "stats": ["99"] + ["0"] * 13}]}]})
    data["events"]["x"] = {"gameDate": "2026-12-31T00:00:00.000+00:00"}
    first = data["seasonTypes"][1]["categories"][0]["events"]
    first.insert(0, {"eventId": first[0]["eventId"], "stats": ["--"] + first[0]["stats"][1:]})
    recent = parse_gamelog(data)
    assert all(m < 60 for m in recent.last)


def test_gamelog_with_no_games_is_none():
    data = load("gamelog_reed.json")
    for season in data["seasonTypes"]:
        season["categories"] = []
    assert parse_gamelog(data) is None


@pytest.mark.parametrize("broken", [{}, {"names": ["points"], "seasonTypes": []}, "html"])
def test_unexpected_gamelog_shape_is_an_error(broken):
    with pytest.raises(EspnError):
        parse_gamelog(broken)


def test_search_of_a_shared_name_picks_the_nba_player_by_team():
    candidates = search_candidates(load("search_paul_reed.json"))
    assert len([c for c in candidates if c[1] == "Paul Reed"]) > 1  # real data: several people
    assert pick_athlete(candidates, "Paul Reed", "DET") == "4278562"
    assert pick_athlete(candidates, "Paul Reed", "BOS") is None  # no Celtic of that name
    assert pick_athlete(candidates, "Somebody Else", "DET") is None


def test_ambiguous_name_is_resolved_by_team_or_left_alone():
    candidates = [("1", "Jaylin Williams", "Oklahoma City Thunder"), ("2", "Jaylin Williams", "Detroit Pistons")]
    assert pick_athlete(candidates, "Jaylin Williams", "DET") == "2"
    assert pick_athlete(candidates, "Jaylin Williams", "BOS") is None


def test_suffixes_and_accents_match():
    assert pick_athlete([("9", "Robert Williams III", "Portland Trail Blazers")], "Robert Williams", "POR") == "9"


def test_source_calls_search_then_gamelog():
    urls = []

    def fake(url):
        urls.append(url)
        return load("search_paul_reed.json") if "search" in url else load("gamelog_reed.json")

    source = EspnSource(fake)
    athlete = source.athlete_id("Paul Reed", "DET")
    assert athlete and source.recent_minutes(athlete).games == 5
    assert "query=Paul+Reed" in urls[0] and f"/athletes/{athlete}/gamelog" in urls[1]


def test_fetch_refuses_other_hosts():
    with pytest.raises(EspnError, match="only https"):
        espn.fetch_json("https://example.com/x")
    with pytest.raises(EspnError, match="only https"):
        espn.fetch_json("http://site.web.api.espn.com/x")
