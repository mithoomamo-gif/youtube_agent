"""Shared helpers for ``app.execution`` tests (Round 2).

This module is deliberately **not** a test file (its name does not start
with ``test_``) so pytest never collects it directly.  Execution tests
import the helpers from here instead of re-inventing DB setup::

    from tests.execution_fixtures import (
        RecordingHub,
        make_db,
        seed_channel_niche,
        seed_project,
    )

(When pytest runs with ``tests/`` itself on ``sys.path`` the bare
``from execution_fixtures import ...`` form works too.)

Everything here is dependency-light: stdlib + :mod:`app.db` only.  All
rows are inserted against the *real* section-15 schema created by
:func:`app.db.init_db`, so a helper that drifts from
``app/schema_ddl.py`` fails loudly instead of silently testing a fake
schema.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = [
    "RecordingHub",
    "conn_factory_for",
    "make_db",
    "now_ms",
    "seed_channel_niche",
    "seed_project",
]

#: A conn_factory returns a *fresh* connection on every call — the same
#: contract :class:`app.queue.SQLiteJobQueue` and the execution runner
#: rely on (no connection is ever shared between threads).
ConnFactory = Callable[[], sqlite3.Connection]


def now_ms() -> int:
    """Current Unix epoch time in milliseconds (the schema's clock)."""
    return int(time.time() * 1000)


def conn_factory_for(db_path: Path | str) -> ConnFactory:
    """Return a conn_factory opening fresh configured connections.

    Uses :func:`app.db.get_connection` so every connection carries the
    section-15 pragmas (WAL, ``foreign_keys=ON``, ``busy_timeout``...).
    """
    path = str(db_path)

    def factory() -> sqlite3.Connection:
        from app.db import get_connection

        return get_connection(path)

    return factory


def make_db(tmp_path: Path | str) -> tuple[Path, ConnFactory]:
    """Create an initialised test database under ``tmp_path``.

    Returns ``(db_path, conn_factory)``.  The schema is built with
    :func:`app.db.init_db` (the full section-15 DDL, idempotent), so
    tests exercise exactly the tables production uses.
    """
    from app.db import init_db

    db_path = Path(tmp_path) / "app.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(db_path)
    conn.close()
    return db_path, conn_factory_for(db_path)


def _insert(conn_factory: ConnFactory, sql: str, params: tuple) -> None:
    conn = conn_factory()
    try:
        conn.execute(sql, params)
        # app.db connections run in autocommit mode (isolation_level=None)
        # so this is a no-op there; it matters only for exotic factories.
        try:
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()


def seed_channel_niche(
    conn_factory: ConnFactory,
    *,
    niche_key: Optional[str] = None,
    niche_label: Optional[str] = None,
    source: str = "builtin",
    channel_title: Optional[str] = None,
    youtube_channel_id: Optional[str] = None,
    language: str = "en-US",
) -> tuple[str, str]:
    """Insert a minimal valid channel + niche_profile pair.

    Only the NOT NULL / CHECK-constrained columns of ``niche_profiles``
    and ``channels`` (see ``app/schema_ddl.py``) are populated; everything
    optional stays NULL/default.  The channel's
    ``default_niche_profile_id`` points at the new niche.

    Returns ``(channel_id, niche_id)``.  Ids and unique columns
    (``niche_profiles.key``, ``channels.youtube_channel_id``) default to
    fresh uuid-derived values so repeated calls never collide — pass
    explicit values when a test needs deterministic ones.
    """
    suffix = uuid.uuid4().hex[:8]
    niche_id = str(uuid.uuid4())
    channel_id = str(uuid.uuid4())
    ts = now_ms()

    _insert(
        conn_factory,
        """
        INSERT INTO niche_profiles
            (id, key, label, source, risk_class, is_active,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, 'normal', 1, ?, ?)
        """,
        (
            niche_id,
            niche_key or f"test-niche-{suffix}",
            niche_label or "Test Niche",
            source,
            ts,
            ts,
        ),
    )
    _insert(
        conn_factory,
        """
        INSERT INTO channels
            (id, youtube_channel_id, title, default_niche_profile_id,
             language, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
        """,
        (
            channel_id,
            youtube_channel_id or f"UC-test-{suffix}",
            channel_title or "Test Channel",
            niche_id,
            language,
            ts,
            ts,
        ),
    )
    return channel_id, niche_id


def seed_project(
    conn_factory: ConnFactory,
    channel_id: str,
    niche_id: str,
    *,
    publish_mode: str = "review_first",
    topic: str = "Test topic",
    topic_source: str = "user",
    status: str = "draft",
    target_minutes: Optional[int] = None,
) -> str:
    """Insert a minimal valid ``projects`` row and return its id.

    Timestamps are epoch milliseconds, per the section-15 convention.
    ``publish_mode`` must be ``"review_first"`` or ``"auto"`` and
    ``topic_source`` one of ``"agent"`` / ``"user"`` / ``"schedule"``
    (the table's CHECK constraints enforce both).
    """
    project_id = str(uuid.uuid4())
    ts = now_ms()
    _insert(
        conn_factory,
        """
        INSERT INTO projects
            (id, channel_id, niche_profile_id, topic, topic_source,
             target_minutes, status, publish_mode, total_cost_micros,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            project_id,
            channel_id,
            niche_id,
            topic,
            topic_source,
            target_minutes,
            status,
            publish_mode,
            ts,
            ts,
        ),
    )
    return project_id


class RecordingHub:
    """In-memory stand-in for :class:`app.ws_hub.WSHub` in tests.

    Mirrors the hub's ``publish`` signature and envelope shape
    (``v`` / ``type`` / ``event_id`` / ``seq`` / ``boot_id`` / ``ts`` /
    ``payload`` plus optional ``project_id`` / ``job_id`` / ``run_id`` /
    ``agent_id`` scope fields) but, instead of broadcasting, records
    every envelope in :attr:`events` so tests can assert on exactly
    what the execution layer emitted, in order.

    Thread-safe: worker threads may publish concurrently; appends and
    sequence numbers are guarded by a lock.
    """

    def __init__(self, boot_id: str = "test-boot") -> None:
        self.boot_id = boot_id
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._seq = 0

    # -- WSHub-compatible surface ----------------------------------------

    def publish(
        self,
        type: str,  # noqa: A002 - mirrors the task contract / hub naming
        payload: Optional[dict] = None,
        *,
        project_id: Optional[str] = None,
        job_id: Optional[str] = None,
        **scope: Any,
    ) -> dict:
        """Record one envelope and return it (as ``WSHub.publish`` does)."""
        with self._lock:
            self._seq += 1
            seq = self._seq
        envelope: dict[str, Any] = {
            "v": 1,
            "type": type,
            "event_id": uuid.uuid4().hex,
            "seq": seq,
            "boot_id": self.boot_id,
            "ts": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "payload": dict(payload or {}),
        }
        if project_id is not None:
            envelope["project_id"] = project_id
        if job_id is not None:
            envelope["job_id"] = job_id
        for key in ("run_id", "agent_id"):
            if scope.get(key) is not None:
                envelope[key] = scope[key]
        with self._lock:
            self.events.append(envelope)
        return envelope

    @property
    def current_seq(self) -> int:
        with self._lock:
            return self._seq

    # -- assertion conveniences ------------------------------------------

    def clear(self) -> None:
        with self._lock:
            self.events.clear()

    def types(self) -> list[str]:
        """Event types in publish order."""
        with self._lock:
            return [event["type"] for event in self.events]

    def by_type(self, event_type: str) -> list[dict]:
        """All recorded envelopes of one type, in publish order."""
        with self._lock:
            return [e for e in self.events if e["type"] == event_type]

    def last(self, event_type: Optional[str] = None) -> Optional[dict]:
        """Most recent envelope (optionally of one type), or None."""
        with self._lock:
            events = (
                self.events
                if event_type is None
                else [e for e in self.events if e["type"] == event_type]
            )
            return events[-1] if events else None

    def __len__(self) -> int:
        with self._lock:
            return len(self.events)
