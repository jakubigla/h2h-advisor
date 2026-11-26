"""SQLite cache, raw-page snapshots and the per-site request log.

Every method opens its own short-lived connection, so a ``Cache`` is safe to share
between threads and several ``Cache`` objects (or processes) can use the same file:
WAL journal plus a 5 s busy timeout, and writes take the lock up front.
"""

from __future__ import annotations

import dataclasses
import json
import math
import random
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

SNAPSHOTS_PER_PAGE_TYPE = 5
_BUSY_TIMEOUT_MS = 5000
_INIT_RETRY_S = 5.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    page_type  TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    raw        BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_by_type ON snapshots (page_type, id);
CREATE TABLE IF NOT EXISTS requests (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    site    TEXT NOT NULL,
    method  TEXT NOT NULL,
    url     TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reason  TEXT NOT NULL,
    at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_by_site ON requests (site, verdict, at);
"""


@dataclass
class CachedItem:
    key: str
    value: Any
    fetched_at: datetime


def _utc(moment: datetime) -> datetime:
    """Aware datetimes are converted to UTC; naive ones are taken to already be UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _ts(moment: datetime) -> str:
    # Fixed-width so lexicographic order equals chronological order.
    return _utc(moment).isoformat(timespec="microseconds")


def _from_ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


def jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Enum):
        return jsonable(obj.value)
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None  # JSON has no Infinity/NaN (e.g. an unknown ROS value is -inf)
    if isinstance(obj, dict):
        return {_key(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set | frozenset):
        items = [jsonable(v) for v in obj]
        if isinstance(obj, set | frozenset):
            items.sort(key=repr)  # deterministic output
        return items
    return obj


def _key(key: Any) -> Any:
    key = jsonable(key)
    return key if isinstance(key, str | int | float | bool) or key is None else str(key)


def _raw_bytes(raw: str | bytes) -> bytes:
    return raw.encode("utf-8") if isinstance(raw, str) else raw


class Cache:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def _initialise(self) -> None:
        # The busy handler is not honoured for the initial switch to WAL, so
        # concurrent first opens can raise "database is locked": retry with backoff.
        deadline = time.monotonic() + _INIT_RETRY_S
        delay = 0.005
        while True:
            try:
                with self._connect() as conn:
                    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                    if mode.lower() != "wal":
                        conn.execute(
                            "PRAGMA journal_mode=WAL"
                        )  # persistent in the file
                    conn.executescript(_SCHEMA)
                return
            except sqlite3.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(delay * (0.5 + random.random()))
                delay = min(delay * 2, 0.25)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # isolation_level=None: autocommit; transactions are explicit (see _write).
        with closing(
            sqlite3.connect(
                self._path, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None
            )
        ) as conn:
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            yield conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # -- items ---------------------------------------------------------------

    def get(self, key: str) -> CachedItem | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value, fetched_at FROM items WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return CachedItem(
            key=key, value=json.loads(row[0]), fetched_at=_from_ts(row[1])
        )

    def put(
        self,
        key: str,
        value: Any,
        *,
        raw: str | bytes | None = None,
        page_type: str | None = None,
        now: datetime | None = None,
    ) -> None:
        if raw is not None and page_type is None:
            raise ValueError("page_type is required when raw is given")
        stamp = _ts(now or datetime.now(UTC))
        payload = json.dumps(jsonable(value))
        with self._write() as conn:
            conn.execute(
                "INSERT INTO items (key, value, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "fetched_at = excluded.fetched_at",
                (key, payload, stamp),
            )
            if raw is not None:
                conn.execute(
                    "INSERT INTO snapshots (page_type, fetched_at, raw) VALUES (?, ?, ?)",
                    (page_type, stamp, _raw_bytes(raw)),
                )
                conn.execute(
                    "DELETE FROM snapshots WHERE page_type = ? AND id NOT IN "
                    "(SELECT id FROM snapshots WHERE page_type = ? ORDER BY id DESC LIMIT ?)",
                    (page_type, page_type, SNAPSHOTS_PER_PAGE_TYPE),
                )

    def item_ages(self) -> list[tuple[str, datetime]]:
        """Key and ``fetched_at`` of every cached item, ordered by key."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, fetched_at FROM items ORDER BY key"
            ).fetchall()
        return [(key, _from_ts(at)) for key, at in rows]

    @staticmethod
    def is_fresh(item: CachedItem, ttl_s: int, now: datetime) -> bool:
        return _utc(now) - _utc(item.fetched_at) <= timedelta(seconds=ttl_s)

    def snapshots(self, page_type: str) -> list[tuple[datetime, bytes]]:
        """Retained raw pages for ``page_type`` (at most 5), newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT fetched_at, raw FROM snapshots WHERE page_type = ? ORDER BY id DESC",
                (page_type,),
            ).fetchall()
        return [(_from_ts(at), bytes(raw)) for at, raw in rows]

    # -- request log ---------------------------------------------------------

    def log_request(
        self,
        site: str,
        method: str,
        url: str,
        verdict: str,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO requests (site, method, url, verdict, reason, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (site, method, url, verdict, reason, _ts(now or datetime.now(UTC))),
            )

    def page_loads(self, site: str, day: date) -> int:
        """Number of ``load`` requests whose *local* date (system timezone) is ``day``."""
        # Bracket generously in UTC (offsets are < 1 day), then filter on the local date.
        start = datetime(day.year, day.month, day.day, tzinfo=UTC) - timedelta(days=1)
        end = start + timedelta(days=3)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT at FROM requests WHERE site = ? AND verdict = 'load' "
                "AND at >= ? AND at < ?",
                (site, _ts(start), _ts(end)),
            ).fetchall()
        return sum(1 for (at,) in rows if _from_ts(at).astimezone().date() == day)

    def last_load_at(self, site: str) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(at) FROM requests WHERE site = ? AND verdict = 'load'",
                (site,),
            ).fetchone()
        return _from_ts(row[0]) if row and row[0] else None

    def recent_blocked(self, limit: int = 20) -> list[dict]:
        """Most recent requests whose verdict is ``block``, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT site, method, url, verdict, reason, at FROM requests "
                "WHERE verdict = 'block' ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "site": site,
                "method": method,
                "url": url,
                "verdict": verdict,
                "reason": reason,
                "at": _from_ts(at),
            }
            for site, method, url, verdict, reason, at in rows
        ]
