"""In-process sliding-window rate limits (per instance; AWS WAF adds a global limit in 10.4)."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Callable

# (limit, window seconds) per bucket
LIMITS: dict[str, tuple[int, float]] = {
    "signup:ip": (10, 3600),
    "login:ip": (30, 600),
    "login:email": (10, 600),
    "code:email": (5, 900),            # sending verification/reset codes
    "verify:ip": (30, 600),
    "google:ip": (30, 600),
    "refresh:ip": (120, 600),
    "integration:ip": (30, 600),       # OAuth token broker (platform connections)
}


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic, limits: dict[str, tuple[int, float]] | None = None):
        self._clock = clock
        self._limits = limits or LIMITS
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, bucket: str, key: str) -> float:
        """Record an attempt; returns 0 if allowed, else seconds until the next one is."""
        limit, window = self._limits[bucket]
        now = self._clock()
        with self._lock:
            q = self._hits[(bucket, key)]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return max(1.0, q[0] + window - now)
            q.append(now)
            if len(self._hits) > 100_000:              # bound memory under abuse
                self._hits = defaultdict(deque, {k: v for k, v in self._hits.items() if v})
            return 0.0
