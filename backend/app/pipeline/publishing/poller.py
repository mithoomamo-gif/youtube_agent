"""Processing-status poller (Section 34, Step 5 / G3).

Uploaded != processed != live. CHECKS_PASSED requires ALL of:
- processingStatus == succeeded
- uploadStatus == processed
- contentDetails.duration matches the local master ±1s
  (NEVER fileSize — it echoes the declared size even for truncations)
- definition == hd before release in Auto mode
- no rejectionReason (present → BLOCKED_HUMAN, or terminal for duplicate)
- privacy still private pre-release

Duration P0D for >1h = dead upload. Hard timeout (default 6h) →
BLOCKED_HUMAN. Backoff ladder 30s → 60s → 2m → 5m → 10m cap + jitter,
driven by an injected clock so tests run instantly.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum

from .clock import Clock, SystemClock
from .errors import DeadUploadError, ProcessingFailedError
from .manifest import PublishManifest
from .provider import PRIVACY_PRIVATE, VideoStatus, YouTubeProvider
from .state_machine import PublishJob, PublishState

BACKOFF_LADDER: tuple[float, ...] = (30.0, 60.0, 120.0, 300.0, 600.0)
DEFAULT_TIMEOUT_SECONDS = 6 * 3600.0
DEAD_UPLOAD_THRESHOLD_SECONDS = 3600.0
DURATION_TOLERANCE_SECONDS = 1.0

# Rejection reasons that mean "this exact content can never be retried".
TERMINAL_REJECTION_REASONS = {"duplicate"}
# Rejection reasons that need a human (+ channel demotion from Auto).
HUMAN_REJECTION_REASONS = {"claim", "copyright", "termsOfUse", "trademark"}


class PollOutcome(str, Enum):
    CHECKS_PASSED = "CHECKS_PASSED"
    BLOCKED_HUMAN = "BLOCKED_HUMAN"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    DEAD_UPLOAD = "DEAD_UPLOAD"
    TIMEOUT = "TIMEOUT"


@dataclass
class PollResult:
    outcome: PollOutcome
    last_status: VideoStatus | None = None
    polls: int = 0
    elapsed_seconds: float = 0.0
    sleeps: list[float] = field(default_factory=list)
    detail: str = ""


class ProcessingPoller:
    def __init__(
        self,
        provider: YouTubeProvider,
        *,
        clock: Clock | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        require_hd: bool = True,
        jitter_fraction: float = 0.0,
        rng: random.Random | None = None,
    ) -> None:
        self._provider = provider
        self._clock = clock or SystemClock()
        self.timeout_seconds = timeout_seconds
        self.require_hd = require_hd
        self.jitter_fraction = jitter_fraction
        self._rng = rng or random.Random(0)

    def _delay(self, attempt: int) -> float:
        base = BACKOFF_LADDER[min(attempt, len(BACKOFF_LADDER) - 1)]
        if self.jitter_fraction > 0:
            base += base * self.jitter_fraction * self._rng.random()
        return base

    @staticmethod
    def checks_failure(status: VideoStatus, manifest: PublishManifest,
                       require_hd: bool) -> str | None:
        """None = all G3 checks pass; otherwise why not (yet or ever)."""
        if status.processing_status != "succeeded":
            return f"processingStatus={status.processing_status}"
        if status.upload_status != "processed":
            return f"uploadStatus={status.upload_status}"
        if status.duration_seconds is None:
            return "duration unknown"
        if abs(status.duration_seconds - manifest.duration_seconds) > DURATION_TOLERANCE_SECONDS:
            return (
                f"duration mismatch: remote {status.duration_seconds}s vs "
                f"local {manifest.duration_seconds}s"
            )
        if require_hd and status.definition != "hd":
            return f"definition={status.definition} (hd required)"
        if status.privacy_status != PRIVACY_PRIVATE:
            return f"privacy drifted to {status.privacy_status} pre-release"
        return None

    def poll(
        self, job: PublishJob, manifest: PublishManifest
    ) -> PollResult:
        """Drive job (in PROCESSING) to CHECKS_PASSED or a failure exit."""
        if job.video_id is None:
            raise ValueError("cannot poll processing without a video_id")
        clock = self._clock
        start = clock.now_seconds()
        polls = 0
        sleeps: list[float] = []
        last: VideoStatus | None = None
        p0d_since: float | None = None

        while True:
            elapsed = clock.now_seconds() - start
            last = self._provider.get_video_status(job.video_id)
            polls += 1
            elapsed = clock.now_seconds() - start

            # Rejection: never auto-retry.
            if last.rejection_reason:
                reason = last.rejection_reason
                if reason in TERMINAL_REJECTION_REASONS:
                    job.transition(
                        PublishState.FAILED_TERMINAL,
                        f"rejectionReason={reason}",
                    )
                    return PollResult(PollOutcome.FAILED_TERMINAL, last, polls,
                                      elapsed, sleeps, f"rejectionReason={reason}")
                job.channel_demoted = reason in HUMAN_REJECTION_REASONS
                job.transition(
                    PublishState.BLOCKED_HUMAN,
                    f"rejectionReason={reason}",
                )
                return PollResult(PollOutcome.BLOCKED_HUMAN, last, polls,
                                  elapsed, sleeps, f"rejectionReason={reason}")

            if last.processing_status == "failed":
                err = ProcessingFailedError(last.processing_failure_reason)
                job.transition(PublishState.FAILED_TERMINAL, str(err))
                return PollResult(PollOutcome.FAILED_TERMINAL, last, polls,
                                  elapsed, sleeps, str(err))

            # Dead-upload detection: P0D means nothing usable arrived.
            if last.duration_seconds is not None and last.duration_seconds == 0:
                if p0d_since is None:
                    p0d_since = elapsed
                elif elapsed - p0d_since > DEAD_UPLOAD_THRESHOLD_SECONDS:
                    err = DeadUploadError(
                        f"video {job.video_id} stuck at duration P0D for "
                        f">{DEAD_UPLOAD_THRESHOLD_SECONDS:.0f}s"
                    )
                    return PollResult(PollOutcome.DEAD_UPLOAD, last, polls,
                                      elapsed, sleeps, str(err))
            elif last.duration_seconds:
                p0d_since = None

            failure = self.checks_failure(last, manifest, self.require_hd)
            if failure is None:
                job.transition(PublishState.CHECKS_PASSED, "all G3 checks passed")
                return PollResult(PollOutcome.CHECKS_PASSED, last, polls,
                                  elapsed, sleeps)

            if elapsed >= self.timeout_seconds:
                job.transition(
                    PublishState.BLOCKED_HUMAN,
                    f"processing timeout after {elapsed:.0f}s: {failure}",
                )
                return PollResult(PollOutcome.TIMEOUT, last, polls, elapsed,
                                  sleeps, failure or "timeout")

            delay = self._delay(polls - 1)
            sleeps.append(delay)
            clock.sleep(delay)
