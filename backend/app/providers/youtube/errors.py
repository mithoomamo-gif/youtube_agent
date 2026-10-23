"""Error taxonomy for the YouTube provider.

Every failure mode the roadmap (Section 25) calls out gets a distinct
exception type so callers (job runner / UI) can react correctly:

- :class:`QuotaExhausted` — a quota bucket is empty (or a pre-flight
  reservation would cross the safety ceiling). Stop ALL project API work
  until the buckets reset at midnight Pacific. Never retry.
- :class:`UploadLimitExceeded` — per-channel daily upload limit
  (``uploadLimitExceeded``). Pause that channel only; other channels and
  other API work continue.
- :class:`NeedsReauth` — the stored refresh token was rejected with
  ``invalid_grant``. The channel is marked ``needs_reauth`` and its jobs
  pause until a human reconnects it.
- :class:`ChannelMismatch` — ``channels.list?mine=true`` did not return
  exactly the expected channel. A wrong-channel upload is the unforgivable
  error, so verification failures are hard stops.
- :class:`YouTubeAPIError` — any other non-2xx API response.
"""

from __future__ import annotations


class YouTubeError(Exception):
    """Base class for all YouTube provider errors."""


class NeedsReauth(YouTubeError):
    """The channel's refresh token is dead; a human must reconnect it."""

    def __init__(self, channel_id: str, message: str | None = None) -> None:
        self.channel_id = channel_id
        super().__init__(
            message or f"channel {channel_id!r} needs re-authorisation (invalid_grant)"
        )


class QuotaExhausted(YouTubeError):
    """A quota bucket cannot pay for a call.

    Raised either pre-flight (the reservation would cross the configured
    safety ceiling — no HTTP call is made) or when Google answers
    ``403 quotaExceeded``. Callers must stop all project API work until
    the bucket resets at midnight Pacific Time.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        bucket: str | None = None,
        method: str | None = None,
        used: int | None = None,
        limit: int | None = None,
    ) -> None:
        self.bucket = bucket
        self.method = method
        self.used = used
        self.limit = limit
        super().__init__(
            message
            or (
                f"quota exhausted for bucket={bucket!r} method={method!r} "
                f"used={used} limit={limit}"
            )
        )


class UploadLimitExceeded(YouTubeError):
    """Per-channel daily upload limit hit (``400 uploadLimitExceeded``).

    This is a per-channel pause signal, unrelated to project quota:
    pause only ``channel_id``'s uploads (roadmap: ~24h); everything else
    continues.
    """

    def __init__(self, channel_id: str | None = None, message: str | None = None) -> None:
        self.channel_id = channel_id
        super().__init__(
            message or f"per-channel upload limit exceeded for channel {channel_id!r}"
        )


class ChannelMismatch(YouTubeError):
    """``verify_channel`` did not get back exactly the expected channel."""

    def __init__(self, expected: str, actual: str | None, message: str | None = None) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            message
            or f"channel verification failed: expected {expected!r}, got {actual!r}"
        )


class YouTubeAPIError(YouTubeError):
    """Any other non-2xx response from a Google endpoint."""

    def __init__(
        self,
        message: str | None = None,
        *,
        status_code: int | None = None,
        reason: str | None = None,
        body: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.reason = reason
        self.body = body
        super().__init__(
            message or f"YouTube API error status={status_code} reason={reason!r}"
        )


class OAuthError(YouTubeError):
    """The OAuth flow itself failed (denied consent, bad state, timeout)."""


class OAuthDenied(OAuthError):
    """The user (or Google) denied the authorization request."""


class StateMismatch(OAuthError):
    """The loopback callback carried a ``state`` we did not issue."""


class SessionExpired(YouTubeError):
    """A resumable-upload session URI is gone (404). Start a new session."""
