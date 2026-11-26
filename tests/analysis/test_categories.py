import pytest

from h2h_advisor.analysis.categories import better, category_value
from h2h_advisor.models import NINE_CAT, Category, StatLine


def test_fg_pct_from_makes_attempts():
    s = StatLine(fgm=5, fga=10) + StatLine(fgm=1, fga=10)
    assert category_value(s, Category.FG_PCT) == pytest.approx(0.30)  # not mean(0.5, 0.1)


def test_zero_attempts_is_zero():
    assert category_value(StatLine(), Category.FT_PCT) == 0.0
    assert category_value(StatLine(), Category.FG_PCT) == 0.0


def test_turnovers_lower_wins():
    assert better(Category.TO, 10, 12) == 1 and better(Category.PTS, 10, 12) == -1
    assert better(Category.REB, 5, 5) == 0


def test_turnovers_higher_loses():
    assert better(Category.TO, 12, 10) == -1


def test_scale_returns_new_statline():
    s = StatLine(fgm=2, fga=4, pts=10)
    scaled = s.scale(3)
    assert scaled == StatLine(fgm=6, fga=12, pts=30)
    assert s.pts == 10


def test_counting_stats_read_from_matching_field():
    s = StatLine(ftm=3, fta=4, tpm=2, pts=20, reb=8, ast=5, stl=1, blk=2, to=3)
    assert category_value(s, Category.FT_PCT) == pytest.approx(0.75)
    values = {c: category_value(s, c) for c in NINE_CAT}
    assert values[Category.TPM] == 2
    assert values[Category.PTS] == 20
    assert values[Category.REB] == 8
    assert values[Category.AST] == 5
    assert values[Category.STL] == 1
    assert values[Category.BLK] == 2
    assert values[Category.TO] == 3


def test_nine_cat_order():
    assert [c.value for c in NINE_CAT] == [
        "FG_PCT", "FT_PCT", "TPM", "PTS", "REB", "AST", "STL", "BLK", "TO",
    ]
