"""Per-site politeness: daily page-load caps and a randomised gap between loads.

State lives in the SQLite request log (``Cache``), so caps and gaps hold across
limiter instances, server restarts and concurrent processes sharing one cache file.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import UTC, datetime

from h2h_advisor.cache import Cache
from h2h_advisor.config import Limits


class DailyCapReached(Exception):
    def __init__(self, site: str, cap: int) -> None:
        super().__init__(f"daily page-load cap reached for {site} ({cap})")
        self.site = site
        self.cap = cap


def _now() -> datetime:
    return datetime.now(UTC)


class RateLimiter:
    def __init__(
        self,
        cache: Cache,
        limits: Limits,
        *,
        clock: Callable[[], datetime] = _now,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._cache = cache
        self._limits = limits
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()

    def _cap(self, site: str) -> int:
        if site == "yahoo":
            return self._limits.yahoo_daily_pages
        if site == "bbm":
            return self._limits.bbm_daily_pages
        raise ValueError(f"unknown site: {site!r}")

    def acquire(self, site: str) -> None:
        """Block until ``site`` may load another page; raise if today's cap is spent."""
        cap = self._cap(site)
        now = self._clock()
        if self._cache.page_loads(site, now.astimezone().date()) >= cap:
            raise DailyCapReached(site, cap)
        delay = self._rng.uniform(self._limits.delay_min_s, self._limits.delay_max_s)
        last = self._cache.last_load_at(site)
        # Clock skew (last load in the future) counts as no time elapsed.
        elapsed = max(0.0, (now - last).total_seconds()) if last else delay
        remaining = delay - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def record_load(self, site: str, url: str) -> None:
        self._cache.log_request(site, "GET", url, "load", "", now=self._clock())
