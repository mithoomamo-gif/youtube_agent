"""Style Lab — public competitor-channel metadata via YouTube Data API v3.

This module is the ONLY Style Lab path that talks to YouTube, and it is
metadata-only by construction:

* no scraping, no unofficial endpoints, no video downloading — the only
  calls are ``channels.list`` and ``videos.list`` on the official
  ``https://www.googleapis.com/youtube/v3`` base;
* it runs ONLY when a usable YouTube credential actually resolves (see
  the credential gate below); otherwise it returns
  ``status="metadata_unavailable"`` and makes **no HTTP request at all**;
* exactly one attempt per API call — no retries, no retry-storms;
* every failure is reported as ``status="fetch_failed"`` with a
  machine-readable ``reason`` — raw ``httpx`` exceptions never leak to
  callers.

Credential gate (ONE clear check, consulted in this order)
----------------------------------------------------------
1. **API key** — ``secret_provider.get_secret("youtube/api_key")``.
   The default ``secret_provider`` is
   :func:`app.providers.secrets_env.create_secret_provider`, i.e. the
   production OS-keyring store (``app.secrets_store``) unless the
   dev-only ``YTA_SECRET_BACKEND=env`` backend is selected.  The env
   backend resolves the ref ``youtube/api_key`` through its generic
   normalisation (``youtube/api_key`` -> ``YOUTUBE_API_KEY``), so a key
   supplied as the ``YOUTUBE_API_KEY`` env var / repo ``.env`` entry is
   usable in dev, and a key stored in the keyring under the same ref is
   usable in production.  A credential-store failure (e.g. no keyring
   backend on a headless machine) is treated exactly like
   :mod:`app.providers.connection_test` treats it: as "not configured".
2. **Connected-channel OAuth access token** — only when the caller
   supplies BOTH ``token_manager`` (an
   :class:`app.providers.youtube.tokens.TokenManager`) and
   ``oauth_channel_id`` (one of the user's own connected channels).
   ``token_manager.get_access_token(oauth_channel_id)`` mints the
   bearer token; public ``channels.list`` / ``videos.list`` reads are
   valid with it.  When no blob is stored for that channel the manager
   raises before any HTTP call, which the gate treats as "no usable
   credential".

Whichever credential resolves is registered with the redaction choke
point (:func:`app.redaction.register_secret`) BEFORE it is used, as
``connection_test.py`` does, and is never logged or returned.

Async/sync boundary
-------------------
The primary entry point is the coroutine :func:`fetch_channel_metadata`
(the router layer is FastAPI async).  :func:`fetch_channel_metadata_sync`
is the sync facade for the service layer; it drives the coroutine through
``app.integration.run_loop.run_coroutine`` — the codebase's single
loop-bridging helper (the same pattern as
``app.integration.content.build_content_auto``) — and raises
``RuntimeError``-free results in standalone sync contexts.

Handle convention: handles are stored WITHOUT the leading ``@`` (the
convention of :mod:`app.style_lab.parsing`); the ``@`` is added back
only for the API's ``forHandle`` parameter, and stripped from the
``customUrl`` the API returns.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app.providers.http_client import create_async_client
from app.providers.secrets_env import create_secret_provider
from app.redaction import redact_text, register_secret

__all__ = [
    "API_BASE",
    "API_KEY_REF",
    "STATUS_FETCHED",
    "STATUS_METADATA_UNAVAILABLE",
    "STATUS_FETCH_FAILED",
    "REASON_NO_CREDENTIAL",
    "REASON_OAUTH_TOKEN_UNAVAILABLE",
    "ChannelMetadata",
    "MetadataFetchResult",
    "fetch_channel_metadata",
    "fetch_channel_metadata_sync",
    "extract_video_id",
]

#: Official YouTube Data API v3 base (same constant as
#: ``app.providers.youtube.client.API_BASE``).
API_BASE = "https://www.googleapis.com/youtube/v3"

#: Secret ref for a YouTube Data API key (see module docstring).
API_KEY_REF = "youtube/api_key"

STATUS_FETCHED = "fetched"
STATUS_METADATA_UNAVAILABLE = "metadata_unavailable"
STATUS_FETCH_FAILED = "fetch_failed"

REASON_NO_CREDENTIAL = "no_youtube_credential"
REASON_OAUTH_TOKEN_UNAVAILABLE = "oauth_token_unavailable"

_DEFAULT_TIMEOUT_S = 15.0

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")

_QUOTA_REASONS = {
    "quotaexceeded",
    "dailylimitexceeded",
    "ratelimitexceeded",
    "userratelimitexceeded",
}


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass
class ChannelMetadata:
    """Outcome of one :func:`fetch_channel_metadata` call.

    ``status`` is one of ``fetched`` / ``metadata_unavailable`` /
    ``fetch_failed``.  On ``fetch_failed`` / ``metadata_unavailable``,
    ``reason`` carries a machine-readable code and whatever identifier
    fields were known (or already fetched) are still populated.
    ``title`` / ``description`` / ``tags`` describe the VIDEO when a
    video was requested; they stay ``None`` / ``None`` / ``[]`` for a
    channel-only fetch.  ``raw_json`` holds the untouched API payloads
    as ``{"channels": ..., "videos": ...}`` (whichever were fetched) —
    payloads only, never credentials.
    """

    status: str
    external_channel_id: str | None = None
    display_name: str | None = None
    handle: str | None = None
    channel_url: str | None = None
    title: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    video_id: str | None = None
    subscriber_count: int | None = None
    view_count: int | None = None
    video_count: int | None = None
    reason: str | None = None
    detail: str = ""
    raw_json: dict[str, Any] | None = None


#: Alias for callers that prefer result-flavoured naming.
MetadataFetchResult = ChannelMetadata


# ---------------------------------------------------------------------------
# Identifier extraction (URLs / bare forms)
# ---------------------------------------------------------------------------
def _strip_handle(handle: str | None) -> str | None:
    if handle is None:
        return None
    text = str(handle).strip()
    if text.startswith("@"):
        text = text[1:]
    return text or None


def extract_video_id(value: str | None) -> str | None:
    """Return the 11-char video id from a bare id or any YouTube video URL."""
    if not value:
        return None
    text = str(value).strip()
    if _VIDEO_ID_RE.match(text):
        return text
    candidate = text if "://" in text else f"https://{text}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    segments = [seg for seg in parsed.path.split("/") if seg]
    if host == "youtu.be":
        return segments[0] if segments and _VIDEO_ID_RE.match(segments[0]) else None
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        if segments and segments[0] == "watch":
            values = parse_qs(parsed.query).get("v") or []
            return values[0] if values and _VIDEO_ID_RE.match(values[0]) else None
        if len(segments) >= 2 and segments[0] in ("shorts", "live", "embed", "v"):
            return segments[1] if _VIDEO_ID_RE.match(segments[1]) else None
    return None


def _identifiers_from_channel_url(url: str | None) -> tuple[str | None, str | None]:
    """(channel_id, handle) from a channel URL; (None, None) otherwise."""
    if not url:
        return None, None
    text = str(url).strip()
    if _CHANNEL_ID_RE.match(text):
        return text, None
    if text.startswith("@"):
        return None, _strip_handle(text)
    candidate = text if "://" in text else f"https://{text}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None, None
    host = (parsed.hostname or "").lower()
    if host not in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        return None, None
    segments = [seg for seg in parsed.path.split("/") if seg]
    if not segments:
        return None, None
    if segments[0] == "channel" and len(segments) >= 2:
        return segments[1], None
    if segments[0].startswith("@"):
        return None, _strip_handle(segments[0])
    if segments[0] in ("c", "user") and len(segments) >= 2:
        # Legacy custom names: the Data API cannot resolve these by
        # forHandle reliably, but trying the name as a handle is the
        # only official-API option (no search.list, no scraping).
        return None, _strip_handle(segments[1])
    return None, None


# ---------------------------------------------------------------------------
# Credential gate
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
            # (connection_test.py precedent).
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
# API plumbing (single attempt; errors -> (reason, detail), never raised)
# ---------------------------------------------------------------------------
def _api_error_reason(status_code: int, payload: Any) -> str:
    """Machine-readable reason for a non-2xx YouTube API response."""
    reasons: list[str] = []
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            for item in error.get("errors") or []:
                if isinstance(item, dict) and item.get("reason"):
                    reasons.append(str(item["reason"]))
    lowered = {r.lower() for r in reasons}
    if status_code == 403 and lowered & _QUOTA_REASONS:
        return "quota_exceeded"
    if status_code == 429:
        return "quota_exceeded"
    if status_code == 401:
        return "authentication_failed"
    if status_code == 403 and lowered & {"keyinvalid", "apikeyinvalid", "forbidden"}:
        return "invalid_api_key" if "forbidden" not in lowered else "forbidden"
    if status_code == 404:
        return "not_found"
    if reasons:
        slug = re.sub(r"[^a-z0-9_]+", "_", reasons[0].lower()).strip("_")
        if slug:
            return slug
    return f"http_{status_code}"


async def _api_get(
    client: httpx.AsyncClient,
    resource: str,
    params: dict[str, str],
    credential: _Credential,
) -> tuple[dict[str, Any] | None, str | None, str]:
    """One GET against the Data API. Returns ``(payload, reason, detail)``.

    Exactly one of ``payload`` / ``reason`` is set. Never raises for
    transport, HTTP-status or JSON problems.
    """
    query = dict(params)
    headers: dict[str, str] = {}
    if credential.kind == "api_key":
        query["key"] = credential.value
    else:
        headers["Authorization"] = f"Bearer {credential.value}"
    try:
        resp = await client.get(f"{API_BASE}/{resource}", params=query, headers=headers)
    except httpx.TimeoutException:
        return None, "timeout", "YouTube API request timed out."
    except httpx.TransportError:
        return None, "network_error", "YouTube API could not be reached."
    except httpx.HTTPError:
        return None, "http_error", "YouTube API request failed."
    except Exception:
        return None, "network_error", "YouTube API request failed unexpectedly."
    try:
        payload = resp.json()
    except Exception:
        if 200 <= resp.status_code < 300:
            return None, "invalid_response_json", "YouTube API returned a non-JSON body."
        return None, _api_error_reason(resp.status_code, None), (
            f"YouTube API returned HTTP {resp.status_code}."
        )
    if not 200 <= resp.status_code < 300:
        return None, _api_error_reason(resp.status_code, payload), (
            f"YouTube API returned HTTP {resp.status_code}."
        )
    if not isinstance(payload, dict):
        return None, "malformed_response", "YouTube API response was not a JSON object."
    return payload, None, ""


def _first_item(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """First ``items`` entry: ``(item, None)`` / ``(None, 'not_found')`` /
    ``(None, 'malformed_response')``."""
    items = payload.get("items")
    if items is None:
        return None, "not_found"
    if not isinstance(items, list):
        return None, "malformed_response"
    if not items:
        return None, "not_found"
    item = items[0]
    if not isinstance(item, dict):
        return None, "malformed_response"
    return item, None


def _safe_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _failed(result: ChannelMetadata, reason: str, detail: str) -> ChannelMetadata:
    result.status = STATUS_FETCH_FAILED
    result.reason = reason
    result.detail = redact_text(detail)[:300]
    return result


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
async def fetch_channel_metadata(
    *,
    channel_id: str | None = None,
    handle: str | None = None,
    channel_url: str | None = None,
    video_id: str | None = None,
    video_url: str | None = None,
    parsed: Any = None,
    secret_provider: Any = None,
    token_manager: Any = None,
    oauth_channel_id: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> ChannelMetadata:
    """Fetch a competitor channel's PUBLIC metadata (official API only).

    Identifiers (all optional, first non-empty wins per field):
    ``channel_id`` (``UC…``), ``handle`` (with or without ``@``),
    ``channel_url`` (``/channel/UC…``, ``/@handle``, ``/c/``/``/user/``
    forms), ``video_id`` / ``video_url`` for the video whose
    title/description/tags are wanted, or ``parsed`` — a
    :class:`app.style_lab.parsing.ParsedCompetitor`-shaped object whose
    ``external_channel_id`` / ``handle`` / ``channel_url`` /
    ``external_video_id`` attributes fill any missing field.  A video
    alone also identifies its channel (via the video's own
    ``snippet.channelId``), so a bare video URL is a valid call.

    Statuses: ``fetched`` / ``metadata_unavailable`` (no usable
    credential — NO HTTP request was made) / ``fetch_failed`` (with a
    machine-readable ``reason``).  This function does not raise for
    credential, transport, HTTP or malformed-response problems.
    """
    # -- identifiers ----------------------------------------------------
    if parsed is not None:
        if channel_id is None:
            channel_id = getattr(parsed, "external_channel_id", None)
        if handle is None:
            handle = getattr(parsed, "handle", None)
        if channel_url is None:
            channel_url = getattr(parsed, "channel_url", None)
        if video_id is None:
            video_id = getattr(parsed, "external_video_id", None)
    if channel_url and not channel_id and not handle:
        channel_id, handle = _identifiers_from_channel_url(channel_url)
    if channel_id is None and handle is None and channel_url:
        # Bare "@handle" / bare UC id passed via channel_url.
        channel_id, handle = _identifiers_from_channel_url(channel_url)
    handle = _strip_handle(handle)
    if video_id is None and video_url:
        video_id = extract_video_id(video_url)

    result = ChannelMetadata(
        status=STATUS_METADATA_UNAVAILABLE,
        external_channel_id=channel_id,
        handle=handle,
        channel_url=(
            channel_url
            or (f"https://www.youtube.com/channel/{channel_id}" if channel_id else None)
        ),
        video_id=video_id,
    )

    # -- credential gate (BEFORE any HTTP) -------------------------------
    credential, unavailable_reason = await _resolve_credential(
        secret_provider=secret_provider,
        token_manager=token_manager,
        oauth_channel_id=oauth_channel_id,
    )
    if credential is None:
        result.reason = unavailable_reason
        result.detail = (
            "No usable YouTube credential is configured; public metadata "
            "fetch is unavailable and no API call was made."
        )
        return result

    if not channel_id and not handle and not video_id:
        return _failed(
            result,
            "missing_channel_identifier",
            "No channel id, handle, channel URL or video was supplied.",
        )

    raw: dict[str, Any] = {}
    result.raw_json = raw

    client_kwargs: dict[str, Any] = {"timeout": timeout_s}
    if transport is not None:
        client_kwargs["transport"] = transport
    async with create_async_client(**client_kwargs) as client:
        video_payload: dict[str, Any] | None = None

        # A video alone identifies its channel: fetch it first.
        if video_id and not channel_id and not handle:
            video_payload, reason, detail = await _fetch_video(client, video_id, credential)
            if reason is not None:
                return _failed(result, reason, detail)
            assert video_payload is not None
            raw["videos"] = video_payload
            _apply_video(result, video_payload)
            if result.status == STATUS_FETCH_FAILED:
                return result
            channel_id = result.external_channel_id

        # -- channel ------------------------------------------------------
        params: dict[str, str] = {"part": "snippet,statistics"}
        if channel_id:
            params["id"] = channel_id
        else:
            params["forHandle"] = f"@{handle}"
        payload, reason, detail = await _api_get(client, "channels", params, credential)
        if reason is not None:
            return _failed(result, reason, detail)
        assert payload is not None
        item, item_reason = _first_item(payload)
        if item is None:
            return _failed(
                result,
                "channel_not_found" if item_reason == "not_found" else (item_reason or "malformed_response"),
                "channels.list returned no usable channel item.",
            )
        snippet = item.get("snippet")
        if not isinstance(item.get("id"), str) or not isinstance(snippet, dict):
            return _failed(
                result, "malformed_response", "channels.list item is missing id/snippet."
            )
        raw["channels"] = payload
        result.external_channel_id = item["id"]
        result.display_name = snippet.get("title") or None
        result.handle = _strip_handle(snippet.get("customUrl")) or result.handle
        result.channel_url = f"https://www.youtube.com/channel/{item['id']}"
        statistics = item.get("statistics")
        if isinstance(statistics, dict):
            if not statistics.get("hiddenSubscriberCount"):
                result.subscriber_count = _safe_int(statistics.get("subscriberCount"))
            result.view_count = _safe_int(statistics.get("viewCount"))
            result.video_count = _safe_int(statistics.get("videoCount"))

        # -- video (when requested and not already fetched) ---------------
        if video_id and video_payload is None:
            video_payload, reason, detail = await _fetch_video(client, video_id, credential)
            if reason is not None:
                return _failed(result, reason, detail)
            assert video_payload is not None
            raw["videos"] = video_payload
            _apply_video(result, video_payload)
            if result.status == STATUS_FETCH_FAILED:
                return result

    result.status = STATUS_FETCHED
    return result


async def _fetch_video(
    client: httpx.AsyncClient, video_id: str, credential: _Credential
) -> tuple[dict[str, Any] | None, str | None, str]:
    payload, reason, detail = await _api_get(
        client, "videos", {"part": "snippet", "id": video_id}, credential
    )
    if reason is not None:
        return None, reason, detail
    assert payload is not None
    item, item_reason = _first_item(payload)
    if item is None:
        return None, (
            "video_not_found" if item_reason == "not_found" else (item_reason or "malformed_response")
        ), "videos.list returned no usable video item."
    return payload, None, ""


def _apply_video(result: ChannelMetadata, payload: dict[str, Any]) -> None:
    """Fill video fields (and the owning channel id) from a videos.list
    payload.  Malformed shapes flip ``result`` to fetch_failed."""
    item, _ = _first_item(payload)
    if item is None:
        _failed(result, "malformed_response", "videos.list item is unusable.")
        return
    snippet = item.get("snippet")
    if not isinstance(snippet, dict):
        _failed(result, "malformed_response", "videos.list item is missing snippet.")
        return
    if isinstance(item.get("id"), str):
        result.video_id = item["id"]
    result.title = snippet.get("title") or None
    result.description = snippet.get("description")
    tags = snippet.get("tags")
    result.tags = [str(t) for t in tags] if isinstance(tags, list) else []
    channel_id = snippet.get("channelId")
    if isinstance(channel_id, str) and channel_id:
        result.external_channel_id = channel_id


def fetch_channel_metadata_sync(**kwargs: Any) -> ChannelMetadata:
    """Sync facade over :func:`fetch_channel_metadata` for the service layer.

    Drives the coroutine via ``app.integration.run_loop.run_coroutine``
    (the codebase's single loop bridge — see
    ``app.integration.content.build_content_auto``); async callers (the
    FastAPI router layer) must ``await fetch_channel_metadata(...)``
    directly instead.
    """
    from app.integration.run_loop import run_coroutine

    return run_coroutine(lambda: fetch_channel_metadata(**kwargs))
