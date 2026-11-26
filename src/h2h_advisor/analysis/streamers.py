"""Adaptive drop pool, build-weighted streamer ranking and long-term upgrades."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Literal

from h2h_advisor.analysis.build import Build, category_weights
from h2h_advisor.analysis.outlook import (
    WeekAnalysis,
    WeekContext,
    analyze_week,
    matchup_win_prob,
    prob_at_least,
    remaining_days,
)
from h2h_advisor.analysis.projection import (
    INACTIVE_SLOTS,
    OUT_STATUSES,
    lineup_by_day,
)
from h2h_advisor.identity import normalize_name
from h2h_advisor.models import (
    NINE_CAT,
    Category,
    FreeAgent,
    LeagueSettings,
    Player,
    RosterEntry,
)
from h2h_advisor.sources.schedule import Schedule

CANDIDATE_LIMIT = 25
MAX_POOL = 3  # most players a streaming move may drop, by default
PLAN_CANDIDATES = 30  # free agents kept for the search, by what they add
PLAN_BEAM = 3  # partial plans kept at each step of the search
PLAN_EXPAND = 4  # next moves tried from each of them
PLAN_MIN_GAIN = 0.03  # a move that gives up games must add this much win probability
PLAN_FREE_GAIN = 0.01  # a move that gives up none (the drop would not have started) only this much
# Categories leagues plan on expected categories won (every category counts in the
# standings), so the bars are in categories: the skill's "negligible" cut for a free
# drop, twice that when the drop gives up games.
PLAN_MIN_GAIN_CATS = 0.10
PLAN_FREE_GAIN_CATS = 0.05
# A swap that drops a streamer added earlier in the plan costs nothing past this week: his
# only value is the games given up, already netted out of the gain, and adds left unused are
# lost when the week ends. Such a swap (a C for a SF on Saturday, for REB and BLK) needs only
# a clearly positive gain, not the bar that protects a roster player.
PLAN_SPARE_GAIN = 0.005
PLAN_SPARE_GAIN_CATS = 0.02
PLAN_SAFE = 0.90  # one-win leagues: at or above this the week is safe, no moves worth a drop
ACTIVATION_POOL = 4  # weakest roster players (by ROS) considered to make room for an IL return
FOLLOW_UP_OPTIONS = 5
FOLLOW_UP_CANDIDATES = 5
# Below these a move is noise: the streaming-advisor skill's "negligible" cut.
MIN_GAIN = 0.05  # categories leagues: expected categories won
MIN_WIN_PROB_GAIN = 0.01  # one-win leagues: matchup win probability

_NEG_INF = float("-inf")


@dataclass(frozen=True)
class StreamerOption:
    add: Player
    drop: Player
    start_days: list[date]  # days the add's team plays while he is on my roster
    usable_days: list[date]  # of those, days he actually gets an active slot
    slot_by_day: dict[date, str]
    raw_games: int
    light_days: list[date]
    delta_expected_cats: float  # build-weighted, filled for both scoring types
    per_cat_delta: dict[Category, float]  # unweighted
    adds_left_after: int
    objective: Literal["categories", "one_win"]
    delta_objective: float  # what is ranked on: delta_expected_cats, or the win-probability delta
    drop_ros_value: float | None = None
    add_ros_value: float | None = None
    ros_cost_flag: bool = False  # drop is above replacement level (outside best FA ROS + margin)
    follow_up: StreamerOption | None = None

    @property
    def score(self) -> float:
        return self.delta_objective + (self.follow_up.delta_objective if self.follow_up else 0.0)


@dataclass(frozen=True)
class PlannedMove:
    """One dated add/drop of the week plan: make it on `on`, before his lineup locks."""

    on: date
    add: Player
    drop: Player
    usable_days: list[date]  # days the add gets an active slot, from `on` on
    slot_by_day: dict[date, str]
    drop_days_given_up: list[date]  # days from `on` on the drop would have started
    gain: float  # what the move added when picked, in the plan's `objective` units
    adds_left_after: int
    ros_cost_flag: bool = False  # the drop is above replacement level
    claim: bool = False  # the add is on waivers: a claim that clears on or before `on`


@dataclass
class _PlanState:
    """A partial plan of the beam search: the moves so far and the roster they leave."""

    analysis: WeekAnalysis
    current: WeekContext
    candidates: tuple[FreeAgent, ...]
    droppable: tuple[RosterEntry, ...]
    added_on: dict[str, date]
    moves: tuple[PlannedMove, ...]

    @property
    def key(self) -> frozenset:
        return frozenset((m.on, m.add.key, m.drop.key) for m in self.moves)


@dataclass(frozen=True)
class AddPlan:
    """Dated plan for the adds left: what to do today, what to prepare for later days."""

    base_win_prob: float  # no move at all
    base_expected_cats: float
    matchup_win_prob: float  # after every planned move
    expected_cats_won: float
    adds_left: int
    adds_used: int
    moves: list[PlannedMove]  # in date order; those dated today are "do now"
    reason: str | None = None  # why there are no moves, when there are none
    target_cats: int | None = None  # the plan was ranked by P(at least this many categories)
    base_target_prob: float | None = None  # P(at least target_cats) with no move
    target_prob: float | None = None  # ... after every planned move
    # What the plan maximised, and the unit of each move's `gain`: "categories" (build-
    # weighted expected categories won), "one_win" (matchup win probability) or
    # "target" (P(at least target_cats categories)).
    objective: Literal["categories", "one_win", "target"] = "one_win"


@dataclass(frozen=True)
class LongTermUpgrade:
    add: Player
    drop: Player
    ros_value_gain: float
    per_cat_ros_delta: dict[Category, float]
    usable_days_this_week: list[date]
    ros_games: float | None = None  # BBM ROS games remaining, when known


def _ros(ros_values: dict[str, float], key: str) -> float:
    return ros_values.get(key, _NEG_INF)


def drop_pool(
    roster: list[RosterEntry],
    ros_values: dict[str, float],
    free_agents: list[FreeAgent],
    never_drop: set[str],
    margin: float,
    stream_slots: int | None,
    keep: frozenset[str] = frozenset(),
    drop_first: frozenset[str] = frozenset(),
) -> list[RosterEntry]:
    """Healthy roster players close to replacement level (or the bottom `stream_slots`), weakest first.

    Never more than MAX_POOL by default: a streaming move should cost a throwaway, not a
    useful player. An injured player (O, INJ, ...) is never offered, whatever his games
    this week: "he misses nothing" is how a star on a short absence looks free to cut.
    `keep` holds player keys never offered (rotation players by recent minutes).
    `drop_first` holds player keys the user said to drop first: offered ahead of everyone,
    whatever their value, minutes or status (the user decided), unless in an IL slot.
    """
    protected = {normalize_name(name) for name in never_drop}
    chosen = [e for e in roster if e.player.key in drop_first and e.slot not in INACTIVE_SLOTS]
    eligible = sorted(
        (
            e for e in roster
            if e.slot not in INACTIVE_SLOTS
            and e.player.status not in OUT_STATUSES
            and e.player.key not in keep
            and e.player.key not in drop_first
            and normalize_name(e.player.name) not in protected
        ),
        key=lambda e: (_ros(ros_values, e.player.key), e.player.key),
    )
    if stream_slots is not None:
        return [*chosen, *eligible][: max(stream_slots, len(chosen))]
    threshold = _best_fa_ros(ros_values, free_agents) + margin
    pool = [e for e in eligible if _ros(ros_values, e.player.key) <= threshold]
    if not chosen:
        pool = pool or eligible[:1]
    return [*chosen, *pool][: max(MAX_POOL, len(chosen))]


def _best_fa_ros(ros_values: dict[str, float], free_agents: list[FreeAgent]) -> float:
    """Highest ROS value among free agents not on waivers (replacement level)."""
    return max(
        (ros_values[f.player.key] for f in free_agents if not f.on_waivers and f.player.key in ros_values),
        default=_NEG_INF,
    )


def effective_start(settings: LeagueSettings, today: date) -> date:
    """First day a move made today affects the lineup."""
    return today + timedelta(days=1) if settings.lineup_effective == "tomorrow" else today


def _after_return(
    roster: list[RosterEntry],
    active_days: dict[str, set[date]],
    days: list[date],
    returning: RosterEntry,
    drop: RosterEntry,
    release: date,
) -> tuple[list[RosterEntry], dict[str, set[date]]]:
    """`returning` leaves IL for the bench; `drop` plays only the days before `release`."""
    kept = {d for d in days if d < release} & active_days.get(drop.player.key, set(days))
    new_roster = [
        RosterEntry(returning.player, "BN") if e is returning else e
        for e in roster
        if e is not drop or kept
    ]
    return new_roster, active_days | {drop.player.key: kept}


def activate_il(
    ctx: WeekContext,
    side: Literal["mine", "opponent"],
    never_drop: set[str] = frozenset(),
    keep: frozenset[str] = frozenset(),
) -> tuple[WeekContext, list[tuple[Player, Player]]]:
    """Plan for every IL-slot player who is not ruled out (Q, P, no status): he comes back.

    A player who is merely questionable sits in the IL slot only until he is moved
    out, so counting him as absent understates the roster. The move happens on his
    first day with a game this week (BBM's day projections hold the injury
    timeline); a player with no such day stays in IL. The weakest droppable player
    (lowest ROS value, fewest games lost on a tie; `never_drop` names are
    protected) is released from that day on and plays until then. Returns the new
    context and the (activated, dropped) pairs, so the assumption can be shown.
    """
    protected = {normalize_name(name) for name in never_drop}
    days = remaining_days(ctx)
    start = effective_start(ctx.settings, ctx.today)
    roster = list(ctx.my_roster if side == "mine" else ctx.opp_roster)
    active_days = dict(ctx.active_days or {})

    def plays(key: str, team: str, day: date) -> bool:
        return team in ctx.schedule.get(day, frozenset()) and day in active_days.get(key, set(days))

    roster_field = "my_roster" if side == "mine" else "opp_roster"
    current = ctx
    plan: list[tuple[Player, Player]] = []
    for entry in list(roster):
        player = entry.player
        if entry.slot not in INACTIVE_SLOTS or player.status in OUT_STATUSES or player.key not in ctx.per_game:
            continue
        back = [d for d in days if d >= start and plays(player.key, player.nba_team, d)]
        if not back:
            continue  # out for the rest of the week: he stays in IL
        release = back[0]
        candidates = sorted(
            (
                e for e in roster
                if e.slot not in INACTIVE_SLOTS
                and e.player.key not in keep
                and normalize_name(e.player.name) not in protected
            ),
            key=lambda e: (_ros(ctx.ros_values, e.player.key), e.player.key),
        )[:ACTIVATION_POOL]
        if not candidates:
            continue

        # Of the weakest few by ROS, the one the lineup misses least: a player who
        # would not start anyway costs nothing this week.
        trials = []
        for candidate in candidates:
            new_roster, new_days = _after_return(roster, active_days, days, entry, candidate, release)
            trial = replace(current, **{roster_field: new_roster, "active_days": new_days})
            trials.append((analyze_week(trial).matchup_win_prob, candidate, new_roster, new_days))
        _, drop, roster, active_days = max(trials, key=lambda t: t[0])  # the weakest wins a tie
        current = replace(current, **{roster_field: roster, "active_days": active_days})
        plan.append((player, drop.player))
    return current, plan


def swap_context(ctx: WeekContext, drop: RosterEntry, add: Player, add_days: set[date]) -> WeekContext:
    """`ctx` with `drop` released from effective_start on and `add` (on BN) active only on `add_days`.

    The dropped player stays on the roster for the days before effective_start, when
    the move has not yet taken effect and his games still count.
    """
    return _swap(ctx, drop, add, add_days, release_from=effective_start(ctx.settings, ctx.today))


def _swap(
    ctx: WeekContext, drop: RosterEntry, add: Player, add_days: set[date], release_from: date
) -> WeekContext:
    days = remaining_days(ctx)
    active_days = dict(ctx.active_days or {})
    kept = {d for d in days if d < release_from}
    active_days[drop.player.key] = kept & active_days.get(drop.player.key, set(days))
    # The add plays only on window days he is projected to play (absent key: schedule only).
    known = (ctx.active_days or {}).get(add.key)
    active_days[add.key] = set(add_days) if known is None else set(add_days) & known
    return replace(ctx, my_roster=[*ctx.my_roster, RosterEntry(add, "BN")], active_days=active_days)


def _weighted(weights: dict[Category, float], new: WeekAnalysis, base: WeekAnalysis) -> tuple[float, dict]:
    per_cat = {
        n.category: n.win_prob - b.win_prob for n, b in zip(new.categories, base.categories, strict=True)
    }
    return sum(weights.get(c, 0.0) * d for c, d in per_cat.items()), per_cat


def _games(ctx: WeekContext, team: str, days: list[date]) -> list[date]:
    return [d for d in days if team in ctx.schedule.get(d, frozenset())]


def _usable_slots(ctx: WeekContext, add_key: str, days: list[date]) -> dict[date, str]:
    lineups = lineup_by_day(
        ctx.my_roster, ctx.per_game, ctx.my_lineup_values, ctx.schedule, days,
        ctx.settings.roster_slots, active_days=ctx.active_days,
    )
    return {d: lineups[d][add_key] for d in days if add_key in lineups[d]}


def first_play_day(schedule: Schedule, days: list[date]) -> date | None:
    """The first of ``days`` that is not known to have no NBA games.

    Horizon "today" means the first day an add made now can score: on a day
    without games (Thanksgiving) that is the next day with games. A day missing
    from the schedule (its page did not load) is unknown, so it is not skipped.
    """
    return next((d for d in days if d not in schedule or schedule[d]), None)


def _horizon_days(ctx: WeekContext, horizon: Literal["today", "rest_of_week"]) -> list[date]:
    start = effective_start(ctx.settings, ctx.today)
    days = [d for d in remaining_days(ctx) if d >= start]
    if horizon == "today":
        first = first_play_day(ctx.schedule, days)
        return [first] if first is not None else []
    return days


def _allowed_days(f: FreeAgent, horizon: str, horizon_days: list[date]) -> list[date]:
    if not f.on_waivers:
        return horizon_days
    if horizon == "today" or f.waiver_clear is None:
        return []
    return [d for d in horizon_days if d >= f.waiver_clear]


def _min_gain(objective: Literal["categories", "one_win"]) -> float:
    return MIN_WIN_PROB_GAIN if objective == "one_win" else MIN_GAIN


def _win_prob_delta(frozen: dict[Category, float], new: WeekAnalysis, base: WeekAnalysis) -> float:
    """Change in matchup win probability from `base` to `new`, punted categories held at `frozen`.

    `frozen` holds the punted categories' win probabilities of the ORIGINAL base, so
    chained swaps (a follow-up is scored against the first swap) add up exactly:
    delta1 + delta2 == M(final) - M(original base), with punted cats frozen throughout.
    """
    def matchup(a: WeekAnalysis) -> float:
        return matchup_win_prob([frozen.get(o.category, o.win_prob) for o in a.categories])

    return matchup(new) - matchup(base)


@dataclass(frozen=True)
class _Scoring:
    weights: dict[Category, float]
    objective: Literal["categories", "one_win"]
    frozen: dict[Category, float]  # punted categories' win probability at the original base
    light_day_max_games: int
    ros_values: dict[str, float]
    ros_threshold: float  # best non-waiver FA ROS + margin


@dataclass(frozen=True)
class _Swap:
    option: StreamerOption
    ctx: WeekContext
    analysis: WeekAnalysis


def _evaluate(
    ctx: WeekContext,
    base: WeekAnalysis,
    scoring: _Scoring,
    drop: RosterEntry,
    add: Player,
    window: list[date],
    release_from: date,
    adds_left_after: int,
) -> _Swap | None:
    """Swap `drop` (from `release_from`) for `add` (on `window`) and score it against `base`."""
    start_days = _games(ctx, add.nba_team, window)
    swapped = _swap(ctx, drop, add, set(window), release_from)
    slot_by_day = _usable_slots(swapped, add.key, window)
    if not slot_by_day:
        return None
    new = analyze_week(swapped)
    delta, per_cat = _weighted(scoring.weights, new, base)
    delta_objective = _win_prob_delta(scoring.frozen, new, base) if scoring.objective == "one_win" else delta
    usable = sorted(slot_by_day)
    light = [
        d for d in usable if d in ctx.schedule and len(ctx.schedule[d]) // 2 <= scoring.light_day_max_games
    ]
    drop_ros = scoring.ros_values.get(drop.player.key)
    option = StreamerOption(
        add=add,
        drop=drop.player,
        start_days=start_days,
        usable_days=usable,
        slot_by_day=slot_by_day,
        raw_games=len(start_days),
        light_days=light,
        delta_expected_cats=delta,
        per_cat_delta=per_cat,
        adds_left_after=adds_left_after,
        objective=scoring.objective,
        delta_objective=delta_objective,
        drop_ros_value=drop_ros,
        add_ros_value=scoring.ros_values.get(add.key),
        ros_cost_flag=drop_ros is not None and drop_ros > scoring.ros_threshold,
    )
    return _Swap(option, swapped, new)


def _sort_key(o: StreamerOption) -> tuple[float, str, str]:
    return (-o.score, o.add.key, o.drop.key)


def rank_streamers(
    ctx: WeekContext,
    free_agents: list[FreeAgent],
    pool: list[RosterEntry],
    horizon: Literal["today", "rest_of_week"],
    build: Build,
    light_day_max_games: int = 4,
    max_results: int = 10,
    margin: float = 0.25,
) -> tuple[list[StreamerOption], str | None]:
    """Best (add, drop) pairs for the horizon, scored by the league's objective.

    Categories leagues: build-weighted change in expected categories won. One-win
    leagues: change in matchup win probability, punted categories frozen at their
    current win probability.
    """
    base = analyze_week(ctx)
    if base.adds_left == 0:
        return [], "no adds left this week"
    if not pool:
        return [], "drop pool is empty"
    horizon_days = _horizon_days(ctx, horizon)
    start = effective_start(ctx.settings, ctx.today)
    windows: dict[str, list[date]] = {}
    release: dict[str, date] = {}  # a waiver claim only replaces the drop once it processes
    eligible: list[FreeAgent] = []
    for f in free_agents:
        if f.player.status in OUT_STATUSES:
            continue
        window = _allowed_days(f, horizon, horizon_days)
        if _games(ctx, f.player.nba_team, window):
            windows[f.player.key] = window
            release[f.player.key] = max(start, f.waiver_clear) if f.on_waivers else start
            eligible.append(f)
    candidates = sorted(
        eligible, key=lambda f: (-ctx.my_lineup_values.get(f.player.key, _NEG_INF), f.player.key)
    )[:CANDIDATE_LIMIT]

    scoring = _Scoring(
        weights=category_weights(build, base.categories),
        objective=base.objective,
        frozen={o.category: o.win_prob for o in base.categories if o.category in build.punt},
        light_day_max_games=light_day_max_games,
        ros_values=ctx.ros_values,
        ros_threshold=_best_fa_ros(ctx.ros_values, free_agents) + margin,
    )
    adds_left_after = base.adds_left - 1
    swaps = [
        swap
        for f in candidates
        for drop in pool
        if (swap := _evaluate(
            ctx, base, scoring, drop, f.player, windows[f.player.key],
            release[f.player.key], adds_left_after,
        )) is not None
    ]
    if not swaps:
        return [], "no eligible free agent"
    swaps = [s for s in swaps if s.option.delta_objective > _min_gain(scoring.objective)]
    if not swaps:
        return [], "no move improves this week's outlook"
    swaps.sort(key=lambda s: _sort_key(s.option))

    options = [s.option for s in swaps]
    if horizon == "rest_of_week" and adds_left_after >= 1:
        explored: set[str] = set()
        for i, swap in enumerate(swaps):
            if len(explored) == FOLLOW_UP_OPTIONS:
                break
            if swap.option.add.key in explored:
                continue
            explored.add(swap.option.add.key)
            follow_up = _best_follow_up(swap, candidates, windows, scoring, ctx)
            if follow_up is not None:
                options[i] = replace(swap.option, follow_up=follow_up)
    options.sort(key=_sort_key)
    return options[:max_results], None


def plan_adds(
    ctx: WeekContext,
    free_agents: list[FreeAgent],
    pool: list[RosterEntry],
    margin: float = 0.25,
    max_candidates: int = PLAN_CANDIDATES,
    target_cats: int | None = None,
    build: Build | None = None,
    drop_first: frozenset[str] = frozenset(),
) -> AddPlan:
    """Dated plan by beam search over (day, add, drop) moves, scored by the league's objective.

    One-win leagues: matchup win probability. Categories leagues: build-weighted expected
    categories won, as `rank_streamers` ranks them; every category counts in the
    standings, so a matchup already likely won still has categories worth chasing, and
    the adds left are chained over the days (today's best, then a swap for whoever fits
    the next days). With `target_cats` the score is instead P(win at least that many
    categories), for a user who wants a bigger win than the matchup needs (e.g. 6 of 9).

    An add counts from his move day on and the drop leaves only from that day, so a
    player who plays today is not dropped before he has played. A move can drop an
    earlier planned add after his last useful game. Waiver players can be claimed from
    the day they clear. Conservative on purpose: a move must clear the objective's
    minimum gain (lower when the drop would not have started on any day he is gone);
    nothing above replacement level is dropped unless the user named him in
    `drop_first` (player keys to drop first), and a one-win week already PLAN_SAFE is left alone.
    """
    base = analyze_week(ctx)
    weights = category_weights(build or Build(), base.categories)
    objective: Literal["categories", "one_win", "target"] = (
        "target" if target_cats is not None else base.objective
    )
    min_gain, free_gain, spare_gain = (
        (PLAN_MIN_GAIN_CATS, PLAN_FREE_GAIN_CATS, PLAN_SPARE_GAIN_CATS) if objective == "categories"
        else (PLAN_MIN_GAIN, PLAN_FREE_GAIN, PLAN_SPARE_GAIN)
    )

    def score(a: WeekAnalysis) -> float:
        if objective == "target":
            return prob_at_least([o.win_prob for o in a.categories], target_cats or 0)
        if objective == "categories":
            return sum(weights.get(o.category, 0.0) * o.win_prob for o in a.categories)
        return a.matchup_win_prob

    def plan(moves: list[PlannedMove], analysis: WeekAnalysis, reason: str | None) -> AddPlan:
        ordered = sorted(moves, key=lambda m: (m.on, m.add.key))
        left = base.adds_left
        numbered = []
        for move in ordered:
            left -= 1
            numbered.append(replace(move, adds_left_after=left))
        return AddPlan(
            base_win_prob=base.matchup_win_prob,
            base_expected_cats=base.expected_cats_won,
            matchup_win_prob=analysis.matchup_win_prob,
            expected_cats_won=analysis.expected_cats_won,
            adds_left=base.adds_left,
            adds_used=len(moves),
            moves=numbered,
            reason=reason,
            target_cats=target_cats,
            base_target_prob=None if target_cats is None else score(base),
            target_prob=None if target_cats is None else score(analysis),
            objective=objective,
        )

    if base.adds_left == 0:
        return plan([], base, "no adds left this week")
    if objective == "one_win" and base.matchup_win_prob >= PLAN_SAFE:
        return plan([], base, f"already {base.matchup_win_prob:.0%} to win: no move is worth a drop")
    if not pool:
        return plan([], base, "drop pool is empty")
    days = _horizon_days(ctx, "rest_of_week")
    threshold = _best_fa_ros(ctx.ros_values, free_agents) + margin
    eligible = sorted(
        (f for f in free_agents if f.player.status not in OUT_STATUSES and (not f.on_waivers or f.waiver_clear)),
        key=lambda f: (-ctx.my_lineup_values.get(f.player.key, _NEG_INF), f.player.key),
    )
    # Candidates are ranked by what they would actually add to this lineup on the day they
    # would arrive (against each drop in the pool), not by overall value: a player with a
    # better schedule beats a better player with fewer games left. Anyone who adds nothing
    # is left out.
    scored: list[tuple[float, str, FreeAgent]] = []
    for f in eligible:
        first = max(days[0], f.waiver_clear) if f.on_waivers else days[0]
        best_gain = 0.0
        for day in (d for d in days if d >= first and _games(ctx, f.player.nba_team, [x for x in days if x >= d])):
            window = [d for d in days if d >= day]
            for drop in pool:
                swapped = _swap(ctx, drop, f.player, set(window), day)
                if _usable_slots(swapped, f.player.key, window):
                    best_gain = max(best_gain, score(analyze_week(swapped)) - score(base))
        if best_gain > 0:
            scored.append((best_gain, f.player.key, f))
    scored.sort(key=lambda t: (-t[0], t[1]))
    candidates = [f for _, _, f in scored[:max_candidates]]

    def expand(state: _PlanState) -> list[_PlanState]:
        """The best few next moves from `state`, each as a new state."""
        starting = lineup_by_day(
            state.current.my_roster, state.current.per_game, state.current.my_lineup_values,
            state.current.schedule, days, state.current.settings.roster_slots,
            active_days=state.current.active_days,
        )
        options = []
        for f in state.candidates:
            first = max(days[0], f.waiver_clear) if f.on_waivers else days[0]
            for day in (d for d in days if d >= first and _games(ctx, f.player.nba_team, [x for x in days if x >= d])):
                window = [d for d in days if d >= day]
                for drop in state.droppable:
                    if state.added_on.get(drop.player.key, date.min) >= day:
                        continue  # an earlier add leaves only after the day he arrives
                    if _ros(ctx.ros_values, drop.player.key) > threshold and drop.player.key not in drop_first:
                        continue  # above replacement level: never a stream's price, unless the user chose him
                    swapped = _swap(state.current, drop, f.player, set(window), day)
                    if not _usable_slots(swapped, f.player.key, window):
                        continue
                    new = analyze_week(swapped)
                    gain = score(new) - score(state.analysis)
                    gives_up = any(drop.player.key in starting[d] for d in window)
                    if drop.player.key in state.added_on:
                        bar = spare_gain  # this week's streamer: nothing lost past the week
                    else:
                        bar = min_gain if gives_up else free_gain
                    if gain < bar:
                        continue  # not worth what the drop costs
                    options.append(((score(new), day.toordinal()), new, swapped, f, drop, day, starting))
        options.sort(key=lambda o: o[0], reverse=True)  # later wins a tie: more information
        # One day per (add, drop): the same swap made on four different days would fill
        # the beam with one idea and crowd out a chain (today's add, a swap later).
        best_day: dict[tuple[str, str], tuple] = {}
        for o in options:
            best_day.setdefault((o[3].player.key, o[4].player.key), o)
        out = []
        for _, new, swapped, f, drop, day, starting in list(best_day.values())[:PLAN_EXPAND]:
            window = [d for d in days if d >= day]
            drop_ros = ctx.ros_values.get(drop.player.key)
            move = PlannedMove(
                on=day,
                add=f.player,
                drop=drop.player,
                usable_days=sorted(_usable_slots(swapped, f.player.key, window)),
                slot_by_day=_usable_slots(swapped, f.player.key, window),
                drop_days_given_up=[d for d in window if drop.player.key in starting[d]],
                gain=score(new) - score(state.analysis),
                adds_left_after=0,
                ros_cost_flag=drop_ros is not None and drop_ros > threshold,
                claim=f.on_waivers,
            )
            out.append(
                _PlanState(
                    analysis=new,
                    current=swapped,
                    candidates=tuple(c for c in state.candidates if c.player.key != f.player.key),
                    # a later move may replace this add after his last useful game
                    droppable=(*(e for e in state.droppable if e.player.key != drop.player.key), RosterEntry(f.player, "BN")),
                    added_on={**state.added_on, f.player.key: day},
                    moves=(*state.moves, move),
                )
            )
        return out

    # Beam search: the one best move now is not always the start of the best plan (the best
    # player for the whole week can crowd out "today's best, then a better Fri/Sun fit").
    start = _PlanState(base, ctx, tuple(candidates), tuple(pool), {}, ())
    beam, finished = [start], [start]
    for _ in range(base.adds_left):
        nxt: dict[frozenset, _PlanState] = {}
        for state in beam:
            for new_state in expand(state):
                nxt.setdefault(new_state.key, new_state)
        if not nxt:
            break
        beam = sorted(nxt.values(), key=lambda st: -score(st.analysis))[:PLAN_BEAM]
        finished += beam
    best = max(finished, key=lambda st: (round(score(st.analysis), 6), -len(st.moves)))
    return plan(list(best.moves), best.analysis, None if best.moves else "no move improves this week's outlook")


def _best_follow_up(
    first: _Swap,
    candidates: list[FreeAgent],
    windows: dict[str, list[date]],
    scoring: _Scoring,
    ctx: WeekContext,
) -> StreamerOption | None:
    """Best positive second stream replacing the first add after his last usable day."""
    first_add = first.option.add
    last_day = first.option.usable_days[-1]
    first_entry = next(e for e in first.ctx.my_roster if e.player.key == first_add.key)
    late_windows = {
        f.player.key: [d for d in windows[f.player.key] if d > last_day] for f in candidates
    }
    others = [
        f for f in candidates
        if f.player.key != first_add.key and _games(ctx, f.player.nba_team, late_windows[f.player.key])
    ][:FOLLOW_UP_CANDIDATES]
    follow_ups = [
        swap.option
        for f in others
        if (swap := _evaluate(
            first.ctx, first.analysis, scoring, first_entry, f.player,
            late_windows[f.player.key], last_day + timedelta(days=1),
            first.option.adds_left_after - 1,
        )) is not None and swap.option.delta_objective > _min_gain(scoring.objective)
    ]
    return min(follow_ups, key=_sort_key, default=None)


def long_term_upgrades(
    ctx: WeekContext,
    free_agents: list[FreeAgent],
    pool: list[RosterEntry],
    ros_cat_values: dict[str, dict[Category, float]],
    build: Build,
    margin: float,
    max_results: int = 5,
    ros_games: dict[str, float] | None = None,
) -> list[LongTermUpgrade]:
    """Free agents whose build-weighted ROS value beats the weakest pool player's by ≥ `margin`.

    Players without ROS category values are skipped: unknown is not the same as zero.
    Players with no ROS games remaining (`ros_games` == 0, e.g. out for the season) are
    skipped too; a missing entry is unknown and kept.
    """
    ros_games = ros_games or {}
    def weighted(key: str) -> float:
        cats = ros_cat_values[key]
        return sum(cats.get(c, 0.0) for c in NINE_CAT if c not in build.punt)

    known_pool = [e for e in pool if e.player.key in ros_cat_values]
    if not known_pool:
        return []
    weakest = min(known_pool, key=lambda e: (weighted(e.player.key), e.player.key))
    floor = weighted(weakest.player.key)
    week_days = _horizon_days(ctx, "rest_of_week")
    upgrades = []
    for f in free_agents:
        add = f.player
        if f.on_waivers or add.status in OUT_STATUSES or add.key not in ros_cat_values:
            continue
        if ros_games.get(add.key) == 0:
            continue
        gain = weighted(add.key) - floor
        if gain < margin:
            continue
        add_cats, drop_cats = ros_cat_values[add.key], ros_cat_values[weakest.player.key]
        swapped = swap_context(ctx, weakest, add, set(week_days))
        upgrades.append(
            LongTermUpgrade(
                add=add,
                drop=weakest.player,
                ros_value_gain=gain,
                per_cat_ros_delta={c: add_cats.get(c, 0.0) - drop_cats.get(c, 0.0) for c in NINE_CAT},
                usable_days_this_week=sorted(_usable_slots(swapped, add.key, week_days)),
                ros_games=ros_games.get(add.key),
            )
        )
    upgrades.sort(key=lambda u: (-u.ros_value_gain, u.add.key))
    return upgrades[:max_results]
