from datetime import date

import pytest

from h2h_advisor.analysis.categories import category_value
from h2h_advisor.analysis.projection import (
    DayPlayer,
    assign_active,
    lineup_by_day,
    project_week,
)
from h2h_advisor.models import Category, Player, RosterEntry, StatLine

D1, D2, D3 = date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25)


def entry(key, positions, slot="BN", team="AAA", status=None):
    return RosterEntry(Player(key, key, team, tuple(positions), status), slot)


def schedule_for(*days, teams=("AAA",)):
    return {d: frozenset(teams) for d in days}


def test_adds_remaining_games_to_current():
    roster = [entry("a", ["C"], "C")]
    per_game = {"a": StatLine(pts=20)}
    out = project_week(
        StatLine(pts=50), roster, per_game, {"a": 1.0},
        schedule_for(D1, D2), [D1, D2], {"C": 1},
    )
    assert out.pts == 90


def test_slot_cap_keeps_best():
    roster = [entry(k, ["C"]) for k in "abc"]
    per_game = {k: StatLine(pts=10) for k in "abc"}
    values = {"a": 1.0, "b": 3.0, "c": 2.0}
    out = project_week(
        StatLine(), roster, per_game, values,
        schedule_for(D1), [D1], {"C": 1, "Util": 1},
    )
    assert out.pts == 20
    assert lineup_by_day(
        roster, per_game, values, schedule_for(D1), [D1], {"C": 1, "Util": 1}
    ) == {D1: {"b": "C", "c": "Util"}}


def test_il_and_out_players_contribute_zero():
    roster = [entry("a", ["C"], "IL+"), entry("b", ["C"], "C", status="O")]
    per_game = {"a": StatLine(pts=20), "b": StatLine(pts=20)}
    out = project_week(
        StatLine(pts=5), roster, per_game, {"a": 1.0, "b": 1.0},
        schedule_for(D1), [D1], {"C": 1, "Util": 1},
    )
    assert out == StatLine(pts=5)


def test_guard_fills_g_slot():
    roster = [entry("a", ["PG"]), entry("b", ["PG"])]
    per_game = {"a": StatLine(pts=10), "b": StatLine(pts=10)}
    out = project_week(
        StatLine(), roster, per_game, {"a": 2.0, "b": 1.0},
        schedule_for(D1), [D1], {"PG": 1, "G": 1},
    )
    assert out.pts == 20


def test_percentages_stay_aggregated():
    roster = [entry("a", ["C"]), entry("b", ["PG"])]
    per_game = {
        "a": StatLine(fgm=9, fga=10),
        "b": StatLine(fgm=1, fga=10),
    }
    out = project_week(
        StatLine(fgm=10, fga=20), roster, per_game, {"a": 2.0, "b": 1.0},
        schedule_for(D1), [D1], {"C": 1, "PG": 1},
    )
    assert out.fgm == 20 and out.fga == 40
    assert category_value(out, Category.FG_PCT) == pytest.approx(0.5)


def test_active_days_restricts_player():
    roster = [entry("a", ["C"])]
    out = project_week(
        StatLine(), roster, {"a": StatLine(pts=10)}, {"a": 1.0},
        schedule_for(D1, D2), [D1, D2], {"C": 1},
        active_days={"a": {D2}},
    )
    assert out.pts == 10


def test_lineup_by_day_reports_slots():
    roster = [
        entry("c1", ["C"]), entry("c2", ["C"]), entry("c3", ["C"]), entry("pg", ["PG"]),
    ]
    per_game = {e.player.key: StatLine(pts=1) for e in roster}
    values = {"c1": 4.0, "c2": 3.0, "c3": 2.0, "pg": 1.0}
    lineup = lineup_by_day(
        roster, per_game, values, schedule_for(D1), [D1], {"PG": 1, "C": 1, "Util": 1}
    )
    assert lineup == {D1: {"c1": "C", "c2": "Util", "pg": "PG"}}
    assert "c3" not in lineup[D1]


def test_lineup_by_day_covers_only_requested_days_with_empty_gaps():
    roster = [entry("a", ["C"])]
    lineup = lineup_by_day(
        roster, {"a": StatLine(pts=1)}, {"a": 1.0},
        {D1: frozenset({"AAA"}), D2: frozenset({"BBB"}), D3: frozenset({"AAA"})},
        [D1, D2], {"C": 1},
    )
    assert lineup == {D1: {"a": "C"}, D2: {}}


def test_players_without_projection_are_skipped_and_missing_value_is_zero():
    roster = [entry("a", ["C"]), entry("b", ["C"])]
    # "a" has no projection: must not take the only C slot; "b" has no value entry
    out = project_week(
        StatLine(), roster, {"b": StatLine(pts=7)}, {},
        schedule_for(D1), [D1], {"C": 1},
    )
    assert out.pts == 7


def test_player_without_game_takes_no_slot():
    roster = [entry("a", ["C"], team="AAA"), entry("b", ["C"], team="BBB")]
    per_game = {"a": StatLine(pts=1), "b": StatLine(pts=5)}
    lineup = lineup_by_day(
        roster, per_game, {"a": 9.0, "b": 1.0},
        {D1: frozenset({"BBB"})}, [D1], {"C": 1},
    )
    assert lineup == {D1: {"b": "C"}}


def test_assign_active_eligibility_and_order():
    def dp(key, pos, value):
        return DayPlayer(key, tuple(pos), StatLine(), value)

    players = [
        dp("sf", ["SF"], 5), dp("pf", ["PF"], 4), dp("sg", ["SG"], 3),
        dp("c", ["C"], 2), dp("pg", ["PG"], 1),
    ]
    got = assign_active(players, {"G": 1, "F": 1, "Util": 1, "BN": 3, "IL": 1})
    # sf->F, pf->Util (F taken), sg->G, c and pg find no open slot
    assert [p.key for p in got] == ["sf", "pf", "sg"]


def test_assign_active_ties_break_by_key_and_ignores_bench_slots():
    def dp(key, pos, value):
        return DayPlayer(key, tuple(pos), StatLine(), value)

    got = assign_active([dp("b", ["C"], 1), dp("a", ["C"], 1)], {"C": 1})
    assert [p.key for p in got] == ["a"]
    assert assign_active([dp("x", ["C"], 1)], {"BN": 3}) == []


def test_multi_position_player_uses_positions_in_listed_order():
    roster = [entry("x", ["SF", "PG"])]
    per_game = {"x": StatLine(pts=1)}
    sched = schedule_for(D1)
    both = {"PG": 1, "SF": 1}
    assert lineup_by_day(roster, per_game, {"x": 1.0}, sched, [D1], both) == {D1: {"x": "SF"}}
    # no exact slot open: falls back to G (PG-eligible) before Util
    assert lineup_by_day(
        roster, per_game, {"x": 1.0}, sched, [D1], {"G": 1, "Util": 1}
    ) == {D1: {"x": "G"}}
