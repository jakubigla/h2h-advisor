from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date
from enum import StrEnum
from typing import Literal


class Category(StrEnum):
    FG_PCT = "FG_PCT"
    FT_PCT = "FT_PCT"
    TPM = "TPM"
    PTS = "PTS"
    REB = "REB"
    AST = "AST"
    STL = "STL"
    BLK = "BLK"
    TO = "TO"


NINE_CAT: tuple[Category, ...] = (
    Category.FG_PCT,
    Category.FT_PCT,
    Category.TPM,
    Category.PTS,
    Category.REB,
    Category.AST,
    Category.STL,
    Category.BLK,
    Category.TO,
)


@dataclass(frozen=True)
class StatLine:
    fgm: float = 0.0
    fga: float = 0.0
    ftm: float = 0.0
    fta: float = 0.0
    tpm: float = 0.0
    pts: float = 0.0
    reb: float = 0.0
    ast: float = 0.0
    stl: float = 0.0
    blk: float = 0.0
    to: float = 0.0

    def __add__(self, other: StatLine) -> StatLine:
        if not isinstance(other, StatLine):
            return NotImplemented
        return StatLine(**{f.name: getattr(self, f.name) + getattr(other, f.name) for f in fields(self)})

    def scale(self, k: float) -> StatLine:
        return StatLine(**{f.name: getattr(self, f.name) * k for f in fields(self)})


@dataclass(frozen=True)
class Player:
    key: str
    name: str
    nba_team: str
    positions: tuple[str, ...]
    status: str | None = None
    yahoo_id: str | None = None


@dataclass(frozen=True)
class RosterEntry:
    player: Player
    slot: str  # PG, SG, G, SF, PF, F, C, Util, BN, IL, IL+


@dataclass(frozen=True)
class LeagueSettings:
    league_id: str
    name: str
    categories: tuple[Category, ...]
    roster_slots: dict[str, int]
    max_adds_per_week: int
    week_start: date
    week_end: date
    current_week: int
    lineup_effective: Literal["today", "tomorrow"]
    scoring_type: Literal["categories", "one_win"] = "categories"
    waiver_type: str | None = None  # raw "Waiver Type:" text
    faab: bool = False  # waivers are claimed with a FAB (free agent budget)


@dataclass(frozen=True)
class Matchup:
    league_id: str
    week: int
    opponent_team_id: str
    opponent_name: str
    my_totals: StatLine
    opp_totals: StatLine
    moves_used: int
    my_games_played: int | None = None
    my_games_total: int | None = None
    opp_games_played: int | None = None
    opp_games_total: int | None = None
    my_games_remaining: int | None = None
    opp_games_remaining: int | None = None
    cats_won: int | None = None
    cats_lost: int | None = None


@dataclass(frozen=True)
class FreeAgent:
    player: Player
    on_waivers: bool
    waiver_clear: date | None = None


@dataclass(frozen=True)
class Projection:
    player_key: str
    name: str
    nba_team: str
    per_game: StatLine
    value: float | None
    cat_values: dict[Category, float] = field(default_factory=dict)
    minutes: float | None = None  # projected minutes per game, where the page has the column
