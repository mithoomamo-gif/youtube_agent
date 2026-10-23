"""Comment fetching — ``commentThreads.list`` (Round 5, Agent 28).

This module is the ONLY Comments Intelligence path that talks to
YouTube for comments, and it is READ-ONLY by construction:

* the only endpoint ever called is ``GET /commentThreads``
  (``commentThreads.list``).  No ``comments.insert``, no
  ``comments.update`` / ``comments.delete``, no ``comments.setModerationStatus``
  — no mutating YouTube endpoint exists anywhere in this module;
* it runs ONLY when a usable YouTube credential actually resolves (see
  the credential gate below); otherwise it returns
  ``status="not_connected"`` and makes **no HTTP request at all** —
  the shared client factory is never even invoked;
* every HTTP call goes through the shared factory
  :func:`app.providers.http_client.create_async_client`;
* every failure is reported as a typed status + machine-readable
  ``reason`` — raw ``httpx`` exceptions never leak to callers, and no
  comment data is ever fabricated on failure (``comments`` stays empty
  unless the fetch completed; a mid-pagination failure discards the
  partial pages — see :func:`fetch_video_comments`).

Credential gate (ONE clear check, consulted in this order — the
:mod:`app.style_lab.metadata` precedent, verbatim in shape)
------------------------------------------------------------
1. **API key** — ``secret_provider.get_secret("youtube/api_key")``
   (sync or async provider shapes both accepted; a credential-store
   failure is treated as "not configured", exactly as the precedent
   and ``app.providers.connection_test`` treat it).  Public comments
   on a public video are valid reads with an API key.
2. **Connected-channel OAuth access token** — only when the caller
   supplies BOTH ``token_manager`` (a
   :class:`app.providers.youtube.tokens.TokenManager`) and
   ``oauth_channel_id`` (the channel's EXTERNAL YouTube channel id —
   the key the TokenManager stores blobs under).
   ``token_manager.get_access_token(oauth_channel_id)`` raising (no
   blob, ``NeedsReauth``, …) means "no usable credential".

Whichever credential resolves is registered with the redaction choke
point (:func:`app.redaction.register_secret`) BEFORE it is used and is
never logged or returned.

What is stored (the storage contract this fetch feeds)
------------------------------------------------------
Each ``commentThreads.list`` item maps to ONE top-level record plus
one record per inline reply:

* top-level — ``id`` = ``snippet.topLevelComment.id``,
  ``video_id``, ``author_display_name`` =
  ``topLevelComment.snippet.authorDisplayName``,
  ``author_channel_id`` = ``...authorChannelId.value`` (when present),
  ``text`` = ``textOriginal`` when present else ``textDisplay``,
  ``like_count`` = ``likeCount``, ``reply_count`` =
  thread ``snippet.totalReplyCount``, ``parent_comment_id`` = None,
  ``is_top_level`` = True, ``published_at`` = the raw RFC-3339
  ``publishedAt`` string (the store coerces it to epoch ms).
* replies — the thread's inline ``replies.comments[]`` (the API
  returns at most 5 per thread; remaining replies are NOT expanded
  via ``comments.list`` in this round — their count is already
  reflected in the parent's ``reply_count``).  Same field mapping from
  each reply's own ``snippet``, with ``parent_comment_id`` = the
  top-level comment id, ``is_top_level`` = False, ``reply_count`` = 0.

Quota
-----
``commentThreads.list`` classifies (via
:func:`app.providers.youtube.quota.classify_method`) into the
``shared`` bucket at 1 unit per call — i.e. 1 unit per PAGE.  When the
caller supplies a ``quota_ledger`` (the
:class:`~app.providers.youtube.quota.QuotaLedger` protocol), each
page is pre-flight checked against the safety ceiling (a refusal ends
the fetch with ``status="quota_exceeded"`` BEFORE that page's HTTP
call) and a :class:`~app.providers.youtube.quota.LedgerEntry` is
recorded for every attempted page, success or failure — Google
charges for failed calls too.  ``quota_units_used`` on the result is
the number of units this fetch actually recorded (0 when no ledger
was supplied — never estimated or faked).
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.providers.http_client import create_async_client
from app.providers.secrets_env import create_secret_provider
from app.redaction import register_secret

from .quota import (
    BUCKET_LIMITS,
    DEFAULT_SAFETY_CEILING,
    LedgerEntry,
    classify_method,
    pacific_day,
)

__all__ = [
    "API_BASE",
    "API_KEY_REF",
    "METHOD",
    "STATUS_FETCHED",
    "STATUS_NOT_CONNECTED",
    "STATUS_COMMENTS_DISABLED",
    "STATUS_VIDEO_NOT_FOUND",
    "STATUS_AUTH_FAILED",
    "STATUS_QUOTA_EXCEEDED",
    "STATUS_FETCH_FAILED",
    "REASON_NO_CREDENTIAL",
    "REASON_OAUTH_TOKEN_UNAVAILABLE",
    "FetchedComment",
    "CommentFetchResult",
    "fetch_video_comments",
    "fetch_video_comments_sync",
]

#: Official YouTube Data API v3 base (same constant as
#: ``app.providers.youtube.client.API_BASE``).
API_BASE = "https://www.googleapis.com/youtube/v3"

#: Secret ref for a YouTube Data API key (metadata precedent).
API_KEY_REF = "youtube/api_key"

#: The one and only API method this module ever calls.
METHOD = "commentThreads.list"

STATUS_FETCHED = "fetched"
STATUS_NOT_CONNECTED = "not_connected"
STATUS_COMMENTS_DISABLED = "comments_disabled"
STATUS_VIDEO_NOT_FOUND = "video_not_found"
STATUS_AUTH_FAILED = "auth_failed"
STATUS_QUOTA_EXCEEDED = "quota_exceeded"
STATUS_FETCH_FAILED = "fetch_failed"

REASON_NO_CREDENTIAL = "no_youtube_credential"
REASON_OAUTH_TOKEN_UNAVAILABLE = "oauth_token_unavailable"

_DEFAULT_TIMEOUT_S = 30.0
#: commentThreads.list allows maxResults up to 100.
_PAGE_SIZE = 100
#: Hard pagination cap — a runaway nextPageToken loop can never spin
#: forever (100 pages x 100 threads is far beyond any real sync).
_MAX_PAGES = 100

_QUOTA_REASONS = {
    "quotaexceeded",
    "dailylimitexceeded",
    "ratelimitexceeded",
    "userratelimitexceeded",
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass
class FetchedComment:
    """One comment in the storage contract shape (see module docstring).

    Field names match :meth:`app.comments.store.CommentsStore.upsert_comment`'s
    accepted mapping keys exactly, so a fetched page can be handed to
    the store unmodified (``dataclasses.asdict``).
    """

    id: str
    video_id: str
    author_display_name: str | None = None
    author_channel_id: str | None = None
    text: str = ""
    like_count: int = 0
    reply_count: int = 0
    parent_comment_id: str | None = None
    is_top_level: bool = True
    #: Raw RFC-3339 string from the API; the store coerces to epoch ms.
    published_at: str | None = None


@dataclass
class CommentFetchResult:
    """Outcome of one :func:`fetch_video_comments` call.

    ``status`` is one of ``fetched`` / ``not_connected`` /
    ``comments_disabled`` / ``video_not_found`` / ``auth_failed`` /
    ``quota_exceeded`` / ``fetch_failed``.  ``comments`` is populated
    ONLY on ``fetched`` — failures never carry partial or fabricated
    data.  ``pages_fetched`` counts completed pages;
    ``quota_units_used`` counts units recorded against the supplied
    quota ledger (0 when no ledger was supplied).
    """

    status: str
    video_id: str
    comments: list[FetchedComment] = field(default_factory=list)
    pages_fetched: int = 0
    quota_units_used: int = 0
    reason: str | None = None
    detail: str = ""


# ---------------------------------------------------------------------------
# Credential gate (ONE check before any HTTP — metadata precedent)
# ---------------------------------------------------------------------------
@dataclass
class _Credential:
    kind: str  # "api_key" | "oauth"
    value: str


async def _resolve_credential(
    *,
    secret_provider: Any,
    token_manager: Any,
    oauth_channel_id: str | None,
) -> tuple[_Credential | None, str]:
    """Return ``(credential, unavailable_reason)`` — see module docstring."""
    provider = secret_provider if secret_provider is not None else create_secret_provider()
    key: Any = None
    get_secret = getattr(provider, "get_secret", None)
    if callable(get_secret):
        try:
            key = get_secret(API_KEY_REF)
            if inspect.isawaitable(key):
                key = await key
        except Exception:
            # Credential store unavailable == not configured
            # (connection_test.py / metadata precedent).
            key = None
    elif callable(getattr(provider, "get", None)):
        # Sync SecretStore-shaped provider (youtube package protocol).
        try:
            key = provider.get(API_KEY_REF)
        except Exception:
            key = None
    if isinstance(key, str) and key.strip():
        register_secret(key)
        return _Credential("api_key", key), ""

    if token_manager is not None and oauth_channel_id:
        try:
            token = token_manager.get_access_token(oauth_channel_id)
            if inspect.isawaitable(token):
                token = await token
        except Exception:
            return None, REASON_OAUTH_TOKEN_UNAVAILABLE
        if isinstance(token, str) and token.strip():
            register_secret(token)
            return _Credential("oauth", token), ""
        return None, REASON_OAUTH_TOKEN_UNAVAILABLE

    return None, REASON_NO_CREDENTIAL


# ---------------------------------------------------------------------------
# API plumbing (single attempt per page; errors -> typed, never raised)
# ---------------------------------------------------------------------------
def _api_error(status_code: int, payload: Any) -> tuple[str, str]:
    """Map a non-2xx response to ``(result_status, reason)``."""
    reasons: list[str] = []
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            for item in error.get("errors") or []:
                if isinstance(item, dict) and item.get("reason"):
                    reasons.append(str(item["reason"]))
    lowered = {r.lower() for r in reasons}
    if "commentsdisabled" in lowered:
        return STATUS_COMMENTS_DISABLED, "comments_disabled"
    if status_code == 403 and lowered & _QUOTA_REASONS:
        return STATUS_QUOTA_EXCEEDED, "quota_exceeded"
    if status_code == 429:
        return STATUS_QUOTA_EXCEEDED, "quota_exceeded"
    if status_code == 401 or lowered & {"autherror", "keyinvalid", "apikeyinvalid"}:
        return STATUS_AUTH_FAILED, "authentication_failed"
    if status_code == 403 and "forbidden" in lowered:
        return STATUS_AUTH_FAILED, "forbidden"
    if status_code == 404 or lowered & {"videonotfound", "notfound"}:
        return STATUS_VIDEO_NOT_FOUND, "video_not_found"
    if status_code == 400 and "videonotfound" in lowered:
        return STATUS_VIDEO_NOT_FOUND, "video_not_found"
    if reasons:
        slug = re.sub(r"[^a-z0-9_]+", "_", reasons[0].lower()).strip("_")
        if slug:
            return STATUS_FETCH_FAILED, slug
    return STATUS_FETCH_FAILED, f"http_{status_code}"


async def _api_get_page(
    client: httpx.AsyncClient,
    params: dict[str, str],
    credential: _Credential,
) -> tuple[dict[str, Any] | None, str | None, str | None, str]:
    """One GET of one commentThreads page.

    Returns ``(payload, status, reason, detail)`` — exactly one of
    ``payload`` / ``status`` is set.  Never raises for transport,
    HTTP-status or JSON problems.
    """
    query = dict(params)
    headers: dict[str, str] = {}
    if credential.kind == "api_key":
        query["key"] = credential.value
    else:
        headers["Authorization"] = f"Bearer {credential.value}"
    try:
        resp = await client.get(
            f"{API_BASE}/commentThreads", params=query, headers=headers
        )
    except httpx.TimeoutException:
        return None, STATUS_FETCH_FAILED, "timeout", "YouTube API request timed out."
    except httpx.TransportError:
        return None, STATUS_FETCH_FAILED, "network_error", "YouTube API could not be reached."
    except httpx.HTTPError:
        return None, STATUS_FETCH_FAILED, "http_error", "YouTube API request failed."
    except Exception:
        return None, STATUS_FETCH_FAILED, "network_error", "YouTube API request failed unexpectedly."
    try:
        payload = resp.json()
    except Exception:
        if 200 <= resp.status_code < 300:
            return None, STATUS_FETCH_FAILED, "invalid_response_json", (
                "YouTube API returned a non-JSON body."
            )
        status, reason = _api_error(resp.status_code, None)
        return None, status, reason, f"YouTube API returned HTTP {resp.status_code}."
    if not 200 <= resp.status_code < 300:
        status, reason = _api_error(resp.status_code, payload)
        return None, status, reason, f"YouTube API returned HTTP {resp.status_code}."
    if not isinstance(payload, dict):
        return None, STATUS_FETCH_FAILED, "malformed_response", (
            "YouTube API response was not a JSON object."
        )
    return payload, None, None, ""


# ---------------------------------------------------------------------------
# Payload mapping (pure — unit-testable without HTTP)
# ---------------------------------------------------------------------------
def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _map_comment_snippet(
    comment_id: str,
    snippet: dict[str, Any],
    *,
    video_id: str,
    parent_comment_id: str | None,
    reply_count: int,
) -> FetchedComment:
    author_channel = snippet.get("authorChannelId")
    author_channel_id = (
        author_channel.get("value")
        if isinstance(author_channel, dict)
        else None
    )
    text = snippet.get("textOriginal")
    if not isinstance(text, str) or not text:
        text = snippet.get("textDisplay")
    return FetchedComment(
        id=comment_id,
        video_id=video_id,
        author_display_name=snippet.get("authorDisplayName") or None,
        author_channel_id=author_channel_id or None,
        text=text if isinstance(text, str) else "",
        like_count=_safe_int(snippet.get("likeCount")),
        reply_count=reply_count,
        parent_comment_id=parent_comment_id,
        is_top_level=parent_comment_id is None,
        published_at=snippet.get("publishedAt") or None,
    )


def _map_page(payload: dict[str, Any], video_id: str) -> list[FetchedComment] | None:
    """Map one commentThreads page; ``None`` when the page is malformed."""
    items = payload.get("items")
    if items is None:
        return []
    if not isinstance(items, list):
        return None
    out: list[FetchedComment] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        thread_snippet = item.get("snippet")
        if not isinstance(thread_snippet, dict):
            return None
        top = thread_snippet.get("topLevelComment")
        if not isinstance(top, dict) or not isinstance(top.get("id"), str):
            return None
        top_snippet = top.get("snippet")
        if not isinstance(top_snippet, dict):
            return None
        top_id = top["id"]
        out.append(
            _map_comment_snippet(
                top_id,
                top_snippet,
                video_id=video_id,
                parent_comment_id=None,
                reply_count=_safe_int(thread_snippet.get("totalReplyCount")),
            )
        )
        replies = item.get("replies")
        if isinstance(replies, dict):
            for reply in replies.get("comments") or []:
                if not isinstance(reply, dict) or not isinstance(reply.get("id"), str):
                    continue
                reply_snippet = reply.get("snippet")
                if not isinstance(reply_snippet, dict):
                    continue
                out.append(
                    _map_comment_snippet(
                        reply["id"],
                        reply_snippet,
                        video_id=video_id,
                        parent_comment_id=top_id,
                        reply_count=0,
                    )
                )
    return out


# ---------------------------------------------------------------------------
# Quota seam (optional ledger — pre-flight + record, client precedent)
# ---------------------------------------------------------------------------
def _quota_preflight(ledger: Any, *, channel_id: str | None) -> str | None:
    """Refusal detail when the next page would cross the ceiling."""
    bucket, units = classify_method(METHOD)
    limit = BUCKET_LIMITS[bucket]
    used = ledger.used_units(bucket, pacific_day())
    ceiling_units = int(limit * DEFAULT_SAFETY_CEILING)
    if used + units > ceiling_units:
        return (
            f"quota reservation refused: {METHOD} costs {units} unit(s) "
            f"in bucket {bucket!r}; {used}/{limit} already used and the "
            f"safety ceiling is {ceiling_units}"
        )
    return None


def _quota_record(ledger: Any, *, channel_id: str | None, success: bool) -> int:
    """Record one page call against the ledger; return units charged."""
    bucket, units = classify_method(METHOD)
    ledger.record(
        LedgerEntry(
            bucket=bucket,
            method=METHOD,
            units=units,
            channel_id=channel_id,
            success=success,
        )
    )
    return units


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
async def fetch_video_comments(
    video_id: str,
    *,
    secret_provider: Any = None,
    token_manager: Any = None,
    oauth_channel_id: str | None = None,
    quota_ledger: Any = None,
    quota_channel_id: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    max_pages: int = _MAX_PAGES,
) -> CommentFetchResult:
    """Fetch ALL comment threads for one video (read-only).

    Pages through ``commentThreads.list``
    (``part=snippet,replies``, ``maxResults=100``) until the API stops
    returning ``nextPageToken`` (or ``max_pages`` is hit).  Statuses:
    ``fetched`` / ``not_connected`` (no usable credential — NO HTTP
    request was made) / ``comments_disabled`` / ``video_not_found`` /
    ``auth_failed`` / ``quota_exceeded`` / ``fetch_failed``.  This
    function does not raise for credential, transport, HTTP or
    malformed-response problems, and a failure NEVER carries partial
    pages: ``comments`` is all-or-nothing per fetch.
    """
    video_id = str(video_id or "").strip()
    result = CommentFetchResult(status=STATUS_FETCH_FAILED, video_id=video_id)
    if not video_id:
        result.reason = "missing_video_id"
        result.detail = "No video id was supplied."
        return result

    # -- credential gate (BEFORE any HTTP) -------------------------------
    credential, unavailable_reason = await _resolve_credential(
        secret_provider=secret_provider,
        token_manager=token_manager,
        oauth_channel_id=oauth_channel_id,
    )
    if credential is None:
        result.status = STATUS_NOT_CONNECTED
        result.reason = unavailable_reason
        result.detail = (
            "No usable YouTube credential is configured for this channel; "
            "comment fetch is unavailable and no API call was made."
        )
        return result

    collected: list[FetchedComment] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()
    pages = 0

    # First-page quota pre-flight BEFORE the client exists: a refusal
    # here means the shared factory was never even invoked.
    if quota_ledger is not None:
        refusal = _quota_preflight(quota_ledger, channel_id=quota_channel_id)
        if refusal is not None:
            result.status = STATUS_QUOTA_EXCEEDED
            result.reason = "quota_exceeded"
            result.detail = refusal
            return result

    client_kwargs: dict[str, Any] = {"timeout": timeout_s}
    if transport is not None:
        client_kwargs["transport"] = transport
    async with create_async_client(**client_kwargs) as client:
        while pages < max_pages:
            if quota_ledger is not None and pages > 0:
                # Per-page pre-flight for pages 2+ (page 1 was checked
                # before the client was created, above).
                refusal = _quota_preflight(
                    quota_ledger, channel_id=quota_channel_id
                )
                if refusal is not None:
                    result.status = STATUS_QUOTA_EXCEEDED
                    result.reason = "quota_exceeded"
                    result.detail = refusal
                    result.pages_fetched = pages
                    return result
            params: dict[str, str] = {
                "part": "snippet,replies",
                "videoId": video_id,
                "maxResults": str(_PAGE_SIZE),
                "textFormat": "plainText",
            }
            if page_token:
                params["pageToken"] = page_token
            payload, status, reason, detail = await _api_get_page(
                client, params, credential
            )
            if quota_ledger is not None:
                result.quota_units_used += _quota_record(
                    quota_ledger,
                    channel_id=quota_channel_id,
                    success=payload is not None,
                )
            if payload is None:
                result.status = status or STATUS_FETCH_FAILED
                result.reason = reason
                result.detail = detail
                result.pages_fetched = pages
                result.comments = []
                return result
            mapped = _map_page(payload, video_id)
            if mapped is None:
                result.status = STATUS_FETCH_FAILED
                result.reason = "malformed_response"
                result.detail = "commentThreads.list page had an unusable item shape."
                result.pages_fetched = pages
                result.comments = []
                return result
            collected.extend(mapped)
            pages += 1
            next_token = payload.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                break
            if next_token in seen_tokens:
                break  # token loop — stop rather than spin
            seen_tokens.add(next_token)
            page_token = next_token

    result.status = STATUS_FETCHED
    result.comments = collected
    result.pages_fetched = pages
    return result


def fetch_video_comments_sync(video_id: str, **kwargs: Any) -> CommentFetchResult:
    """Sync facade over :func:`fetch_video_comments`.

    Drives the coroutine via ``app.integration.run_loop.run_coroutine``
    (the codebase's single loop bridge — the metadata precedent);
    async callers must ``await fetch_video_comments(...)`` directly.
    """
    from app.integration.run_loop import run_coroutine

    return run_coroutine(lambda: fetch_video_comments(video_id, **kwargs))
