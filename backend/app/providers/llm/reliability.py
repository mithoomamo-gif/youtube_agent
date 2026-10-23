"""Local-first reliability primitives: token buckets, AIMD, circuit breaker.

One process on one PC, so counters are in-process (asyncio) — no Redis.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque


class TokenBucketRateLimiter:
    """RPM token bucket. A 429 halves the effective rate for 5 minutes
    (AIMD); the rate then recovers gradually back to the configured RPM."""

    def __init__(self, rpm: float = 600.0) -> None:
        self.configured_rpm = float(rpm)
        self._effective_rpm = float(rpm)
        self._halved_until = 0.0
        self._tokens = float(rpm)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def effective_rpm(self) -> float:
        if self._halved_until and time.monotonic() > self._halved_until:
            # gradual recovery: restore half the remaining gap per check
            self._effective_rpm += (self.configured_rpm - self._effective_rpm) / 2
            if self.configured_rpm - self._effective_rpm < 0.5:
                self._effective_rpm = self.configured_rpm
                self._halved_until = 0.0
        return self._effective_rpm

    def on_rate_limited(self) -> None:
        self._effective_rpm = max(1.0, self.effective_rpm / 2)
        self._halved_until = time.monotonic() + 300.0

    async def acquire(self) -> None:
        """Wait (briefly) until one request token is available."""
        while True:
            async with self._lock:
                now = time.monotonic()
                rate = self.effective_rpm / 60.0
                self._tokens = min(
                    self.effective_rpm,
                    self._tokens + (now - self._last) * rate,
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / rate if rate > 0 else 1.0
            await asyncio.sleep(min(wait, 5.0))


class CircuitBreaker:
    """Per (provider, model): 5 failures in 60s -> open 60s -> half-open."""

    def __init__(
        self, threshold: int = 5, window_s: float = 60.0, cooldown_s: float = 60.0
    ) -> None:
        self.threshold = threshold
        self.window_s = window_s
        self.cooldown_s = cooldown_s
        self._failures: deque[float] = deque()
        self._open_until = 0.0

    @property
    def is_open(self) -> bool:
        return time.monotonic() < self._open_until

    def record_failure(self) -> None:
        now = time.monotonic()
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self.window_s:
            self._failures.popleft()
        if len(self._failures) >= self.threshold:
            self._open_until = now + self.cooldown_s

    def record_success(self) -> None:
        self._failures.clear()
        self._open_until = 0.0


class ProviderRuntime:
    """Semaphore + limiter + breaker bundled per provider."""

    def __init__(self, concurrency: int, rpm: float = 600.0) -> None:
        self.semaphore = asyncio.Semaphore(concurrency)
        self.limiter = TokenBucketRateLimiter(rpm)
        self.breakers: dict[str, CircuitBreaker] = {}

    def breaker_for(self, model_id: str) -> CircuitBreaker:
        return self.breakers.setdefault(model_id, CircuitBreaker())
