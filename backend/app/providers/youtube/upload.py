"""Resumable upload client (roadmap Section 25).

Protocol rules implemented here:

- **Resumable uploads only** — never multipart/simple; a failure resumes
  from the last server-acknowledged byte instead of byte 0.
- Session initiation goes through :meth:`YouTubeClient.request`
  (``videos.insert`` -> upload bucket, 1 unit) with
  ``privacyStatus`` ALWAYS ``private`` at insert. The caller's intended
  final privacy is tracked separately and compared against the status
  AS ACTUALLY APPLIED when the upload completes: if ``public`` was
  requested but ``private`` came back, the project is unaudited —
  record ``forced_private=True``, log it, and DO NOT retry.
- The session URI + byte offset + idempotency key are persisted via the
  injected :class:`UploadSessionStore` IMMEDIATELY when the session
  opens, so an app restart/crash can resume. The idempotency key is
  ``sha256(file bytes + channel_id + title)`` — a replacement session
  after a 404 is safe because a duplicate completed upload is prevented
  by the key.
- Chunks are multiples of 256 KiB (default 8 MiB, adaptive down to
  1-2 MiB after repeated failures). After a ``308`` the next chunk starts
  at the SERVER-reported ``Range`` offset — never the client's
  assumption.
- On interruption, a STATUS QUERY comes first (empty PUT with
  ``Content-Range: bytes */*``): ``200/201`` means the upload already
  completed; ``404`` means the session expired -> start a new one.
- A ``401`` mid-upload is handled one level down (the client wrapper
  refreshes + retries the current step); the session URI survives token
  refresh.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

from ..http_client import create_async_client
from .client import UPLOAD_BASE, YouTubeClient
from .errors import NeedsReauth, SessionExpired, YouTubeAPIError
from .tokens import TokenManager

logger = logging.getLogger(__name__)

CHUNK_ALIGNMENT = 262_144  # 256 KiB — every chunk but the last is a multiple.
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB
MIN_CHUNK_SIZE = 1 * 1024 * 1024  # adaptive floor: 1 MiB

MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 5_000
MAX_TAGS_TOTAL_CHARS = 500

_RANGE_RE = re.compile(r"bytes=(\d+)-(\d+)")


# ---------------------------------------------------------------------------
# Pre-flight validation (consumes no quota)
# ---------------------------------------------------------------------------


def validate_metadata(
    *,
    title: str,
    description: str | None,
    tags: list[str] | None,
    category_id: str | None,
    self_declared_made_for_kids: bool | None,
    contains_synthetic_media: bool | None,
) -> dict:
    """Validate upload metadata locally; raise ``ValueError`` on problems.

    Requirements: title <= 100 chars, description <= 5,000 chars, tags
    <= 500 chars in total, ``categoryId`` present, and the two disclosure
    flags (``selfDeclaredMadeForKids``, ``containsSyntheticMedia``) set
    EXPLICITLY — ``None`` (undecided) is an error, never a silent default.
    """
    if not title or not title.strip():
        raise ValueError("title is required")
    if len(title) > MAX_TITLE_CHARS:
        raise ValueError(
            f"title is {len(title)} chars; maximum is {MAX_TITLE_CHARS}"
        )
    description = description or ""
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ValueError(
            f"description is {len(description)} chars; maximum is "
            f"{MAX_DESCRIPTION_CHARS}"
        )
    tags = list(tags or [])
    total_tag_chars = sum(len(t) for t in tags)
    if total_tag_chars > MAX_TAGS_TOTAL_CHARS:
        raise ValueError(
            f"tags total {total_tag_chars} chars; maximum is "
            f"{MAX_TAGS_TOTAL_CHARS}"
        )
    if category_id is None or not str(category_id).strip():
        raise ValueError("categoryId is required")
    if not isinstance(self_declared_made_for_kids, bool):
        raise ValueError(
            "selfDeclaredMadeForKids must be set explicitly (True or False)"
        )
    if not isinstance(contains_synthetic_media, bool):
        raise ValueError(
            "containsSyntheticMedia disclosure must be set explicitly "
            "(True or False)"
        )
    return {
        "title": title,
        "description": description,
        "tags": tags,
        "category_id": str(category_id),
        "self_declared_made_for_kids": self_declared_made_for_kids,
        "contains_synthetic_media": contains_synthetic_media,
    }


def compute_idempotency_key(file_path: str | Path, channel_id: str, title: str) -> str:
    """``sha256(file bytes + channel_id + title)`` as a hex digest."""
    digest = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    digest.update(b"|")
    digest.update(channel_id.encode("utf-8"))
    digest.update(b"|")
    digest.update(title.encode("utf-8"))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Session persistence (injected; SQLite implementation is another team)
# ---------------------------------------------------------------------------


@dataclass
class UploadSessionState:
    idempotency_key: str
    channel_id: str
    session_uri: str
    total_bytes: int
    bytes_confirmed: int = 0
    title: str = ""
    file_path: str = ""
    requested_privacy_status: str = "private"
    chunk_size: int = DEFAULT_CHUNK_SIZE


@runtime_checkable
class UploadSessionStore(Protocol):
    """Persistence hooks for resumable-upload sessions."""

    def save_session(self, state: UploadSessionState) -> None:
        """Persist (insert or replace) a session state."""
        ...

    def load_session(self, idempotency_key: str) -> UploadSessionState | None:
        """Return the saved state for ``idempotency_key``, if any."""
        ...

    def delete_session(self, idempotency_key: str) -> None:
        """Drop a finished/expired session."""
        ...


class InMemoryUploadSessionStore:
    """Dict-backed :class:`UploadSessionStore` for tests and dev."""

    def __init__(self) -> None:
        self._sessions: dict[str, UploadSessionState] = {}

    def save_session(self, state: UploadSessionState) -> None:
        self._sessions[state.idempotency_key] = UploadSessionState(
            **{**state.__dict__}
        )

    def load_session(self, idempotency_key: str) -> UploadSessionState | None:
        state = self._sessions.get(idempotency_key)
        if state is None:
            return None
        return UploadSessionState(**{**state.__dict__})

    def delete_session(self, idempotency_key: str) -> None:
        self._sessions.pop(idempotency_key, None)


@dataclass
class UploadResult:
    video_id: str
    channel_id: str
    requested_privacy_status: str
    #: The privacyStatus AS ACTUALLY APPLIED, from the completed resource.
    applied_privacy_status: str | None
    #: True when public was requested but private was applied — the
    #: signature of an unaudited API project. Logged, never retried.
    forced_private: bool
    bytes_uploaded: int
    #: Compliance Gate (Round 6): True when the app's own compliance
    #: gate (not Google) forced the requested visibility to private
    #: because the compliance audit has not passed.
    compliance_forced_private: bool = False
    #: Overall compliance state seen by the gate, when one was wired.
    compliance_overall: str | None = None


def parse_range_offset(range_header: str | None) -> int:
    """Next-byte offset from a ``Range: bytes=0-123`` header (-> 124)."""
    if not range_header:
        return 0
    match = _RANGE_RE.search(range_header)
    if not match:
        return 0
    return int(match.group(2)) + 1


# ---------------------------------------------------------------------------
# Uploader
# ---------------------------------------------------------------------------


class ResumableUploader:
    """Drives one resumable upload session per video file."""

    def __init__(
        self,
        client: YouTubeClient,
        session_store: UploadSessionStore,
        *,
        token_manager: TokenManager | None = None,
        http: httpx.AsyncClient | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_resyncs: int = 5,
        compliance_checker=None,
    ) -> None:
        if chunk_size % CHUNK_ALIGNMENT != 0:
            raise ValueError(
                f"chunk_size must be a multiple of {CHUNK_ALIGNMENT} bytes "
                f"(256 KiB); got {chunk_size}"
            )
        self._client = client
        self._store = session_store
        self._tokens = token_manager or client.token_manager
        self._http = http or client.http
        self._chunk_size = chunk_size
        self._max_resyncs = max_resyncs
        # Compliance Gate (Round 6): optional checker (value/callable
        # resolving to an overall compliance state).  When wired and
        # not PASSED, the requested visibility is forced to private
        # before any session is opened.  None = ungated (legacy).
        self._compliance_checker = compliance_checker
        self._compliance_forced: dict[str, tuple] = {}

    def _require_upload_scope(self, channel_id: str) -> None:
        """Local OAuth scope gate (R6-F4) — runs BEFORE any upload HTTP.

        The stored grant must include the ``youtube.upload`` scope
        (checked via :meth:`TokenManager.require_upload_scope`, which
        raises the typed :class:`NeedsReauth`). No token manager means
        no stored grant at all, which is likewise a typed refusal —
        never an unauthenticated upload attempt.
        """
        if self._tokens is None:
            raise NeedsReauth(
                channel_id,
                f"channel {channel_id!r} is not connected "
                "(no token manager wired for the upload path)",
            )
        self._tokens.require_upload_scope(channel_id)

    # -- session initiation ----------------------------------------------

    async def initiate_session(
        self,
        *,
        channel_id: str,
        metadata: dict,
        total_bytes: int,
        mime_type: str,
        idempotency_key: str,
        file_path: str = "",
        requested_privacy_status: str = "private",
        chunk_size: int | None = None,
    ) -> UploadSessionState:
        """Open a resumable session and persist it IMMEDIATELY.

        The insert body always carries ``privacyStatus: private`` —
        publishing is a separate, later step — while the caller's
        intended final privacy is remembered on the session state.
        """
        # Scope gate first: no session HTTP on an out-of-scope grant.
        self._require_upload_scope(channel_id)
        body = {
            "snippet": {
                "title": metadata["title"],
                "description": metadata["description"],
                "tags": metadata["tags"],
                "categoryId": metadata["category_id"],
            },
            "status": {
                "privacyStatus": "private",  # ALWAYS private at insert
                "selfDeclaredMadeForKids": metadata["self_declared_made_for_kids"],
                "containsSyntheticMedia": metadata["contains_synthetic_media"],
                "embeddable": True,
            },
        }
        resp = await self._client.request(
            "videos.insert",
            channel_id=channel_id,
            url=f"{UPLOAD_BASE}/videos",
            params={"uploadType": "resumable", "part": "snippet,status"},
            json_body=body,
            headers={
                "X-Upload-Content-Length": str(total_bytes),
                "X-Upload-Content-Type": mime_type,
            },
            http_method="POST",
        )
        session_uri = resp.headers.get("location") or resp.headers.get("Location")
        if not session_uri:
            raise YouTubeAPIError(
                "videos.insert resumable initiation returned no Location header",
                status_code=resp.status_code,
            )
        state = UploadSessionState(
            idempotency_key=idempotency_key,
            channel_id=channel_id,
            session_uri=session_uri,
            total_bytes=total_bytes,
            bytes_confirmed=0,
            title=metadata["title"],
            file_path=file_path,
            requested_privacy_status=requested_privacy_status,
            chunk_size=chunk_size or self._chunk_size,
        )
        # Persist IMMEDIATELY — a crash after this point can resume.
        self._store.save_session(state)
        return state

    # -- status query ------------------------------------------------------

    async def query_upload_status(
        self, state: UploadSessionState
    ) -> tuple[str, int | dict]:
        """Ask the server where an interrupted upload stands.

        Returns ``("complete", video_resource)`` on 200/201 or
        ``("incomplete", server_offset)`` on 308. Raises
        :class:`SessionExpired` on 404.
        """
        resp = await self._raw_put(
            state,
            content=b"",
            headers={
                "Content-Length": "0",
                "Content-Range": f"bytes */{state.total_bytes}",
            },
        )
        if resp.status_code in (200, 201):
            return "complete", resp.json()
        if resp.status_code == 308:
            return "incomplete", parse_range_offset(resp.headers.get("range"))
        if resp.status_code == 404:
            raise SessionExpired(
                f"upload session for key {state.idempotency_key!r} expired (404)"
            )
        raise YouTubeAPIError(
            f"upload status query failed with {resp.status_code}",
            status_code=resp.status_code,
            body=resp.text[:500] if resp.text else None,
        )

    # -- main entry point ---------------------------------------------------

    async def upload(
        self,
        *,
        file_path: str | Path,
        channel_id: str,
        title: str,
        description: str | None = "",
        tags: list[str] | None = None,
        category_id: str | None,
        self_declared_made_for_kids: bool | None,
        contains_synthetic_media: bool | None,
        requested_privacy: str = "private",
        mime_type: str = "video/mp4",
        chunk_size: int | None = None,
    ) -> UploadResult:
        """Upload ``file_path`` to ``channel_id``, resuming if possible."""
        # OAuth scope gate (R6-F4): BEFORE any upload HTTP (session
        # initiation, status query or chunk PUT), the stored grant
        # must include the youtube.upload scope — otherwise the typed
        # NeedsReauth refusal is raised and nothing hits the network.
        self._require_upload_scope(channel_id)
        # Compliance Gate (Round 6): pre-PASS, the requested
        # visibility is forced to private HERE, before any session
        # exists — the caller's original request is remembered only
        # so the result can disclose the forcing honestly.
        compliance_overall: str | None = None
        compliance_forced = False
        original_requested_privacy = requested_privacy
        if self._compliance_checker is not None:
            from app.compliance.gate import evaluate_visibility, resolve_checker

            compliance_overall = resolve_checker(self._compliance_checker)
            decision = evaluate_visibility(requested_privacy, compliance_overall)
            if not decision["passed"]:
                compliance_forced = bool(decision["forced_private"])
                requested_privacy = str(decision["effective_visibility"])
        metadata = validate_metadata(
            title=title,
            description=description,
            tags=tags,
            category_id=category_id,
            self_declared_made_for_kids=self_declared_made_for_kids,
            contains_synthetic_media=contains_synthetic_media,
        )
        effective_chunk = chunk_size or self._chunk_size
        if effective_chunk % CHUNK_ALIGNMENT != 0:
            raise ValueError(
                f"chunk_size must be a multiple of {CHUNK_ALIGNMENT} bytes "
                f"(256 KiB); got {effective_chunk}"
            )
        path = Path(file_path)
        if not path.is_file():
            raise ValueError(f"file does not exist: {file_path}")
        total = os.path.getsize(path)
        if total <= 0:
            raise ValueError("file is empty")

        idempotency_key = compute_idempotency_key(path, channel_id, title)
        if self._compliance_checker is not None:
            self._compliance_forced[idempotency_key] = (
                compliance_forced,
                compliance_overall,
                original_requested_privacy,
            )

        state = self._store.load_session(idempotency_key)
        if state is not None and state.session_uri:
            # Interruption path: status query FIRST, before sending bytes.
            try:
                kind, payload = await self.query_upload_status(state)
            except SessionExpired:
                state = None  # fall through and open a fresh session
            else:
                if kind == "complete":
                    return self._complete(state, payload)  # type: ignore[arg-type]
                state.bytes_confirmed = int(payload)  # type: ignore[arg-type]
                state.requested_privacy_status = requested_privacy
                self._store.save_session(state)

        restarts = 0
        while True:
            if state is None:
                state = await self.initiate_session(
                    channel_id=channel_id,
                    metadata=metadata,
                    total_bytes=total,
                    mime_type=mime_type,
                    idempotency_key=idempotency_key,
                    file_path=str(path),
                    requested_privacy_status=requested_privacy,
                    chunk_size=effective_chunk,
                )
            try:
                resource = await self._upload_chunks(path, state)
                return self._complete(state, resource)
            except SessionExpired:
                # Session died mid-upload: one fresh session is safe
                # because the idempotency key guards against duplicates.
                restarts += 1
                if restarts > 1:
                    raise
                self._store.delete_session(idempotency_key)
                state = None

    # -- chunk loop -----------------------------------------------------------

    async def _upload_chunks(
        self, path: Path, state: UploadSessionState
    ) -> dict:
        offset = state.bytes_confirmed
        chunk_size = state.chunk_size
        consecutive_failures = 0
        with open(path, "rb") as fh:
            while offset < state.total_bytes:
                fh.seek(offset)
                chunk = fh.read(min(chunk_size, state.total_bytes - offset))
                if not chunk:
                    break
                start = offset
                end = offset + len(chunk) - 1
                resp = await self._raw_put(
                    state,
                    content=chunk,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {start}-{end}/{state.total_bytes}",
                        "Content-Type": "video/*",
                    },
                )
                if resp.status_code in (200, 201):
                    state.bytes_confirmed = state.total_bytes
                    self._store.save_session(state)
                    return resp.json()
                if resp.status_code == 308:
                    # Resume from the SERVER-reported offset, never our
                    # assumption about how much arrived.
                    offset = parse_range_offset(resp.headers.get("range"))
                    state.bytes_confirmed = offset
                    self._store.save_session(state)
                    consecutive_failures = 0
                    continue
                if resp.status_code == 404:
                    raise SessionExpired(
                        f"upload session for key {state.idempotency_key!r} "
                        "expired mid-upload (404)"
                    )
                if resp.status_code >= 500:
                    consecutive_failures += 1
                    if consecutive_failures > self._max_resyncs:
                        raise YouTubeAPIError(
                            f"upload failed repeatedly; last status "
                            f"{resp.status_code}",
                            status_code=resp.status_code,
                        )
                    # Adapt: smaller chunks on a struggling connection,
                    # and resync from the server's own accounting.
                    if chunk_size > MIN_CHUNK_SIZE:
                        chunk_size = max(MIN_CHUNK_SIZE, chunk_size // 2)
                        state.chunk_size = chunk_size
                    kind, payload = await self.query_upload_status(state)
                    if kind == "complete":
                        return payload  # type: ignore[return-value]
                    offset = int(payload)  # type: ignore[arg-type]
                    state.bytes_confirmed = offset
                    self._store.save_session(state)
                    continue
                raise YouTubeAPIError(
                    f"chunk upload failed with {resp.status_code}",
                    status_code=resp.status_code,
                    body=resp.text[:500] if resp.text else None,
                )
        # Loop ended without a completion response: ask the server.
        kind, payload = await self.query_upload_status(state)
        if kind == "complete":
            return payload  # type: ignore[return-value]
        raise YouTubeAPIError(
            "upload ended without a completion response from Google"
        )

    async def _raw_put(
        self,
        state: UploadSessionState,
        *,
        content: bytes,
        headers: dict[str, str],
    ) -> httpx.Response:
        req_headers = dict(headers)
        if self._tokens is not None:
            # Chunk/status PUTs are upload HTTP too: same scope gate,
            # before the token is used and before the PUT is sent.
            self._tokens.require_upload_scope(state.channel_id)
            access_token = await self._tokens.get_access_token(state.channel_id)
            req_headers.setdefault("Authorization", f"Bearer {access_token}")
        if self._http is not None:
            return await self._http.put(
                state.session_uri, content=content, headers=req_headers
            )
        async with create_async_client(timeout=120.0) as client:
            return await client.put(
                state.session_uri, content=content, headers=req_headers
            )

    # -- completion -----------------------------------------------------------

    def _complete(self, state: UploadSessionState, resource: dict) -> UploadResult:
        applied = (resource.get("status") or {}).get("privacyStatus")
        requested = state.requested_privacy_status
        forced_private = requested == "public" and applied == "private"
        if forced_private:
            # Unaudited project: Google quietly forced the video private.
            # Log it and surface it — do NOT retry the upload.
            logger.warning(
                "forced_private=True video_id=%s channel_id=%s "
                "requested=%s applied=%s (unaudited API project)",
                resource.get("id"),
                state.channel_id,
                requested,
                applied,
            )
        self._store.delete_session(state.idempotency_key)
        compliance_forced, compliance_overall, original_requested = (
            self._compliance_forced.pop(
                state.idempotency_key, (False, None, None)
            )
        )
        if compliance_forced and original_requested:
            # The result reports what the CALLER requested; the
            # forcing to private is disclosed by the flag + overall.
            requested = original_requested
        return UploadResult(
            video_id=resource.get("id", ""),
            channel_id=state.channel_id,
            requested_privacy_status=requested,
            applied_privacy_status=applied,
            forced_private=forced_private,
            bytes_uploaded=state.total_bytes,
            compliance_forced_private=compliance_forced,
            compliance_overall=compliance_overall,
        )
