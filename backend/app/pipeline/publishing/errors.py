"""Typed errors for the publishing workflow (Section 34 failure matrix).

Provider implementations raise these; the workflow maps them to states.
No provider exception type ever leaks past the workflow unmapped.
"""
from __future__ import annotations


class PublishingError(Exception):
    """Base class for all publishing errors."""


class IllegalTransitionError(PublishingError):
    """A state-machine transition that is not in the legal transition table."""


class InvalidPublishRequestError(PublishingError, ValueError):
    """Client-side refusal to construct a dangerous request.

    Raised e.g. when publishAt is combined with a non-private privacy
    status: YouTube silently ignores publishAt in that case and would
    publish the video immediately (Section 34, scheduling hard rules).
    """


class ComplianceGateError(PublishingError):
    """Gate C refusal: public publishing while compliance is not PASSED.

    Round 6 Compliance Gate (the roadmap's #1 launch blocker): while
    the stored per-app compliance checklist's derived overall state
    is not ``passed``, uploads are forced to private and any attempt
    to publish publicly is refused with this typed error.  ``code`` /
    ``reason`` carry the machine reason ``compliance_not_passed`` and
    ``overall`` the compliance state that blocked the publish.
    """

    code = "compliance_not_passed"
    reason = "compliance_not_passed"

    def __init__(
        self,
        message: str = "",
        *,
        overall: str | None = None,
        requested_visibility: str | None = None,
    ) -> None:
        super().__init__(
            message
            or (
                "public publishing refused: compliance audit has not "
                f"passed (overall state: {overall or 'not_started'}); "
                "uploads are forced to private until it passes"
            )
        )
        self.overall = overall or "not_started"
        self.requested_visibility = requested_visibility


class ManifestError(PublishingError, ValueError):
    """Manifest validation / freezing problem (G0 gate)."""


class DeadUploadError(PublishingError):
    """Duration stayed P0D for >1h: the upload is dead.

    Procedure (Section 34): mark dead, privatise the old ID, re-upload as
    a NEW video and rewrite every downstream reference to the new ID.
    Never retried in place.
    """


class ProviderError(PublishingError):
    """Base for errors reported by the YouTube provider."""

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        reason: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message or reason or self.__class__.__name__)
        self.status_code = status_code
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds


class AuthError(ProviderError):
    """401 / token revoked. Workflow: refresh once, then BLOCKED_HUMAN."""

    def __init__(self, message: str = "unauthorised", **kw: object) -> None:
        super().__init__(message, status_code=401, reason="authError", **kw)  # type: ignore[arg-type]


class QuotaExceededError(ProviderError):
    """403 quotaExceeded. Park the job until the Pacific-midnight reset.

    Never retried in a spin, never worked around with extra projects.
    """

    def __init__(self, message: str = "quota exceeded", **kw: object) -> None:
        super().__init__(message, status_code=403, reason="quotaExceeded", **kw)  # type: ignore[arg-type]


class UploadLimitExceededError(ProviderError):
    """403 uploadLimitExceeded: per-channel daily cap. Terminal for the day."""

    def __init__(self, message: str = "channel upload limit exceeded", **kw: object) -> None:
        super().__init__(message, status_code=403, reason="uploadLimitExceeded", **kw)  # type: ignore[arg-type]


class SessionExpiredError(ProviderError):
    """404 on a resumable session: the session URI is gone.

    A new session may be opened ONLY while no video_id exists yet.
    """

    def __init__(self, message: str = "resumable session expired", **kw: object) -> None:
        super().__init__(message, status_code=404, reason="sessionExpired", **kw)  # type: ignore[arg-type]


class ServerError(ProviderError):
    """5xx from the provider. Status query first, then resume; honour Retry-After."""

    def __init__(self, message: str = "server error", *, status_code: int = 503, **kw: object) -> None:
        super().__init__(message, status_code=status_code, reason="serverError", **kw)  # type: ignore[arg-type]


class NetworkTimeoutError(ProviderError):
    """Transport timeout. Treated like a 5xx: query status before resending."""


class ProcessingFailedError(PublishingError):
    """processingStatus=failed. Terminal; carries processingFailureReason."""

    def __init__(self, failure_reason: str | None) -> None:
        super().__init__(f"processing failed: {failure_reason}")
        self.failure_reason = failure_reason
