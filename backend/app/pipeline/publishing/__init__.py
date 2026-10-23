"""Publishing workflow (worker 7): QC-passed master → LIVE_CONFIRMED.

Spec: roadmap section 34 (publishing workflow) + section 25 (quota).
Publishing code depends ONLY on the YouTubeProvider interface — no
real YouTube calls are made from this package.
"""
from .clock import Clock, FakeClock, SystemClock
from .errors import (
    AuthError,
    ComplianceGateError,
    DeadUploadError,
    IllegalTransitionError,
    InvalidPublishRequestError,
    ManifestError,
    NetworkTimeoutError,
    ProcessingFailedError,
    ProviderError,
    PublishingError,
    QuotaExceededError,
    ServerError,
    SessionExpiredError,
    UploadLimitExceededError,
)
from .manifest import PublishManifest, ReleaseMode, compute_idempotency_key
from .poller import PollOutcome, PollResult, ProcessingPoller
from .provider import (
    ChunkResult,
    VideoMetadata,
    VideoStatus,
    VideoUpdate,
    YouTubeProvider,
    build_cancel_schedule_update,
    build_release_update,
    build_schedule_update,
    metadata_for_insert,
    parse_range_header,
    validate_publish_at,
)
from .scheduler import (
    NoSlotAvailableError,
    ScheduleResult,
    SlotWatcher,
    TwoStepScheduler,
    WatcherAction,
    next_pacific_midnight,
)
from .state_machine import (
    LEGAL_TRANSITIONS,
    PublishJob,
    PublishState,
    is_legal_transition,
)
from .store import InMemoryStore, JsonFileStore, SessionRecord, UploadStore
from .uploader import ChunkedUploader, UploadResult
from .workflow import PublishingWorkflow, TokenRefresher

__all__ = [
    "AuthError",
    "ChunkResult",
    "ComplianceGateError",
    "ChunkedUploader",
    "Clock",
    "DeadUploadError",
    "FakeClock",
    "IllegalTransitionError",
    "InMemoryStore",
    "InvalidPublishRequestError",
    "JsonFileStore",
    "LEGAL_TRANSITIONS",
    "ManifestError",
    "NetworkTimeoutError",
    "NoSlotAvailableError",
    "PollOutcome",
    "PollResult",
    "ProcessingFailedError",
    "ProcessingPoller",
    "ProviderError",
    "PublishJob",
    "PublishManifest",
    "PublishState",
    "PublishingError",
    "PublishingWorkflow",
    "QuotaExceededError",
    "ReleaseMode",
    "ScheduleResult",
    "ServerError",
    "SessionExpiredError",
    "SessionRecord",
    "SlotWatcher",
    "SystemClock",
    "TokenRefresher",
    "TwoStepScheduler",
    "UploadLimitExceededError",
    "UploadResult",
    "UploadStore",
    "VideoMetadata",
    "VideoStatus",
    "VideoUpdate",
    "WatcherAction",
    "YouTubeProvider",
    "build_cancel_schedule_update",
    "build_release_update",
    "build_schedule_update",
    "compute_idempotency_key",
    "is_legal_transition",
    "metadata_for_insert",
    "next_pacific_midnight",
    "parse_range_header",
    "validate_publish_at",
]
