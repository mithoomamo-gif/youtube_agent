"""Subscription-quota awareness (roadmap Part 10.4.6).

Quota plans (e.g. ElevenLabs credits) are DUAL-DENOMINATED: dollars AND
provider quota units. This tracker records burn — split by product, so a
music/SFX drain on a shared pool is visible the day it starts (Leak 9) —
and projects the exhaustion date. A channel projected to exhaust its quota
before the reset date is flagged for downgrade BEFORE it strands a
half-narrated video.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .exceptions import QuotaExhausted


@dataclass
class QuotaAccount:
    provider: str
    plan_name: str
    quota_total: int  # e.g. 121_000 credits on ElevenLabs Creator
    quota_used: int = 0
    quota_unit: str = "credits"
    cycle_start: date | None = None  # first day of the current billing cycle
    reset_date: date | None = None  # when quota_used resets to 0
    by_product: dict[str, int] = field(default_factory=dict)  # narration | music | sfx | dubbing

    @property
    def quota_remaining(self) -> int:
        return max(0, self.quota_total - self.quota_used)

    @property
    def utilization_pct(self) -> float:
        if self.quota_total <= 0:
            return 100.0
        return self.quota_used * 100.0 / self.quota_total

    def burn(self, units: int, *, product: str = "narration") -> int:
        """Record quota consumption. Raises QuotaExhausted past the total."""
        units = int(units)
        if units < 0:
            raise ValueError("quota burn cannot be negative")
        if self.quota_used + units > self.quota_total:
            raise QuotaExhausted(
                f"{self.provider} {self.plan_name}: burning {units} {self.quota_unit} "
                f"would exceed quota ({self.quota_used}/{self.quota_total} used)"
            )
        self.quota_used += units
        self.by_product[product] = self.by_product.get(product, 0) + units
        return self.quota_used

    def daily_burn_rate(self, today: date | None = None) -> float | None:
        """Average quota units burned per day this cycle (None if no burn)."""
        if self.quota_used <= 0 or self.cycle_start is None:
            return None
        today = today or date.today()
        days = max(1, (today - self.cycle_start).days + 1)
        return self.quota_used / days

    def projected_exhaustion_date(self, today: date | None = None) -> date | None:
        """Date the quota hits zero at the current burn rate (None if n/a)."""
        today = today or date.today()
        if self.quota_remaining <= 0:
            return today
        rate = self.daily_burn_rate(today)
        if not rate:
            return None
        days_left = int(self.quota_remaining // rate)
        return today + timedelta(days=days_left)

    def will_exhaust_before_reset(self, today: date | None = None) -> bool:
        """True when projected exhaustion lands before the reset date."""
        projected = self.projected_exhaustion_date(today)
        if projected is None or self.reset_date is None:
            return False
        return projected < self.reset_date

    def flagged_for_downgrade(self, today: date | None = None) -> bool:
        """Flag BEFORE a mid-month exhaustion strands a half-made video."""
        return self.will_exhaust_before_reset(today)
