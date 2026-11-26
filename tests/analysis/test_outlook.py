from dataclasses import replace
from datetime import date

import pytest

from h2h_advisor.analysis.outlook import (
    WeekContext,
    analyze_week,
    category_win_prob,
    matchup_win_prob,
    remaining_days,
)
from h2h_advisor.models import (
    NINE_CAT,
    Category,
    LeagueSettings,
    Matchup,
    Player,
    RosterEntry,
    StatLine,
)

ZERO = StatLine()
WEEK_START, WEEK_END = date(2026, 11, 23), date(2026, 11, 29)


def make_settings(**overrides):
    base = {
        "league_id": "L1",
        "name": "League",
        "categories": NINE_CAT,
        "roster_slots": {"C": 1, "Util": 1},
        "max_adds_per_week": 7,
        "week_start": WEEK_START,
        "week_end": WEEK_END,
        "current_week": 5,
        "lineup_effective": "tomorrow",
    }
    return LeagueSettings(**{**base, **overrides})


def make_ctx(
    *,
    my_totals=ZERO,
    opp_totals=ZERO,
    my_roster=(),
    opp_roster=(),
    per_game=None,
    schedule=None,
    today=date(2026, 11, 25),
    moves_used=0,
    **settings_overrides,
):
    return WeekContext(
        settings=make_settings(**settings_overrides),
        matchup=Matchup("L1", 5, "T2", "Opp", my_totals, opp_totals, moves_used),
        my_roster=list(my_roster),
        opp_roster=list(opp_roster),
        per_game=per_game or {},
        values={},
        ros_values={},
        schedule=schedule or {},
        today=today,
    )


def entry(key, team="AAA", slot="C"):
    return RosterEntry(Player(key, key, team, ("C",), None), slot)


def by_cat(analysis):
    return {o.category: o for o in analysis.categories}


def test_locked_when_no_games_left():
    ctx = make_ctx(
        my_totals=StatLine(pts=500),
        opp_totals=StatLine(pts=400),
        today=date(2026, 11, 30),
    )
    out = analyze_week(ctx)
    pts = by_cat(out)[Category.PTS]
    assert out.days_left == []
    assert pts.win_prob == 1.0
    assert pts.status == "locked_win"
    assert by_cat(out)[Category.REB].status == "tied"


def test_locked_loss():
    ctx = make_ctx(
        my_totals=StatLine(pts=300), opp_totals=StatLine(pts=400), today=date(2026, 11, 30)
    )
    pts = by_cat(analyze_week(ctx))[Category.PTS]
    assert pts.win_prob == 0.0
    assert pts.status == "locked_loss"


def test_even_margin_is_half():
    proj = StatLine(pts=100)
    assert category_win_prob(Category.PTS, proj, proj, 10) == pytest.approx(0.5, abs=1e-9)


def test_turnovers_inverted():
    assert category_win_prob(Category.TO, StatLine(to=50), StatLine(to=60), 10) > 0.5
    assert category_win_prob(Category.TO, StatLine(to=60), StatLine(to=50), 10) < 0.5


def test_pct_uses_attempts():
    # 0.522 vs 0.4125 with only 10 of my 900 attempts still to come: lead is safe
    mine, theirs = StatLine(fgm=470, fga=900), StatLine(fgm=330, fga=800)
    prob = category_win_prob(
        Category.FG_PCT, mine, theirs, 5, my_now=StatLine(fgm=465, fga=890), opp_now=theirs
    )
    assert prob > 0.95


def test_pct_more_remaining_attempts_is_less_certain():
    mine, theirs = StatLine(fgm=450, fga=900), StatLine(fgm=392, fga=800)  # 0.50 vs 0.49
    few = category_win_prob(
        Category.FG_PCT, mine, theirs, 5, my_now=StatLine(fgm=440, fga=880), opp_now=theirs
    )
    many = category_win_prob(
        Category.FG_PCT, mine, theirs, 5, my_now=StatLine(), opp_now=theirs
    )
    assert 0.5 < many < few < 1.0


def test_pct_no_remaining_attempts_is_deterministic():
    mine, theirs = StatLine(ftm=80, fta=100), StatLine(ftm=70, fta=100)
    assert category_win_prob(Category.FT_PCT, mine, theirs, 0, my_now=mine, opp_now=theirs) == 1.0
    assert category_win_prob(Category.FT_PCT, theirs, mine, 0, my_now=theirs, opp_now=mine) == 0.0
    assert category_win_prob(Category.FT_PCT, mine, mine, 0, my_now=mine, opp_now=mine) == 0.5


def test_remaining_days_follow_settings_week():
    ctx = make_ctx(
        week_start=date(2027, 2, 9), week_end=date(2027, 2, 22), today=date(2027, 2, 18)
    )
    days = remaining_days(ctx)
    assert len(days) == 5
    assert days[0] == date(2027, 2, 18)
    assert days[-1] == date(2027, 2, 22)


def test_remaining_days_start_at_week_start_when_today_earlier():
    ctx = make_ctx(today=date(2026, 11, 20))
    days = remaining_days(ctx)
    assert days[0] == WEEK_START
    assert days[-1] == WEEK_END
    assert len(days) == 7


def test_remaining_days_empty_after_week_end():
    assert remaining_days(make_ctx(today=date(2026, 11, 30))) == []


def test_adds_left_from_settings():
    out = analyze_week(make_ctx(max_adds_per_week=7, moves_used=3))
    assert out.adds_left == 4


def test_adds_left_never_negative():
    assert analyze_week(make_ctx(max_adds_per_week=2, moves_used=5)).adds_left == 0


def test_analyze_week_projects_and_counts_games():
    sched = {d: frozenset({"AAA", "BBB"}) for d in (date(2026, 11, 25), date(2026, 11, 26))}
    ctx = make_ctx(
        my_totals=StatLine(pts=100),
        opp_totals=StatLine(pts=100),
        my_roster=[entry("a", "AAA")],
        opp_roster=[entry("b", "BBB")],
        per_game={"a": StatLine(pts=30), "b": StatLine(pts=10)},
        schedule=sched,
        today=date(2026, 11, 25),
    )
    out = analyze_week(ctx)
    assert out.my_proj.pts == 160
    assert out.opp_proj.pts == 120
    pts = by_cat(out)[Category.PTS]
    assert pts.my_now == 100 and pts.opp_proj == 120
    assert pts.status in {"swing", "likely_win"}
    assert pts.win_prob > 0.5
    assert out.expected_cats_won == pytest.approx(sum(o.win_prob for o in out.categories))


def test_active_days_apply_to_both_sides():
    d1, d2 = date(2026, 11, 25), date(2026, 11, 26)
    sched = {d: frozenset({"AAA", "BBB"}) for d in (d1, d2)}
    ctx = replace(
        make_ctx(
            my_roster=[entry("a", "AAA")],
            opp_roster=[entry("b", "BBB")],
            per_game={"a": StatLine(pts=30), "b": StatLine(pts=10)},
            schedule=sched,
            today=d1,
        ),
        active_days={"a": {d1}, "b": {d2}},  # each rests one of the two days
    )
    out = analyze_week(ctx)
    assert out.my_proj.pts == 30
    assert out.opp_proj.pts == 10
    # opponent's resting player is not a remaining player-game either
    assert by_cat(out)[Category.PTS].status != "locked_win"


def test_only_league_categories_in_order():
    cats = (Category.PTS, Category.TO)
    out = analyze_week(make_ctx(categories=cats))
    assert [o.category for o in out.categories] == list(cats)
    assert out.expected_cats_won == pytest.approx(1.0)  # two tied locked-free cats? see below


@pytest.mark.parametrize(
    ("probs", "expected"),
    [
        ([1.0] * 5 + [0.0] * 4, 1.0),
        ([0.5] * 9, 0.5),
        ([0.5] * 2, 0.5),  # P(2-0)=.25 + .5 * P(1-1)=.25
        ([0.6, 0.6], 0.36 + 0.5 * 0.48),
        ([], 0.5),
        ([0.9] * 3, 0.972),  # P(W >= 2)
        ([1.5, -0.5, 1.0], 1.0),  # out-of-range inputs are clamped
    ],
)
def test_matchup_win_prob(probs, expected):
    assert matchup_win_prob(probs) == pytest.approx(expected)


@pytest.mark.parametrize("scoring_type", ["categories", "one_win"])
def test_analyze_week_reports_matchup_win_prob_and_objective(scoring_type):
    sched = {date(2026, 11, 25): frozenset({"AAA", "BBB"})}
    ctx = make_ctx(
        my_totals=StatLine(pts=100, reb=30, fgm=40, fga=90),
        opp_totals=StatLine(pts=100, reb=28, fgm=38, fga=90),
        my_roster=[entry("a", "AAA")],
        opp_roster=[entry("b", "BBB")],
        per_game={"a": StatLine(pts=30, reb=5), "b": StatLine(pts=10, reb=9)},
        schedule=sched,
        scoring_type=scoring_type,
    )
    out = analyze_week(ctx)
    assert out.matchup_win_prob == pytest.approx(matchup_win_prob([o.win_prob for o in out.categories]))
    assert out.matchup_win_prob != pytest.approx(0.5)
    assert out.objective == scoring_type
    assert out.expected_cats_won == pytest.approx(sum(o.win_prob for o in out.categories))
