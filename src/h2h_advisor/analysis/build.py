"""League build (season-long strategy) inference and category weights for streamer scoring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from h2h_advisor.analysis.categories import better, category_value
from h2h_advisor.analysis.outlook import CatOutlook
from h2h_advisor.models import NINE_CAT, Category, Matchup

MIN_WEEKS = 3
STRONG_RATE = 0.75
PUNT_RATE = 0.2
_BUILD_KEYS = frozenset({"strong", "punt", "weak"})
_SAFE_STATUSES = frozenset({"likely_win", "locked_win"})
_AT_RISK_STATUSES = frozenset({"swing", "likely_loss", "locked_loss", "tied"})


@dataclass(frozen=True)
class Build:
    strong: frozenset[Category] = frozenset()
    punt: frozenset[Category] = frozenset()
    weak: frozenset[Category] = frozenset()
    source: Literal["pinned", "inferred", "none"] = "none"


def build_from_config(raw: dict[str, list[str]] | None) -> Build | None:
    """Parse a pinned build from config; names are `Category` values."""
    if not raw:
        return None
    unknown = set(raw) - _BUILD_KEYS
    if unknown:
        raise ValueError(f"unknown build keys {sorted(unknown)}; expected {sorted(_BUILD_KEYS)}")

    def parse(key: str) -> frozenset[Category]:
        return frozenset(Category(name) for name in raw.get(key, []))

    return Build(strong=parse("strong"), punt=parse("punt"), weak=parse("weak"), source="pinned")


def matchup_result(m: Matchup, cats: tuple[Category, ...]) -> dict[Category, int]:
    """Per category: 1 if I won the week, 0 on a tie, -1 if I lost."""
    return {c: better(c, category_value(m.my_totals, c), category_value(m.opp_totals, c)) for c in cats}


def infer_build(roster_cat_values: dict[Category, float], history: list[dict[Category, int]]) -> Build:
    """Infer a build: win rates over past weeks where there are enough, else roster strengths."""
    strong: set[Category] = set()
    punt: set[Category] = set()
    undecided: set[Category] = set()
    for c in NINE_CAT:
        results = [week[c] for week in history if c in week]
        if len(results) < MIN_WEEKS:
            undecided.add(c)
            continue
        rate = sum(1.0 if r > 0 else 0.5 if r == 0 else 0.0 for r in results) / len(results)
        if rate >= STRONG_RATE:
            strong.add(c)
        elif rate <= PUNT_RATE:
            punt.add(c)

    ranked = sorted((c for c in NINE_CAT if c in roster_cat_values), key=lambda c: -roster_cat_values[c])
    strong |= set(ranked[: len(NINE_CAT) // 3]) & undecided
    return Build(strong=frozenset(strong), punt=frozenset(punt), source="inferred")


def resolve_build(pinned: Build | None, inferred: Build) -> Build:
    """A pinned build wins whole; nothing is merged with the inferred one."""
    return pinned if pinned is not None else inferred


def category_weights(build: Build, outlook: list[CatOutlook]) -> dict[Category, float]:
    """0.0 for punted categories and strong ones already safely won this week, else 1.0."""
    weights: dict[Category, float] = {}
    for o in outlook:
        c = o.category
        safe_strong = c in build.strong and o.status in _SAFE_STATUSES
        weights[c] = 0.0 if c in build.punt or safe_strong else 1.0
    return weights


def at_risk_strong(build: Build, outlook: list[CatOutlook]) -> list[Category]:
    """Strong categories not currently safe this week (the weekly override flag)."""
    return [o.category for o in outlook if o.category in build.strong and o.status in _AT_RISK_STATUSES]


def one_win_targets(build: Build, outlook: list[CatOutlook]) -> list[Category]:
    """The majority (N // 2 + 1) of categories to aim at in a one-win league.

    Non-punted categories by win probability, best first (ties by `outlook` order);
    punted ones fill in, best first, only when too few others exist.
    """
    k = len(outlook) // 2 + 1
    ranked = sorted(outlook, key=lambda o: -o.win_prob)  # stable: ties keep outlook order
    chase = [o.category for o in ranked if o.category not in build.punt]
    conceded = [o.category for o in ranked if o.category in build.punt]
    return (chase + conceded)[:k]
