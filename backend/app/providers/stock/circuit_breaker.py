"""Per-provider circuit breaker (§24 failover).

A provider outage (5xx / 429 storm) trips the breaker for the rest of the
job; the failover chain then starts one step lower for subsequent scenes.
"""
from __future__ import annotations


class CircuitBreaker:
    def __init__(self, name: str = "", threshold: int = 3):
        self.name = name
        self.threshold = max(1, int(threshold))
        self.consecutive_failures = 0
        self.tripped = False

    @property
    def is_open(self) -> bool:
        return self.tripped

    def should_allow(self) -> bool:
        return not self.tripped

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            self.tripped = True

    def trip(self) -> None:
        self.tripped = True

    def reset(self) -> None:
        """Reset for a NEW job (within a job a tripped breaker stays tripped)."""
        self.consecutive_failures = 0
        self.tripped = False
