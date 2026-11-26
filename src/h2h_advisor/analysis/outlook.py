"""Category win probability and week outlook for the current H2H matchup."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from math import sqrt
from statistics import NormalDist
from typing import Literal

from h2h_advisor.analysis.categories import better, category_value
from h2h_advisor.analysis.projection import lineup_by_day, project_week
from h2h_advisor.models import (
    Category,
    LeagueSettings,
    Matchup,
    RosterEntry,
    StatLine,
)
from h2h_advisor.sources.schedule import Schedule

# Per-player-game standard deviation of each counting category, for a
# league-typical player (TYPICAL_PER_GAME below).
SD_PER_PLAYER_GAME: dict[Category, float] = {
    Category.PTS: 7.0,
    Category.REB: 3.0,
    Category.AST: 2.2,
    Category.STL: 0.9,
    Category.BLK: 0.8,
    Category.TPM: 1.3,
    Category.TO: 1.3,
}

# Per-game mean of a league-typical rostered player. A player-game adds variance
# in proportion to the player's own mean relative to this (variance grows with
# the mean, as for counts), so a player who produces nothing in a category adds
# no uncertainty to it.
TYPICAL_PER_GAME: dict[Category, float] = {
    Category.PTS: 15.0,
    Category.REB: 5.5,
    Category.AST: 3.5,
    Category.STL: 1.0,
    Category.BLK: 0.6,
    Category.TPM: 1.8,
    Category.TO: 1.8,
}

LIKELY_WIN = 0.90
LIKELY_LOSS = 0.10

Status = Literal["locked_win", "locked_loss", "likely_win", "likely_loss", "swing", "tied"]

_PERCENTAGE_ATTEMPTS = {
    Category.FG_PCT: ("fgm", "fga"),
    Category.FT_PCT: ("ftm", "fta"),
}
_NORMAL = NormalDist()


@dataclass(frozen=True)
class CatOutlook:
    category: Category
    my_now: float
    opp_now: float
    my_proj: float
    opp_proj: float
    win_prob: float
    status: Status


@dataclass(frozen=True)
class WeekContext:
    settings: LeagueSettings
    matchup: Matchup
    my_roster: list[RosterEntry]
    opp_roster: list[RosterEntry]
    per_game: dict[str, StatLine]
    values: dict[str, float]
    ros_values: dict[str, float]
    schedule: Schedule
    today: date
    active_days: dict[str, set[date]] | None = None
    # Lineup values for MY roster only, weighted by my build; the opponent's lineup is
    # chosen on `values` (we do not know, or share, their build).
    my_values: dict[str, float] | None = None

    @property
    def my_lineup_values(self) -> dict[str, float]:
        return self.values if self.my_values is None else self.my_values


@dataclass(frozen=True)
class WeekAnalysis:
    categories: list[CatOutlook]
    expected_cats_won: float
    my_proj: StatLine
    opp_proj: StatLine
    adds_left: int
    days_left: list[date]
    matchup_win_prob: float  # P(win the matchup) from all categories, whatever the scoring type
    objective: Literal["categories", "one_win"]  # the league's scoring type


def remaining_days(ctx: WeekContext) -> list[date]:
    """Days from max(today, week_start) to week_end inclusive; empty once the week is over."""
    start = max(ctx.today, ctx.settings.week_start)
    end = ctx.settings.week_end
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _pct_sd(c: Category, my_proj: StatLine, opp_proj: StatLine, my_now: StatLine, opp_now: StatLine) -> float:
    makes, attempts = _PERCENTAGE_ATTEMPTS[c]
    total_attempts = getattr(my_proj, attempts) + getattr(opp_proj, attempts)
    p = (getattr(my_proj, makes) + getattr(opp_proj, makes)) / total_attempts if total_attempts else 0.5
    # SD of each side's final pct is sqrt(r * p * (1 - p)) / A: only the r attempts
    # still to come are uncertain, out of A projected attempts in total.
    variance = 0.0
    for proj, now in ((my_proj, my_now), (opp_proj, opp_now)):
        projected = getattr(proj, attempts)
        remaining = max(0.0, projected - getattr(now, attempts))
        if projected > 0 and remaining > 0:
            variance += remaining * p * (1 - p) / projected**2
    return sqrt(variance)


def category_win_prob(
    c: Category,
    my_proj: StatLine,
    opp_proj: StatLine,
    remaining_player_games: float,
    my_now: StatLine | None = None,
    opp_now: StatLine | None = None,
    variance: float | None = None,
) -> float:
    """Probability that I win category `c`, treating the projected margin as normal.

    `my_now`/`opp_now` (current totals) give the attempts still to come for the
    percentage categories; omitted, all projected attempts count as remaining.
    `variance` (counting categories) is the margin's variance from the player-games
    still to come (see `counting_variance`); omitted, each of the
    `remaining_player_games` counts as a league-typical player's.
    """
    mine, theirs = category_value(my_proj, c), category_value(opp_proj, c)
    margin = mine - theirs
    if c is Category.TO:
        margin = -margin
    if c in _PERCENTAGE_ATTEMPTS:
        sd = _pct_sd(c, my_proj, opp_proj, my_now or StatLine(), opp_now or StatLine())
    else:
        if variance is None:
            variance = SD_PER_PLAYER_GAME[c] ** 2 * max(0.0, remaining_player_games)
        sd = sqrt(max(0.0, variance))
    if sd == 0:
        return {1: 1.0, 0: 0.5, -1: 0.0}[better(c, mine, theirs)]
    return _NORMAL.cdf(margin / sd)


def _wins_distribution(win_probs: Sequence[float]) -> list[float]:
    """dist[w] = P(exactly w categories won) over independent categories (Poisson-binomial)."""
    dist = [1.0]
    for p in win_probs:
        p = min(1.0, max(0.0, p))
        nxt = [0.0] * (len(dist) + 1)
        for w, mass in enumerate(dist):
            nxt[w] += mass * (1 - p)
            nxt[w + 1] += mass * p
        dist = nxt
    return dist


def matchup_win_prob(win_probs: Sequence[float]) -> float:
    """P(win the matchup) over independent categories, a tie counting half.

    Exact Poisson-binomial: P(W > N - W) + 0.5 * P(W == N - W), where W is the
    number of categories won. Each probability is clamped into [0, 1].
    """
    n = len(win_probs)
    if n == 0:
        return 0.5
    dist = _wins_distribution(win_probs)
    win = sum(mass for w, mass in enumerate(dist) if 2 * w > n)
    tie = dist[n // 2] if n % 2 == 0 else 0.0
    return win + 0.5 * tie


def prob_at_least(win_probs: Sequence[float], k: int) -> float:
    """P(win at least `k` categories), e.g. a 6-3 or better week with k=6."""
    return sum(mass for w, mass in enumerate(_wins_distribution(win_probs)) if w >= k)


def _status(c: Category, win_prob: float, my_proj: float, opp_proj: float, locked: bool) -> Status:
    if locked:
        result = better(c, my_proj, opp_proj)
        return "tied" if result == 0 else "locked_win" if result > 0 else "locked_loss"
    if win_prob >= LIKELY_WIN:
        return "likely_win"
    if win_prob <= LIKELY_LOSS:
        return "likely_loss"
    return "swing"


def _player_games(lineups: dict[date, dict[str, str]]) -> int:
    return sum(len(day) for day in lineups.values())


def counting_variance(c: Category, lineups: dict[date, dict[str, str]], per_game: dict[str, StatLine]) -> float:
    """Variance of a counting category's total over the active player-games.

    Each player-game adds ``SD_PER_PLAYER_GAME[c] ** 2`` scaled by the player's own
    per-game mean relative to ``TYPICAL_PER_GAME[c]``: a zero producer adds none.
    """
    scale = SD_PER_PLAYER_GAME[c] ** 2 / TYPICAL_PER_GAME[c]
    return scale * sum(
        max(0.0, category_value(per_game.get(key, StatLine()), c)) for day in lineups.values() for key in day
    )


def analyze_week(ctx: WeekContext) -> WeekAnalysis:
    settings, matchup = ctx.settings, ctx.matchup
    days = remaining_days(ctx)
    slots = settings.roster_slots

    my_proj = project_week(
        matchup.my_totals, ctx.my_roster, ctx.per_game, ctx.my_lineup_values,
        ctx.schedule, days, slots, active_days=ctx.active_days,
    )
    opp_proj = project_week(
        matchup.opp_totals, ctx.opp_roster, ctx.per_game, ctx.values,
        ctx.schedule, days, slots, active_days=ctx.active_days,
    )
    lineups = [
        lineup_by_day(
            roster, ctx.per_game, values, ctx.schedule, days, slots,
            active_days=ctx.active_days,
        )
        for roster, values in ((ctx.my_roster, ctx.my_lineup_values), (ctx.opp_roster, ctx.values))
    ]
    remaining_player_games = sum(_player_games(lineup) for lineup in lineups)

    outlooks = []
    for c in settings.categories:
        variance = (
            sum(counting_variance(c, lineup, ctx.per_game) for lineup in lineups)
            if c in SD_PER_PLAYER_GAME
            else None
        )
        win_prob = category_win_prob(
            c, my_proj, opp_proj, remaining_player_games,
            my_now=matchup.my_totals, opp_now=matchup.opp_totals, variance=variance,
        )
        my_p, opp_p = category_value(my_proj, c), category_value(opp_proj, c)
        outlooks.append(
            CatOutlook(
                category=c,
                my_now=category_value(matchup.my_totals, c),
                opp_now=category_value(matchup.opp_totals, c),
                my_proj=my_p,
                opp_proj=opp_p,
                win_prob=win_prob,
                status=_status(c, win_prob, my_p, opp_p, locked=remaining_player_games == 0),
            )
        )
    return WeekAnalysis(
        categories=outlooks,
        expected_cats_won=sum(o.win_prob for o in outlooks),
        my_proj=my_proj,
        opp_proj=opp_proj,
        adds_left=max(0, settings.max_adds_per_week - matchup.moves_used),
        days_left=days,
        matchup_win_prob=matchup_win_prob([o.win_prob for o in outlooks]),
        objective=settings.scoring_type,
    )
