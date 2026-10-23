"""Quota-aware HTTP wrapper — the ONLY way the app calls the YouTube API.

Every call goes through :meth:`YouTubeClient.request`, which:

1. Classifies the method into the June 2026 granular buckets
   (see :mod:`.quota`) and RESERVES its units against the ledger BEFORE
   the call, refusing with :class:`QuotaExhausted` when the reservation
   would cross the safety ceiling (default 90% of the bucket). No HTTP
   request is made for a refused call.
2. Writes the ledger entry even for FAILED calls — Google charges >=1
   unit for invalid requests, so the ledger records what was charged,
   not what succeeded.
3. Applies the roadmap error taxonomy:

   - ``403 quotaExceeded`` -> :class:`QuotaExhausted` (stop all project
     API work until the midnight-PT reset; no retry).
   - ``400 uploadLimitExceeded`` -> :class:`UploadLimitExceeded`
     (per-channel pause signal; other channels continue).
   - ``401`` -> one token refresh via the TokenManager, then retry the
     CURRENT step only, once.
   - anything else non-2xx -> :class:`YouTubeAPIError`.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..http_client import create_async_client
from .errors import (
    ChannelMismatch,
    QuotaExhausted,
    UploadLimitExceeded,
    YouTubeAPIError,
)
from .quota import (
    BUCKET_LIMITS,
    DEFAULT_SAFETY_CEILING,
    LedgerEntry,
    QuotaLedger,
    classify_method,
    pacific_day,
)
from .tokens import TokenManager

API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3"


@dataclass
class ChannelInfo:
    """Result of a successful :meth:`YouTubeClient.verify_channel`."""

    channel_id: str
    title: str | None
    #: channels.list ``status.longUploadsStatus`` — channels whose value is
    #: not ``allowed`` (e.g. ``longUploadsUnspecified`` / unverified
    #: accounts) cannot publish videos longer than 15 minutes.
    long_uploads_status: str | None

    @property
    def can_upload_long(self) -> bool:
        return self.long_uploads_status == "allowed"


def _error_reasons(resp: httpx.Response) -> list[str]:
    """Extract Google error ``reason`` strings from a response body."""
    reasons: list[str] = []
    try:
        data = resp.json()
    except Exception:
        text = resp.text or ""
        for needle in ("quotaExceeded", "uploadLimitExceeded"):
            if needle in text:
                reasons.append(needle)
        return reasons
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        for item in error.get("errors", []) or []:
            if isinstance(item, dict) and item.get("reason"):
                reasons.append(str(item["reason"]))
        if not reasons and error.get("status"):
            reasons.append(str(error["status"]))
    return reasons


class YouTubeClient:
    """All YouTube Data API traffic for the app funnels through here."""

    def __init__(
        self,
        *,
        ledger: QuotaLedger,
        token_manager: TokenManager | None = None,
        http: httpx.AsyncClient | None = None,
        project_id: str | None = None,
        safety_ceiling: float = DEFAULT_SAFETY_CEILING,
    ) -> None:
        self._ledger = ledger
        self._tokens = token_manager
        self._http = http
        self._project_id = project_id
        self._safety_ceiling = safety_ceiling

    @property
    def http(self) -> httpx.AsyncClient | None:
        """The injected HTTP client (shared with the upload service)."""
        return self._http

    @property
    def token_manager(self) -> TokenManager | None:
        return self._tokens

    @property
    def ledger(self) -> QuotaLedger:
        return self._ledger

    # -- core request ---------------------------------------------------

    async def request(
        self,
        method: str,
        *,
        channel_id: str | None = None,
        job_id: str | None = None,
        url: str | None = None,
        params: dict | None = None,
        json_body: dict | None = None,
        data: dict | None = None,
        content: bytes | None = None,
        headers: dict | None = None,
        http_method: str = "GET",
        _is_retry: bool = False,
    ) -> httpx.Response:
        """Make one quota-ledgered YouTube API call.

        ``method`` is the API method name (``videos.list``,
        ``thumbnails.set``, ...) used for bucket classification; the wire
        verb is ``http_method`` and the endpoint defaults to
        ``{API_BASE}/<resource>`` unless ``url`` is given.
        """
        # Upload-scope gate (R6-F4): a videos.insert call must never
        # reach the wire on a grant that lacks the youtube.upload
        # scope. Checked locally via the TokenManager BEFORE the quota
        # reservation and before any HTTP — an out-of-scope (or absent)
        # grant raises the typed NeedsReauth refusal here.
        _normalized_method = method.strip().lower()
        if _normalized_method.startswith("youtube."):
            _normalized_method = _normalized_method[len("youtube."):]
        if (
            _normalized_method == "videos.insert"
            and channel_id is not None
            and self._tokens is not None
        ):
            self._tokens.require_upload_scope(channel_id)

        bucket, units = classify_method(method)
        limit = BUCKET_LIMITS[bucket]
        day = pacific_day()

        # (b) Pre-flight reservation against the safety ceiling.
        used = self._ledger.used_units(bucket, day)
        ceiling_units = int(limit * self._safety_ceiling)
        if used + units > ceiling_units:
            raise QuotaExhausted(
                f"quota reservation refused: {method} costs {units} unit(s) "
                f"in bucket {bucket!r}; {used}/{limit} already used and the "
                f"safety ceiling is {ceiling_units}",
                bucket=bucket,
                method=method,
                used=used,
                limit=limit,
            )

        if url is None:
            resource = method.strip().split(".")[0]
            url = f"{API_BASE}/{resource}"

        req_headers: dict[str, str] = dict(headers or {})
        if channel_id is not None and self._tokens is not None:
            access_token = await self._tokens.get_access_token(channel_id)
            req_headers.setdefault("Authorization", f"Bearer {access_token}")

        entry = LedgerEntry(
            bucket=bucket,
            method=method,
            units=units,
            day_pt=day,
            channel_id=channel_id,
            project_id=self._project_id,
            job_id=job_id,
            success=False,
        )
        try:
            resp = await self._send(
                http_method,
                url,
                params=params,
                json_body=json_body,
                data=data,
                content=content,
                headers=req_headers,
            )
        except Exception:
            # (c) Even a call that blew up mid-flight gets a ledger entry.
            self._ledger.record(entry)
            raise
        entry.success = 200 <= resp.status_code < 300
        # (c) Ledger first: failed calls still cost quota.
        self._ledger.record(entry)

        if entry.success:
            return resp

        reasons = _error_reasons(resp)
        if resp.status_code == 403 and "quotaExceeded" in reasons:
            raise QuotaExhausted(
                f"Google reports quotaExceeded for {method}; stop all "
                "project API work until the midnight-PT reset",
                bucket=bucket,
                method=method,
                used=self._ledger.used_units(bucket, day),
                limit=limit,
            )
        if resp.status_code == 400 and "uploadLimitExceeded" in reasons:
            raise UploadLimitExceeded(channel_id)
        if (
            resp.status_code == 401
            and channel_id is not None
            and self._tokens is not None
            and not _is_retry
        ):
            # Refresh once, retry the current step only.
            await self._tokens.refresh(channel_id)
            return await self.request(
                method,
                channel_id=channel_id,
                job_id=job_id,
                url=url,
                params=params,
                json_body=json_body,
                data=data,
                content=content,
                headers=headers,
                http_method=http_method,
                _is_retry=True,
            )
        raise YouTubeAPIError(
            status_code=resp.status_code,
            reason=reasons[0] if reasons else None,
            body=resp.text[:500] if resp.text else None,
        )

    async def _send(
        self,
        http_method: str,
        url: str,
        *,
        params: dict | None,
        json_body: dict | None,
        data: dict | None,
        content: bytes | None,
        headers: dict,
    ) -> httpx.Response:
        kwargs: dict = {"params": params, "headers": headers}
        if json_body is not None:
            kwargs["json"] = json_body
        if data is not None:
            kwargs["data"] = data
        if content is not None:
            kwargs["content"] = content
        if self._http is not None:
            return await self._http.request(http_method, url, **kwargs)
        async with create_async_client(timeout=60.0) as client:
            return await client.request(http_method, url, **kwargs)

    # -- channel verification --------------------------------------------

    async def verify_channel(self, expected_channel_id: str) -> ChannelInfo:
        """Verify the connected account owns EXACTLY ``expected_channel_id``.

        Uses ``channels.list?mine=true``. A wrong-channel upload is the
        unforgivable error, so any deviation — no channel, a different
        channel — raises :class:`ChannelMismatch`.
        """
        resp = await self.request(
            "channels.list",
            channel_id=expected_channel_id,
            params={"part": "snippet,status", "mine": "true"},
        )
        payload = resp.json()
        items = payload.get("items") or []
        if not items:
            raise ChannelMismatch(
                expected_channel_id,
                None,
                "channels.list?mine=true returned no channel for the "
                "connected account",
            )
        item = items[0]
        actual_id = item.get("id")
        if actual_id != expected_channel_id:
            raise ChannelMismatch(expected_channel_id, actual_id)
        snippet = item.get("snippet") or {}
        status = item.get("status") or {}
        return ChannelInfo(
            channel_id=actual_id,
            title=snippet.get("title"),
            long_uploads_status=status.get("longUploadsStatus"),
        )
