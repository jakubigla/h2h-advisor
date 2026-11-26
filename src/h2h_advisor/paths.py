"""Filesystem locations. Everything hangs off ``home()`` so tests can redirect it."""

import os
from pathlib import Path


def home() -> Path:
    override = os.environ.get("H2H_ADVISOR_HOME")
    return Path(override) if override else Path.home()


def _data_dir() -> Path:
    return home() / ".local" / "share" / "h2h-advisor"


def profile_dir() -> Path:
    return _data_dir() / "chrome-profile"


def cache_db() -> Path:
    return _data_dir() / "cache.db"


def notes_file() -> Path:
    return _data_dir() / "notes.json"


def config_file() -> Path:
    return home() / ".config" / "h2h-advisor" / "config.toml"
