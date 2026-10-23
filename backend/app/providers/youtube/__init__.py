"""YouTube Data API v3 provider for the YouTube Agent desktop app.

Implements roadmap Section 25 / Part 7:

- OAuth 2.0 Authorization Code + PKCE loopback flow (desktop client).
  **API keys cannot upload and service accounts cannot be linked to a
  YouTube channel — OAuth with the channel owner's grant is the only
  way.** Refresh tokens / client secrets live only behind the injected
  :class:`SecretStore`; token values are never logged and never reach
  the frontend (status strings only).
- :class:`TokenManager` — per-channel credential blobs, single-flight
  refresh, ``invalid_grant`` -> needs_reauth, Testing-mode 7-day token
  detection, revoke + delete on disconnect.
- :class:`YouTubeClient` — the single quota-aware HTTP wrapper every API
  call goes through (June 2026 granular buckets, 90% safety-ceiling
  reservations, ledger entries for failed calls too, midnight-PT reset).
- :class:`ResumableUploader` — resumable uploads with server-driven
  resume, immediate session persistence and forced-private detection.
- :meth:`YouTubeClient.verify_channel` — ``channels.list?mine=true``
  must return exactly the expected channel.

All HTTP is async via httpx.
"""

from .analytics import (
    ANALYTICS_BASE,
    CTR_UNAVAILABLE_REASON,
    QUOTA_COSTS,
    AnalyticsResult,
    ChannelLifetimeStats,
    ChannelTotals,
    ReportColumn,
    ReportTable,
    RetentionCurve,
    RetentionPoint,
    VideoMetrics,
    YouTubeAnalyticsClient,
)
from .client import ChannelInfo, YouTubeClient
from .errors import (
    ChannelMismatch,
    NeedsReauth,
    OAuthDenied,
    OAuthError,
    QuotaExhausted,
    SessionExpired,
    StateMismatch,
    UploadLimitExceeded,
    YouTubeAPIError,
    YouTubeError,
)
from .oauth import (
    CONNECT_SCOPES,
    SCOPE_UPLOAD,
    LoopbackReceiver,
    TokenResponse,
    build_auth_url,
    exchange_code,
    generate_pkce_pair,
    generate_state,
    wait_for_code,
)
from .quota import (
    BUCKET_LIMITS,
    InMemoryQuotaLedger,
    LedgerEntry,
    QuotaLedger,
    classify_method,
    next_reset,
    pacific_day,
)
from .secrets import InMemorySecretStore, SecretStore
from .tokens import CredentialStatus, TokenManager
from .upload import (
    InMemoryUploadSessionStore,
    ResumableUploader,
    UploadResult,
    UploadSessionState,
    UploadSessionStore,
    compute_idempotency_key,
    parse_range_offset,
    validate_metadata,
)

__all__ = [
    # secrets
    "SecretStore",
    "InMemorySecretStore",
    # oauth
    "CONNECT_SCOPES",
    "SCOPE_UPLOAD",
    "LoopbackReceiver",
    "TokenResponse",
    "build_auth_url",
    "exchange_code",
    "generate_pkce_pair",
    "generate_state",
    "wait_for_code",
    # tokens
    "CredentialStatus",
    "TokenManager",
    # quota
    "BUCKET_LIMITS",
    "InMemoryQuotaLedger",
    "LedgerEntry",
    "QuotaLedger",
    "classify_method",
    "next_reset",
    "pacific_day",
    # client
    "ChannelInfo",
    "YouTubeClient",
    # analytics (Agent 29 transport)
    "ANALYTICS_BASE",
    "CTR_UNAVAILABLE_REASON",
    "QUOTA_COSTS",
    "AnalyticsResult",
    "ChannelLifetimeStats",
    "ChannelTotals",
    "ReportColumn",
    "ReportTable",
    "RetentionCurve",
    "RetentionPoint",
    "VideoMetrics",
    "YouTubeAnalyticsClient",
    # upload
    "InMemoryUploadSessionStore",
    "ResumableUploader",
    "UploadResult",
    "UploadSessionState",
    "UploadSessionStore",
    "compute_idempotency_key",
    "parse_range_offset",
    "validate_metadata",
    # errors
    "ChannelMismatch",
    "NeedsReauth",
    "OAuthDenied",
    "OAuthError",
    "QuotaExhausted",
    "SessionExpired",
    "StateMismatch",
    "UploadLimitExceeded",
    "YouTubeAPIError",
    "YouTubeError",
]
