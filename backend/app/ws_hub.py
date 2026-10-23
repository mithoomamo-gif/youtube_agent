"""WebSocket hub: envelope, sequencing, journal persistence, topic routing,
backpressure and heartbeat tracking for the local backend protocol.

Implements the server side of roadmap section 31 / MASTER_ROADMAP §4.4:

- Every frame is an envelope ``{"v": 1, "type": str, "event_id": ULID,
  "seq": int, "boot_id": str, "ts": ISO-8601 UTC "Z", "payload": dict}``
  with optional top-level scope fields (``project_id`` / ``job_id`` /
  ``run_id`` / ``agent_id``) when applicable.
- ``seq`` is strictly monotonic per ``boot_id`` and is the resync cursor.
  Only :meth:`WSHub.publish` consumes a sequence number (and persists the
  frame to the ``event_log`` journal). Direct reply frames (hello_ack,
  pong, snapshot_required, command results) carry the *current* seq as a
  cursor without advancing it — they are per-connection and must never be
  replayed to anyone else.
- Topics: ``all``, ``project:{id}``, ``logs``, ``cost``, ``queue``.
  ``system.*`` frames go to every connection regardless of subscriptions.
- Backpressure: per-connection bounded queue. Under pressure frames are
  dropped in the order ``progress.*`` -> ``log.line`` -> ``cost.ticker``.
  ``agent.status_changed``, ``gate.*``, ``budget.warning``, ``system.*``
  (and, conservatively, the rest of the guaranteed pipeline/agent class)
  are never dropped; if one of those cannot be queued the socket is
  closed (code 1013) so the client resyncs via snapshot.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

__all__ = [
    "WSHub",
    "ConnectionState",
    "topic_for",
    "drop_priority",
    "is_never_drop",
    "SNAPSHOT_REASONS",
    "CLOSE_TRY_AGAIN_LATER",
]

#: WebSocket close code used when a connection is too slow to accept even
#: guaranteed frames (1013 = "Try Again Later", per RFC 6455).
CLOSE_TRY_AGAIN_LATER = 1013

SNAPSHOT_REASONS = frozenset({"seq_gap", "boot_changed", "buffer_overflow"})

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: Default heartbeat policy: the client pings every ~20 s; a connection with
#: no pong for more than this many seconds is treated as a zombie.
DEFAULT_HEARTBEAT_TIMEOUT_S = 40.0

#: Application-level heartbeat cadence (system.heartbeat frames).
HEARTBEAT_INTERVAL_S = 15.0


def _utcnow_iso() -> str:
    """Current UTC time as ISO-8601 with a trailing ``Z``."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def topic_for(event_type: str, project_id: Optional[str] = None) -> Optional[str]:
    """Derive the subscription topic for an event type.

    Returns ``None`` for project-scoped events that carry no project_id
    (they are then delivered only to ``all`` subscribers).
    """
    prefix = event_type.split(".", 1)[0]
    if prefix == "system":
        return "all"
    if prefix in ("agent", "pipeline", "gate", "project", "publication"):
        return f"project:{project_id}" if project_id else None
    if prefix == "log":
        return "logs"
    if prefix in ("cost", "quota", "budget"):
        return "cost"
    if prefix in ("queue", "job"):
        return "queue"
    return None


def drop_priority(event_type: str) -> Optional[int]:
    """Backpressure drop priority: lower numbers are dropped first.

    ``None`` means the frame class is never dropped.
    """
    if event_type.startswith("progress."):
        return 0
    if event_type == "log.line":
        return 1
    if event_type == "cost.ticker":
        return 2
    return None


def is_never_drop(event_type: str) -> bool:
    return drop_priority(event_type) is None


@dataclass(eq=False)
class ConnectionState:
    """Per-socket state held by the hub."""

    ws: Any
    subscriptions: set = field(default_factory=set)
    queue: "asyncio.Queue" = field(default_factory=asyncio.Queue)
    last_pong: float = field(default_factory=time.monotonic)
    closed: bool = False
    sender_task: Optional[asyncio.Task] = None
    loop: Optional[asyncio.AbstractEventLoop] = None


class WSHub:
    """In-process publish/subscribe hub for WebSocket connections."""

    def __init__(
        self,
        boot_id: str,
        conn_factory: Optional[Callable[[], Any]] = None,
        *,
        queue_maxsize: int = 200,
        heartbeat_timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S,
    ) -> None:
        self.boot_id = boot_id
        self._conn_factory = conn_factory
        self.queue_maxsize = queue_maxsize
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self._connections: set[ConnectionState] = set()
        self._lock = threading.Lock()
        self._ulid_counter = 0
        self._seq_col: Optional[str] = None  # detected lazily: "seq" or "id"
        self._columns: Optional[dict] = None
        self._seq = self._load_initial_seq()

    # ------------------------------------------------------------------ #
    # Envelope construction
    # ------------------------------------------------------------------ #

    @property
    def current_seq(self) -> int:
        return self._seq

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    def _new_event_id(self) -> str:
        """ULID-like id: 48-bit millisecond time + counter/random, Crockford."""
        with self._lock:
            self._ulid_counter = (self._ulid_counter + 1) & 0xFFFF
            counter = self._ulid_counter
        ms = int(time.time() * 1000)
        rand = (counter << 64) | secrets.randbits(64)
        value = (ms << 80) | rand
        chars = []
        for _ in range(26):
            chars.append(_CROCKFORD[value & 0x1F])
            value >>= 5
        return "".join(reversed(chars))

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def make_envelope(
        self,
        event_type: str,
        payload: Optional[dict] = None,
        *,
        project_id: Optional[str] = None,
        job_id: Optional[str] = None,
        consume_seq: bool = True,
        **scope: Any,
    ) -> dict:
        """Build an envelope. ``consume_seq=False`` stamps the current seq
        without advancing it (per-connection reply frames)."""
        envelope: dict[str, Any] = {
            "v": 1,
            "type": event_type,
            "event_id": self._new_event_id(),
            "seq": self._next_seq() if consume_seq else self._seq,
            "boot_id": self.boot_id,
            "ts": _utcnow_iso(),
            "payload": dict(payload or {}),
        }
        if project_id is not None:
            envelope["project_id"] = project_id
        if job_id is not None:
            envelope["job_id"] = job_id
        for key in ("run_id", "agent_id"):
            if scope.get(key) is not None:
                envelope[key] = scope[key]
        return envelope

    # ------------------------------------------------------------------ #
    # Publishing
    # ------------------------------------------------------------------ #

    def publish(
        self,
        event_type: str,
        payload: Optional[dict] = None,
        *,
        project_id: Optional[str] = None,
        job_id: Optional[str] = None,
        **scope: Any,
    ) -> dict:
        """Assign seq, persist to the journal (best-effort) and broadcast."""
        envelope = self.make_envelope(
            event_type,
            payload,
            project_id=project_id,
            job_id=job_id,
            consume_seq=True,
            **scope,
        )
        self._persist(envelope)
        for state in list(self._connections):
            if self._matches(state, envelope):
                self.enqueue_frame(state, envelope)
        return envelope

    def _matches(self, state: ConnectionState, envelope: dict) -> bool:
        event_type = envelope.get("type", "")
        if event_type.startswith("system."):
            return True  # system frames reach every connection
        if "all" in state.subscriptions:
            return True
        topic = topic_for(event_type, envelope.get("project_id"))
        return topic is not None and topic in state.subscriptions

    # ------------------------------------------------------------------ #
    # Connection management
    # ------------------------------------------------------------------ #

    def register(
        self,
        ws: Any,
        subscriptions: Optional[Iterable[str]] = None,
        *,
        start_sender: bool = True,
    ) -> ConnectionState:
        state = ConnectionState(
            ws=ws,
            subscriptions=set(subscriptions or {"all"}),
            queue=asyncio.Queue(maxsize=self.queue_maxsize),
        )
        self._connections.add(state)
        if start_sender:
            self.start_sender(state)
        return state

    def start_sender(self, state: ConnectionState) -> None:
        """Start the background task draining ``state.queue`` to the socket.

        No-op when there is no running event loop (unit tests drain the
        queue manually) or when a sender is already running.
        """
        if state.sender_task is not None and not state.sender_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        state.loop = loop
        state.sender_task = loop.create_task(self._sender_loop(state))

    def unregister(self, target: Any) -> None:
        """Remove a connection given its state or its socket object."""
        state = self._find(target)
        if state is None:
            return
        state.closed = True
        self._connections.discard(state)
        task = state.sender_task
        if task is not None and not task.done():
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if task is not current:
                task.cancel()
        state.sender_task = None

    def _find(self, target: Any) -> Optional[ConnectionState]:
        if isinstance(target, ConnectionState):
            return target
        for state in self._connections:
            if state.ws is target:
                return state
        return None

    def resolve(self, target: Any) -> Optional[ConnectionState]:
        return self._find(target)

    async def _sender_loop(self, state: ConnectionState) -> None:
        try:
            while not state.closed:
                frame = await state.queue.get()
                try:
                    await state.ws.send_json(frame)
                except Exception:
                    break
                finally:
                    state.queue.task_done()
        except asyncio.CancelledError:
            raise
        finally:
            self.unregister(state)

    # ------------------------------------------------------------------ #
    # Backpressure
    # ------------------------------------------------------------------ #

    def enqueue_frame(self, state: ConnectionState, frame: dict) -> bool:
        """Queue ``frame`` for one connection, applying the drop policy.

        Unit-testable without a real socket: only ``state.queue`` and, on
        guaranteed-frame overflow, ``state.ws.close(code=1013)`` are used.

        Returns True when the frame was queued, False when it was dropped
        or the connection was closed instead.
        """
        if state.closed:
            return False
        try:
            state.queue.put_nowait(frame)
            self._nudge(state)
            return True
        except asyncio.QueueFull:
            pass

        if self._make_room(state):
            try:
                state.queue.put_nowait(frame)
                self._nudge(state)
                return True
            except asyncio.QueueFull:
                pass

        if is_never_drop(frame.get("type", "")):
            # A guaranteed frame cannot be delivered: close the socket so
            # the client resyncs via snapshot instead of silently losing it.
            self._force_close(state, CLOSE_TRY_AGAIN_LATER)
            return False
        # Droppable frame and a queue full of guaranteed frames: drop it.
        return False

    @staticmethod
    def _nudge(state: ConnectionState) -> None:
        """Wake the connection's event loop after a cross-thread enqueue.

        ``publish`` is fire-and-forget and may be called from worker
        threads; ``asyncio.Queue`` wakeups alone do not reliably interrupt
        a loop blocked in ``select()``, so poke it thread-safely.
        """
        loop = state.loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(lambda: None)
            except RuntimeError:
                pass

    def _make_room(self, state: ConnectionState) -> bool:
        """Evict the single most-droppable queued frame, if any."""
        items: list[dict] = []
        while True:
            try:
                items.append(state.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        victim_idx: Optional[int] = None
        victim_prio: Optional[int] = None
        for idx, item in enumerate(items):
            prio = drop_priority(item.get("type", ""))
            if prio is None:
                continue
            if victim_prio is None or prio < victim_prio:
                victim_idx, victim_prio = idx, prio
        if victim_idx is not None:
            items.pop(victim_idx)
        for item in items:
            state.queue.put_nowait(item)
        return victim_idx is not None

    def _force_close(self, state: ConnectionState, code: int) -> None:
        self.unregister(state)
        close = getattr(state.ws, "close", None)
        if close is None:
            return
        try:
            result = close(code=code)
        except TypeError:
            try:
                result = close(code)
            except Exception:
                return
        except Exception:
            return
        if asyncio.iscoroutine(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                result.close()  # no loop to run it on; avoid warnings
            else:
                loop.create_task(result)

    # ------------------------------------------------------------------ #
    # Heartbeats
    # ------------------------------------------------------------------ #

    def note_pong(self, target: Any) -> None:
        state = self._find(target)
        if state is not None:
            state.last_pong = time.monotonic()

    def check_heartbeats(self, now: Optional[float] = None) -> list:
        """Close connections whose last pong is older than the timeout.

        ``now`` uses the same clock as ``ConnectionState.last_pong``
        (``time.monotonic``); injectable for tests. Returns closed states.
        """
        if now is None:
            now = time.monotonic()
        closed = []
        for state in list(self._connections):
            if now - state.last_pong > self.heartbeat_timeout_s:
                self._force_close(state, CLOSE_TRY_AGAIN_LATER)
                closed.append(state)
        return closed

    def heartbeat_payload(self) -> dict:
        return {"ts": _utcnow_iso(), "seq": self._seq}

    def heartbeat_frame(self) -> dict:
        """A ``system.heartbeat`` envelope; does not consume seq or persist."""
        return self.make_envelope(
            "system.heartbeat", self.heartbeat_payload(), consume_seq=False
        )

    # ------------------------------------------------------------------ #
    # Replay / snapshot
    # ------------------------------------------------------------------ #

    def snapshot_required_frame(self, reason: str) -> dict:
        if reason not in SNAPSHOT_REASONS:
            raise ValueError(f"unknown snapshot reason: {reason!r}")
        return self.make_envelope(
            "system.snapshot_required", {"reason": reason}, consume_seq=False
        )

    def replay_since(
        self,
        boot_id: str,
        last_seq: Optional[int],
        limit: int = 1000,
    ) -> Optional[list]:
        """Persisted envelopes with ``seq > last_seq`` for this boot.

        Returns ``None`` when ``boot_id`` is not the current boot (the
        caller must send ``system.snapshot_required`` instead). Returns an
        empty list when no journal is available.
        """
        if boot_id != self.boot_id:
            return None
        if self._conn_factory is None:
            return []
        try:
            conn = self._conn_factory()
        except Exception:
            return []
        try:
            col = self._detect_seq_column(conn)
            if col is None:
                return []
            rows = conn.execute(
                f"SELECT payload_json FROM event_log "
                f"WHERE boot_id = ? AND {col} > ? ORDER BY {col} ASC LIMIT ?",
                (self.boot_id, int(last_seq or 0), int(limit)),
            ).fetchall()
        except Exception:
            return []
        finally:
            self._close_quietly(conn)
        frames = []
        for row in rows:
            raw = row[0] if not isinstance(row, dict) else row.get("payload_json")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(frame, dict):
                frames.append(frame)
        return frames

    # ------------------------------------------------------------------ #
    # Journal persistence (best-effort)
    # ------------------------------------------------------------------ #

    def _load_initial_seq(self) -> int:
        if self._conn_factory is None:
            return 0
        try:
            conn = self._conn_factory()
        except Exception:
            return 0
        try:
            col = self._detect_seq_column(conn)
            if col is None:
                return 0
            row = conn.execute(
                f"SELECT COALESCE(MAX({col}), 0) FROM event_log WHERE boot_id = ?",
                (self.boot_id,),
            ).fetchone()
            return int(row[0] or 0) if row else 0
        except Exception:
            return 0
        finally:
            self._close_quietly(conn)

    def _table_columns(self, conn: Any) -> dict:
        """Map of event_log column name -> declared type (cached)."""
        if self._columns is not None:
            return self._columns
        try:
            rows = conn.execute("PRAGMA table_info(event_log)").fetchall()
        except Exception:
            return {}
        cols = {r[1]: (r[2] or "") for r in rows if len(r) > 2}
        if cols:
            self._columns = cols
        return cols

    def _detect_seq_column(self, conn: Any) -> Optional[str]:
        """The resync cursor column: ``seq`` when present (the real schema
        also has a TEXT ``id`` PK holding the event_id), else an INTEGER
        ``id`` autoincrement column."""
        if self._seq_col is not None:
            return self._seq_col
        names = self._table_columns(conn)
        if "seq" in names:
            self._seq_col = "seq"
        elif "id" in names:
            self._seq_col = "id"
        else:
            return None
        return self._seq_col

    def _persist(self, envelope: dict) -> None:
        if self._conn_factory is None:
            return
        try:
            conn = self._conn_factory()
        except Exception:
            return
        try:
            cols = self._table_columns(conn)
            values: dict[str, Any] = {
                "seq": envelope["seq"],
                "boot_id": self.boot_id,
                "type": envelope["type"],
                "payload_json": json.dumps(envelope),
                "created_at_ms": int(time.time() * 1000),
            }
            # The real schema's id is a TEXT PK holding the event_id; an
            # INTEGER id PK must be left to AUTOINCREMENT instead.
            if "id" in cols and "INT" not in cols["id"].upper():
                values = {"id": envelope["event_id"], **values}
            use = [c for c in values if not cols or c in cols]
            if not use:
                return
            placeholders = ", ".join("?" for _ in use)
            conn.execute(
                f"INSERT INTO event_log ({', '.join(use)})"
                f" VALUES ({placeholders})",
                tuple(values[c] for c in use),
            )
            conn.commit()
        except Exception:
            pass  # the journal is best-effort; live delivery must not fail
        finally:
            self._close_quietly(conn)

    @staticmethod
    def _close_quietly(conn: Any) -> None:
        try:
            conn.close()
        except Exception:
            pass
