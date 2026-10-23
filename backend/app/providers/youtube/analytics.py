"""YouTube Analytics API (+ Data API v3 lifetime stats) client — Agent 29
transport layer.

This module is the ONLY analytics transport in the app. It is a *client
only*: no storage, no sync/scheduling, no API routes. Callers (the Round 5
sync team / Agent 29) consume the typed results below and decide what to
persist.

What it talks to
----------------
* **YouTube Analytics API v2** — ``GET
  https://youtubeanalytics.googleapis.com/v2/reports`` for channel
  totals, per-video metrics and audience-retention curves. Responses are
  the real Analytics ``resultTable`` shape (``columnHeaders`` + ``rows``)
  and are parsed by column *name* (never by assumed position).
* **YouTube Data API v3** — ``GET {API_BASE}/channels?part=statistics``
  for *lifetime* channel stats only (see :meth:`get_channel_lifetime_stats`).
  Period numbers always come from Analytics; the two are never mixed in
  one model.

Credential gate (Style Lab precedent, followed exactly in shape)
----------------------------------------------------------------
Analytics data is private channel data: the Analytics API requires an
OAuth 2.0 access token granted by the channel owner (scope
``yt-analytics.readonly``, see :mod:`.oauth`). **An API key cannot be
used** — key-only requests to ``youtubeanalytics.googleapis.com`` are
rejected, so unlike :mod:`app.style_lab.metadata` there is deliberately
NO API-key path here. The one credential is a connected channel:

1. a ``token_manager`` (:class:`.tokens.TokenManager`) must be supplied
   AND a ``channel_id`` must be given;
2. ``token_manager.get_status(channel_id)`` must report ``connected``;
3. ``token_manager.get_access_token(channel_id)`` must mint a token
   (it refreshes internally when expired — that is the TokenManager's
   own single-flight job, not this client's).

When any step fails, EVERY method returns a typed result with
``status="not_connected"`` and makes **zero HTTP calls** — the shared
HTTP client factory is never even invoked. Not-connected is a *result*,
never an exception, and never a fabricated number: on any non-``ok``
result ``data`` is ``None``.

Failure mapping (also typed results, never raised)
--------------------------------------------------
Exactly one HTTP attempt per call — no retries, no retry-storms (the
sync layer owns retry policy). Non-2xx / transport / malformed-body
problems return ``status="failed"`` with a machine-readable ``reason``:

* ``401`` -> ``authentication_failed``
* ``403``/``429`` with a quota reason -> ``quota_exceeded``
* other ``403`` -> the Google reason slug (e.g.
  ``insufficient_permissions``) or ``forbidden``
* ``404`` -> ``not_found``; ``400`` -> the Google reason slug or
  ``invalid_request``
* timeout -> ``timeout``; other transport errors -> ``network_error``
* 200 with a non-JSON body -> ``invalid_response_json``; 200 with a
  body that is not a ``resultTable`` -> ``malformed_response``

Quota accounting (mirrors :class:`.client.YouTubeClient`)
---------------------------------------------------------
Google's shared-bucket model used across this app (see :mod:`.quota`)
charges **1 unit per Analytics reports query**, exactly like a Data API
read: ``classify_method("youtubeAnalytics.reports.query")`` resolves to
``(shared, 1)`` and ``classify_method("channels.list")`` to
``(shared, 1)``. The per-endpoint constants are exported as
:data:`QUOTA_COSTS`. Every result reports the units it charged in
``quota_units`` (0 when no call was attempted). When a
:class:`.quota.QuotaLedger` is injected, the client additionally:

* pre-flights each call against the bucket's safety ceiling (default
  90%, as :class:`.client.YouTubeClient` does) and refuses with
  ``failed``/``quota_exceeded`` — zero HTTP — when it would cross it;
* records a :class:`.quota.LedgerEntry` for every attempted call,
  successful or not (Google charges for failed calls too).

CTR — the honest decision
-------------------------
A plain Analytics ``reports`` query does **not** expose thumbnail
impressions CTR. The legacy metrics ``cardClickRate`` /
``annotationClickRate`` measure cards/annotations (discontinued
features), are NOT thumbnail CTR, and are never requested or relabelled
here. Real thumbnail CTR is only available via YouTube Studio /
Analytics *groups* with additional permissions, which this transport
does not have. :class:`VideoMetrics` therefore carries
``ctr: float | None = None`` plus ``ctr_unavailable_reason`` (see
:data:`CTR_UNAVAILABLE_REASON`) so storage can hold the field without
anyone inventing a value.

Metric wire-name mapping (Analytics API -> this module)
------------------------------------------------------
==========================  ==========================
model field                 Analytics metric
==========================  ==========================
``views``                   ``views``
``watch_time_minutes``      ``estimatedMinutesWatched``
``avg_view_duration_s``     ``averageViewDuration`` (seconds)
``avg_view_percentage``     ``averageViewPercentage``
``likes``                   ``likes``
``comment_count``           ``comments``
``shares``                  ``shares``
``subscribers_gained``      ``subscribersGained``
``subscribers_lost``        ``subscribersLost``
``watch_ratio`` (retention) ``audienceWatchRatio``
==========================  ==========================

The parser also accepts the task-facing aliases ``watchTimeMinutes``
and ``commentCount`` as column names if a future API revision returns
them, but requests always use the real wire names above.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Generic, TypeVar

import httpx

from ..http_client import create_async_client
from ...redaction import redact_text, register_secret
from .client import API_BASE
from .quota import (
    BUCKET_LIMITS,
    DEFAULT_SAFETY_CEILING,
    LedgerEntry,
    QuotaLedger,
    classify_method,
    pacific_day,
)
from .tokens import STATUS_CONNECTED

__all__ = [
    "ANALYTICS_BASE",
    "REPORTS_PATH",
    "METHOD_ANALYTICS_QUERY",
    "METHOD_CHANNELS_LIST",
    "QUOTA_COST_ANALYTICS_QUERY",
    "QUOTA_COST_CHANNELS_LIST",
    "QUOTA_COSTS",
    "STATUS_OK",
    "STATUS_NOT_CONNECTED",
    "STATUS_FAILED",
    "REASON_NO_CREDENTIAL",
    "REASON_OAUTH_TOKEN_UNAVAILABLE",
    "CTR_UNAVAILABLE_REASON",
    "REPORT_TYPE_AUDIENCE_RETENTION",
    "AnalyticsResult",
    "ReportColumn",
    "ReportTable",
    "ChannelTotals",
    "ChannelLifetimeStats",
    "VideoMetrics",
    "RetentionPoint",
    "RetentionCurve",
    "YouTubeAnalyticsClient",
]

#: YouTube Analytics API v2 base.
ANALYTICS_BASE = "https://youtubeanalytics.googleapis.com/v2"
#: Reports endpoint path under :data:`ANALYTICS_BASE`.
REPORTS_PATH = "/reports"

#: Ledger method names (fed through :func:`.quota.classify_method`).
METHOD_ANALYTICS_QUERY = "youtubeAnalytics.reports.query"
METHOD_CHANNELS_LIST = "channels.list"

#: Quota cost per call, in the app's shared-bucket model. Analytics
#: charges 1 unit per reports query; channels.list is a 1-unit read.
QUOTA_COST_ANALYTICS_QUERY = 1
QUOTA_COST_CHANNELS_LIST = 1
QUOTA_COSTS: dict[str, int] = {
    METHOD_ANALYTICS_QUERY: QUOTA_COST_ANALYTICS_QUERY,
    METHOD_CHANNELS_LIST: QUOTA_COST_CHANNELS_LIST,
}

STATUS_OK = "ok"
STATUS_NOT_CONNECTED = "not_connected"
STATUS_FAILED = "failed"

REASON_NO_CREDENTIAL = "no_youtube_credential"
REASON_OAUTH_TOKEN_UNAVAILABLE = "oauth_token_unavailable"

#: Why ``ctr`` is always ``None`` from this client (see module docstring).
CTR_UNAVAILABLE_REASON = (
    "thumbnail CTR is not exposed by a plain YouTube Analytics reports "
    "query (legacy cardClickRate/annotationClickRate are card/annotation "
    "metrics, not thumbnail CTR); it requires YouTube Studio / Analytics "
    "groups access, so ctr is reported as unavailable, never estimated"
)

#: Report-definition id for the retention report in Analytics/Studio.
#: On the wire it is a reports query with dimension
#: ``elapsedVideoTimeRatio`` and metric ``audienceWatchRatio``.
REPORT_TYPE_AUDIENCE_RETENTION = "audience-retention"

_DEFAULT_TIMEOUT_S = 30.0

_QUOTA_REASONS = {
    "quotaexceeded",
    "dailylimitexceeded",
    "ratelimitexceeded",
    "userratelimitexceeded",
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Wire metrics requested for channel totals / per-video metrics.
_TOTAL_METRICS = (
    "views,estimatedMinutesWatched,averageViewDuration,"
    "averageViewPercentage,likes,comments,shares,"
    "subscribersGained,subscribersLost"
)
_VIDEO_METRICS = (
    "views,estimatedMinutesWatched,averageViewDuration,"
    "averageViewPercentage,likes,comments,shares,subscribersGained"
)


T = TypeVar("T")


# ---------------------------------------------------------------------------
# Typed results / models
# ---------------------------------------------------------------------------
@dataclass
class AnalyticsResult(Generic[T]):
    """Outcome of one client call.

    ``status`` is ``ok`` / ``not_connected`` / ``failed``. ``data`` is
    populated ONLY on ``ok`` — failures never carry fabricated numbers.
    ``quota_units`` is what the call charged (0 when nothing was
    attempted: not-connected, invalid arguments, pre-flight refusal).
    ``raw_json`` holds the untouched API payload on ``ok`` (payload
    only, never credentials).
    """

    status: str
    data: T | None = None
    reason: str | None = None
    detail: str = ""
    quota_units: int = 0
    quota_method: str | None = None
    raw_json: dict[str, Any] | None = None

    @property
    def is_ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def is_not_connected(self) -> bool:
        return self.status == STATUS_NOT_CONNECTED


@dataclass
class ReportColumn:
    name: str
    column_type: str | None = None  # DIMENSION | METRIC
    data_type: str | None = None  # STRING | INTEGER | FLOAT | PERCENT ...


@dataclass
class ReportTable:
    """A parsed Analytics ``resultTable``: headers + rows keyed by name."""

    columns: list[ReportColumn] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ChannelTotals:
    """Period totals for one channel (Analytics reports, no dimension).

    Every field is ``None`` when the API returned no row for the range
    (``has_data`` is then False) — absence is never reported as zero.
    """

    channel_id: str
    start_date: str
    end_date: str
    views: int | None = None
    watch_time_minutes: float | None = None
    average_view_duration_seconds: float | None = None
    average_view_percentage: float | None = None
    likes: int | None = None
    comment_count: int | None = None
    shares: int | None = None
    subscribers_gained: int | None = None
    subscribers_lost: int | None = None
    has_data: bool = False

    @property
    def net_subscribers(self) -> int | None:
        if self.subscribers_gained is None or self.subscribers_lost is None:
            return None
        return self.subscribers_gained - self.subscribers_lost


@dataclass
class ChannelLifetimeStats:
    """Lifetime channel statistics from Data API ``channels.list``.

    Distinct from :class:`ChannelTotals` (a date-range): these are the
    all-time counters YouTube maintains on the channel resource.
    ``subscriber_count`` is ``None`` when the channel hides it.
    """

    channel_id: str
    view_count: int | None = None
    subscriber_count: int | None = None
    video_count: int | None = None
    hidden_subscriber_count: bool = False


@dataclass
class VideoMetrics:
    """Per-video metrics for one date range (Analytics reports).

    ``ctr`` is ALWAYS ``None`` from this client — see
    :data:`CTR_UNAVAILABLE_REASON` and the module docstring. Fields are
    ``None`` when the video has no row in range (``has_data`` False).
    """

    video_id: str
    start_date: str
    end_date: str
    views: int | None = None
    watch_time_minutes: float | None = None
    average_view_duration_seconds: float | None = None
    average_view_percentage: float | None = None
    likes: int | None = None
    comment_count: int | None = None
    shares: int | None = None
    subscribers_gained: int | None = None
    ctr: float | None = None
    ctr_unavailable_reason: str | None = CTR_UNAVAILABLE_REASON
    has_data: bool = False


@dataclass
class RetentionPoint:
    """One point of an audience-retention curve.

    ``elapsed_ratio`` is the fraction of the video elapsed (0..1+);
    ``watch_ratio`` is ``audienceWatchRatio`` — the fraction of views
    still watching at that point relative to the start.
    """

    elapsed_ratio: float
    watch_ratio: float


@dataclass
class RetentionCurve:
    """Audience-retention curve for one video (report type
    :data:`REPORT_TYPE_AUDIENCE_RETENTION`). Points are sorted by
    ``elapsed_ratio`` ascending; an empty list means the API returned
    no retention rows for the range (not a fabricated flat curve)."""

    video_id: str
    start_date: str
    end_date: str
    points: list[RetentionPoint] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing helpers (pure; no network)
# ---------------------------------------------------------------------------
def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_report_table(payload: Any) -> ReportTable | None:
    """Parse a real Analytics ``resultTable`` payload, or ``None`` when
    the shape is not a result table (missing/invalid ``columnHeaders``)."""
    if not isinstance(payload, dict):
        return None
    headers = payload.get("columnHeaders")
    if not isinstance(headers, list) or not headers:
        return None
    columns: list[ReportColumn] = []
    for header in headers:
        if not isinstance(header, dict) or not isinstance(header.get("name"), str):
            return None
        columns.append(
            ReportColumn(
                name=header["name"],
                column_type=header.get("columnType"),
                data_type=header.get("dataType"),
            )
        )
    raw_rows = payload.get("rows") or []
    if not isinstance(raw_rows, list):
        return None
    rows: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, (list, tuple)):
            return None
        row: dict[str, Any] = {}
        for idx, column in enumerate(columns):
            row[column.name] = raw_row[idx] if idx < len(raw_row) else None
        rows.append(row)
    return ReportTable(columns=columns, rows=rows)


def _metric(row: dict[str, Any], *names: str) -> Any:
    """First present value among alias ``names`` in a parsed row."""
    for name in names:
        if row.get(name) is not None:
            return row[name]
    return None


def _fill_metrics(target: Any, row: dict[str, Any]) -> None:
    """Fill the shared metric fields of a totals/video model from a row."""
    target.views = _to_int(_metric(row, "views"))
    target.watch_time_minutes = _to_float(
        _metric(row, "estimatedMinutesWatched", "watchTimeMinutes")
    )
    target.average_view_duration_seconds = _to_float(
        _metric(row, "averageViewDuration")
    )
    target.average_view_percentage = _to_float(_metric(row, "averageViewPercentage"))
    target.likes = _to_int(_metric(row, "likes"))
    target.comment_count = _to_int(_metric(row, "comments", "commentCount"))
    target.shares = _to_int(_metric(row, "shares"))
    target.subscribers_gained = _to_int(_metric(row, "subscribersGained"))
    if hasattr(target, "subscribers_lost"):
        target.subscribers_lost = _to_int(_metric(row, "subscribersLost"))


def _fmt_date(value: Any) -> str | None:
    """Normalise a ``date`` / ``YYYY-MM-DD`` string, else ``None``."""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and _DATE_RE.match(value.strip()):
        text = value.strip()
        try:
            date.fromisoformat(text)  # rejects e.g. 2026-13-40
        except ValueError:
            return None
        return text
    return None


def _api_error_reason(status_code: int, payload: Any) -> str:
    """Machine-readable reason for a non-2xx Google API response.
    Mirrors :func:`app.style_lab.metadata._api_error_reason`."""
    reasons: list[str] = []
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            for item in error.get("errors") or []:
                if isinstance(item, dict) and item.get("reason"):
                    reasons.append(str(item["reason"]))
    lowered = {r.lower() for r in reasons}
    if lowered & _QUOTA_REASONS or status_code == 429:
        return "quota_exceeded"
    if status_code == 401:
        return "authentication_failed"
    if status_code == 404:
        return "not_found"
    if reasons:
        # Google reasons are camelCase (insufficientPermissions,
        # videoNotFound): split case boundaries, then slugify.
        camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", reasons[0])
        slug = re.sub(r"[^a-z0-9_]+", "_", camel_split.lower()).strip("_")
        if slug:
            return slug
    if status_code == 403:
        return "forbidden"
    if status_code == 400:
        return "invalid_request"
    return f"http_{status_code}"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class YouTubeAnalyticsClient:
    """Transport-only Analytics (+ Data API lifetime stats) client.

    Parameters
    ----------
    token_manager:
        A :class:`.tokens.TokenManager` (or duck-compatible object with
        ``get_status`` / ``get_access_token``). ``None`` means every
        method returns ``not_connected`` with zero HTTP — by design,
        so wiring the client before any channel is connected is safe.
    transport:
        Optional ``httpx`` transport (tests / MockTransport), forwarded
        to the shared :func:`create_async_client` factory, which gives
        it precedence over environment proxy handling.
    quota_ledger:
        Optional :class:`.quota.QuotaLedger`. When given, calls are
        pre-flighted against the safety ceiling and every attempted
        call is recorded (see module docstring).
    timeout_s:
        Per-request timeout. Exactly one attempt is made per call.
    """

    def __init__(
        self,
        *,
        token_manager: Any = None,
        transport: httpx.AsyncBaseTransport | None = None,
        quota_ledger: QuotaLedger | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        safety_ceiling: float = DEFAULT_SAFETY_CEILING,
    ) -> None:
        self._tokens = token_manager
        self._transport = transport
        self._ledger = quota_ledger
        self._timeout_s = timeout_s
        self._safety_ceiling = safety_ceiling

    # -- credential gate (BEFORE any HTTP) -------------------------------

    async def _resolve_access_token(
        self, channel_id: str | None
    ) -> tuple[str | None, str]:
        """``(access_token, unavailable_reason)`` — see module docstring."""
        if self._tokens is None or not channel_id:
            return None, REASON_NO_CREDENTIAL
        get_status = getattr(self._tokens, "get_status", None)
        if callable(get_status):
            try:
                status = get_status(channel_id)
                if inspect.isawaitable(status):
                    status = await status
            except Exception:
                return None, REASON_OAUTH_TOKEN_UNAVAILABLE
            if getattr(status, "status", None) != STATUS_CONNECTED:
                return None, REASON_OAUTH_TOKEN_UNAVAILABLE
        try:
            token = self._tokens.get_access_token(channel_id)
            if inspect.isawaitable(token):
                token = await token
        except Exception:
            # Includes NeedsReauth (dead refresh token): the channel is
            # effectively not connected for analytics purposes.
            return None, REASON_OAUTH_TOKEN_UNAVAILABLE
        if isinstance(token, str) and token.strip():
            register_secret(token)
            return token, ""
        return None, REASON_OAUTH_TOKEN_UNAVAILABLE

    def _not_connected(self, reason: str) -> AnalyticsResult[Any]:
        return AnalyticsResult(
            status=STATUS_NOT_CONNECTED,
            reason=reason,
            detail=(
                "No connected YouTube channel credential is available; "
                "analytics is unavailable and no API call was made."
            ),
            quota_units=0,
        )

    @staticmethod
    def _failed(
        reason: str, detail: str, *, quota_units: int = 0,
        quota_method: str | None = None,
    ) -> AnalyticsResult[Any]:
        return AnalyticsResult(
            status=STATUS_FAILED,
            reason=reason,
            detail=redact_text(detail)[:300],
            quota_units=quota_units,
            quota_method=quota_method,
        )

    # -- quota seam --------------------------------------------------------

    def _quota_preflight(self, method: str) -> AnalyticsResult[Any] | None:
        """Refusal result when the injected ledger's ceiling would be
        crossed, else ``None``. No ledger -> no pre-flight (the result
        still reports ``quota_units`` for the caller's own accounting)."""
        if self._ledger is None:
            return None
        bucket, units = classify_method(method)
        limit = BUCKET_LIMITS[bucket]
        day = pacific_day()
        used = self._ledger.used_units(bucket, day)
        if used + units > int(limit * self._safety_ceiling):
            return self._failed(
                "quota_exceeded",
                f"quota reservation refused: {method} costs {units} unit(s) "
                f"in bucket {bucket!r}; {used}/{limit} already used today (PT).",
                quota_units=0,
                quota_method=method,
            )
        return None

    def _quota_record(
        self, method: str, channel_id: str | None, *, success: bool
    ) -> None:
        if self._ledger is None:
            return
        bucket, units = classify_method(method)
        self._ledger.record(
            LedgerEntry(
                bucket=bucket,
                method=method,
                units=units,
                day_pt=pacific_day(),
                channel_id=channel_id,
                success=success,
            )
        )

    # -- HTTP plumbing (single attempt; errors -> typed result) -----------

    async def _get_json(
        self,
        *,
        url: str,
        params: dict[str, str],
        access_token: str,
        method: str,
        channel_id: str | None,
    ) -> tuple[dict[str, Any] | None, AnalyticsResult[Any] | None]:
        """One GET. Returns ``(payload, None)`` or ``(None, failure)``;
        the failure already carries the charged ``quota_units`` and the
        ledger entry has been written. Never raises."""
        _, units = classify_method(method)
        kwargs: dict[str, Any] = {"timeout": self._timeout_s}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        try:
            async with create_async_client(**kwargs) as client:
                resp = await client.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
        except httpx.TimeoutException:
            self._quota_record(method, channel_id, success=False)
            return None, self._failed(
                "timeout", "YouTube API request timed out.",
                quota_units=units, quota_method=method,
            )
        except httpx.TransportError:
            self._quota_record(method, channel_id, success=False)
            return None, self._failed(
                "network_error", "YouTube API could not be reached.",
                quota_units=units, quota_method=method,
            )
        except httpx.HTTPError:
            self._quota_record(method, channel_id, success=False)
            return None, self._failed(
                "http_error", "YouTube API request failed.",
                quota_units=units, quota_method=method,
            )
        except Exception:
            self._quota_record(method, channel_id, success=False)
            return None, self._failed(
                "network_error", "YouTube API request failed unexpectedly.",
                quota_units=units, quota_method=method,
            )
        success = 200 <= resp.status_code < 300
        self._quota_record(method, channel_id, success=success)
        try:
            payload = resp.json()
        except Exception:
            if success:
                return None, self._failed(
                    "invalid_response_json",
                    "YouTube API returned a non-JSON body.",
                    quota_units=units, quota_method=method,
                )
            return None, self._failed(
                _api_error_reason(resp.status_code, None),
                f"YouTube API returned HTTP {resp.status_code}.",
                quota_units=units, quota_method=method,
            )
        if not success:
            return None, self._failed(
                _api_error_reason(resp.status_code, payload),
                f"YouTube API returned HTTP {resp.status_code}.",
                quota_units=units, quota_method=method,
            )
        if not isinstance(payload, dict):
            return None, self._failed(
                "malformed_response",
                "YouTube API response was not a JSON object.",
                quota_units=units, quota_method=method,
            )
        return payload, None

    async def _reports_query(
        self,
        *,
        channel_id: str,
        access_token: str,
        params: dict[str, str],
    ) -> tuple[ReportTable | None, dict[str, Any] | None, AnalyticsResult[Any] | None]:
        """One Analytics reports query -> ``(table, payload, failure)``."""
        payload, failure = await self._get_json(
            url=f"{ANALYTICS_BASE}{REPORTS_PATH}",
            params=params,
            access_token=access_token,
            method=METHOD_ANALYTICS_QUERY,
            channel_id=channel_id,
        )
        if failure is not None:
            return None, None, failure
        assert payload is not None
        table = _parse_report_table(payload)
        if table is None:
            _, units = classify_method(METHOD_ANALYTICS_QUERY)
            return None, None, self._failed(
                "malformed_response",
                "Analytics response was not a resultTable "
                "(missing/invalid columnHeaders).",
                quota_units=units, quota_method=METHOD_ANALYTICS_QUERY,
            )
        return table, payload, None

    def _validate_range(
        self, start_date: Any, end_date: Any
    ) -> tuple[str, str] | AnalyticsResult[Any]:
        start = _fmt_date(start_date)
        end = _fmt_date(end_date)
        if start is None or end is None or end < start:
            return self._failed(
                "invalid_date_range",
                "start_date/end_date must be YYYY-MM-DD dates with "
                "end_date on or after start_date.",
            )
        return start, end

    async def _gate(
        self, channel_id: str | None, method: str
    ) -> tuple[str, AnalyticsResult[Any] | None]:
        """Credential gate + quota pre-flight, in that order.

        Returns ``(access_token, None)`` to proceed or ``("", failure)``
        where failure is the typed result to return verbatim (both
        outcomes guarantee zero HTTP so far)."""
        token, reason = await self._resolve_access_token(channel_id)
        if token is None:
            return "", self._not_connected(reason)
        refusal = self._quota_preflight(method)
        if refusal is not None:
            return "", refusal
        return token, None

    # -- public API ---------------------------------------------------------

    async def query_report(
        self,
        *,
        channel_id: str,
        start_date: Any,
        end_date: Any,
        metrics: str,
        dimensions: str | None = None,
        filters: str | None = None,
        sort: str | None = None,
    ) -> AnalyticsResult[ReportTable]:
        """Generic Analytics reports query (the low-level seam the
        typed methods below are built on, exposed for the sync team).

        ``metrics`` / ``dimensions`` / ``filters`` / ``sort`` are passed
        through in the API's own comma syntax. Costs
        :data:`QUOTA_COST_ANALYTICS_QUERY` (1 unit, shared bucket)."""
        valid = self._validate_range(start_date, end_date)
        if isinstance(valid, AnalyticsResult):
            return valid
        start, end = valid
        token, failure = await self._gate(channel_id, METHOD_ANALYTICS_QUERY)
        if failure is not None:
            return failure
        params: dict[str, str] = {
            "ids": f"channel=={channel_id}",
            "startDate": start,
            "endDate": end,
            "metrics": metrics,
        }
        if dimensions:
            params["dimensions"] = dimensions
        if filters:
            params["filters"] = filters
        if sort:
            params["sort"] = sort
        table, payload, failure = await self._reports_query(
            channel_id=channel_id, access_token=token, params=params
        )
        if failure is not None:
            return failure
        assert table is not None
        return AnalyticsResult(
            status=STATUS_OK, data=table,
            quota_units=QUOTA_COST_ANALYTICS_QUERY,
            quota_method=METHOD_ANALYTICS_QUERY, raw_json=payload,
        )

    async def get_channel_totals(
        self, *, channel_id: str, start_date: Any, end_date: Any
    ) -> AnalyticsResult[ChannelTotals]:
        """Date-range totals for the channel (one Analytics query)."""
        valid = self._validate_range(start_date, end_date)
        if isinstance(valid, AnalyticsResult):
            return valid
        start, end = valid
        token, failure = await self._gate(channel_id, METHOD_ANALYTICS_QUERY)
        if failure is not None:
            return failure
        table, payload, failure = await self._reports_query(
            channel_id=channel_id,
            access_token=token,
            params={
                "ids": f"channel=={channel_id}",
                "startDate": start,
                "endDate": end,
                "metrics": _TOTAL_METRICS,
            },
        )
        if failure is not None:
            return failure
        assert table is not None
        totals = ChannelTotals(channel_id=channel_id, start_date=start, end_date=end)
        if table.rows:
            _fill_metrics(totals, table.rows[0])
            totals.has_data = True
        return AnalyticsResult(
            status=STATUS_OK, data=totals,
            quota_units=QUOTA_COST_ANALYTICS_QUERY,
            quota_method=METHOD_ANALYTICS_QUERY, raw_json=payload,
        )

    async def get_video_metrics(
        self, *, channel_id: str, video_id: str, start_date: Any, end_date: Any
    ) -> AnalyticsResult[VideoMetrics]:
        """Metrics for ONE video. A video with no views in range comes
        back ``ok`` with ``has_data=False`` and all metrics ``None``."""
        result = await self.get_videos_metrics(
            channel_id=channel_id, video_ids=[video_id],
            start_date=start_date, end_date=end_date,
        )
        if result.data is None:
            return AnalyticsResult(
                status=result.status, reason=result.reason,
                detail=result.detail, quota_units=result.quota_units,
                quota_method=result.quota_method,
            )
        metrics = result.data[0] if result.data else VideoMetrics(
            video_id=video_id, start_date=_fmt_date(start_date) or "",
            end_date=_fmt_date(end_date) or "",
        )
        return AnalyticsResult(
            status=STATUS_OK, data=metrics,
            quota_units=result.quota_units,
            quota_method=result.quota_method, raw_json=result.raw_json,
        )

    async def get_videos_metrics(
        self,
        *,
        channel_id: str,
        video_ids: list[str],
        start_date: Any,
        end_date: Any,
    ) -> AnalyticsResult[list[VideoMetrics]]:
        """Metrics for several videos in ONE Analytics query
        (``dimensions=video`` + ``filters=video==a,b,...``).

        The result preserves the REQUESTED id order; ids the API
        returned no row for appear with ``has_data=False`` / metrics
        ``None`` rather than being silently dropped."""
        valid = self._validate_range(start_date, end_date)
        if isinstance(valid, AnalyticsResult):
            return valid
        start, end = valid
        ids = [str(v).strip() for v in (video_ids or []) if str(v).strip()]
        if not ids:
            return self._failed(
                "invalid_argument", "video_ids must contain at least one video id."
            )
        token, failure = await self._gate(channel_id, METHOD_ANALYTICS_QUERY)
        if failure is not None:
            return failure
        table, payload, failure = await self._reports_query(
            channel_id=channel_id,
            access_token=token,
            params={
                "ids": f"channel=={channel_id}",
                "startDate": start,
                "endDate": end,
                "metrics": _VIDEO_METRICS,
                "dimensions": "video",
                "filters": "video==" + ",".join(ids),
                "sort": "-views",
            },
        )
        if failure is not None:
            return failure
        assert table is not None
        by_id: dict[str, VideoMetrics] = {}
        for row in table.rows:
            vid = row.get("video")
            if not isinstance(vid, str) or not vid:
                continue
            metrics = VideoMetrics(video_id=vid, start_date=start, end_date=end)
            _fill_metrics(metrics, row)
            metrics.has_data = True
            by_id[vid] = metrics
        ordered = [
            by_id.get(vid)
            or VideoMetrics(video_id=vid, start_date=start, end_date=end)
            for vid in ids
        ]
        return AnalyticsResult(
            status=STATUS_OK, data=ordered,
            quota_units=QUOTA_COST_ANALYTICS_QUERY,
            quota_method=METHOD_ANALYTICS_QUERY, raw_json=payload,
        )

    async def get_retention_curve(
        self, *, channel_id: str, video_id: str, start_date: Any, end_date: Any
    ) -> AnalyticsResult[RetentionCurve]:
        """Audience-retention curve for one video (report type
        :data:`REPORT_TYPE_AUDIENCE_RETENTION`): ``audienceWatchRatio``
        by ``elapsedVideoTimeRatio``, one Analytics query."""
        valid = self._validate_range(start_date, end_date)
        if isinstance(valid, AnalyticsResult):
            return valid
        start, end = valid
        if not video_id or not str(video_id).strip():
            return self._failed("invalid_argument", "video_id is required.")
        token, failure = await self._gate(channel_id, METHOD_ANALYTICS_QUERY)
        if failure is not None:
            return failure
        table, payload, failure = await self._reports_query(
            channel_id=channel_id,
            access_token=token,
            params={
                "ids": f"channel=={channel_id}",
                "startDate": start,
                "endDate": end,
                "metrics": "audienceWatchRatio",
                "dimensions": "elapsedVideoTimeRatio",
                "filters": f"video=={video_id}",
                "sort": "elapsedVideoTimeRatio",
            },
        )
        if failure is not None:
            return failure
        assert table is not None
        points: list[RetentionPoint] = []
        for row in table.rows:
            elapsed = _to_float(row.get("elapsedVideoTimeRatio"))
            watch = _to_float(row.get("audienceWatchRatio"))
            if elapsed is None or watch is None:
                continue
            points.append(RetentionPoint(elapsed_ratio=elapsed, watch_ratio=watch))
        points.sort(key=lambda p: p.elapsed_ratio)
        curve = RetentionCurve(
            video_id=str(video_id), start_date=start, end_date=end, points=points
        )
        return AnalyticsResult(
            status=STATUS_OK, data=curve,
            quota_units=QUOTA_COST_ANALYTICS_QUERY,
            quota_method=METHOD_ANALYTICS_QUERY, raw_json=payload,
        )

    async def get_channel_lifetime_stats(
        self, *, channel_id: str
    ) -> AnalyticsResult[ChannelLifetimeStats]:
        """Lifetime channel counters via Data API ``channels.list``
        (``part=statistics``) — the Data API half of this client.
        Distinct from the date-range :meth:`get_channel_totals`."""
        token, failure = await self._gate(channel_id, METHOD_CHANNELS_LIST)
        if failure is not None:
            return failure
        payload, failure = await self._get_json(
            url=f"{API_BASE}/channels",
            params={"part": "statistics", "id": channel_id},
            access_token=token,
            method=METHOD_CHANNELS_LIST,
            channel_id=channel_id,
        )
        if failure is not None:
            return failure
        assert payload is not None
        items = payload.get("items")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            _, units = classify_method(METHOD_CHANNELS_LIST)
            return self._failed(
                "not_found", "channels.list returned no usable channel item.",
                quota_units=units, quota_method=METHOD_CHANNELS_LIST,
            )
        statistics = items[0].get("statistics")
        if not isinstance(statistics, dict):
            _, units = classify_method(METHOD_CHANNELS_LIST)
            return self._failed(
                "malformed_response",
                "channels.list item is missing statistics.",
                quota_units=units, quota_method=METHOD_CHANNELS_LIST,
            )
        hidden = bool(statistics.get("hiddenSubscriberCount"))
        stats = ChannelLifetimeStats(
            channel_id=channel_id,
            view_count=_to_int(statistics.get("viewCount")),
            subscriber_count=(
                None if hidden else _to_int(statistics.get("subscriberCount"))
            ),
            video_count=_to_int(statistics.get("videoCount")),
            hidden_subscriber_count=hidden,
        )
        return AnalyticsResult(
            status=STATUS_OK, data=stats,
            quota_units=QUOTA_COST_CHANNELS_LIST,
            quota_method=METHOD_CHANNELS_LIST, raw_json=payload,
        )
