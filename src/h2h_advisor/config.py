"""User configuration loaded from ``config.toml`` (missing file means defaults)."""

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from h2h_advisor import paths
from h2h_advisor.identity import normalize_name

HOUR = 3600
DAY = 24 * HOUR


def _default_ttl_s() -> dict[str, int]:
    return {
        "settings": 7 * DAY,
        "matchup": 20 * 60,
        "roster": 20 * 60,
        "free_agents": 60 * 60,
        "bbm_day": 3 * HOUR,
        "bbm_week": 12 * HOUR,
        "bbm_ros": 12 * HOUR,
        "espn_minutes": 6 * HOUR,
    }


@dataclass
class Limits:
    yahoo_daily_pages: int = 250
    bbm_daily_pages: int = 80
    delay_min_s: float = 4.0
    delay_max_s: float = 9.0


@dataclass
class LeagueConfig:
    never_drop: list[str] = field(default_factory=list)
    stream_slots: int | None = None
    max_adds: int | None = None
    build: dict[str, list[str]] | None = None  # raw; parsed by analysis.build
    bbm_league_id: str | None = None  # BBM league option value; None: match by name


@dataclass
class Config:
    never_drop: list[str] = field(default_factory=list)
    leagues: dict[str, LeagueConfig] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)
    ttl_s: dict[str, int] = field(default_factory=_default_ttl_s)
    drop_margin: float = 0.25
    aliases: dict[str, str] = field(default_factory=dict)
    light_day_max_games: int = 4
    rotation_mpg: float = 20.0  # recent minutes a night below which nobody counts as a rotation player
    rotation_gain_mpg: float = 3.0  # minutes above BBM's projection that show a role it has not caught up with

    def never_drop_for(self, league_id: str) -> set[str]:
        names = list(self.never_drop)
        league = self.leagues.get(league_id)
        if league is not None:
            names += league.never_drop
        return {normalize_name(n) for n in names}


def load_config(path: Path | None = None) -> Config:
    path = path or paths.config_file()
    if not path.exists():
        return Config()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"invalid TOML in {path}: {e}") from e

    global_section = data.get("global", {})
    defaults = Config()
    leagues = data.get("league", {})
    _check_never_drop(global_section, "[global]", path)
    for league_id, section in leagues.items():
        _check_never_drop(section, f"[league.{league_id}]", path)
    return Config(
        never_drop=list(global_section.get("never_drop", [])),
        leagues={
            league_id: _build(LeagueConfig, section, f"[league.{league_id}]", path)
            for league_id, section in leagues.items()
        },
        limits=_build(Limits, data.get("limits", {}), "[limits]", path),
        ttl_s={**defaults.ttl_s, **data.get("ttl_s", {})},
        drop_margin=global_section.get("drop_margin", defaults.drop_margin),
        aliases=dict(data.get("aliases", {})),
        light_day_max_games=global_section.get("light_day_max_games", defaults.light_day_max_games),
        rotation_mpg=global_section.get("rotation_mpg", defaults.rotation_mpg),
        rotation_gain_mpg=global_section.get("rotation_gain_mpg", defaults.rotation_gain_mpg),
    )


def _build[T](cls: type[T], section: dict, where: str, path: Path) -> T:
    """``cls(**section)``, with a key ``cls`` does not have reported as a ValueError."""
    known = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    unknown = sorted(set(section) - known)
    if unknown:
        raise ValueError(
            f"{where} has unknown key(s) {', '.join(unknown)} in {path}; "
            f"known keys: {', '.join(sorted(known))}"
        )
    return cls(**section)


def _check_never_drop(section: dict, where: str, path: Path) -> None:
    if isinstance(section.get("never_drop"), str):
        raise ValueError(f"{where} never_drop must be a list of names, not a string, in {path}")  # noqa: TRY004
