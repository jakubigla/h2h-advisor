from __future__ import annotations

from h2h_advisor.models import Category, StatLine


def category_value(s: StatLine, c: Category) -> float:
    """Value of one category; percentages come from makes/attempts."""
    match c:
        case Category.FG_PCT:
            return s.fgm / s.fga if s.fga else 0.0
        case Category.FT_PCT:
            return s.ftm / s.fta if s.fta else 0.0
        case Category.TPM:
            return s.tpm
        case Category.PTS:
            return s.pts
        case Category.REB:
            return s.reb
        case Category.AST:
            return s.ast
        case Category.STL:
            return s.stl
        case Category.BLK:
            return s.blk
        case Category.TO:
            return s.to


def better(c: Category, a: float, b: float) -> int:
    """1 if a wins the category, -1 if b wins, 0 on a tie. Turnovers: lower wins."""
    if a == b:
        return 0
    a_higher = a > b
    if c is Category.TO:
        return -1 if a_higher else 1
    return 1 if a_higher else -1
