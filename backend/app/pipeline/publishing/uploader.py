"""Chunked resumable upload (Section 34, steps 1–3; Section 25 protocol).

Rules encoded here:
- Privacy at insert is ALWAYS private; no publishAt at insert.
- Session URI + offset + manifest hash are persisted BEFORE transfer.
- On 308, resume from the SERVER-reported Range, never client assumption.
- On 5xx/timeout, QUERY the session status before resending anything,
  and honour Retry-After.
- The video ID is written to the ledger against the idempotency key
  IMMEDIATELY when received, before any other API call.
- Every entry path first checks: does a video_id already exist?
"""
from __future__ import annotations

from dataclasses import dataclass

from .clock import Clock, SystemClock
from .errors import (
    NetworkTimeoutError,
    ServerError,
    SessionExpiredError,
)
from .manifest import PublishManifest
from .provider import YouTubeProvider, metadata_for_insert
from .state_machine import PublishJob, PublishState
from .store import SessionRecord, UploadStore

MIB = 1024 * 1024
DEFAULT_CHUNK_SIZE = 8 * MIB
MIN_CHUNK_SIZE = 1 * MIB
MAX_CHUNK_SIZE = 32 * MIB


@dataclass
class UploadResult:
    video_id: str
    bytes_total: int
    resumed: bool = False
    sessions_opened: int = 0


class ChunkedUploader:
    def __init__(
        self,
        provider: YouTubeProvider,
        store: UploadStore,
        *,
        clock: Clock | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_status_query_retries: int = 3,
    ) -> None:
        if not (MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE):
            raise ValueError(
                f"chunk_size must be within 1–32 MiB, got {chunk_size}"
            )
        self._provider = provider
        self._store = store
        self._clock = clock or SystemClock()
        self.chunk_size = chunk_size
        self._max_status_query_retries = max_status_query_retries

    # -- session management -------------------------------------------------

    def _open_session(self, job: PublishJob, manifest: PublishManifest) -> str:
        metadata = metadata_for_insert(
            title=manifest.title,
            description=manifest.description,
            tags=manifest.tags,
            category_id=manifest.category_id,
            default_language=manifest.default_language,
            default_audio_language=manifest.default_audio_language,
            made_for_kids=manifest.made_for_kids,
            contains_synthetic_media=manifest.contains_synthetic_media,
            embeddable=manifest.embeddable,
            licence=manifest.licence,
        )
        session_uri = self._provider.create_resumable_session(
            metadata, manifest.master_size_bytes
        )
        # Persist BEFORE any byte moves: a crash here costs nothing.
        self._store.save_session(
            SessionRecord(
                idempotency_key=job.idempotency_key,
                session_uri=session_uri,
                bytes_confirmed=0,
                manifest_hash=manifest.manifest_hash,
                channel_id=manifest.channel_id,
            )
        )
        job.session_uri = session_uri
        job.bytes_confirmed = 0
        return session_uri

    def _capture_video_id(self, job: PublishJob, video_id: str) -> None:
        """Ledger FIRST, everything else after. Non-negotiable ordering."""
        self._store.save_video_id(job.idempotency_key, video_id)
        job.video_id = video_id
        self._store.clear_session(job.idempotency_key)

    # -- main entry ----------------------------------------------------------

    def upload(
        self, job: PublishJob, manifest: PublishManifest, data: bytes
    ) -> UploadResult:
        """Upload `data` (the master bytes) for `manifest`.

        Precondition: job is PREFLIGHT_OK (or re-entering from
        CHECKS_PASSED for a dead-upload replacement).
        """
        if len(data) != manifest.master_size_bytes:
            raise ValueError(
                f"data length {len(data)} != manifest size "
                f"{manifest.master_size_bytes}"
            )

        # Idempotency gate: a completed upload is never repeated. The
        # caller is responsible for state placement in this shortcut.
        existing = self._store.get_video_id(job.idempotency_key)
        if existing is not None:
            job.video_id = existing
            return UploadResult(existing, len(data), resumed=True)

        sessions_opened = 0
        resumed = False

        # Resume an earlier session if one was persisted.
        record = self._store.load_session(job.idempotency_key)
        if record is not None:
            session_uri = record.session_uri
            job.session_uri = session_uri
            job.bytes_confirmed = record.bytes_confirmed
            resumed = record.bytes_confirmed > 0
            if job.state is PublishState.PREFLIGHT_OK:
                job.transition(PublishState.SESSION_OPEN, "resumed session")
            # Confirm with the server before sending another byte.
            try:
                status = self._provider.query_upload_status(session_uri)
            except SessionExpiredError:
                record = None  # fall through and open a fresh session
            else:
                if status.complete and status.video_id:
                    self._capture_video_id(job, status.video_id)
                    job.transition(PublishState.UPLOADING, "already complete")
                    job.transition(PublishState.UPLOADED, "status query: complete")
                    return UploadResult(status.video_id, len(data), resumed=True)
                job.bytes_confirmed = status.confirmed_bytes
                self._store.update_offset(job.idempotency_key, status.confirmed_bytes)

        if record is None:
            # Open first, transition after: if the insert itself fails
            # (quota, auth) the job stays at its pre-session state.
            session_uri = self._open_session(job, manifest)
            sessions_opened += 1
            if job.state is PublishState.PREFLIGHT_OK:
                job.transition(PublishState.SESSION_OPEN, "session opened")
            elif job.state is PublishState.CHECKS_PASSED:
                job.transition(PublishState.SESSION_OPEN, "re-upload (dead video)")

        job.transition(PublishState.UPLOADING, "transfer started")
        offset = job.bytes_confirmed

        while offset < len(data):
            chunk = data[offset : offset + self.chunk_size]
            try:
                result = self._provider.upload_chunk(session_uri, chunk, offset)
            except (ServerError, NetworkTimeoutError) as exc:
                # Honour Retry-After, then ASK THE SERVER what it holds
                # before resending a single byte.
                if exc.retry_after_seconds:
                    self._clock.sleep(exc.retry_after_seconds)
                offset = self._recover_offset(job, session_uri, len(data))
                if job.video_id is not None:  # query showed completion
                    job.transition(PublishState.UPLOADED, "completed during recovery")
                    return UploadResult(job.video_id, len(data), resumed=True)
                if offset < 0:  # session expired mid-transfer
                    session_uri = self._restart_session(job, manifest)
                    sessions_opened += 1
                    offset = 0
                continue
            except SessionExpiredError:
                # Safe ONLY because no video_id exists yet.
                session_uri = self._restart_session(job, manifest)
                sessions_opened += 1
                offset = 0
                continue

            if result.retry_after_seconds:
                self._clock.sleep(result.retry_after_seconds)

            if result.complete and result.video_id:
                self._capture_video_id(job, result.video_id)
                job.bytes_confirmed = len(data)
                job.transition(PublishState.UPLOADED, "final chunk acknowledged")
                return UploadResult(
                    result.video_id, len(data),
                    resumed=resumed, sessions_opened=sessions_opened,
                )

            # 308 Resume Incomplete: trust the server's Range, exclusively.
            offset = result.confirmed_bytes
            job.bytes_confirmed = offset
            self._store.update_offset(job.idempotency_key, offset)

        # Loop ended without a completing response: ask the server once.
        final = self._provider.query_upload_status(session_uri)
        if final.complete and final.video_id:
            self._capture_video_id(job, final.video_id)
            job.transition(PublishState.UPLOADED, "status query: complete")
            return UploadResult(
                final.video_id, len(data),
                resumed=resumed, sessions_opened=sessions_opened,
            )
        raise ServerError("upload ended without a completing response")

    # -- helpers ---------------------------------------------------------------

    def _restart_session(self, job: PublishJob, manifest: PublishManifest) -> str:
        if self._store.get_video_id(job.idempotency_key) is not None:
            raise SessionExpiredError(
                "session expired AFTER a video_id was captured; refusing "
                "to open a new session (would create a duplicate)"
            )
        job.transition(PublishState.SESSION_OPEN, "session expired; new session")
        uri = self._open_session(job, manifest)
        job.transition(PublishState.UPLOADING, "re-transfer from byte 0")
        job.bytes_confirmed = 0
        return uri

    def _recover_offset(
        self, job: PublishJob, session_uri: str, total: int
    ) -> int:
        """Status query after a failure. Returns the server offset,
        -1 if the session is gone, or `total` via job.video_id when the
        upload actually completed server-side."""
        for _ in range(self._max_status_query_retries):
            try:
                status = self._provider.query_upload_status(session_uri)
            except SessionExpiredError:
                return -1
            if status.complete and status.video_id:
                self._capture_video_id(job, status.video_id)
                return total
            job.bytes_confirmed = status.confirmed_bytes
            self._store.update_offset(job.idempotency_key, status.confirmed_bytes)
            return status.confirmed_bytes
        return job.bytes_confirmed
