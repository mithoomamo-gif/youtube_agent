"""Two-step scheduling + slot watcher (Section 34).

Default flow deliberately decouples upload from scheduling:
1. upload private with NO publishAt;
2. poll to CHECKS_PASSED;
3. only then update with privacyStatus=private re-sent explicitly +
   publishAt = slot — if the slot is still >= the minimum lead time,
   else the next fallback slot;
4. verify by re-fetch (publishAt present, privacy still private).

Watcher: if an armed slot approaches while checks are incomplete, the
publishAt is CANCELLED back to plain private and a fallback is
re-booked — an unchecked or SD-only video must never flip public.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

from .errors import PublishingError
from .manifest import PublishManifest
from .provider import (
    PRIVACY_PRIVATE,
    YouTubeProvider,
    build_cancel_schedule_update,
    build_schedule_update,
)
from .state_machine import PublishJob, PublishState

PACIFIC = ZoneInfo("America/Los_Angeles")
DEFAULT_MIN_LEAD_SECONDS = 30 * 60.0


def next_pacific_midnight(now: datetime) -> datetime:
    """Next midnight America/Los_Angeles (quota buckets reset then)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(PACIFIC)
    nxt = (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return nxt.astimezone(timezone.utc)


class NoSlotAvailableError(PublishingError):
    """Neither the target slot nor any fallback satisfies the lead time."""


@dataclass
class ScheduleResult:
    slot: datetime
    used_fallback: bool
    verified: bool


class TwoStepScheduler:
    def __init__(
        self,
        provider: YouTubeProvider,
        *,
        min_lead_seconds: float = DEFAULT_MIN_LEAD_SECONDS,
    ) -> None:
        self._provider = provider
        self.min_lead_seconds = min_lead_seconds

    def pick_slot(
        self, manifest: PublishManifest, now: datetime
    ) -> tuple[datetime, bool]:
        """(slot, used_fallback). Raises NoSlotAvailableError if none fit."""
        earliest = now + timedelta(seconds=self.min_lead_seconds)
        candidates: list[tuple[datetime, bool]] = []
        if manifest.target_slot is not None:
            candidates.append((manifest.target_slot, False))
        candidates += [(s, True) for s in manifest.fallback_slots]
        for slot, is_fallback in candidates:
            if slot >= earliest:
                return slot, is_fallback
        raise NoSlotAvailableError(
            f"no slot at or after {earliest.isoformat()} "
            f"(target={manifest.target_slot}, fallbacks={manifest.fallback_slots})"
        )

    def schedule(
        self, job: PublishJob, manifest: PublishManifest, now: datetime
    ) -> ScheduleResult:
        """Arm publishAt for a CHECKS_PASSED job and verify by re-fetch."""
        if job.state is not PublishState.CHECKS_PASSED:
            raise PublishingError(
                f"two-step scheduling requires CHECKS_PASSED, got {job.state}"
            )
        if job.video_id is None:
            raise PublishingError("cannot schedule without a video_id")
        slot, used_fallback = self.pick_slot(manifest, now)
        self._provider.update_video(job.video_id, build_schedule_update(slot))
        # Verify by re-fetch: publishAt present AND privacy still private.
        status = self._provider.get_video_status(job.video_id)
        verified = (
            status.publish_at == slot
            and status.privacy_status == PRIVACY_PRIVATE
        )
        if not verified:
            raise PublishingError(
                f"schedule verification failed: publish_at={status.publish_at} "
                f"privacy={status.privacy_status}"
            )
        job.armed_slot = slot
        job.transition(PublishState.SCHEDULED, f"publishAt armed for {slot.isoformat()}")
        return ScheduleResult(slot=slot, used_fallback=used_fallback, verified=True)


class WatcherAction(str, Enum):
    NONE = "NONE"
    CANCELLED_AND_REBOOKED = "CANCELLED_AND_REBOOKED"
    CANCELLED_NO_FALLBACK = "CANCELLED_NO_FALLBACK"


class SlotWatcher:
    """Guards armed slots against incomplete checks."""

    def __init__(
        self,
        provider: YouTubeProvider,
        scheduler: TwoStepScheduler,
        *,
        cancel_window_seconds: float = DEFAULT_MIN_LEAD_SECONDS,
    ) -> None:
        self._provider = provider
        self._scheduler = scheduler
        self.cancel_window_seconds = cancel_window_seconds

    def check(
        self,
        job: PublishJob,
        manifest: PublishManifest,
        now: datetime,
        *,
        checks_complete: bool,
    ) -> WatcherAction:
        if job.state is not PublishState.SCHEDULED or job.armed_slot is None:
            return WatcherAction.NONE
        if checks_complete:
            return WatcherAction.NONE
        remaining = (job.armed_slot - now).total_seconds()
        if remaining > self.cancel_window_seconds:
            return WatcherAction.NONE

        # Cancel FIRST: plain private, no publishAt. Never let an
        # unchecked video flip public because a timer fired.
        assert job.video_id is not None
        self._provider.update_video(
            job.video_id, build_cancel_schedule_update()
        )
        cancelled_slot = job.armed_slot
        job.armed_slot = None
        job.transition(
            PublishState.CHECKS_PASSED,
            f"watcher cancelled publishAt {cancelled_slot.isoformat()}: "
            "checks incomplete near slot",
        )
        # Re-book a FALLBACK (never the slot just cancelled) if one
        # still satisfies the lead time.
        fallback_only = manifest.model_copy(update={"target_slot": None})
        try:
            self._scheduler.schedule(job, fallback_only, now)
        except (NoSlotAvailableError, PublishingError):
            return WatcherAction.CANCELLED_NO_FALLBACK
        return WatcherAction.CANCELLED_AND_REBOOKED
