import pytest

from h2h_advisor.analysis.build import (
    Build,
    at_risk_strong,
    build_from_config,
    category_weights,
    infer_build,
    matchup_result,
    one_win_targets,
    resolve_build,
)
from h2h_advisor.analysis.outlook import CatOutlook
from h2h_advisor.models import NINE_CAT, Category, Matchup, StatLine

C = Category


def outlook(cat, status):
    return CatOutlook(cat, 0.0, 0.0, 0.0, 0.0, 0.5, status)


def test_build_from_config_pinned_league():
    build = build_from_config(
        {
            "punt": ["FG_PCT", "TO", "BLK"],
            "weak": ["REB"],
            "strong": ["FT_PCT", "TPM", "PTS", "AST", "STL"],
        }
    )
    assert build == Build(
        strong=frozenset({C.FT_PCT, C.TPM, C.PTS, C.AST, C.STL}),
        punt=frozenset({C.FG_PCT, C.TO, C.BLK}),
        weak=frozenset({C.REB}),
        source="pinned",
    )
    with pytest.raises(ValueError, match="FG%"):
        build_from_config({"punt": ["FG%"]})


@pytest.mark.parametrize("raw", [None, {}])
def test_build_from_config_empty_is_none(raw):
    assert build_from_config(raw) is None


def test_build_from_config_unknown_key_rejected():
    with pytest.raises(ValueError, match="stong"):
        build_from_config({"stong": ["PTS"]})


def test_matchup_result_turnovers_inverted():
    m = Matchup(
        league_id="L",
        week=1,
        opponent_team_id="2",
        opponent_name="Opp",
        my_totals=StatLine(fgm=40, fga=100, pts=300, to=50, blk=10),
        opp_totals=StatLine(fgm=45, fga=100, pts=300, to=60, blk=5),
        moves_used=0,
    )
    result = matchup_result(m, (C.FG_PCT, C.PTS, C.TO, C.BLK))
    assert result == {C.FG_PCT: -1, C.PTS: 0, C.TO: 1, C.BLK: 1}


def test_infer_from_history():
    history = [{C.BLK: 1, C.FT_PCT: -1, C.PTS: 1} for _ in range(4)]
    roster = {c: 100.0 for c in NINE_CAT}
    build = infer_build(roster, history)
    assert C.BLK in build.strong and C.FT_PCT in build.punt
    assert build.source == "inferred" and build.weak == frozenset()


def test_infer_history_thresholds_with_ties():
    # Over 4 weeks: BLK (2 wins, 2 ties) = 0.75 strong; PTS 0.0 punt; AST 0.25 neutral;
    # STL (2 ties, 2 losses) = 0.25 neutral. A fifth loss drops STL to 1/5 = 0.2: punt.
    history = [
        {C.BLK: 1, C.PTS: -1, C.AST: 1, C.STL: 0},
        {C.BLK: 1, C.PTS: -1, C.AST: -1, C.STL: 0},
        {C.BLK: 0, C.PTS: -1, C.AST: -1, C.STL: -1},
        {C.BLK: 0, C.PTS: -1, C.AST: -1, C.STL: -1},
        {C.BLK: 0, C.PTS: -1, C.AST: -1, C.STL: -1},
    ]
    build = infer_build({}, history[:4])
    assert C.BLK in build.strong and C.PTS in build.punt
    assert C.AST not in build.strong | build.punt
    assert C.STL not in build.strong | build.punt
    assert C.STL in infer_build({}, history).punt


def test_infer_short_history_uses_roster_values():
    roster = dict(zip(NINE_CAT, (1.0, 9.0, 3.0, 8.0, 2.0, 7.0, 4.0, 6.0, 5.0)))
    build = infer_build(roster, [{c: -1 for c in NINE_CAT}])
    assert build.strong == frozenset({C.FT_PCT, C.PTS, C.AST})
    assert build.punt == frozenset()


def test_infer_roster_ties_break_by_nine_cat_order():
    build = infer_build({c: 1.0 for c in NINE_CAT}, [])
    assert build.strong == frozenset({C.FG_PCT, C.FT_PCT, C.TPM})


def test_infer_mixed_history_roster_fills_undecided_only():
    history = [{C.BLK: 1} for _ in range(3)]
    roster = {c: 1.0 for c in NINE_CAT} | {C.REB: 9.0}
    build = infer_build(roster, history)
    # BLK decided by history; the rest are undecided -> top 3 of all 9 by roster value
    assert C.BLK in build.strong
    assert C.REB in build.strong


def test_pinned_overrides_inferred():
    pinned = Build(punt=frozenset({C.BLK}), source="pinned")
    inferred = Build(strong=frozenset({C.BLK}), source="inferred")
    assert resolve_build(pinned, inferred) is pinned
    assert resolve_build(None, inferred) is inferred


def test_weights_zero_for_punt_and_safe_strong():
    build = Build(strong=frozenset({C.BLK}), punt=frozenset({C.FT_PCT}), source="pinned")
    safe = [outlook(C.BLK, "likely_win"), outlook(C.FT_PCT, "swing"), outlook(C.PTS, "likely_win")]
    w = category_weights(build, safe)
    assert w == {C.BLK: 0.0, C.FT_PCT: 0.0, C.PTS: 1.0}
    assert at_risk_strong(build, safe) == []

    risky = [outlook(C.BLK, "swing"), outlook(C.FT_PCT, "likely_loss")]
    assert category_weights(build, risky)[C.BLK] == 1.0
    assert at_risk_strong(build, risky) == [C.BLK]
    assert category_weights(build, [outlook(C.BLK, "locked_win")])[C.BLK] == 0.0


@pytest.mark.parametrize("status", ["swing", "likely_loss", "locked_loss", "tied"])
def test_at_risk_statuses(status):
    build = Build(strong=frozenset({C.BLK}))
    assert at_risk_strong(build, [outlook(C.BLK, status)]) == [C.BLK]


def _graded_outlook():
    """Nine categories with win_prob 0.9 down to 0.1 in NINE_CAT order."""
    return [
        CatOutlook(c, 0.0, 0.0, 0.0, 0.0, 0.9 - 0.1 * i, "swing") for i, c in enumerate(NINE_CAT)
    ]


def test_one_win_targets_five_best_non_punted():
    punt = frozenset({C.FG_PCT, C.TO, C.BLK})
    outlook_ = _graded_outlook()
    wp = {o.category: o.win_prob for o in outlook_}
    wp[C.TO] = 0.95  # a punted category is never preferred, however likely
    outlook_ = [CatOutlook(o.category, 0, 0, 0, 0, wp[o.category], "swing") for o in outlook_]
    targets = one_win_targets(Build(punt=punt), outlook_)
    expected = sorted((c for c in NINE_CAT if c not in punt), key=lambda c: -wp[c])[:5]
    assert targets == expected
    assert len(targets) == 5 and not set(targets) & punt


def test_one_win_targets_fill_with_best_punted():
    punt = frozenset(list(NINE_CAT)[:5])
    outlook_ = _graded_outlook()
    targets = one_win_targets(Build(punt=punt), outlook_)
    non_punted = list(NINE_CAT)[5:]
    assert targets == [*non_punted, NINE_CAT[0]]  # best punted by win_prob comes last


def test_one_win_targets_ties_follow_outlook_order():
    outlook_ = [CatOutlook(c, 0.0, 0.0, 0.0, 0.0, 0.5, "swing") for c in NINE_CAT]
    assert one_win_targets(Build(), outlook_) == list(NINE_CAT)[:5]
    assert one_win_targets(Build(), outlook_[:4]) == list(NINE_CAT)[:3]  # N=4 -> k=3
