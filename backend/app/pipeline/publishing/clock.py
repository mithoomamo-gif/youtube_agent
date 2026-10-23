"""Injectable clock/sleeper so pollers and backoff run instantly in tests."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Monotonic seconds for elapsed/backoff math + wall time for slots."""

    def now_seconds(self) -> float: ...
    def now(self) -> datetime: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now_seconds(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock:
    """Deterministic clock: sleep() advances virtual time instantly."""

    def __init__(self, start: datetime | None = None) -> None:
        self._start = start or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        self._elapsed = 0.0
        self.sleeps: list[float] = []

    def now_seconds(self) -> float:
        return self._elapsed

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._elapsed)

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot sleep a negative duration")
        self.sleeps.append(seconds)
        self._elapsed += seconds

    def advance(self, seconds: float) -> None:
        """Advance without recording a sleep (simulates time passing)."""
        self._elapsed += seconds
