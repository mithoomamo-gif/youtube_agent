"""Tests for the WebSocket layer: ticket store, WSHub (envelope, topics,
backpressure, heartbeat, replay) and the /api/v1/ws handshake.

Self-contained: builds a minimal FastAPI app mounting only the WS routers
(the launch-token middleware is another module's job and is not needed to
exercise the ticket store + hub directly).
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from app.routers.ws import TicketStore, router, ws_router  # noqa: E402
from app.ws_hub import WSHub  # noqa: E402

ENVELOPE_KEYS = {"v", "type", "event_id", "seq", "boot_id", "ts", "payload"}


class FakeSocket:
    """Minimal stand-in for a WebSocket: records sent frames and closes."""

    def __init__(self) -> None:
        self.sent: list = []
        self.close_code = None

    async def send_json(self, frame) -> None:
        self.sent.append(frame)

    def close(self, code: int = 1000) -> None:
        # Deliberately synchronous: the hub must be able to force-close a
        # connection from unit tests with no running event loop. (The hub
        # also schedules coroutine close() results when a loop exists.)
        self.close_code = code


def drain(state) -> list:
    frames = []
    while True:
        try:
            frames.append(state.queue.get_nowait())
        except Exception:
            return frames


# ---------------------------------------------------------------------- #
# Ticket store
# ---------------------------------------------------------------------- #


def test_ticket_consume_works_once():
    store = TicketStore()
    ticket = store.create()
    assert isinstance(ticket, str) and len(ticket) >= 32
    assert store.consume(ticket) is True
    assert store.consume(ticket) is False  # single-use


def test_ticket_unknown_and_empty_fail():
    store = TicketStore()
    assert store.consume("does-not-exist") is False
    assert store.consume("") is False
    assert store.consume(None) is False


def test_ticket_expires_with_injected_clock():
    now = [1000.0]
    store = TicketStore(ttl=60.0, clock=lambda: now[0])
    ticket = store.create()
    now[0] += 59.0
    assert store.consume(ticket) is True

    ticket2 = store.create()
    now[0] += 61.0  # past the 60 s TTL
    assert store.consume(ticket2) is False


def test_tickets_are_unique():
    store = TicketStore()
    assert store.create() != store.create()


# ---------------------------------------------------------------------- #
# Hub: envelope + sequencing
# ---------------------------------------------------------------------- #


def test_publish_envelope_shape_and_monotonic_seq():
    hub = WSHub("boot-A")
    e1 = hub.publish("agent.status_changed", {"status": "running"},
                     project_id="p1", job_id="j1")
    e2 = hub.publish("progress.job", {"percent": 10}, project_id="p1")
    for env in (e1, e2):
        assert ENVELOPE_KEYS <= set(env)
        assert env["v"] == 1
        assert env["boot_id"] == "boot-A"
        assert env["ts"].endswith("Z")
        assert isinstance(env["payload"], dict)
        assert env["event_id"]
    assert e1["project_id"] == "p1" and e1["job_id"] == "j1"
    assert "job_id" not in e2  # scope fields only when provided
    assert e2["seq"] == e1["seq"] + 1
    assert e1["event_id"] != e2["event_id"]
    assert hub.current_seq == e2["seq"]


def test_snapshot_required_frame():
    hub = WSHub("boot-A")
    frame = hub.snapshot_required_frame("seq_gap")
    assert frame["type"] == "system.snapshot_required"
    assert frame["payload"] == {"reason": "seq_gap"}
    with pytest.raises(ValueError):
        hub.snapshot_required_frame("nonsense")


def test_heartbeat_payload_and_frame():
    hub = WSHub("boot-A")
    payload = hub.heartbeat_payload()
    assert payload["ts"].endswith("Z") and payload["seq"] == hub.current_seq
    frame = hub.heartbeat_frame()
    assert frame["type"] == "system.heartbeat"
    assert hub.current_seq == 0  # heartbeat must not consume seq


# ---------------------------------------------------------------------- #
# Hub: topic filtering
# ---------------------------------------------------------------------- #


def test_topic_filtering_project_and_all():
    hub = WSHub("boot-A")
    s_p1 = hub.register(FakeSocket(), {"project:p1"}, start_sender=False)
    s_p2 = hub.register(FakeSocket(), {"project:p2"}, start_sender=False)
    s_all = hub.register(FakeSocket(), {"all"}, start_sender=False)
    s_logs = hub.register(FakeSocket(), {"logs"}, start_sender=False)

    hub.publish("agent.status_changed", {"status": "running"}, project_id="p1")
    assert [f["type"] for f in drain(s_p1)] == ["agent.status_changed"]
    assert drain(s_p2) == []  # other project's event is not delivered
    assert [f["type"] for f in drain(s_all)] == ["agent.status_changed"]
    assert drain(s_logs) == []

    hub.publish("log.line", {"level": "info", "message": "hi"})
    assert [f["type"] for f in drain(s_logs)] == ["log.line"]
    assert drain(s_p1) == []

    hub.publish("cost.ticker", {"est_usd_delta": 0.01})
    s_cost = hub.register(FakeSocket(), {"cost"}, start_sender=False)
    hub.publish("cost.ticker", {"est_usd_delta": 0.02})
    assert [f["type"] for f in drain(s_cost)] == ["cost.ticker"]


def test_system_frames_reach_every_connection():
    hub = WSHub("boot-A")
    s_logs = hub.register(FakeSocket(), {"logs"}, start_sender=False)
    hub.publish("system.draining", {"reason": "shutdown"})
    assert [f["type"] for f in drain(s_logs)] == ["system.draining"]


def test_queue_topic():
    hub = WSHub("boot-A")
    s_q = hub.register(FakeSocket(), {"queue"}, start_sender=False)
    hub.publish("queue.stats_changed", {"running": 2})
    assert len(drain(s_q)) == 1


# ---------------------------------------------------------------------- #
# Hub: backpressure
# ---------------------------------------------------------------------- #


def _filled_hub(event_type: str, n: int = 3):
    hub = WSHub("boot-A", queue_maxsize=n)
    fake = FakeSocket()
    state = hub.register(fake, {"all"}, start_sender=False)
    for i in range(n):
        hub.publish(event_type, {"i": i})
    assert state.queue.qsize() == n
    return hub, fake, state


def test_backpressure_drops_progress_first():
    hub, fake, state = _filled_hub("progress.job")
    # Queue full of progress: a new progress frame is dropped, no close.
    hub.publish("progress.job", {"percent": 99})
    assert state.queue.qsize() == 3
    assert fake.close_code is None
    assert hub.connection_count == 1
    # A guaranteed frame evicts a progress frame and is delivered.
    hub.publish("gate.pending", {"gate": "A", "approval_id": "a1"},
                project_id="p1")
    frames = drain(state)
    assert frames[-1]["type"] == "gate.pending"
    assert sum(1 for f in frames if f["type"] == "progress.job") == 2
    assert fake.close_code is None


def test_backpressure_never_drop_overflow_closes_socket():
    hub, fake, state = _filled_hub("gate.pending")
    # Queue full of guaranteed frames + another guaranteed frame arrives:
    # documented behaviour — socket closed with 1013 and unregistered.
    hub.publish("gate.decided", {"decision": "approve"})
    assert fake.close_code == 1013
    assert state.closed is True
    assert hub.connection_count == 0


def test_backpressure_droppable_incoming_dropped_when_queue_all_guaranteed():
    hub, fake, state = _filled_hub("gate.pending")
    hub.publish("progress.job", {"percent": 1})
    hub.publish("cost.ticker", {"est_usd_delta": 1})
    assert state.queue.qsize() == 3  # both incoming frames dropped
    assert fake.close_code is None
    assert hub.connection_count == 1


def test_backpressure_log_and_cost_evicted_before_guaranteed():
    hub = WSHub("boot-A", queue_maxsize=2)
    fake = FakeSocket()
    state = hub.register(fake, {"all"}, start_sender=False)
    hub.publish("cost.ticker", {"est_usd_delta": 1})
    hub.publish("log.line", {"message": "x"})
    assert state.queue.qsize() == 2
    # progress (incoming, droppable) evicts log.line (lower priority than
    # cost.ticker? no — log.line priority 1 < cost.ticker 2, so log goes).
    hub.publish("progress.job", {"percent": 5})
    types = [f["type"] for f in drain(state)]
    assert types == ["cost.ticker", "progress.job"]


# ---------------------------------------------------------------------- #
# Hub: heartbeats
# ---------------------------------------------------------------------- #


def test_check_heartbeats_closes_stale_connections():
    hub = WSHub("boot-A", heartbeat_timeout_s=40.0)
    stale_fake, fresh_fake = FakeSocket(), FakeSocket()
    stale = hub.register(stale_fake, {"all"}, start_sender=False)
    fresh = hub.register(fresh_fake, {"all"}, start_sender=False)
    stale.last_pong = 1000.0
    fresh.last_pong = 1030.0
    closed = hub.check_heartbeats(now=1041.0)  # stale: 41 s, fresh: 11 s
    assert closed == [stale]
    assert stale_fake.close_code == 1013
    assert fresh_fake.close_code is None
    assert hub.connection_count == 1
    # note_pong stamps time.monotonic() (the REAL clock), while the
    # 1000.0/1030.0/1041.0 values above are injected fake times for
    # check_heartbeats — never compare the two domains. CI run #5 failed
    # here because a fresh runner's monotonic clock was only ~849 s when
    # this test ran (assert 849.548172748 > 1030.0); run #3 passed only
    # because that runner had been alive > 1030 s by then. Assert the
    # stamp is "now" in the real clock's own domain instead.
    before = time.monotonic()
    hub.note_pong(fresh)
    assert before <= fresh.last_pong <= time.monotonic()


# ---------------------------------------------------------------------- #
# Hub: journal persistence + replay
# ---------------------------------------------------------------------- #


def _journal_factory(tmp_path, style):
    path = tmp_path / "journal.db"
    conn = sqlite3.connect(path)
    if style == "seq":
        conn.execute(
            "CREATE TABLE event_log (seq INTEGER PRIMARY KEY, boot_id TEXT,"
            " type TEXT, payload_json TEXT, created_at_ms INTEGER)"
        )
    elif style == "real":
        # The actual schema from app/schema_ddl.py (worker 3).
        conn.execute(
            "CREATE TABLE event_log (id TEXT PRIMARY KEY, seq INTEGER NOT"
            " NULL, boot_id TEXT NOT NULL, type TEXT NOT NULL,"
            " payload_json TEXT NULL, created_at_ms INTEGER NOT NULL,"
            " UNIQUE (boot_id, seq))"
        )
    else:  # "id"
        conn.execute(
            "CREATE TABLE event_log (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " boot_id TEXT, type TEXT, payload_json TEXT,"
            " created_at_ms INTEGER)"
        )
    conn.commit()
    conn.close()
    return lambda: sqlite3.connect(path)


@pytest.mark.parametrize("style", ["seq", "id", "real"])
def test_replay_since_same_boot_and_seq_resume(tmp_path, style):
    factory = _journal_factory(tmp_path, style)
    hub = WSHub("boot-R", conn_factory=factory)
    hub.publish("agent.status_changed", {"n": 1})
    hub.publish("agent.decision", {"n": 2})
    hub.publish("project.updated", {"n": 3})

    frames = hub.replay_since("boot-R", 1)
    assert [f["seq"] for f in frames] == [2, 3]
    assert [f["payload"]["n"] for f in frames] == [2, 3]
    assert hub.replay_since("boot-R", 3) == []
    # A different boot can never replay: caller sends snapshot_required.
    assert hub.replay_since("boot-OTHER", 0) is None

    # A fresh hub for the same boot resumes seq after the persisted max.
    hub2 = WSHub("boot-R", conn_factory=factory)
    assert hub2.current_seq == 3
    env = hub2.publish("agent.status_changed", {"n": 4})
    assert env["seq"] == 4


def test_replay_cap_is_respected(tmp_path):
    factory = _journal_factory(tmp_path, "seq")
    hub = WSHub("boot-R", conn_factory=factory)
    for i in range(5):
        hub.publish("progress.job", {"i": i})
    assert len(hub.replay_since("boot-R", 0, limit=3)) == 3


# ---------------------------------------------------------------------- #
# HTTP + WebSocket integration (minimal FastAPI app)
# ---------------------------------------------------------------------- #


def build_app(hub=None, store=None):
    app = FastAPI()
    app.include_router(router)
    app.include_router(ws_router)
    # NOTE: explicit None checks — TicketStore defines __len__, so an empty
    # store is falsy and `store or TicketStore()` would silently swap it.
    app.state.ws_ticket_store = store if store is not None else TicketStore()
    app.state.ws_hub = hub if hub is not None else WSHub("boot-ws")
    return app


def hello_frame(ticket, **overrides):
    frame = {
        "type": "client.hello",
        "ticket": ticket,
        "ws_schema_versions": [1],
        "subscriptions": ["all"],
    }
    frame.update(overrides)
    return frame


def test_ws_ticket_endpoint():
    client = TestClient(build_app())
    resp = client.post("/api/v1/ws-ticket")
    assert resp.status_code == 200
    body = resp.json()
    assert body["expires_in"] == 60
    assert isinstance(body["ticket"], str) and len(body["ticket"]) >= 32


def test_ws_handshake_ping_and_unwired_command():
    store = TicketStore()
    hub = WSHub("boot-ws")
    client = TestClient(build_app(hub=hub, store=store))
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json(hello_frame(store.create()))
        ack = ws.receive_json()
        assert ack["type"] == "system.hello_ack"
        assert ack["boot_id"] == "boot-ws"
        assert ENVELOPE_KEYS <= set(ack)
        assert ack["payload"]["ws_schema"] == 1
        assert ack["payload"]["last_seq"] == hub.current_seq

        ws.send_json({"type": "client.ping"})
        pong = ws.receive_json()
        assert pong["type"] == "client.pong"

        ws.send_json({"type": "client.command", "command_id": "c1",
                      "command": "pause_all"})
        err = ws.receive_json()
        assert err["type"] == "system.error"
        assert err["payload"]["code"] == "command_unavailable"


def test_ws_bad_ticket_closed_4401():
    client = TestClient(build_app())
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json(hello_frame("bogus-ticket"))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4401


def test_ws_ticket_replay_closed_4401():
    store = TicketStore()
    client = TestClient(build_app(store=store))
    ticket = store.create()
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json(hello_frame(ticket))
        assert ws.receive_json()["type"] == "system.hello_ack"
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json(hello_frame(ticket))  # same ticket again = replay
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4401


def test_ws_version_mismatch_closed_4410():
    store = TicketStore()
    client = TestClient(build_app(store=store))
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json(hello_frame(store.create(), ws_schema_versions=[2]))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4410


def test_ws_replay_on_reconnect(tmp_path):
    factory = _journal_factory(tmp_path, "seq")
    hub = WSHub("boot-ws", conn_factory=factory)
    hub.publish("agent.status_changed", {"n": 1})
    hub.publish("gate.pending", {"n": 2})
    store = TicketStore()
    client = TestClient(build_app(hub=hub, store=store))
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json(hello_frame(store.create(),
                                 last_boot_id="boot-ws", last_seq=0))
        assert ws.receive_json()["type"] == "system.hello_ack"
        f1 = ws.receive_json()
        f2 = ws.receive_json()
        assert [f1["seq"], f2["seq"]] == [1, 2]
        assert f1["type"] == "agent.status_changed"


def test_ws_unfillable_gap_sends_snapshot_required():
    # Hub with no journal: an event exists past the client's cursor but
    # cannot be replayed, so the client must resync via REST.
    hub = WSHub("boot-ws")
    hub.publish("agent.status_changed", {"status": "running"})
    store = TicketStore()
    client = TestClient(build_app(hub=hub, store=store))
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json(hello_frame(store.create(),
                                 last_boot_id="boot-ws", last_seq=0))
        assert ws.receive_json()["type"] == "system.hello_ack"
        frame = ws.receive_json()
        assert frame["type"] == "system.snapshot_required"
        assert frame["payload"]["reason"] == "seq_gap"


def test_ws_boot_changed_sends_snapshot_required():
    store = TicketStore()
    client = TestClient(build_app(store=store))
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json(hello_frame(store.create(),
                                 last_boot_id="old-boot", last_seq=7))
        assert ws.receive_json()["type"] == "system.hello_ack"
        frame = ws.receive_json()
        assert frame["type"] == "system.snapshot_required"
        assert frame["payload"]["reason"] == "boot_changed"


def test_ws_command_with_injected_handler():
    store = TicketStore()
    app = build_app(store=store)
    seen = {}

    async def handler(name, args):
        seen["name"] = name
        seen["args"] = args
        return {"ok": True}

    app.state.ws_command_handler = handler
    client = TestClient(app)
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json(hello_frame(store.create()))
        assert ws.receive_json()["type"] == "system.hello_ack"
        ws.send_json({"type": "client.command", "command_id": "c9",
                      "name": "pause_job", "args": {"job_id": "j1"}})
        result = ws.receive_json()
        assert result["type"] == "client.command_result"
        assert result["payload"]["ok"] is True
        assert result["payload"]["command_id"] == "c9"
    assert seen == {"name": "pause_job", "args": {"job_id": "j1"}}
