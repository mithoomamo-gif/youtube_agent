"""WebSocket ticket exchange + socket endpoint (roadmap section 31 / §4.4).

Auth flow: the WebView cannot set headers on a WebSocket and the launch
token must never appear in a URL, so the UI first calls
``POST /api/v1/ws-ticket`` over REST (launch token in the header, enforced
by middleware) and receives a single-use, 60-second CSPRNG ticket. The
ticket is then presented inside the ``client.hello`` frame — the first
frame on ``/api/v1/ws``, within 5 seconds — or the socket is closed 4401.

Handshake replies with ``system.hello_ack`` (negotiated schema version,
boot_id, current seq). If the client presents a matching
``(last_boot_id, last_seq)`` cursor, missed journal frames are replayed
(capped at 1,000); a boot change or an oversized gap yields
``system.snapshot_required`` and the client resyncs over REST.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from typing import Any, Optional

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from app.ws_hub import CLOSE_TRY_AGAIN_LATER, WSHub

__all__ = [
    "router",
    "ws_router",
    "TicketStore",
    "get_hub",
    "get_ticket_store",
    "TICKET_TTL_S",
    "HELLO_TIMEOUT_S",
    "SUPPORTED_WS_SCHEMAS",
]

TICKET_TTL_S = 60.0
HELLO_TIMEOUT_S = 5.0
REPLAY_LIMIT = 1000
SUPPORTED_WS_SCHEMAS = (1,)

CLOSE_UNAUTHORIZED = 4401
CLOSE_DRAINING = 4402
CLOSE_VERSION_MISMATCH = 4410

router = APIRouter()
ws_router = APIRouter()


# ---------------------------------------------------------------------- #
# Single-use ticket store
# ---------------------------------------------------------------------- #


class TicketStore:
    """In-memory single-use WS tickets with a TTL.

    ``clock`` is injectable so tests can control expiry deterministically.
    """

    def __init__(
        self, ttl: float = TICKET_TTL_S, clock: Any = time.time
    ) -> None:
        self.ttl = float(ttl)
        self._clock = clock
        self._tickets: dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        # 32 random bytes from the OS CSPRNG, base64url-encoded (43 chars).
        ticket = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            self._tickets[ticket] = now + self.ttl
        return ticket

    def consume(self, ticket: Optional[str]) -> bool:
        """Single-use: a ticket validates exactly once, before expiry."""
        if not ticket or not isinstance(ticket, str):
            return False
        now = self._clock()
        with self._lock:
            expires_at = self._tickets.pop(ticket, None)
            if expires_at is None:
                return False
            return now < expires_at

    def _purge_locked(self, now: float) -> None:
        expired = [t for t, exp in self._tickets.items() if exp <= now]
        for t in expired:
            del self._tickets[t]

    def __len__(self) -> int:
        return len(self._tickets)


_default_ticket_store = TicketStore()


def get_ticket_store(app: Any) -> TicketStore:
    store = getattr(app.state, "ws_ticket_store", None)
    if store is None:
        store = _default_ticket_store
        try:
            app.state.ws_ticket_store = store
        except Exception:
            pass
    return store


# ---------------------------------------------------------------------- #
# Hub access
# ---------------------------------------------------------------------- #


def get_hub(source: Any) -> WSHub:
    """Return ``app.state.ws_hub``, creating it on demand.

    ``source`` may be a FastAPI app, a Starlette Request or a WebSocket
    (anything with an ``.app`` attribute, or the app itself).
    """
    app = getattr(source, "app", source)
    hub = getattr(app.state, "ws_hub", None)
    if hub is not None:
        return hub

    boot_id = "dev-boot"
    conn_factory = None
    try:
        from app.config import get_settings

        settings = get_settings()
        boot_id = getattr(settings, "boot_id", None) or boot_id
        try:
            from app.db import connect

            db_path = (
                getattr(settings, "db_path", None)
                or getattr(settings, "database_path", None)
                or getattr(settings, "data_dir", None)
            )
            if db_path is not None:
                conn_factory = lambda: connect(db_path)  # noqa: E731
        except Exception:
            conn_factory = None
    except Exception:
        pass

    hub = WSHub(boot_id=str(boot_id), conn_factory=conn_factory)
    try:
        app.state.ws_hub = hub
    except Exception:
        pass
    return hub


# ---------------------------------------------------------------------- #
# REST: ticket exchange
# ---------------------------------------------------------------------- #


@router.post("/api/v1/ws-ticket")
async def create_ws_ticket(request: Request) -> dict:
    """Mint a single-use WS ticket. The launch token on this REST call is
    enforced by the auth middleware (another module's responsibility)."""
    store = get_ticket_store(request.app)
    return {"ticket": store.create(), "expires_in": int(TICKET_TTL_S)}


# ---------------------------------------------------------------------- #
# WebSocket endpoint
# ---------------------------------------------------------------------- #


def _frame_topics(message: dict) -> list:
    payload = message.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    topics = (
        message.get("subscriptions")
        or message.get("topics")
        or payload.get("subscriptions")
        or payload.get("topics")
        or []
    )
    return [t for t in topics if isinstance(t, str)]


@ws_router.websocket("/api/v1/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    app = websocket.app
    hub = get_hub(app)
    store = get_ticket_store(app)

    await websocket.accept()
    state = None
    try:
        # --- client.hello: first frame, within 5 s, valid ticket -------- #
        try:
            hello = await asyncio.wait_for(
                websocket.receive_json(), timeout=HELLO_TIMEOUT_S
            )
        except Exception:
            await websocket.close(code=CLOSE_UNAUTHORIZED)
            return
        if not isinstance(hello, dict) or hello.get("type") != "client.hello":
            await websocket.close(code=CLOSE_UNAUTHORIZED)
            return
        if not store.consume(hello.get("ticket")):
            await websocket.close(code=CLOSE_UNAUTHORIZED)
            return
        versions = hello.get("ws_schema_versions") or []
        negotiated = next(
            (v for v in SUPPORTED_WS_SCHEMAS if v in versions), None
        )
        if negotiated is None:
            await websocket.close(code=CLOSE_VERSION_MISMATCH)
            return

        subscriptions = _frame_topics(hello) or ["all"]
        state = hub.register(websocket, set(subscriptions), start_sender=False)

        # --- system.hello_ack ------------------------------------------- #
        last_seq = hello.get("last_seq")
        ack = hub.make_envelope(
            "system.hello_ack",
            {
                "ws_schema": negotiated,
                "backend_version": getattr(app.state, "backend_version", None)
                or "0.1.0",
                "boot_id": hub.boot_id,
                "last_seq": hub.current_seq,
                "replay_from": last_seq,
            },
            consume_seq=False,
        )
        await websocket.send_json(ack)

        # --- replay or snapshot ----------------------------------------- #
        last_boot_id = hello.get("last_boot_id")
        if last_boot_id is not None and last_boot_id != hub.boot_id:
            await websocket.send_json(
                hub.snapshot_required_frame("boot_changed")
            )
        elif last_boot_id == hub.boot_id and last_seq is not None:
            frames = hub.replay_since(hub.boot_id, last_seq, limit=REPLAY_LIMIT)
            if frames is None:
                await websocket.send_json(
                    hub.snapshot_required_frame("boot_changed")
                )
            elif (
                len(frames) >= REPLAY_LIMIT
                and frames
                and frames[-1].get("seq", 0) < hub.current_seq
            ):
                # Gap larger than the replay cap: REST resync instead.
                await websocket.send_json(
                    hub.snapshot_required_frame("seq_gap")
                )
            elif not frames and last_seq < hub.current_seq:
                # Events were emitted after the client's cursor but are not
                # in the journal (pruned / journal unavailable): the gap
                # cannot be filled from the socket — REST resync instead.
                await websocket.send_json(
                    hub.snapshot_required_frame("seq_gap")
                )
            else:
                for frame in frames:
                    await websocket.send_json(frame)

        # Live push starts only after ack + replay so ordering is preserved.
        hub.start_sender(state)

        # --- main loop --------------------------------------------------- #
        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                continue
            mtype = message.get("type")
            if mtype == "client.ping":
                hub.note_pong(state)
                await websocket.send_json(
                    hub.make_envelope(
                        "client.pong",
                        {"ts": hub.heartbeat_payload()["ts"]},
                        consume_seq=False,
                    )
                )
            elif mtype == "client.subscribe":
                state.subscriptions.update(_frame_topics(message))
            elif mtype == "client.unsubscribe":
                state.subscriptions.difference_update(_frame_topics(message))
            elif mtype == "client.command":
                await _handle_command(websocket, hub, app, message)
            # Unknown client frames are ignored (additive evolution rule).
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close(code=CLOSE_TRY_AGAIN_LATER)
        except Exception:
            pass
    finally:
        if state is not None:
            hub.unregister(state)


async def _handle_command(
    websocket: WebSocket, hub: WSHub, app: Any, message: dict
) -> None:
    payload = message.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    name = (
        message.get("name")
        or message.get("command")
        or payload.get("name")
        or payload.get("command")
    )
    command_id = message.get("command_id") or payload.get("command_id")
    args = message.get("args") or payload.get("args") or {}

    handler = getattr(app.state, "ws_command_handler", None)
    if handler is None:
        # Real wiring (same service layer as REST) is another team's job.
        await websocket.send_json(
            hub.make_envelope(
                "system.error",
                {
                    "code": "command_unavailable",
                    "message": "WebSocket commands are not wired yet",
                    "command": name,
                    "command_id": command_id,
                },
                consume_seq=False,
            )
        )
        return

    try:
        result = handler(name, args)
        if asyncio.iscoroutine(result):
            result = await result
        ok = True
        extra: dict = {}
        if isinstance(result, dict):
            extra = dict(result)
            ok = bool(extra.pop("ok", True))
        elif isinstance(result, bool):
            ok = result
        await websocket.send_json(
            hub.make_envelope(
                "client.command_result",
                {"command_id": command_id, "ok": ok, **extra},
                consume_seq=False,
            )
        )
    except Exception as exc:  # handler failure must not kill the socket
        await websocket.send_json(
            hub.make_envelope(
                "client.command_result",
                {"command_id": command_id, "ok": False, "error": str(exc)},
                consume_seq=False,
            )
        )
