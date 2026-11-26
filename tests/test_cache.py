import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from h2h_advisor.cache import Cache, CachedItem
from h2h_advisor.models import Category

T0 = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def cache(tmp_path):
    return Cache(tmp_path / "sub" / "cache.db")


@dataclass(frozen=True)
class Inner:
    day: date
    cats: frozenset[Category]


@dataclass(frozen=True)
class Outer:
    name: str
    inner: Inner
    stamps: tuple[datetime, ...]
    by_cat: dict[Category, float]


def test_put_get_roundtrip_dataclass(cache):
    value = Outer(
        name="Jokic",
        inner=Inner(day=date(2026, 11, 25), cats=frozenset({Category.PTS})),
        stamps=(T0,),
        by_cat={Category.AST: 1.5},
    )
    cache.put("k", value, now=T0)
    item = cache.get("k")
    assert item == CachedItem(
        key="k",
        value={
            "name": "Jokic",
            "inner": {"day": "2026-11-25", "cats": ["PTS"]},
            "stamps": [T0.isoformat()],
            "by_cat": {"AST": 1.5},
        },
        fetched_at=T0,
    )
    assert item.fetched_at.tzinfo is not None


def test_get_missing_returns_none(cache):
    assert cache.get("nope") is None


def test_put_overwrites(cache):
    cache.put("k", {"a": 1}, now=T0)
    cache.put("k", [1, 2], now=T0 + timedelta(seconds=5))
    item = cache.get("k")
    assert item.value == [1, 2]
    assert item.fetched_at == T0 + timedelta(seconds=5)


def test_item_ages_lists_keys_and_fetched_at(cache):
    assert cache.item_ages() == []
    cache.put("b", 1, now=T0)
    cache.put("a", 2, now=T0 + timedelta(seconds=5))
    assert cache.item_ages() == [("a", T0 + timedelta(seconds=5)), ("b", T0)]


def test_is_fresh_respects_ttl(cache):
    cache.put("k", {"a": 1}, now=T0)
    item = cache.get("k")
    assert cache.is_fresh(item, 1200, T0 + timedelta(seconds=1199))
    assert not cache.is_fresh(item, 1200, T0 + timedelta(seconds=1201))


def test_snapshots_keep_last_five(cache):
    for i in range(7):
        cache.put(
            "k",
            {"i": i},
            raw=f"page{i}",
            page_type="matchup",
            now=T0 + timedelta(minutes=i),
        )
    snaps = cache.snapshots("matchup")
    assert [raw for _, raw in snaps] == [f"page{i}".encode() for i in (6, 5, 4, 3, 2)]
    assert snaps[0][0] == T0 + timedelta(minutes=6)


def test_snapshot_retention_is_per_page_type(cache):
    for i in range(6):
        cache.put(
            "a", i, raw=b"a%d" % i, page_type="matchup", now=T0 + timedelta(minutes=i)
        )
    cache.put("b", 0, raw=b"b0", page_type="roster", now=T0)
    assert len(cache.snapshots("matchup")) == 5
    assert [raw for _, raw in cache.snapshots("roster")] == [b"b0"]
    assert cache.snapshots("unknown") == []


def test_put_without_raw_stores_no_snapshot(cache):
    cache.put("k", 1, page_type="matchup", now=T0)
    assert cache.snapshots("matchup") == []


def test_raw_requires_page_type(cache):
    with pytest.raises(ValueError):
        cache.put("k", 1, raw="x", now=T0)
    assert cache.get("k") is None


def test_page_loads_counts_per_site_per_day(cache):
    now = datetime.now(UTC)
    today = now.astimezone().date()
    for _ in range(3):
        cache.log_request("yahoo", "GET", "https://y/x", "load", "ok", now=now)
    cache.log_request(
        "yahoo", "GET", "https://y/x", "load", "ok", now=now - timedelta(days=1)
    )
    cache.log_request("yahoo", "POST", "https://y/drop", "block", "action url", now=now)
    for _ in range(2):
        cache.log_request("bbm", "GET", "https://b/x", "load", "ok", now=now)
    assert cache.page_loads("yahoo", today) == 3
    assert cache.page_loads("yahoo", today - timedelta(days=1)) == 1
    assert cache.page_loads("bbm", today) == 2
    assert cache.page_loads("bbm", today + timedelta(days=1)) == 0


def test_page_loads_uses_local_date(cache, monkeypatch):
    monkeypatch.setenv("TZ", "Pacific/Auckland")  # UTC+13 in November
    time.tzset()
    try:
        # 2026-11-25 20:00 UTC is 2026-11-26 09:00 in Auckland.
        cache.log_request(
            "yahoo",
            "GET",
            "u",
            "load",
            "ok",
            now=datetime(2026, 11, 25, 20, 0, tzinfo=UTC),
        )
        assert cache.page_loads("yahoo", date(2026, 11, 26)) == 1
        assert cache.page_loads("yahoo", date(2026, 11, 25)) == 0
    finally:
        monkeypatch.undo()
        time.tzset()


def test_last_load_at(cache):
    assert cache.last_load_at("yahoo") is None
    cache.log_request("yahoo", "GET", "u", "load", "ok", now=T0)
    cache.log_request("yahoo", "GET", "u", "load", "ok", now=T0 + timedelta(seconds=7))
    cache.log_request("yahoo", "GET", "u", "block", "x", now=T0 + timedelta(seconds=99))
    cache.log_request("bbm", "GET", "u", "load", "ok", now=T0 + timedelta(seconds=200))
    assert cache.last_load_at("yahoo") == T0 + timedelta(seconds=7)


def test_recent_blocked_newest_first_and_limited(cache):
    cache.log_request("yahoo", "GET", "u0", "load", "ok", now=T0)
    for i in range(3):
        cache.log_request(
            "yahoo", "POST", f"u{i}", "block", f"r{i}", now=T0 + timedelta(seconds=i)
        )
    blocked = cache.recent_blocked(limit=2)
    assert [b["url"] for b in blocked] == ["u2", "u1"]
    assert blocked[0] == {
        "site": "yahoo",
        "method": "POST",
        "url": "u2",
        "verdict": "block",
        "reason": "r2",
        "at": T0 + timedelta(seconds=2),
    }


def test_two_connections_share_counts(tmp_path):
    path = tmp_path / "cache.db"
    a, b = Cache(path), Cache(path)
    now = datetime.now(UTC)
    today = now.astimezone().date()
    a.log_request("yahoo", "GET", "u", "load", "ok", now=now)
    b.log_request("yahoo", "GET", "u", "load", "ok", now=now)
    assert a.page_loads("yahoo", today) == 2
    assert b.page_loads("yahoo", today) == 2
    a.put("k", {"x": 1}, now=T0)
    assert b.get("k").value == {"x": 1}


def test_wal_mode_enabled(cache):
    with cache._connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_concurrent_writers_do_not_lock(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "cache.db"
    now = datetime.now(UTC)

    def work(n: int) -> None:
        c = Cache(path)
        for i in range(20):
            c.log_request("yahoo", "GET", f"u{n}-{i}", "load", "ok", now=now)
            c.put(f"k{n}", i, raw=b"x", page_type="p", now=now)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(work, range(4)))
    assert Cache(path).page_loads("yahoo", now.astimezone().date()) == 80
    assert len(Cache(path).snapshots("p")) == 5


def _open_fresh(args: tuple[str, float]) -> str:
    path, start_at = args
    while time.time() < start_at:  # line the processes up
        pass
    try:
        Cache(Path(path))
    except Exception as exc:  # noqa: BLE001
        return repr(exc)
    return ""


def test_concurrent_first_open_from_processes(tmp_path):
    import multiprocessing

    workers = 6
    with multiprocessing.get_context("spawn").Pool(workers) as pool:
        for round_no in range(8):
            path = str(tmp_path / f"fresh{round_no}" / "cache.db")
            start_at = time.time() + 0.3
            errors = [
                e for e in pool.map(_open_fresh, [(path, start_at)] * workers, 1) if e
            ]
            assert errors == []


def test_jsonable_turns_non_finite_floats_into_none():
    from h2h_advisor.cache import jsonable

    @dataclass
    class Values:
        ros: float
        best: float
        missing: float
        normal: float

    out = jsonable(Values(float("-inf"), float("inf"), float("nan"), 1.5))
    assert out == {"ros": None, "best": None, "missing": None, "normal": 1.5}
    assert jsonable({"a": [float("inf"), 2]}) == {"a": [None, 2]}
