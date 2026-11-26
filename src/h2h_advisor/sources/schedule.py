"""The NBA schedule as the engine uses it: teams playing per day.

It is derived from the games table of Basketball Monster's daily pages
(``sources/bbm/parsers.py``); nothing here fetches anything.
"""

from __future__ import annotations

from datetime import date

# NBA tricodes playing on a given US Eastern calendar date
Schedule = dict[date, frozenset[str]]


def games_between(s: Schedule, team: str, start: date, end: date) -> list[date]:
    """Sorted dates in [start, end] (inclusive) on which ``team`` plays."""
    return sorted(
        day for day, teams in s.items() if start <= day <= end and team in teams
    )
