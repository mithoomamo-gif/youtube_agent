"""Sliding-window rate limiter (per adapter, token-bucket equivalent)."""
from __future__ import annotations

import asyncio
import time
from collections import deque


class RateLimitExceeded(Exception):
    pass


class SlidingWindowRateLimiter:
    """Allow at most ``limit`` calls in ``window_s`` seconds.

    ``acquire`` waits (async) until a slot frees; tests use high limits or
    an injected monotonic clock so nothing ever actually sleeps.
    """

    def __init__(self, limit: int, window_s: float, *, clock=time.monotonic):
        self.limit = int(limit)
        self.window_s = float(window_s)
        self._clock = clock
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        while self._timestamps and now - self._timestamps[0] >= self.window_s:
            self._timestamps.popleft()

    @property
    def used(self) -> int:
        self._prune(self._clock())
        return len(self._timestamps)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def try_acquire(self) -> bool:
        now = self._clock()
        self._prune(now)
        if len(self._timestamps) >= self.limit:
            return False
        self._timestamps.append(now)
        return True

    async def acquire(self) -> None:
        async with self._lock:
            if self.try_acquire():
                return
            # Window is full: wait for the oldest entry to slide out.
            now = self._clock()
            wait = max(0.01, self.window_s - (now - self._timestamps[0]))
        await asyncio.sleep(wait)
        async with self._lock:
            if not self.try_acquire():
                raise RateLimitExceeded(
                    f"rate limit {self.limit}/{self.window_s}s still exhausted"
                )
