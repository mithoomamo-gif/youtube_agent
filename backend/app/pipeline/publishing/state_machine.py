"""Upload state machine (Section 34).

READY → PREFLIGHT_OK → SESSION_OPEN → UPLOADING → UPLOADED → ENRICHING
→ PROCESSING → CHECKS_PASSED → SCHEDULED / RELEASING → LIVE →
LIVE_CONFIRMED → POST_LIVE_DONE, with failure exits FAILED_RETRYABLE |
FAILED_TERMINAL | BLOCKED_HUMAN from (almost) any working state.

Governing rule: a 200/201 from insert means "bytes received", never
"live". Promotion may fire ONLY from LIVE_CONFIRMED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .errors import IllegalTransitionError
from .manifest import PublishManifest


class PublishState(str, Enum):
    READY = "READY"
    PREFLIGHT_OK = "PREFLIGHT_OK"
    SESSION_OPEN = "SESSION_OPEN"
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    ENRICHING = "ENRICHING"
    PROCESSING = "PROCESSING"
    CHECKS_PASSED = "CHECKS_PASSED"
    SCHEDULED = "SCHEDULED"
    RELEASING = "RELEASING"
    LIVE = "LIVE"
    LIVE_CONFIRMED = "LIVE_CONFIRMED"
    POST_LIVE_DONE = "POST_LIVE_DONE"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    BLOCKED_HUMAN = "BLOCKED_HUMAN"


_FAILURES = {
    PublishState.FAILED_RETRYABLE,
    PublishState.FAILED_TERMINAL,
    PublishState.BLOCKED_HUMAN,
}

_WORKING = {
    PublishState.READY,
    PublishState.PREFLIGHT_OK,
    PublishState.SESSION_OPEN,
    PublishState.UPLOADING,
    PublishState.UPLOADED,
    PublishState.ENRICHING,
    PublishState.PROCESSING,
    PublishState.CHECKS_PASSED,
    PublishState.SCHEDULED,
    PublishState.RELEASING,
    PublishState.LIVE,
}

# Legal forward transitions (failure exits are added programmatically).
_FORWARD: dict[PublishState, set[PublishState]] = {
    PublishState.READY: {PublishState.PREFLIGHT_OK},
    PublishState.PREFLIGHT_OK: {PublishState.SESSION_OPEN},
    # A fresh session may be opened again when the old one expired
    # (only ever before a video_id exists).
    PublishState.SESSION_OPEN: {PublishState.UPLOADING, PublishState.SESSION_OPEN},
    PublishState.UPLOADING: {
        PublishState.UPLOADED,
        PublishState.SESSION_OPEN,  # expired session → new session, re-transfer
    },
    PublishState.UPLOADED: {PublishState.ENRICHING},
    PublishState.ENRICHING: {PublishState.PROCESSING},
    PublishState.PROCESSING: {PublishState.CHECKS_PASSED},
    PublishState.CHECKS_PASSED: {
        PublishState.SCHEDULED,
        PublishState.RELEASING,
        # Dead upload re-uploaded as a new video re-enters the pipeline.
        PublishState.SESSION_OPEN,
    },
    # Watcher cancel returns an armed job to CHECKS_PASSED (plain private),
    # from where a fallback slot can be armed again.
    PublishState.SCHEDULED: {PublishState.RELEASING, PublishState.CHECKS_PASSED},
    PublishState.RELEASING: {PublishState.LIVE},
    PublishState.LIVE: {PublishState.LIVE_CONFIRMED},
    PublishState.LIVE_CONFIRMED: {PublishState.POST_LIVE_DONE},
    PublishState.POST_LIVE_DONE: set(),
    PublishState.FAILED_TERMINAL: set(),
    # A retry restarts from preflight; resume logic inside the uploader
    # (existing video_id / session) prevents any duplicate upload.
    PublishState.FAILED_RETRYABLE: {
        PublishState.READY,
        PublishState.PREFLIGHT_OK,
        PublishState.SESSION_OPEN,
        PublishState.UPLOADING,
        PublishState.PROCESSING,
    },
    # A human resolving the block returns the job to a sensible state.
    PublishState.BLOCKED_HUMAN: {
        PublishState.READY,
        PublishState.PREFLIGHT_OK,
        PublishState.PROCESSING,
        PublishState.CHECKS_PASSED,
        PublishState.SCHEDULED,
        PublishState.LIVE,
    },
}

LEGAL_TRANSITIONS: dict[PublishState, set[PublishState]] = {
    state: (targets | (_FAILURES if state in _WORKING else set()))
    for state, targets in _FORWARD.items()
}


def is_legal_transition(src: PublishState, dst: PublishState) -> bool:
    return dst in LEGAL_TRANSITIONS.get(src, set())


@dataclass
class TransitionRecord:
    src: PublishState
    dst: PublishState
    reason: str = ""
    at_seconds: float | None = None


@dataclass
class PublishJob:
    """Mutable runtime state for one manifest's publish attempt."""

    manifest: PublishManifest
    state: PublishState = PublishState.READY
    video_id: str | None = None
    session_uri: str | None = None
    bytes_confirmed: int = 0
    failure_reason: str | None = None
    # Quota parking: set on quotaExceeded, cleared when the reset passes.
    parked_until: datetime | None = None
    park_reason: str | None = None
    # Claim/copyright rejection demotes the channel Auto → Review-first.
    channel_demoted: bool = False
    armed_slot: datetime | None = None
    published_at: datetime | None = None
    verification: dict | None = None
    dead_video_ids: list[str] = field(default_factory=list)
    history: list[TransitionRecord] = field(default_factory=list)
    # Compliance Gate (Round 6): set by the workflow when a configured
    # compliance checker is not PASSED — the upload was forced to
    # private and the public release was refused (typed reason in
    # ``compliance_block``).  Defaults keep legacy jobs untouched.
    compliance_forced_private: bool = False
    compliance_block: dict | None = None

    @property
    def idempotency_key(self) -> str:
        return self.manifest.idempotency_key

    @property
    def is_terminal(self) -> bool:
        return self.state in (
            PublishState.POST_LIVE_DONE,
            PublishState.FAILED_TERMINAL,
        )

    @property
    def is_live_confirmed(self) -> bool:
        """The ONLY state from which downstream promotion may fire."""
        return self.state in (
            PublishState.LIVE_CONFIRMED,
            PublishState.POST_LIVE_DONE,
        )

    def transition(
        self,
        dst: PublishState,
        reason: str = "",
        *,
        at_seconds: float | None = None,
    ) -> None:
        if dst is self.state and dst not in (
            PublishState.SESSION_OPEN,
        ):
            raise IllegalTransitionError(
                f"self-transition {self.state.value} → {dst.value} is not legal"
            )
        if not is_legal_transition(self.state, dst):
            raise IllegalTransitionError(
                f"illegal transition {self.state.value} → {dst.value}"
            )
        self.history.append(
            TransitionRecord(self.state, dst, reason, at_seconds)
        )
        self.state = dst
        if dst in _FAILURES:
            self.failure_reason = reason or dst.value

    def park(self, until: datetime, reason: str) -> None:
        """Park without changing state (quota reset wait): no retry spin."""
        self.parked_until = until
        self.park_reason = reason

    def unpark(self) -> None:
        self.parked_until = None
        self.park_reason = None
