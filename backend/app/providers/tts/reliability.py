"""Reliability primitives: token-bucket parallelism, retries with backoff +
jitter honouring Retry-After, and a per-provider circuit breaker.

Spec (roadmap section 30):
* retries: 3 attempts, backoff 2s -> 8s -> 30s + jitter, honour Retry-After,
  then escalate to the fallback chain
* circuit breaker: 3 consecutive chunk failures -> provider open 15 minutes
* parallelism: per-provider token bucket (edge-tts 4, Azure 8,
  ElevenLabs per-plan default 4)
"""
from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .base import CircuitOpen, ProviderError, ProviderUnavailable

T = TypeVar("T")

DEFAULT_RETRY_DELAYS = (2.0, 8.0, 30.0)
DEFAULT_PARALLELISM = {"edge": 4, "azure": 8, "elevenlabs": 4,
                       "google": 8, "openai": 8}


class TokenBucketLimiter:
    """Bounds concurrent in-flight calls per provider (async semaphore
    refilled per call — a concurrency token bucket)."""

    def __init__(self, limits: dict[str, int] | None = None):
        self._limits = dict(DEFAULT_PARALLELISM)
        if limits:
            self._limits.update(limits)
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    def limit_for(self, provider: str) -> int:
        return self._limits.get(provider, 4)

    def semaphore(self, provider: str) -> asyncio.Semaphore:
        sem = self._semaphores.get(provider)
        if sem is None:
            sem = asyncio.Semaphore(self.limit_for(provider))
            self._semaphores[provider] = sem
        return sem

    async def run(self, provider: str, coro_factory: Callable[[], Awaitable[T]]) -> T:
        async with self.semaphore(provider):
            return await coro_factory()


class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures for ``open_seconds``."""

    def __init__(self, threshold: int = 3, open_seconds: float = 15 * 60,
                 clock: Callable[[], float] = time.monotonic):
        self.threshold = threshold
        self.open_seconds = open_seconds
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self.reasons: dict[str, str] = {}

    def is_open(self, provider: str) -> bool:
        opened = self._opened_at.get(provider)
        if opened is None:
            return False
        if self._clock() - opened >= self.open_seconds:
            self._opened_at.pop(provider, None)
            self._failures[provider] = 0
            return False
        return True

    def check(self, provider: str) -> None:
        if self.is_open(provider):
            raise CircuitOpen(
                f"circuit breaker open for {provider}: "
                f"{self.reasons.get(provider, 'consecutive failures')}")

    def record_success(self, provider: str) -> None:
        self._failures[provider] = 0

    def record_failure(self, provider: str, reason: str = "") -> None:
        self._failures[provider] = self._failures.get(provider, 0) + 1
        if reason:
            self.reasons[provider] = reason
        if self._failures[provider] >= self.threshold:
            self._opened_at[provider] = self._clock()

    @property
    def consecutive_failures(self) -> dict[str, int]:
        return dict(self._failures)


async def call_with_retries(factory: Callable[[], Awaitable[T]], *,
                            attempts: int = 3,
                            delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
                            jitter: float = 0.25,
                            sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                            rng: random.Random | None = None) -> T:
    """Run ``factory`` up to ``attempts`` times.

    * :class:`ProviderUnavailable` (auth/quota/stub/circuit) — never retried.
    * :class:`ProviderError` — retried with backoff; a ``retry_after_s`` on
      the error wins over the schedule.
    """
    rng = rng or random.Random()
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return await factory()
        except ProviderUnavailable:
            raise
        except ProviderError as exc:
            last_exc = exc
            if attempt == attempts - 1:
                raise
            delay = exc.retry_after_s
            if delay is None:
                base = delays[min(attempt, len(delays) - 1)]
                delay = base * (1.0 + rng.uniform(0, jitter))
            await sleep(delay)
    assert last_exc is not None  # pragma: no cover
    raise last_exc
