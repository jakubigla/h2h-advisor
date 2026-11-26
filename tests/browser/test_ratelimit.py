from datetime import UTC, datetime, timedelta

import pytest

from h2h_advisor.browser.ratelimit import DailyCapReached, RateLimiter
from h2h_advisor.cache import Cache
from h2h_advisor.config import Limits

T0 = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FixedRng:
    """Stands in for random.Random: uniform() returns a preset delay."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls: list[tuple[float, float]] = []

    def uniform(self, a: float, b: float) -> float:
        self.calls.append((a, b))
        return self.delay


@pytest.fixture
def cache(tmp_path):
    return Cache(tmp_path / "cache.db")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def sleeps():
    return []


def make_limiter(cache, clock, sleeps, *, delay=6.0, limits=None):
    return RateLimiter(
        cache,
        limits or Limits(),
        clock=clock,
        sleep=sleeps.append,
        rng=FixedRng(delay),
    )


def test_first_load_no_sleep(cache, clock, sleeps):
    make_limiter(cache, clock, sleeps).acquire("yahoo")
    assert sleeps == []


def test_delay_drawn_from_configured_range(cache, clock, sleeps):
    rng = FixedRng(6.0)
    limiter = RateLimiter(
        cache,
        Limits(delay_min_s=1.0, delay_max_s=2.0),
        clock=clock,
        sleep=sleeps.append,
        rng=rng,
    )
    limiter.acquire("yahoo")
    assert rng.calls == [(1.0, 2.0)]


def test_sleeps_remaining_gap(cache, clock, sleeps):
    limiter = make_limiter(cache, clock, sleeps, delay=6.0)
    cache.log_request("yahoo", "GET", "u", "load", "", now=T0 - timedelta(seconds=2))
    limiter.acquire("yahoo")
    assert sleeps == [4.0]


def test_no_sleep_when_gap_elapsed(cache, clock, sleeps):
    limiter = make_limiter(cache, clock, sleeps, delay=6.0)
    cache.log_request("yahoo", "GET", "u", "load", "", now=T0 - timedelta(seconds=20))
    limiter.acquire("yahoo")
    assert sleeps == []


def test_gap_is_tracked_per_site(cache, clock, sleeps):
    limiter = make_limiter(cache, clock, sleeps)
    cache.log_request("yahoo", "GET", "u", "load", "", now=T0 - timedelta(seconds=1))
    limiter.acquire("bbm")
    assert sleeps == []


def test_blocked_requests_do_not_count_as_loads(cache, clock, sleeps):
    limiter = make_limiter(cache, clock, sleeps)
    cache.log_request(
        "yahoo", "POST", "u", "block", "action", now=T0 - timedelta(seconds=1)
    )
    limiter.acquire("yahoo")
    assert sleeps == []


def test_clock_skew_counts_as_full_delay(cache, clock, sleeps):
    limiter = make_limiter(cache, clock, sleeps, delay=6.0)
    cache.log_request("yahoo", "GET", "u", "load", "", now=T0 + timedelta(seconds=30))
    limiter.acquire("yahoo")
    assert sleeps == [6.0]


def test_record_load_logs_get_load_at_clock_time(cache, clock, sleeps):
    limiter = make_limiter(cache, clock, sleeps)
    limiter.record_load("yahoo", "https://example.test/page")
    assert cache.page_loads("yahoo", T0.astimezone().date()) == 1
    assert cache.last_load_at("yahoo") == T0
    with cache._connect() as conn:
        row = conn.execute(
            "SELECT method, url, verdict, reason FROM requests"
        ).fetchone()
    assert row == ("GET", "https://example.test/page", "load", "")


def test_record_load_then_acquire_sleeps(cache, clock, sleeps):
    limiter = make_limiter(cache, clock, sleeps, delay=6.0)
    limiter.record_load("bbm", "u")
    clock.now = T0 + timedelta(seconds=1)
    limiter.acquire("bbm")
    assert sleeps == [5.0]


def test_daily_cap_raises(cache, clock, sleeps):
    limiter = make_limiter(cache, clock, sleeps)
    for _ in range(250):
        cache.log_request(
            "yahoo", "GET", "u", "load", "", now=T0 - timedelta(minutes=5)
        )
    with pytest.raises(DailyCapReached) as exc:
        limiter.acquire("yahoo")
    assert exc.value.site == "yahoo"
    assert exc.value.cap == 250
    assert sleeps == []
    limiter.acquire("bbm")  # other site unaffected


def test_below_cap_is_allowed(cache, clock, sleeps):
    limiter = make_limiter(cache, clock, sleeps)
    for _ in range(249):
        cache.log_request(
            "yahoo", "GET", "u", "load", "", now=T0 - timedelta(minutes=5)
        )
    limiter.acquire("yahoo")


def test_bbm_cap_uses_bbm_limit(cache, clock, sleeps):
    limiter = make_limiter(cache, clock, sleeps)
    for _ in range(80):
        cache.log_request("bbm", "GET", "u", "load", "", now=T0 - timedelta(minutes=5))
    with pytest.raises(DailyCapReached) as exc:
        limiter.acquire("bbm")
    assert exc.value.cap == 80


def test_cap_ignores_loads_from_other_days(cache, clock, sleeps):
    limiter = make_limiter(cache, clock, sleeps, limits=Limits(yahoo_daily_pages=2))
    for _ in range(5):
        cache.log_request("yahoo", "GET", "u", "load", "", now=T0 - timedelta(days=2))
    limiter.acquire("yahoo")


def test_unknown_site_rejected(cache, clock, sleeps):
    with pytest.raises(ValueError):
        make_limiter(cache, clock, sleeps).acquire("espn")


def test_cap_shared_across_instances(tmp_path, clock, sleeps):
    path = tmp_path / "shared.db"
    limits = Limits(yahoo_daily_pages=3)
    first = make_limiter(Cache(path), clock, sleeps, limits=limits)
    second = make_limiter(Cache(path), clock, sleeps, limits=limits)
    for i in range(3):
        first.record_load("yahoo", f"u{i}")
    with pytest.raises(DailyCapReached):
        second.acquire("yahoo")
