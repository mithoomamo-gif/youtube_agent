"""Idempotency support for work-creating POST endpoints.

Implements the contract from roadmap section 31 / MASTER_ROADMAP §4.3:
every state-changing POST that creates work (project creation, pipeline
actions, approvals, schedule run-now, ...) must carry an
``Idempotency-Key`` header (a client-generated UUID).  The backend stores
the key together with a hash of the request for 24 hours; a repeat of the
same key + same request returns the ORIGINAL response instead of
duplicating work, and the same key with a DIFFERENT request is a
409 ``idempotency_conflict``.

Storage is a single SQLite table ``idempotency_keys`` created by this
module (create-table-if-not-exists) so it does not depend on the schema
owned by the app.db module (worker 3).  When app.db is available its
connection / data directory is reused; otherwise the store falls back to
its own SQLite file under ``YTA_DATA_DIR`` (or an in-memory database when
no data dir is configured — acceptable for the skeleton, keys simply do
not survive a restart in that mode).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Request
from starlette.responses import JSONResponse

try:  # ApiError + envelope are owned by worker 2 (app.errors).
    from app.errors import ApiError
except Exception:  # pragma: no cover - fallback until app.errors lands

    class ApiError(Exception):  # type: ignore[no-redef]
        """Fallback ApiError with the same public shape as app.errors.ApiError."""

        def __init__(
            self,
            status_code: int = 500,
            code: str = "internal_error",
            message: str = "",
            details: Optional[dict] = None,
            retryable: bool = False,
        ) -> None:
            super().__init__(message or code)
            self.status_code = status_code
            self.code = code
            self.message = message or code
            self.details = details or {}
            self.retryable = retryable


#: How long a stored idempotency key is honoured: 24 hours (section 31).
RETENTION_MS = 24 * 60 * 60 * 1000
RETENTION_SECONDS = RETENTION_MS // 1000

_DDL = """
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL
)
"""


def hash_request(method: str, path: str, body_bytes: bytes) -> str:
    """SHA-256 over method + path + raw body — identifies 'the same request'."""
    h = hashlib.sha256()
    h.update(method.upper().encode("utf-8"))
    h.update(b"\n")
    h.update(path.encode("utf-8"))
    h.update(b"\n")
    h.update(body_bytes or b"")
    return h.hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _default_db_path() -> Optional[Path]:
    data_dir = os.environ.get("YTA_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "idempotency.db"
    return None


class IdempotencyStore:
    """SQLite-backed store for idempotency keys.

    Parameters
    ----------
    db_path:
        Explicit SQLite file.  ``None`` (default) resolves lazily:
        app.db's database file if that module exposes one, else
        ``$YTA_DATA_DIR/idempotency.db``, else an in-memory database.
    connection:
        An existing sqlite3 connection to use instead of opening a file
        (mainly for tests).
    """

    def __init__(
        self,
        db_path: Optional[str | Path] = None,
        connection: Optional[sqlite3.Connection] = None,
    ) -> None:
        self._db_path = Path(db_path) if db_path else None
        self._conn = connection
        self._lock = threading.Lock()

    # -- connection handling -------------------------------------------------
    def _connection(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        path = self._db_path or _default_db_path()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
        else:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.execute(_DDL)
        self._conn.commit()
        return self._conn

    # -- low-level API --------------------------------------------------------
    def get(self, key: str) -> Optional[dict[str, Any]]:
        """Return the stored record for ``key`` or ``None``.

        The record is ``{key, request_hash, response, status_code,
        created_at_ms}`` where ``response`` is the already-parsed JSON body.
        Expired records (> 24h old) are pruned and treated as absent.
        """
        if not key:
            return None
        conn = self._connection()
        with self._lock:
            row = conn.execute(
                "SELECT key, request_hash, response_json, status_code, created_at_ms "
                "FROM idempotency_keys WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            if _now_ms() - int(row[4]) > RETENTION_MS:
                conn.execute("DELETE FROM idempotency_keys WHERE key = ?", (key,))
                conn.commit()
                return None
        try:
            response = json.loads(row[2])
        except (TypeError, ValueError):
            response = None
        return {
            "key": row[0],
            "request_hash": row[1],
            "response": response,
            "status_code": int(row[3]),
            "created_at_ms": int(row[4]),
        }

    def put(
        self,
        key: str,
        request_hash: str,
        status_code: int,
        response_obj: Any,
    ) -> None:
        """Store (or replace) the response produced for ``key``."""
        if not key:
            return
        conn = self._connection()
        with self._lock:
            # Prune expired keys opportunistically so the table stays small.
            conn.execute(
                "DELETE FROM idempotency_keys WHERE created_at_ms < ?",
                (_now_ms() - RETENTION_MS,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO idempotency_keys "
                "(key, request_hash, response_json, status_code, created_at_ms) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    key,
                    request_hash,
                    json.dumps(response_obj, default=str),
                    int(status_code),
                    _now_ms(),
                ),
            )
            conn.commit()

    def delete(self, key: str) -> None:
        conn = self._connection()
        with self._lock:
            conn.execute("DELETE FROM idempotency_keys WHERE key = ?", (key,))
            conn.commit()

    # -- FastAPI-facing helpers ------------------------------------------------
    async def check(self, key: str, request: Request) -> Optional[JSONResponse]:
        """Replay guard used by routers before doing any work.

        Returns the stored :class:`JSONResponse` when ``key`` was already
        used for the SAME request, raises ``ApiError`` 409
        ``idempotency_conflict`` when the key was used for a DIFFERENT
        request, and returns ``None`` when the key is new (caller should
        proceed and later call :meth:`remember`).
        """
        if not key:
            return None
        record = self.get(key)
        if record is None:
            return None
        body = await request.body()
        request_hash = hash_request(request.method, request.url.path, body)
        if request_hash != record["request_hash"]:
            raise ApiError(
                409,
                "idempotency_conflict",
                "Idempotency-Key was already used with a different request.",
                details={"idempotency_key": key},
            )
        return JSONResponse(
            content=record["response"],
            status_code=record["status_code"],
            headers={"Idempotency-Replayed": "true"},
        )

    async def remember(
        self,
        key: str,
        request: Request,
        status_code: int,
        response_obj: Any,
    ) -> None:
        """Store the successful response for ``key`` (call after success)."""
        if not key:
            return
        body = await request.body()
        request_hash = hash_request(request.method, request.url.path, body)
        self.put(key, request_hash, status_code, response_obj)


#: Shared default store — routers import this so all endpoints consult the
#: same underlying table.
store = IdempotencyStore()
default_store = store


def get(key: str) -> Optional[dict[str, Any]]:
    """Module-level shortcut for ``store.get(key)``."""
    return store.get(key)


def put(key: str, request_hash: str, status_code: int, response_obj: Any) -> None:
    """Module-level shortcut for ``store.put(...)``."""
    store.put(key, request_hash, status_code, response_obj)


def require_idempotency_key(request: Request) -> str:
    """Return the ``Idempotency-Key`` header or raise the 400 envelope error.

    Work-creating POSTs must fail fast with code
    ``idempotency_key_required`` when the header is missing (roadmap §4.3).
    """
    key = request.headers.get("Idempotency-Key") or request.headers.get(
        "idempotency-key"
    )
    if not key or not key.strip():
        raise ApiError(
            400,
            "idempotency_key_required",
            "This endpoint requires an Idempotency-Key header.",
        )
    return key.strip()
