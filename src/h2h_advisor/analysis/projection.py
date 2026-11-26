"""Week projection: per-day greedy assignment of players to active lineup slots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from h2h_advisor.models import Player, RosterEntry, StatLine
from h2h_advisor.sources.schedule import Schedule

INACTIVE_SLOTS = {"IL", "IL+"}
OUT_STATUSES = {"O", "INJ", "SUSP", "NA"}

_NON_ACTIVE = INACTIVE_SLOTS | {"BN"}
_GUARDS = {"PG", "SG"}
_FORWARDS = {"SF", "PF"}


@dataclass(frozen=True)
class DayPlayer:
    key: str
    positions: tuple[str, ...]
    per_game: StatLine
    value: float


def _slot_preference(positions: tuple[str, ...]) -> list[str]:
    """Slots a player may fill, most specific first: own positions, G/F, Util."""
    prefs = list(positions)
    if _GUARDS & set(positions):
        prefs.append("G")
    if _FORWARDS & set(positions):
        prefs.append("F")
    prefs.append("Util")
    return list(dict.fromkeys(prefs))


def _assign(players: list[DayPlayer], slots: dict[str, int]) -> list[tuple[DayPlayer, str]]:
    open_slots = {s: n for s, n in slots.items() if s not in _NON_ACTIVE and n > 0}
    assigned: list[tuple[DayPlayer, str]] = []
    for player in sorted(players, key=lambda p: (-p.value, p.key)):
        for slot in _slot_preference(player.positions):
            if open_slots.get(slot, 0) > 0:
                open_slots[slot] -= 1
                assigned.append((player, slot))
                break
    return assigned


def assign_active(players: list[DayPlayer], slots: dict[str, int]) -> list[DayPlayer]:
    """Greedy by value: each player takes the most specific open eligible slot."""
    return [player for player, _ in _assign(players, slots)]


def _day_players(
    roster: list[RosterEntry],
    per_game: dict[str, StatLine],
    values: dict[str, float],
    games_today: frozenset[str],
    day: date,
    active_days: dict[str, set[date]] | None,
) -> list[DayPlayer]:
    players = []
    for entry in roster:
        player = entry.player
        if entry.slot in INACTIVE_SLOTS or player.status in OUT_STATUSES:
            continue
        if player.nba_team not in games_today or player.key not in per_game:
            continue
        if active_days is not None and player.key in active_days and day not in active_days[player.key]:
            continue
        players.append(
            DayPlayer(player.key, player.positions, per_game[player.key], values.get(player.key, 0.0))
        )
    return players


def projected_rest(
    roster: list[RosterEntry],
    schedule: Schedule,
    days: list[date],
    active_days: dict[str, set[date]] | None,
) -> list[tuple[Player, list[date]]]:
    """Healthy rostered players BBM projects not to play on days their team does.

    Rest on a back-to-back, a managed workload: BBM's day pages say so (games = 0),
    and the projection already follows them. This only lists them, for display.
    """
    rest = []
    for entry in roster:
        player = entry.player
        if entry.slot in INACTIVE_SLOTS or player.status in OUT_STATUSES:
            continue
        if active_days is None or player.key not in active_days:
            continue
        sitting = [
            d for d in days
            if player.nba_team in schedule.get(d, frozenset()) and d not in active_days[player.key]
        ]
        if sitting:
            rest.append((player, sitting))
    return rest


def _lineup_with_stats(
    roster: list[RosterEntry],
    per_game: dict[str, StatLine],
    values: dict[str, float],
    schedule: Schedule,
    days: list[date],
    slots: dict[str, int],
    active_days: dict[str, set[date]] | None,
) -> dict[date, list[tuple[DayPlayer, str]]]:
    return {
        day: _assign(
            _day_players(roster, per_game, values, schedule.get(day, frozenset()), day, active_days),
            slots,
        )
        for day in days
    }


def lineup_by_day(
    roster: list[RosterEntry],
    per_game: dict[str, StatLine],
    values: dict[str, float],
    schedule: Schedule,
    days: list[date],
    slots: dict[str, int],
    active_days: dict[str, set[date]] | None = None,
) -> dict[date, dict[str, str]]:
    """For each day in `days`, map active player key -> slot filled."""
    by_day = _lineup_with_stats(roster, per_game, values, schedule, days, slots, active_days)
    return {day: {p.key: slot for p, slot in assigned} for day, assigned in by_day.items()}


def project_week(
    current: StatLine,
    roster: list[RosterEntry],
    per_game: dict[str, StatLine],
    values: dict[str, float],
    schedule: Schedule,
    days: list[date],
    slots: dict[str, int],
    active_days: dict[str, set[date]] | None = None,
) -> StatLine:
    """`current` plus per-game stats of every player that starts on each remaining day."""
    by_day = _lineup_with_stats(roster, per_game, values, schedule, days, slots, active_days)
    total = StatLine()
    for assigned in by_day.values():
        for player, _ in assigned:
            total = total + player.per_game
    return current + total
