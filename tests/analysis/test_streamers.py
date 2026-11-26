from dataclasses import replace
from datetime import date, timedelta

import pytest

from h2h_advisor.analysis import streamers
from h2h_advisor.analysis.build import Build
from h2h_advisor.analysis.outlook import (
    SD_PER_PLAYER_GAME,
    TYPICAL_PER_GAME,
    WeekContext,
    analyze_week,
    counting_variance,
    matchup_win_prob,
)
from h2h_advisor.analysis.projection import projected_rest
from h2h_advisor.analysis.streamers import (
    MIN_GAIN,
    MIN_WIN_PROB_GAIN,
    _swap,
    activate_il,
    drop_pool,
    effective_start,
    long_term_upgrades,
    plan_adds,
    rank_streamers,
    swap_context,
)
from h2h_advisor.models import (
    NINE_CAT,
    Category,
    FreeAgent,
    LeagueSettings,
    Matchup,
    Player,
    RosterEntry,
    StatLine,
)

C = Category
ZERO = StatLine()
WEEK_START, WEEK_END = date(2026, 11, 23), date(2026, 11, 29)
TODAY = date(2026, 11, 25)
WEEK_DAYS = [TODAY + timedelta(days=i) for i in range(5)]  # Wed..Sun
AVG = StatLine(fgm=4, fga=9, ftm=2, fta=2.5, tpm=1, pts=11, reb=4, ast=2, stl=0.8, blk=0.5, to=1.2)
NO_BUILD = Build()


def day(offset: int) -> date:
    return TODAY + timedelta(days=offset)


def player(key, team, positions=("Util",), name=None, status=None):
    return Player(key, name or key, team, tuple(positions), status)


def entry(key, team, positions=("PG",), slot="BN", name=None):
    return RosterEntry(player(key, team, positions, name), slot)


def fa(key, team, positions=("PG",), on_waivers=False, waiver_clear=None, status=None):
    return FreeAgent(player(key, team, positions, status=status), on_waivers, waiver_clear)


def make_schedule(team_days: dict[str, list[date]], filler: dict[date, int] | None = None):
    """Team tricode -> days it plays; `filler` pads a day to that many teams playing."""
    schedule: dict[date, set[str]] = {d: set() for d in WEEK_DAYS}
    for team, days in team_days.items():
        for d in days:
            schedule.setdefault(d, set()).add(team)
    for d, n_teams in (filler or {}).items():
        i = 0
        while len(schedule[d]) < n_teams:
            schedule[d].add(f"Z{i:02d}")
            i += 1
    return {d: frozenset(teams) for d, teams in schedule.items()}


def make_ctx(
    *,
    my_roster,
    per_game,
    values=None,
    ros_values=None,
    schedule,
    opp_roster=(),
    my_totals=ZERO,
    opp_totals=ZERO,
    moves_used=0,
    roster_slots=None,
    lineup_effective="today",
    active_days=None,
    scoring_type="categories",
):
    settings = LeagueSettings(
        league_id="L1",
        name="League",
        categories=NINE_CAT,
        roster_slots=roster_slots or {"PG": 1, "SG": 1, "G": 1, "Util": 2, "BN": 3, "IL": 1},
        max_adds_per_week=7,
        week_start=WEEK_START,
        week_end=WEEK_END,
        current_week=5,
        lineup_effective=lineup_effective,
        scoring_type=scoring_type,
    )
    return WeekContext(
        settings=settings,
        matchup=Matchup("L1", 5, "T2", "Opp", my_totals, opp_totals, moves_used),
        my_roster=list(my_roster),
        opp_roster=list(opp_roster),
        per_game=per_game,
        values=values or {},
        ros_values=ros_values or {},
        schedule=schedule,
        today=TODAY,
        active_days=active_days,
    )


def streaming_ctx(fa_days: dict[str, list[date]], *, fa_per_game=None, fa_values=None, **kwargs):
    """My M1 vs opponent O1 (both AVG, every day), plus droppable D who never plays.

    Free agents `fa_days` key -> days; each FA plays for team key.upper()."""
    team_days = {"MMM": WEEK_DAYS, "OOO": WEEK_DAYS, "DDD": []}
    team_days |= {k.upper(): d for k, d in fa_days.items()}
    per_game = {"M1": AVG, "O1": AVG, "D": AVG}
    per_game |= {k: (fa_per_game or {}).get(k, AVG) for k in fa_days}
    values = {"M1": 10.0, "O1": 10.0, "D": 0.0} | {k: 1.0 for k in fa_days} | (fa_values or {})
    ctx = make_ctx(
        my_roster=[entry("M1", "MMM"), entry("D", "DDD")],
        opp_roster=[entry("O1", "OOO")],
        per_game=per_game,
        values=values,
        schedule=make_schedule(team_days, kwargs.pop("filler", None)),
        **kwargs,
    )
    free_agents = [fa(k, k.upper()) for k in fa_days]
    pool = [ctx.my_roster[1]]
    return ctx, free_agents, pool


# --- drop pool ---------------------------------------------------------------


def test_never_drop_excluded():
    roster = [
        entry("wemby", "SAS", name="Victor Wembanyama"),
        entry("a", "AAA", name="Alpha"),
        entry("b", "BBB", name="Beta"),
    ]
    ros = {"wemby": -5.0, "a": 1.0, "b": 9.0}
    pool = drop_pool(roster, ros, [fa("f", "FFF")], {"Victor Wembanyama"}, 0.5, None)
    assert [e.player.key for e in pool] == ["a"]
    pool = drop_pool(roster, ros, [fa("f", "FFF")], {"VICTOR WEMBANYAMA"}, 0.5, stream_slots=3)
    assert "wemby" not in [e.player.key for e in pool]


def test_pool_size_adapts():
    fas = [fa("f1", "FFF"), fa("f2", "GGG"), fa("w", "WWW", on_waivers=True)]
    ros_fa = {"f1": 2.0, "f2": 1.0, "w": 50.0}  # waiver player does not set the bar
    deep = [entry(f"p{i}", "AAA") for i in range(8)]
    deep_ros = {f"p{i}": 20.0 + i for i in range(8)} | ros_fa
    assert [e.player.key for e in drop_pool(deep, deep_ros, fas, set(), 0.5, None)] == ["p0"]

    churn_ros = {f"p{i}": 2.0 + 0.1 * i for i in range(5)} | {f"p{i}": 20.0 for i in range(5, 8)} | ros_fa
    pool = drop_pool(deep, churn_ros, fas, set(), 0.5, None)
    assert [e.player.key for e in pool] == ["p0", "p1", "p2"]  # capped at MAX_POOL


def test_missing_ros_value_is_droppable():
    roster = [entry("a", "AAA"), entry("unknown", "BBB")]
    pool = drop_pool(roster, {"a": 30.0, "f": 1.0}, [fa("f", "FFF")], set(), 0.5, None)
    assert [e.player.key for e in pool] == ["unknown"]


def test_stream_slots_override():
    roster = [entry(f"p{i}", "AAA") for i in range(6)]
    ros = {f"p{i}": 30.0 - i for i in range(6)}
    pool = drop_pool(roster, ros, [fa("f", "FFF")], set(), 0.5, stream_slots=3)
    assert [e.player.key for e in pool] == ["p5", "p4", "p3"]


def test_injured_player_is_never_in_the_pool():
    roster = [entry("star", "AAA", slot="C"), entry("ok", "BBB")]
    roster[0] = RosterEntry(replace(roster[0].player, status="O"), "C")
    pool = drop_pool(roster, {"star": 0.25, "ok": 9.0, "f": 1.0}, [fa("f", "FFF")], set(), 0.5, None)
    assert "star" not in [e.player.key for e in pool]


def test_kept_rotation_player_is_not_in_the_pool():
    roster = [entry("rot", "AAA"), entry("bench", "BBB")]
    ros = {"rot": -1.0, "bench": -0.5, "f": 1.0}
    pool = drop_pool(roster, ros, [fa("f", "FFF")], set(), 5.0, None, keep=frozenset({"rot"}))
    assert [e.player.key for e in pool] == ["bench"]


def test_drop_first_player_leads_the_pool_whatever_his_value_or_minutes():
    # The user said "drop him first": above replacement level and a kept rotation
    # player by minutes, he is still offered, ahead of the usual throwaways.
    roster = [entry("cheap", "AAA"), entry("wolf", "BBB"), entry("good", "CCC")]
    ros = {"cheap": -1.0, "wolf": 5.0, "good": 9.0, "f": 0.0}
    pool = drop_pool(
        roster, ros, [fa("f", "FFF")], set(), 0.5, None,
        keep=frozenset({"wolf"}), drop_first=frozenset({"wolf"}),
    )
    assert [e.player.key for e in pool] == ["wolf", "cheap"]
    slots = drop_pool(roster, ros, [fa("f", "FFF")], set(), 0.5, stream_slots=1, drop_first=frozenset({"wolf"}))
    assert [e.player.key for e in slots] == ["wolf"]


def test_drop_first_never_offers_an_il_slot():
    roster = [entry("hurt", "AAA", slot="IL"), entry("ok", "BBB")]
    pool = drop_pool(roster, {"hurt": -9.0, "ok": -1.0}, [], set(), 0.5, None, drop_first=frozenset({"hurt"}))
    assert [e.player.key for e in pool] == ["ok"]


def test_il_players_not_in_pool():
    roster = [entry("hurt", "AAA", slot="IL"), entry("hurt2", "AAA", slot="IL+"), entry("ok", "BBB")]
    ros = {"hurt": -10.0, "hurt2": -9.0, "ok": 30.0}
    assert [e.player.key for e in drop_pool(roster, ros, [], set(), 0.5, None)] == ["ok"]
    assert [e.player.key for e in drop_pool(roster, ros, [], set(), 0.5, stream_slots=2)] == ["ok"]


# --- effective start / swap simulation ----------------------------------------


def test_effective_start():
    ctx, _, _ = streaming_ctx({})
    assert effective_start(ctx.settings, TODAY) == TODAY
    ctx, _, _ = streaming_ctx({}, lineup_effective="tomorrow")
    assert effective_start(ctx.settings, TODAY) == day(1)


def test_active_days_affect_my_side():
    """A dropped player keeps games before effective_start and loses those after it."""
    team_days = {"MMM": WEEK_DAYS, "OOO": WEEK_DAYS, "LATE": [day(2), day(3)], "NOW": [day(0)]}
    per_game = {"M1": AVG, "O1": AVG, "late": StatLine(pts=30, reb=10), "now": StatLine(pts=30, reb=10)}
    ctx = make_ctx(
        my_roster=[entry("M1", "MMM"), entry("late", "LATE"), entry("now", "NOW")],
        opp_roster=[entry("O1", "OOO")],
        per_game=per_game,
        values={"M1": 10.0, "late": 5.0, "now": 5.0},
        schedule=make_schedule(team_days),
        lineup_effective="tomorrow",
    )
    nobody = player("ghost", "GHOST")  # an add with no games
    base = analyze_week(ctx)

    dropped_late = analyze_week(swap_context(ctx, ctx.my_roster[1], nobody, set()))
    assert dropped_late.my_proj.pts == base.my_proj.pts - 60
    assert dropped_late.my_proj.reb == base.my_proj.reb - 20

    dropped_now = analyze_week(swap_context(ctx, ctx.my_roster[2], nobody, set()))
    assert dropped_now.my_proj == base.my_proj  # today's game still counts (lineup locks tomorrow)


def test_swap_limits_add_to_his_projected_days():
    """An add with known active days plays only on window days he is projected to play."""
    ctx, _, _ = streaming_ctx({"fa": WEEK_DAYS})
    ctx = replace(ctx, active_days={"fa": {day(1)}})
    swapped = swap_context(ctx, ctx.my_roster[1], player("fa", "FA"), set(WEEK_DAYS))
    assert swapped.active_days["fa"] == {day(1)}
    unknown = swap_context(replace(ctx, active_days=None), ctx.my_roster[1], player("fa", "FA"), {day(0), day(2)})
    assert unknown.active_days["fa"] == {day(0), day(2)}


def test_free_agent_projected_idle_every_day_is_not_ranked():
    ctx, free_agents, pool = streaming_ctx({"idle": WEEK_DAYS})
    ctx = replace(ctx, active_days={"idle": set()})
    options, reason = rank_streamers(ctx, free_agents, pool, "rest_of_week", NO_BUILD)
    assert options == [] and reason == "no eligible free agent"


# --- streamers ----------------------------------------------------------------


def test_no_adds_left_returns_reason():
    ctx, fas, pool = streaming_ctx({"a": WEEK_DAYS}, moves_used=7)
    assert rank_streamers(ctx, fas, pool, "rest_of_week", NO_BUILD) == ([], "no adds left this week")


def test_empty_pool_and_no_eligible_reasons():
    ctx, fas, pool = streaming_ctx({"a": WEEK_DAYS})
    assert rank_streamers(ctx, fas, [], "rest_of_week", NO_BUILD) == ([], "drop pool is empty")
    assert rank_streamers(ctx, [], pool, "rest_of_week", NO_BUILD) == ([], "no eligible free agent")


def test_more_games_wins_when_cats_are_close():
    ctx, fas, pool = streaming_ctx({"one": [day(4)], "three": [day(0), day(1), day(2)]})
    options, reason = rank_streamers(ctx, fas, pool, "rest_of_week", NO_BUILD)
    assert reason is None
    assert [o.add.key for o in options] == ["three", "one"]
    top = options[0]
    assert top.drop.key == "D"
    assert top.raw_games == 3
    assert top.start_days == [day(0), day(1), day(2)]
    assert top.adds_left_after == 6
    assert top.delta_expected_cats > options[1].delta_expected_cats > 0
    assert top.per_cat_delta[C.PTS] > 0


def test_out_status_free_agent_excluded():
    ctx, _, pool = streaming_ctx({"hurt": WEEK_DAYS})
    fas = [fa("hurt", "HURT", status="O")]
    assert rank_streamers(ctx, fas, pool, "rest_of_week", NO_BUILD) == ([], "no eligible free agent")


def test_tomorrow_lineup_shifts_start():
    ctx, fas, pool = streaming_ctx(
        {"todayonly": [day(0)], "tmrw": [day(1)]}, lineup_effective="tomorrow"
    )
    options, _ = rank_streamers(ctx, fas, pool, "today", NO_BUILD)
    assert [o.add.key for o in options] == ["tmrw"]
    assert options[0].start_days == [day(1)]
    assert options[0].usable_days == [day(1)]


def test_today_rolls_past_a_day_without_nba_games():
    # Thanksgiving: no games today, so "today" means the first day an add can play.
    ctx, fas, pool = streaming_ctx({"tmrw": [day(1)], "later": [day(2)]})
    ctx = replace(ctx, schedule={**ctx.schedule, day(0): frozenset()})
    options, reason = rank_streamers(ctx, fas, pool, "today", NO_BUILD)
    assert reason is None
    assert [o.add.key for o in options] == ["tmrw"]
    assert options[0].start_days == [day(1)]


def test_today_does_not_roll_past_a_day_without_a_schedule():
    # A missing day page is unknown, not a day without games.
    ctx, fas, pool = streaming_ctx({"tmrw": [day(1)]})
    schedule = {d: teams for d, teams in ctx.schedule.items() if d != day(0)}
    ctx = replace(ctx, schedule=schedule)
    assert rank_streamers(ctx, fas, pool, "today", NO_BUILD) == (
        [],
        "no eligible free agent",
    )


def test_waiver_player_excluded_today():
    ctx, _, pool = streaming_ctx({"w": WEEK_DAYS, "f": [day(0)]})
    waiver = fa("w", "W", on_waivers=True, waiver_clear=day(2))
    free = fa("f", "F")
    options, _ = rank_streamers(ctx, [waiver, free], pool, "today", NO_BUILD)
    assert [o.add.key for o in options] == ["f"]

    no_clear = fa("w", "W", on_waivers=True)
    options, _ = rank_streamers(ctx, [no_clear], pool, "rest_of_week", NO_BUILD)
    assert options == []


def test_waiver_claim_keeps_drop_until_it_clears():
    """The drop keeps playing until the claim processes; the add starts exactly at waiver_clear."""
    star = StatLine(fgm=8, fga=16, ftm=4, fta=5, tpm=3, pts=25, reb=8, ast=5, stl=1.5, blk=1, to=2)
    ctx = make_ctx(
        my_roster=[entry("M1", "MMM"), entry("D", "DDD")],
        opp_roster=[entry("O1", "OOO"), entry("O2", "OOO")],
        per_game={"M1": AVG, "D": AVG, "O1": AVG, "O2": AVG, "w": star},
        values={"M1": 10.0, "D": 1.0, "w": 5.0},
        schedule=make_schedule({"MMM": WEEK_DAYS, "OOO": WEEK_DAYS, "DDD": WEEK_DAYS, "W": WEEK_DAYS}),
    )
    drop = ctx.my_roster[1]
    waiver = fa("w", "W", on_waivers=True, waiver_clear=day(2))
    options, _ = rank_streamers(ctx, [waiver], [drop], "rest_of_week", NO_BUILD)
    (option,) = options
    assert option.start_days == [day(2), day(3), day(4)]
    assert option.usable_days == [day(2), day(3), day(4)]

    base = analyze_week(ctx)
    expected = analyze_week(
        replace(
            ctx,
            my_roster=[*ctx.my_roster, RosterEntry(waiver.player, "BN")],
            active_days={"D": {day(0), day(1)}, "w": {day(2), day(3), day(4)}},
        )
    )
    assert expected.my_proj.pts == base.my_proj.pts + 3 * (25 - 11)  # D's first two games kept
    for got, exp, b in zip(option.per_cat_delta.items(), expected.categories, base.categories, strict=True):
        assert got[1] == pytest.approx(exp.win_prob - b.win_prob)


def test_follow_up_attached_when_adds_remain():
    ctx, fas, pool = streaming_ctx(
        {"early": [day(0), day(1), day(2)], "late": [day(3), day(4)]},
        fa_values={"early": 2.0, "late": 1.0},
    )
    options, _ = rank_streamers(ctx, fas, pool, "rest_of_week", NO_BUILD)
    top = options[0]
    assert top.add.key == "early"
    fu = top.follow_up
    assert fu is not None
    assert (fu.add.key, fu.drop.key) == ("late", "early")
    assert fu.usable_days == [day(3), day(4)]
    assert fu.adds_left_after == top.adds_left_after - 1
    assert fu.delta_expected_cats > 0
    assert fu.follow_up is None


def test_follow_up_counts_in_ranking():
    """Two early games plus a three-game follow-up beat three late games with no room after."""
    ctx, fas, pool = streaming_ctx({"early": [day(0), day(1)], "late": [day(2), day(3), day(4)]})
    options, _ = rank_streamers(ctx, fas, pool, "rest_of_week", NO_BUILD)
    by_add = {o.add.key: o for o in options}
    assert by_add["late"].delta_expected_cats > by_add["early"].delta_expected_cats
    assert by_add["late"].follow_up is None
    assert by_add["early"].follow_up.add.key == "late"
    assert [o.add.key for o in options] == ["early", "late"]


def test_follow_up_candidates_need_late_games():
    """Higher-valued FAs with no game after the first add's last day don't crowd out a late one."""
    early = [day(0)]
    fa_days = {f"e{i}": early for i in range(5)} | {"a": [day(0), day(1)], "late": [day(3), day(4)]}
    fa_values = {f"e{i}": 5.0 + i for i in range(5)} | {"a": 10.0, "late": 0.5}
    ctx, fas, pool = streaming_ctx(fa_days, fa_values=fa_values)
    options, _ = rank_streamers(ctx, fas, pool, "rest_of_week", NO_BUILD)
    a_option = next(o for o in options if o.add.key == "a")
    assert a_option.follow_up is not None and a_option.follow_up.add.key == "late"


def test_follow_ups_explored_for_distinct_adds():
    """Five drops of the same add must not use up the five follow-up explorations."""
    fa_days = {"a": [day(0), day(1), day(2)], "b": [day(0), day(1)], "late": [day(3), day(4)]}
    ctx, fas, _ = streaming_ctx(fa_days, fa_values={"a": 3.0, "b": 2.0, "late": 1.0})
    drops = [entry(f"D{i}", "DDD") for i in range(5)]
    ctx = replace(ctx, my_roster=[ctx.my_roster[0], *drops])
    options, _ = rank_streamers(ctx, fas, drops, "rest_of_week", NO_BUILD, max_results=50)
    b_best = next(o for o in options if o.add.key == "b")
    assert b_best.follow_up is not None and b_best.follow_up.add.key == "late"


def test_ros_cost_flag_marks_solid_drop():
    ros = {"a": 1.0, "D": 30.0}
    ctx, fas, pool = streaming_ctx({"a": [day(0)]}, ros_values=ros)
    (option,) = rank_streamers(ctx, fas, pool, "rest_of_week", NO_BUILD)[0]
    assert (option.drop_ros_value, option.add_ros_value, option.ros_cost_flag) == (30.0, 1.0, True)

    ctx, fas, pool = streaming_ctx({"a": [day(0)]}, ros_values={"a": 1.0, "D": 1.2})
    (option,) = rank_streamers(ctx, fas, pool, "rest_of_week", NO_BUILD)[0]
    assert option.ros_cost_flag is False
    (option,) = rank_streamers(ctx, fas, pool, "rest_of_week", NO_BUILD, margin=0.1)[0]
    assert option.ros_cost_flag is True

    ctx, fas, pool = streaming_ctx({"a": [day(0)]})
    (option,) = rank_streamers(ctx, fas, pool, "rest_of_week", NO_BUILD)[0]
    assert (option.drop_ros_value, option.add_ros_value, option.ros_cost_flag) == (None, None, False)


def test_no_follow_up_today_or_without_adds():
    ctx, fas, pool = streaming_ctx({"early": [day(0)], "late": [day(3)]})
    options, _ = rank_streamers(ctx, fas, pool, "today", NO_BUILD)
    assert all(o.follow_up is None for o in options)
    ctx, fas, pool = streaming_ctx({"early": [day(0)], "late": [day(3)]}, moves_used=6)
    options, _ = rank_streamers(ctx, fas, pool, "rest_of_week", NO_BUILD)
    assert all(o.adds_left_after == 0 and o.follow_up is None for o in options)


def _bigs_ctx(fa_specs, team_days_extra=None, filler=None, roster=None, slots=None):
    team_days = {"CCC": WEEK_DAYS, "PPP": WEEK_DAYS, "OOO": WEEK_DAYS, "DDD": []}
    team_days |= {spec[0].upper(): spec[2] for spec in fa_specs}
    team_days |= team_days_extra or {}
    roster = roster or [
        entry("C1", "CCC", ("C",)),
        entry("PF1", "PPP", ("PF",)),
        entry("D", "DDD", ("C",)),
    ]
    per_game = {e.player.key: AVG for e in roster} | {"O1": AVG} | {s[0]: AVG for s in fa_specs}
    values = {e.player.key: 10.0 for e in roster} | {"D": 0.0} | {s[0]: 1.0 for s in fa_specs}
    ctx = make_ctx(
        my_roster=roster,
        opp_roster=[entry("O1", "OOO")],
        per_game=per_game,
        values=values,
        schedule=make_schedule(team_days, filler),
        roster_slots=slots or {"PG": 1, "G": 1, "PF": 1, "C": 1, "BN": 3},
    )
    fas = [fa(key, key.upper(), positions) for key, positions, _ in fa_specs]
    pool = [e for e in roster if e.player.key == "D"]
    return ctx, fas, pool


def test_position_fit_excludes_unusable_big():
    ctx, fas, pool = _bigs_ctx([("big", ("C",), WEEK_DAYS), ("guard", ("PG",), WEEK_DAYS)])
    options, _ = rank_streamers(ctx, fas, pool, "rest_of_week", NO_BUILD)
    assert [o.add.key for o in options] == ["guard"]
    assert set(options[0].slot_by_day.values()) <= {"PG", "G"}
    assert sorted(options[0].slot_by_day) == options[0].usable_days == WEEK_DAYS


def test_usable_vs_raw_games():
    roster = [entry("G1", "GGG", ("PG",)), entry("G2", "HHH", ("SG",)), entry("D", "DDD", ("C",))]
    ctx, fas, pool = _bigs_ctx(
        [("pg", ("PG",), [day(0), day(1), day(2), day(3)])],
        team_days_extra={"GGG": [day(0), day(1)], "HHH": [day(0), day(1)]},
        roster=roster,
        slots={"PG": 1, "G": 1, "BN": 3},
    )
    options, _ = rank_streamers(ctx, fas, pool, "rest_of_week", NO_BUILD)
    assert len(options) == 1
    assert options[0].raw_games == 4
    assert options[0].usable_days == [day(2), day(3)]


def test_light_days_flagged():
    ctx, fas, pool = _bigs_ctx(
        [("pg", ("PG",), [day(1), day(2)])],
        filler={day(1): 6, day(2): 20},  # 3 games vs 10 games
    )
    options, _ = rank_streamers(ctx, fas, pool, "rest_of_week", NO_BUILD)
    assert options[0].usable_days == [day(1), day(2)]
    assert options[0].light_days == [day(1)]
    options, _ = rank_streamers(ctx, fas, pool, "rest_of_week", NO_BUILD, light_day_max_games=10)
    assert options[0].light_days == [day(1), day(2)]


def test_punted_category_gives_no_credit():
    days = [day(0), day(1)]
    ctx, fas, pool = streaming_ctx(
        {"blk": days, "ast": days},
        fa_per_game={"blk": StatLine(blk=2.0), "ast": StatLine(ast=2.0)},
        fa_values={"blk": 2.0, "ast": 1.0},
    )
    punt_blk = Build(punt=frozenset({C.BLK}), source="pinned")
    options, _ = rank_streamers(ctx, fas, pool, "rest_of_week", punt_blk)
    assert [o.add.key for o in options] == ["ast"]  # BLK-only add earns nothing, so it is not offered
    blk_only = ([fas[0]], pool)
    assert rank_streamers(ctx, *blk_only, "rest_of_week", punt_blk) == (
        [], "no move improves this week's outlook"
    )

    options, _ = rank_streamers(ctx, fas, pool, "rest_of_week", NO_BUILD)
    by_add = {o.add.key: o for o in options}
    assert set(by_add) == {"ast", "blk"}
    assert by_add["blk"].per_cat_delta[C.BLK] > 0


def test_ties_sorted_by_keys():
    ctx, fas, pool = streaming_ctx({"b": [day(0)], "a": [day(0)]})
    options, _ = rank_streamers(ctx, fas, pool, "today", NO_BUILD)
    assert [o.add.key for o in options] == ["a", "b"]


# --- one-win objective ---------------------------------------------------------

# Opponent leads or trails by a fixed margin in every category (z-scores below use the
# per-player-game SDs over ~10 remaining player games): FG%, FT%, PTS and TO are safe wins,
# AST is the swing category (p ~ 0.4), and REB, STL, BLK, TPM are near-hopeless (p ~ 0.05).
_MINE = StatLine(fgm=200, fga=400, ftm=160, fta=200, pts=300, to=40, ast=30)
_THEIRS = StatLine(fgm=160, fga=400, ftm=120, fta=200, pts=200, to=60, ast=31.8, reb=9.5, stl=4.7, blk=4.15, tpm=6.7)
PUNT_BLK = Build(punt=frozenset({C.BLK}), source="pinned")


def _scenario(scoring_type):
    """A spreads small gains over hopeless cats (one punted); B flips the swing cat AST."""
    ctx, fas, pool = streaming_ctx(
        {"a": [day(0)], "b": [day(0)]},
        fa_per_game={
            "a": StatLine(reb=3.5, stl=2.2, tpm=3.2, blk=2.5),
            "b": StatLine(ast=5.4),
        },
        fa_values={"a": 2.0, "b": 2.0},
        my_totals=_MINE,
        opp_totals=_THEIRS,
        scoring_type=scoring_type,
    )
    return ctx, fas, pool


def test_both_objectives_prefer_the_swing_flip_over_scattered_gains():
    # A's gains are in categories at ~5% (one punted): 5% to 9% is noise, not a category
    # within reach, so a categories league no longer adds them up past B's AST flip either.
    top = {}
    for scoring_type in ("categories", "one_win"):
        ctx, fas, pool = _scenario(scoring_type)
        options, reason = rank_streamers(ctx, fas, pool, "today", PUNT_BLK)
        assert reason is None
        top[scoring_type] = options[0]
    assert top["categories"].add.key == "b"
    assert top["one_win"].add.key == "b"


def test_objective_fields_follow_scoring_type():
    ctx, fas, pool = _scenario("categories")
    options, _ = rank_streamers(ctx, fas, pool, "today", PUNT_BLK)
    assert {o.objective for o in options} == {"categories"}
    assert all(o.delta_objective == o.delta_expected_cats > 0 for o in options)

    ctx, fas, pool = _scenario("one_win")
    base = analyze_week(ctx)
    options, _ = rank_streamers(ctx, fas, pool, "today", PUNT_BLK)
    assert {o.objective for o in options} == {"one_win"}
    by_add = {o.add.key: o for o in options}
    assert by_add["b"].delta_objective > by_add["a"].delta_objective > MIN_WIN_PROB_GAIN
    for o in options:
        assert o.delta_expected_cats > 0  # still filled (build-weighted) for the skill to show
        assert o.delta_objective != pytest.approx(o.delta_expected_cats)
        assert o.score == o.delta_objective
    # B's delta is the swap's matchup win probability with the punted BLK held at its base value.
    swapped = analyze_week(swap_context(ctx, pool[0], by_add["b"].add, {day(0)}))
    frozen = [
        b.win_prob if b.category in PUNT_BLK.punt else n.win_prob
        for n, b in zip(swapped.categories, base.categories, strict=True)
    ]
    assert by_add["b"].delta_objective == pytest.approx(matchup_win_prob(frozen) - base.matchup_win_prob)


def test_one_win_ignores_punted_category_changes():
    ctx, fas, pool = streaming_ctx(
        {"a": [day(0)]},
        fa_per_game={"a": StatLine(ast=5.4, blk=2.5)},
        my_totals=_MINE,
        opp_totals=_THEIRS,
        scoring_type="one_win",
    )
    plain = StatLine(ast=5.4)
    ctx_plain, fas_plain, pool_plain = streaming_ctx(
        {"a": [day(0)]}, fa_per_game={"a": plain}, my_totals=_MINE, opp_totals=_THEIRS,
        scoring_type="one_win",
    )
    with_blk, _ = rank_streamers(ctx, fas, pool, "today", PUNT_BLK)
    without_blk, _ = rank_streamers(ctx_plain, fas_plain, pool_plain, "today", PUNT_BLK)
    assert with_blk[0].per_cat_delta[C.BLK] > 0  # the punted category does move...
    assert with_blk[0].delta_objective == pytest.approx(without_blk[0].delta_objective)  # ...but earns nothing
    # Without the punt it does count.
    chased, _ = rank_streamers(ctx, fas, pool, "today", NO_BUILD)
    assert chased[0].delta_objective > with_blk[0].delta_objective


def test_one_win_needs_a_real_gain():
    # Every category is already decided in my favour: no swap can raise the win probability.
    sweep = StatLine(fgm=200, fga=400, ftm=160, fta=200, pts=1000, reb=500, ast=500, stl=100, blk=100, tpm=100)
    beaten = StatLine(fgm=100, fga=400, ftm=80, fta=200, to=500)
    ctx, fas, pool = streaming_ctx(
        {"a": [day(0)]}, fa_per_game={"a": StatLine(pts=30)}, my_totals=sweep, opp_totals=beaten,
        scoring_type="one_win",
    )
    assert analyze_week(ctx).matchup_win_prob == pytest.approx(1.0)
    assert rank_streamers(ctx, fas, pool, "today", NO_BUILD) == (
        [], "no move improves this week's outlook"
    )


def test_one_win_follow_up_uses_the_same_objective():
    ctx, fas, pool = streaming_ctx(
        {"early": [day(0)], "late": [day(1), day(2)]},
        fa_per_game={"early": StatLine(ast=5.4), "late": StatLine(ast=5.4)},
        fa_values={"early": 2.0, "late": 1.5},
        my_totals=_MINE,
        opp_totals=_THEIRS,
        scoring_type="one_win",
    )
    options, _ = rank_streamers(ctx, fas, pool, "rest_of_week", PUNT_BLK)
    follow_ups = [o.follow_up for o in options if o.follow_up]
    for fu in follow_ups:
        assert fu.objective == "one_win"
        assert fu.delta_objective > MIN_WIN_PROB_GAIN
    for o in options:
        assert o.score == pytest.approx(o.delta_objective + (o.follow_up.delta_objective if o.follow_up else 0))


def test_one_win_follow_up_deltas_add_up_with_punted_cats_frozen_at_the_original_base():
    # The first add moves a punted category (BLK); the follow-up moves the swing category (AST).
    ctx, fas, pool = streaming_ctx(
        {"early": [day(0)], "late": [day(1), day(2)]},
        fa_per_game={"early": StatLine(blk=6.0, ast=0.5), "late": StatLine(ast=5.4)},
        fa_values={"early": 2.0, "late": 1.5},
        my_totals=_MINE,
        opp_totals=_THEIRS,
        scoring_type="one_win",
    )
    base = analyze_week(ctx)
    options, _ = rank_streamers(ctx, fas, pool, "rest_of_week", PUNT_BLK)
    first = next(o for o in options if o.add.key == "early")
    fu = first.follow_up
    assert fu is not None and fu.add.key == "late"

    # Rebuild the final lineup: early on day 0, then late replaces him from day 1.
    after_first = _swap(ctx, pool[0], first.add, set(first.usable_days), day(0))
    early_entry = next(e for e in after_first.my_roster if e.player.key == "early")
    final = analyze_week(_swap(after_first, early_entry, fu.add, set(fu.usable_days), day(1)))
    frozen = [
        b.win_prob if b.category in PUNT_BLK.punt else f.win_prob
        for f, b in zip(final.categories, base.categories, strict=True)
    ]
    expected_total = matchup_win_prob(frozen) - base.matchup_win_prob
    assert first.delta_objective + fu.delta_objective == pytest.approx(expected_total)
    assert first.score == pytest.approx(expected_total)
    assert first.delta_objective < 0.05  # BLK is punted: only his half an assist counts
    assert fu.delta_objective > 0.2  # ...so the follow-up carries the AST flip, not just the residual


@pytest.mark.parametrize(("scoring_type", "kept"), [("one_win", False), ("categories", True)])
def test_gain_threshold_depends_on_scoring_type(scoring_type, kept, monkeypatch):
    # AST is all but won, so the matchup is safe: an add helping the hopeless REB, STL and
    # TPM is worth ~0.16 expected categories but well under 1 point of win probability.
    mine = replace(_MINE, ast=34)
    theirs = replace(_THEIRS, ast=25.5)

    def ranked(scoring_type):
        ctx, fas, pool = streaming_ctx(
            {"a": [day(0)]}, fa_per_game={"a": StatLine(reb=2.0, stl=1.0, tpm=1.5)},
            my_totals=mine, opp_totals=theirs, scoring_type=scoring_type,
        )
        return rank_streamers(ctx, fas, pool, "today", NO_BUILD)

    monkeypatch.setattr("h2h_advisor.analysis.streamers.MIN_WIN_PROB_GAIN", 0.0)
    unfiltered, _ = ranked("one_win")
    assert 0 < unfiltered[0].delta_objective < MIN_WIN_PROB_GAIN  # positive but negligible
    monkeypatch.undo()

    options, reason = ranked(scoring_type)
    if kept:
        assert options[0].delta_objective > MIN_GAIN
    else:
        assert (options, reason) == ([], "no move improves this week's outlook")


def test_gains_below_the_skills_negligible_cut_are_not_offered():
    assert (MIN_GAIN, MIN_WIN_PROB_GAIN) == (0.05, 0.01)
    # A tenth of a rebound a game on one day: real, but noise.
    ctx, fas, pool = streaming_ctx({"a": [day(0)]}, fa_per_game={"a": StatLine(reb=0.1)})
    assert rank_streamers(ctx, fas, pool, "today", NO_BUILD) == ([], "no move improves this week's outlook")


# --- long-term upgrades -------------------------------------------------------

LT_ROS = {
    "D": {c: 0.1 for c in NINE_CAT},
    "up": {C.AST: 3.0, C.PTS: 2.0, C.REB: 1.0},
    "blocker": {C.BLK: 9.0},
    "w": {C.AST: 9.0},
}


def test_long_term_upgrade_listed_with_one_game():
    ctx, _, pool = streaming_ctx({"up": [day(3)]})
    free_agents = [fa("up", "UP"), fa("w", "W", on_waivers=True)]
    upgrades = long_term_upgrades(ctx, free_agents, pool, LT_ROS, NO_BUILD, margin=1.0)
    assert [u.add.key for u in upgrades] == ["up"]
    up = upgrades[0]
    assert up.drop.key == "D"
    assert up.ros_value_gain == pytest.approx(6.0 - 0.9)
    assert up.per_cat_ros_delta[C.AST] == pytest.approx(3.0 - 0.1)
    assert up.per_cat_ros_delta[C.BLK] == -0.1
    assert up.usable_days_this_week == [day(3)]


def test_long_term_ignores_punted_value():
    ctx, _, pool = streaming_ctx({"blocker": WEEK_DAYS, "up": [day(3)]})
    free_agents = [fa("blocker", "BLOCKER"), fa("up", "UP")]
    punt_blk = Build(punt=frozenset({C.BLK}), source="pinned")
    upgrades = long_term_upgrades(ctx, free_agents, pool, LT_ROS, punt_blk, margin=1.0)
    assert [u.add.key for u in upgrades] == ["up"]
    assert long_term_upgrades(ctx, free_agents, [], LT_ROS, punt_blk, margin=1.0) == []


def test_long_term_skips_free_agents_without_ros_data():
    ctx, _, pool = streaming_ctx({"mystery": [day(3)]})
    negative = {"D": {c: -1.0 for c in NINE_CAT}}
    assert long_term_upgrades(ctx, [fa("mystery", "MYSTERY")], pool, negative, NO_BUILD, margin=1.0) == []


def test_long_term_skips_players_with_no_ros_games_and_reports_games():
    ctx, _, pool = streaming_ctx({"up": [day(3)]})
    free_agents = [fa("up", "UP")]
    assert long_term_upgrades(ctx, free_agents, pool, LT_ROS, NO_BUILD, margin=1.0, ros_games={"up": 0}) == []
    (upgrade,) = long_term_upgrades(ctx, free_agents, pool, LT_ROS, NO_BUILD, margin=1.0, ros_games={"up": 12})
    assert upgrade.ros_games == 12
    (unknown,) = long_term_upgrades(ctx, free_agents, pool, LT_ROS, NO_BUILD, margin=1.0)
    assert unknown.ros_games is None


def test_long_term_baseline_from_pool_players_with_data():
    ctx, _, _ = streaming_ctx({"up": [day(3)]})
    unknown, known = entry("unknown", "DDD"), entry("D", "DDD")
    free_agents = [fa("up", "UP")]
    assert long_term_upgrades(ctx, free_agents, [unknown], LT_ROS, NO_BUILD, margin=1.0) == []
    (upgrade,) = long_term_upgrades(ctx, free_agents, [unknown, known], LT_ROS, NO_BUILD, margin=1.0)
    assert upgrade.drop.key == "D"
    assert upgrade.ros_value_gain == pytest.approx(6.0 - 0.9)


# --- variance: an add that produces nothing must not look like an upgrade ---------

_EVEN = StatLine(fgm=150, fga=320, ftm=60, fta=75, tpm=40, pts=400, reb=160, ast=80, stl=25, blk=15, to=40)
_AHEAD = StatLine(fgm=150, fga=320, ftm=60, fta=75, tpm=44, pts=420, reb=168, ast=85, stl=27, blk=17, to=38)
ZERO_ADD_SCENARIOS = {
    "early_even": (TODAY, ZERO, ZERO),  # Wednesday, nothing played yet
    "late_trailing": (day(3), _EVEN, _AHEAD),  # Saturday, behind in every counting category
}


@pytest.mark.parametrize("scoring_type", ["categories", "one_win"])
@pytest.mark.parametrize("scenario", sorted(ZERO_ADD_SCENARIOS))
def test_zero_stat_add_with_games_never_gains(scenario, scoring_type):
    today, mine, theirs = ZERO_ADD_SCENARIOS[scenario]
    ctx, _, pool = streaming_ctx(
        {"z": WEEK_DAYS}, fa_per_game={"z": ZERO}, my_totals=mine, opp_totals=theirs, scoring_type=scoring_type
    )
    ctx = replace(ctx, today=today)
    base = analyze_week(ctx)
    swapped = analyze_week(swap_context(ctx, pool[0], player("z", "Z"), set(WEEK_DAYS)))
    assert swapped.expected_cats_won - base.expected_cats_won <= 1e-12
    assert swapped.matchup_win_prob - base.matchup_win_prob <= 1e-12

    for name in ("MIN_GAIN", "MIN_WIN_PROB_GAIN"):  # unfiltered: delta_objective itself
        setattr(streamers, name, float("-inf"))
    try:
        options, _ = rank_streamers(ctx, [fa("z", "Z")], pool, "rest_of_week", NO_BUILD)
    finally:
        streamers.MIN_GAIN, streamers.MIN_WIN_PROB_GAIN = MIN_GAIN, MIN_WIN_PROB_GAIN
    assert options and all(o.delta_objective <= 1e-12 for o in options)


def test_production_scales_the_variance_a_player_game_adds():
    lineup = {TODAY: {"star": "PG", "zero": "SG"}}
    per_game = {"star": StatLine(pts=30.0), "zero": ZERO}
    typical = SD_PER_PLAYER_GAME[C.PTS] ** 2
    assert counting_variance(C.PTS, lineup, per_game) == pytest.approx(typical * 30.0 / TYPICAL_PER_GAME[C.PTS])
    assert counting_variance(C.PTS, {TODAY: {"zero": "SG"}}, per_game) == 0.0


# --- dated add plan ----------------------------------------------------------

def test_plan_beats_the_frozen_roster_and_dates_the_move():
    ctx, fas, pool = streaming_ctx({"early": [day(0), day(1), day(2)]}, fa_values={"early": 2.0})
    plan = plan_adds(ctx, fas, pool)
    assert plan.adds_used == 1 and plan.reason is None
    move = plan.moves[0]
    assert (move.add.key, move.drop.key) == ("early", "D")
    assert plan.matchup_win_prob > plan.base_win_prob


def test_plan_can_be_ranked_by_the_chance_of_a_bigger_win():
    ctx, fas, pool = streaming_ctx({"early": [day(0), day(1), day(2)]}, fa_values={"early": 2.0})
    plain = plan_adds(ctx, fas, pool)
    assert plain.target_cats is None and plain.target_prob is None
    plan = plan_adds(ctx, fas, pool, target_cats=6)
    assert plan.target_cats == 6
    assert plan.target_prob >= plan.base_target_prob


def test_plan_target_is_not_cut_short_by_a_week_that_is_already_safe():
    # a week already "safe" to win is still worth improving when the goal is a bigger win
    ctx, fas, pool = streaming_ctx({"early": [day(0), day(1), day(2)]}, fa_values={"early": 2.0})
    plan = plan_adds(ctx, fas, pool, target_cats=9)
    assert plan.reason is None or "already" not in plan.reason


def test_plan_without_adds_is_the_base_outlook():
    ctx, fas, pool = streaming_ctx({"early": [day(0), day(1)]}, moves_used=7)
    plan = plan_adds(ctx, fas, pool)
    assert plan.moves == [] and plan.reason == "no adds left this week"
    assert plan.matchup_win_prob == plan.base_win_prob


def test_plan_waits_for_the_day_the_drop_has_played():
    # the drop plays on day 0 and is useful; the add only has games from day 2
    ctx, fas, pool = streaming_ctx({"late": [day(2), day(3), day(4)]}, fa_values={"late": 2.0})
    team_days = {"MMM": WEEK_DAYS, "OOO": WEEK_DAYS, "DDD": [day(0)], "LATE": [day(2), day(3), day(4)]}
    ctx = replace(ctx, schedule=make_schedule(team_days))
    plan = plan_adds(ctx, fas, pool)
    (move,) = plan.moves
    assert move.on >= day(1)  # not today: D still has a game to play on day 0
    assert day(0) not in move.drop_days_given_up


def test_plan_moves_are_dated_in_order_and_count_adds_left():
    ctx, fas, pool = streaming_ctx(
        {"early": [day(0), day(1), day(2)], "late": [day(3), day(4)]},
        fa_values={"early": 2.0, "late": 1.5},
    )
    plan = plan_adds(ctx, fas, pool)
    assert [m.on for m in plan.moves] == sorted(m.on for m in plan.moves)
    assert plan.moves[-1].adds_left_after == plan.adds_left - plan.adds_used


# --- IL activation -----------------------------------------------------------

def _il_ctx(status):
    ctx, _, _ = streaming_ctx({})
    roster = [*ctx.my_roster, entry("B", "MMM", slot="IL")]
    roster[2] = RosterEntry(replace(roster[2].player, status=status), "IL")
    return replace(
        ctx,
        my_roster=roster,
        per_game=ctx.per_game | {"B": AVG},
        values=ctx.values | {"B": 5.0},
        ros_values=ctx.ros_values | {"B": 5.0, "M1": 10.0, "D": 0.0},
    )


def test_questionable_il_player_comes_back_and_weakest_is_dropped():
    ctx, plan = activate_il(_il_ctx("Q"), "mine")
    assert [(a.key, d.key) for a, d in plan] == [("B", "D")]
    slots = {e.player.key: e.slot for e in ctx.my_roster}
    assert slots["B"] == "BN" and "D" not in slots


def test_out_il_player_stays_out():
    ctx, plan = activate_il(_il_ctx("O"), "mine")
    assert plan == [] and {e.player.key: e.slot for e in ctx.my_roster}["B"] == "IL"


def test_never_drop_protects_the_weakest():
    _, plan = activate_il(_il_ctx("Q"), "mine", {"D"})
    assert [(a.key, d.key) for a, d in plan] == [("B", "M1")]


def test_il_player_with_no_game_this_week_stays_in_il():
    ctx = replace(_il_ctx("Q"), active_days={"B": set()})
    out, plan = activate_il(ctx, "mine")
    assert plan == [] and {e.player.key: e.slot for e in out.my_roster}["B"] == "IL"


def test_drop_keeps_playing_until_the_il_player_is_back():
    ctx = replace(_il_ctx("Q"), active_days={"B": {day(2), day(3), day(4)}})
    out, plan = activate_il(ctx, "mine")
    assert [(a.key, d.key) for a, d in plan] == [("B", "D")]
    # D (the weakest) stays on the roster for the days before B returns
    assert out.active_days["D"] <= {day(0), day(1)}


def test_drop_is_the_weak_player_who_would_not_start_anyway():
    # B returns; M1 (high value) starts, "bench" never starts (all slots full of better
    # players) so dropping him costs nothing, while dropping the starter "low" costs a game
    _, plan = activate_il(_il_ctx("Q"), "mine")
    assert plan[0][1].key == "D"


def test_my_build_values_do_not_reshape_the_opponent_lineup():
    # one slot, two opponent players: the opponent starts his best on plain `values`,
    # whatever weights my build puts on my own players
    ctx, _, _ = streaming_ctx({}, roster_slots={"PG": 1, "BN": 2})
    opp = [entry("O1", "OOO"), entry("O2", "OOO")]
    ctx = replace(
        ctx,
        opp_roster=opp,
        per_game=ctx.per_game | {"O2": AVG.scale(2)},
        values=ctx.values | {"O2": 20.0},
    )
    tuned = replace(ctx, my_values={"M1": -50.0, "D": 0.0, "O1": -9.0, "O2": -9.0})
    assert analyze_week(tuned).opp_proj == analyze_week(ctx).opp_proj


def test_projected_rest_lists_players_who_sit_on_a_game_day():
    ctx, _, _ = streaming_ctx({})
    ctx = replace(ctx, active_days={"M1": set(WEEK_DAYS) - {day(3)}, "D": set()})
    sitting = projected_rest(ctx.my_roster, ctx.schedule, WEEK_DAYS, ctx.active_days)
    # M1 rests on day 3 (his team plays); D's team never plays, so he is not "resting"
    assert [(p.key, d) for p, d in sitting] == [("M1", [day(3)])]


def test_plan_leaves_a_safe_one_win_week_alone():
    ctx, fas, pool = streaming_ctx(
        {"early": [day(0), day(1), day(2)]},
        my_totals=StatLine(fgm=900, fga=1200, ftm=500, fta=600, tpm=300, pts=3000, reb=1200, ast=900, stl=400, blk=300),
        fa_values={"early": 2.0},
        scoring_type="one_win",
    )
    plan = plan_adds(ctx, fas, pool)
    assert plan.moves == [] and "worth a drop" in plan.reason
    assert plan.objective == "one_win"


# Every category but AST is locked (FT_PCT and TO sit near a coin flip and no add moves
# them); AST trails. "early" plays day 0-1, "late" day 3-4, both pure assists.
_LOCKED = StatLine(fgm=900, fga=1200, ftm=500, fta=600, tpm=300, pts=3000, reb=1200, ast=40, stl=400, blk=300)
_AST_LEAD = StatLine(ast=46)


def _chain_ctx(scoring_type="categories"):
    return streaming_ctx(
        {"early": [day(0), day(1)], "late": [day(3), day(4)]},
        fa_per_game={"early": StatLine(ast=6.0), "late": StatLine(ast=6.0)},
        fa_values={"early": 2.0, "late": 1.5},
        my_totals=_LOCKED,
        opp_totals=_AST_LEAD,
        scoring_type=scoring_type,
    )


def test_categories_plan_chains_the_adds_though_the_matchup_is_won():
    # Every category counts in a categories league: a won matchup still has AST to
    # take, and the adds left are chained - today's add, then a swap once he has played.
    ctx, fas, pool = _chain_ctx()
    plan = plan_adds(ctx, fas, pool)
    assert plan.objective == "categories" and plan.base_win_prob >= 0.9
    assert [(m.on, m.add.key, m.drop.key) for m in plan.moves] == [
        (day(0), "early", "D"),
        (day(3), "late", "early"),
    ]
    assert plan.expected_cats_won > plan.base_expected_cats + 0.5
    assert all(m.gain >= 0.05 for m in plan.moves)  # gains are in expected categories


@pytest.mark.parametrize(
    ("scoring_type", "bar"), [("one_win", "PLAN_MIN_GAIN"), ("categories", "PLAN_MIN_GAIN_CATS")]
)
def test_a_streamer_is_swapped_for_a_better_fit_on_a_day_he_plays(monkeypatch, scoring_type, bar):
    # "early" (a weak streamer) plays days 0-2; "late" (a better fit for the chase) only day 2.
    # Swapping early for late on day 2 gives up early's day-2 game, but early is this week's
    # streamer: his only value is that game, already netted out of the gain, and the add would
    # otherwise go unused. The roster-player bar must not block it (a C for a SF on Saturday).
    monkeypatch.setattr(streamers, bar, 99.0)  # a drop that gives up a roster player's game: never
    ctx, fas, pool = streaming_ctx(
        {"early": [day(0), day(1), day(2)], "late": [day(2)]},
        fa_per_game={"early": StatLine(ast=2.0), "late": StatLine(ast=12.0)},
        fa_values={"early": 2.0, "late": 1.5},
        # one-win: an open week (a locked one is left alone); categories: only AST in play
        **({} if scoring_type == "one_win" else {"my_totals": _LOCKED, "opp_totals": StatLine(ast=50)}),
        scoring_type=scoring_type,
    )
    plan = plan_adds(ctx, fas, pool)
    assert [(m.on, m.add.key, m.drop.key) for m in plan.moves] == [
        (day(0), "early", "D"),
        (day(2), "late", "early"),
    ]
    assert plan.moves[1].drop_days_given_up == [day(2)]


def test_a_category_already_decided_this_week_is_no_reason_to_add():
    # STL is 94% won (likely_win, not a strong category), AST a coin flip. "thief" only adds
    # steals: pushing a won category to 99% is no reason to spend an add, and it must not
    # outrank the swing-category fit (a SF's PTS and TPM, out of reach, outranked the
    # REB fit and broke the Fri add / Sat swap chain).
    ctx, fas, pool = streaming_ctx(
        {"thief": WEEK_DAYS, "fit": WEEK_DAYS},
        fa_per_game={"thief": StatLine(stl=3.0), "fit": StatLine(ast=1.5)},
        my_totals=replace(_LOCKED, stl=14, ast=40),
        opp_totals=StatLine(stl=10, ast=40),
    )
    statuses = {o.category: o.status for o in analyze_week(ctx).categories}
    assert (statuses[C.STL], statuses[C.AST]) == ("likely_win", "swing")
    options, _ = rank_streamers(ctx, fas, pool, "rest_of_week", NO_BUILD)
    assert [o.add.key for o in options] == ["fit"]
    plan = plan_adds(ctx, fas, pool, build=NO_BUILD)
    assert [m.add.key for m in plan.moves] == ["fit"]


def test_categories_plan_gives_no_credit_for_a_punted_category():
    ctx, fas, pool = _chain_ctx()
    plan = plan_adds(ctx, fas, pool, build=Build(punt=frozenset({C.AST}), source="pinned"))
    assert plan.moves == []


def test_plan_drops_a_player_above_replacement_only_when_the_user_chose_him():
    ctx, fas, pool = _chain_ctx()
    ctx = replace(ctx, ros_values={"D": 5.0, "early": 0.0, "late": 0.0})  # D is no throwaway
    assert plan_adds(ctx, fas, pool).moves == []
    chosen = plan_adds(ctx, fas, pool, drop_first=frozenset({"D"}))
    assert chosen.moves and chosen.moves[0].drop.key == "D"
    assert chosen.moves[0].ros_cost_flag  # still flagged: the cost is real, the user accepted it


def test_activation_never_drops_a_kept_rotation_player():
    ctx = _il_ctx("Q")
    _, plan = activate_il(ctx, "mine", keep=frozenset({"D"}))
    assert [(a.key, d.key) for a, d in plan] == [("B", "M1")]


def test_plan_scores_a_lower_valued_player_who_plays_on_the_day():
    # 30 better free agents have no game left; the low-valued one plays from day 0.
    # Ranking by value before looking at the schedule would never score him.
    fa_days = {f"star{i}": [] for i in range(30)} | {"modest": [day(0), day(1), day(2)]}
    values = {f"star{i}": 5.0 + i for i in range(30)} | {"modest": 0.5}
    ctx, fas, pool = streaming_ctx(fa_days, fa_values=values)
    plan = plan_adds(ctx, fas, pool, max_candidates=5)
    assert [m.add.key for m in plan.moves] == ["modest"]


@pytest.mark.parametrize(
    ("scoring_type", "bar"), [("one_win", "PLAN_MIN_GAIN"), ("categories", "PLAN_MIN_GAIN_CATS")]
)
def test_a_free_drop_needs_a_smaller_gain_than_a_costly_one(monkeypatch, scoring_type, bar):
    monkeypatch.setattr(streamers, bar, 99.0)  # impossible: a costly drop never qualifies
    ctx, fas, pool = streaming_ctx(
        {"mild": [day(0), day(1), day(2)]}, fa_values={"mild": 1.0}, scoring_type=scoring_type
    )
    free = plan_adds(ctx, fas, pool)  # the pool is D, who never plays: his drop is free
    assert [m.drop.key for m in free.moves] == ["D"]
    costly = plan_adds(ctx, fas, [ctx.my_roster[0]])  # M1 starts every day: dropping him costs games
    assert costly.moves == []


def test_every_eligible_free_agent_is_scored_not_only_the_best_by_value():
    # 30 free agents of higher overall value who add next to nothing, and a low-valued one
    # who fits the week. A cut by value before scoring (the top 25) never saw him.
    fa_days = {f"star{i}": [day(0)] for i in range(30)} | {"fit": [day(0)]}
    per_game = {f"star{i}": StatLine(pts=0.1) for i in range(30)} | {"fit": AVG}
    values = {f"star{i}": 5.0 + i for i in range(30)} | {"fit": -3.0}
    ctx, fas, pool = streaming_ctx(fa_days, fa_per_game=per_game, fa_values=values)
    options, _ = rank_streamers(ctx, fas, pool, "rest_of_week", NO_BUILD, max_results=3)
    assert options[0].add.key == "fit"


def test_plan_finds_an_early_add_that_a_bigger_later_add_replaces():
    # "wing" plays only day 0, five "bigs" only day 1, each adding more than he does. Moves
    # on different days chain (wing on day 0, then the best big for him on day 1), but
    # ranked by gain alone the bigs fill the search and the wing's game is never played.
    bigs = {f"big{i}": [day(1)] for i in range(5)}
    ctx, fas, pool = streaming_ctx(
        {"wing": [day(0)]} | bigs,
        fa_per_game={"wing": StatLine(ast=2.0)}
        | {f"big{i}": StatLine(ast=4.0 - i * 0.2) for i in range(5)},
        my_totals=_LOCKED,
        opp_totals=_AST_LEAD,
    )
    plan = plan_adds(ctx, fas, pool)
    assert [(m.on, m.add.key, m.drop.key) for m in plan.moves] == [
        (day(0), "wing", "D"),
        (day(1), "big0", "wing"),
    ]


def test_plan_finds_a_chain_that_starts_with_a_move_ranked_below_many_near_equal_ones():
    # A wing on day 0, six centres on day 1 (near equal), a guard on day 2. "G1", a weak
    # roster guard, holds the only PG slot on day 2. Best: the wing for D, the best centre
    # for G1 (not for the wing: that leaves G1 in the guard's way), then the guard for a
    # spent streamer. "Centre for G1" ranks below all six "centre for the wing" moves, so a
    # search that keeps only a few moves per step never tries it.
    centres = {f"c{i}": [day(1)] for i in range(6)}
    fa_days = {"wing": [day(0)], "guard": [day(2)]} | centres
    positions = {"wing": ("SF",), "guard": ("PG",)} | {k: ("C",) for k in centres}
    per_game = {"M1": AVG, "O1": AVG, "D": AVG, "G1": StatLine(ast=1.0), "wing": StatLine(ast=2.0),
                "guard": StatLine(ast=2.5)} | {f"c{i}": StatLine(ast=4.0 - i * 0.1) for i in range(6)}
    team_days = {"MMM": WEEK_DAYS, "OOO": WEEK_DAYS, "DDD": [], "GGG": [day(2)]}
    ctx = make_ctx(
        my_roster=[entry("M1", "MMM", ("Util",)), entry("D", "DDD"), entry("G1", "GGG")],
        opp_roster=[entry("O1", "OOO")],
        per_game=per_game,
        values={"M1": 10.0, "O1": 10.0, "D": 0.0, "G1": 5.0} | {k: 1.0 for k in fa_days},
        schedule=make_schedule(team_days | {k.upper(): d for k, d in fa_days.items()}),
        my_totals=_LOCKED,
        opp_totals=_AST_LEAD,
        moves_used=4,
        roster_slots={"PG": 1, "SF": 1, "C": 1, "Util": 1, "BN": 3},
    )
    fas = [fa(k, k.upper(), positions[k]) for k in fa_days]
    plan = plan_adds(ctx, fas, [ctx.my_roster[1], ctx.my_roster[2]])
    moves = [(m.on, m.add.key, m.drop.key) for m in plan.moves]
    assert moves[:2] == [(day(0), "wing", "D"), (day(1), "c0", "G1")]
    assert [(on, add) for on, add, _ in moves[2:]] == [(day(2), "guard")]
