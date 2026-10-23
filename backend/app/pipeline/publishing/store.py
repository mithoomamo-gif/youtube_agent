"""Persistence for resumable sessions and the idempotency ledger.

The critical write: video_id is recorded against the idempotency key
IMMEDIATELY when the final chunk returns, before any other API call —
every retry path first asks "does a video_id already exist for this key?"
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class SessionRecord:
    idempotency_key: str
    session_uri: str
    bytes_confirmed: int
    manifest_hash: str | None
    channel_id: str


@runtime_checkable
class UploadStore(Protocol):
    def save_session(self, record: SessionRecord) -> None: ...
    def load_session(self, idempotency_key: str) -> SessionRecord | None: ...
    def update_offset(self, idempotency_key: str, bytes_confirmed: int) -> None: ...
    def clear_session(self, idempotency_key: str) -> None: ...
    def save_video_id(self, idempotency_key: str, video_id: str) -> None: ...
    def get_video_id(self, idempotency_key: str) -> str | None: ...
    def clear_video_id(self, idempotency_key: str) -> None: ...


class InMemoryStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, SessionRecord] = {}
        self._video_ids: dict[str, str] = {}

    def save_session(self, record: SessionRecord) -> None:
        with self._lock:
            self._sessions[record.idempotency_key] = record

    def load_session(self, idempotency_key: str) -> SessionRecord | None:
        return self._sessions.get(idempotency_key)

    def update_offset(self, idempotency_key: str, bytes_confirmed: int) -> None:
        with self._lock:
            rec = self._sessions.get(idempotency_key)
            if rec is not None:
                rec.bytes_confirmed = bytes_confirmed

    def clear_session(self, idempotency_key: str) -> None:
        with self._lock:
            self._sessions.pop(idempotency_key, None)

    def save_video_id(self, idempotency_key: str, video_id: str) -> None:
        with self._lock:
            self._video_ids[idempotency_key] = video_id

    def get_video_id(self, idempotency_key: str) -> str | None:
        return self._video_ids.get(idempotency_key)

    def clear_video_id(self, idempotency_key: str) -> None:
        """Used ONLY by the dead-upload procedure: the old ID is recorded
        on the job, the ledger is freed so a NEW video can be uploaded."""
        with self._lock:
            self._video_ids.pop(idempotency_key, None)


class JsonFileStore(InMemoryStore):
    """File-backed store: survives an app restart mid-upload (SQLite in
    production; this JSON variant is the test-friendly equivalent)."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self._path = Path(path)
        if self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for key, rec in raw.get("sessions", {}).items():
                self._sessions[key] = SessionRecord(**rec)
            self._video_ids.update(raw.get("video_ids", {}))

    def _flush(self) -> None:
        payload = {
            "sessions": {
                k: vars(v) for k, v in self._sessions.items()
            },
            "video_ids": dict(self._video_ids),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def save_session(self, record: SessionRecord) -> None:
        super().save_session(record)
        self._flush()

    def update_offset(self, idempotency_key: str, bytes_confirmed: int) -> None:
        super().update_offset(idempotency_key, bytes_confirmed)
        self._flush()

    def clear_session(self, idempotency_key: str) -> None:
        super().clear_session(idempotency_key)
        self._flush()

    def save_video_id(self, idempotency_key: str, video_id: str) -> None:
        super().save_video_id(idempotency_key, video_id)
        self._flush()

    def clear_video_id(self, idempotency_key: str) -> None:
        super().clear_video_id(idempotency_key)
        self._flush()
