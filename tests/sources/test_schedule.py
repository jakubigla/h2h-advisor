from datetime import date

from h2h_advisor.sources.schedule import Schedule, games_between

SCHEDULE: Schedule = {
    date(2026, 10, 21): frozenset({"OKC", "HOU", "LAL", "GSW"}),
    date(2026, 10, 23): frozenset({"GSW", "OKC"}),
    date(2026, 10, 25): frozenset({"HOU", "LAL"}),
}


def test_games_between_inclusive():
    assert games_between(SCHEDULE, "OKC", date(2026, 10, 21), date(2026, 10, 23)) == [
        date(2026, 10, 21),
        date(2026, 10, 23),
    ]
    assert games_between(SCHEDULE, "OKC", date(2026, 10, 22), date(2026, 10, 22)) == []
    assert games_between(SCHEDULE, "HOU", date(2026, 10, 1), date(2026, 10, 31)) == [
        date(2026, 10, 21),
        date(2026, 10, 25),
    ]
