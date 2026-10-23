"""Acceptance test C (Round 2): project execution + WebSocket events.

Boots the real sidecar as a subprocess via the documented launch-env
contract (``backend/app/config.py``: ``YTA_TOKEN`` required, ``YTA_PORT``,
``YTA_BOOT_ID``, ``YTA_DATA_DIR``; ``backend/app/main.py`` prints exactly
one structured stdout line ``{"event": "listening", "port": ..., ...}``),
then drives the public REST + WebSocket surface end to end:

0. Seed channel + niche  -- There is NO channel-creation endpoint (only
                            GET /api/v1/channels and the 202 OAuth-connect
                            stub), while the ``projects`` table has FKs to
                            ``channels(id)`` and ``niche_profiles(id)`` —
                            an unseeded placeholder makes the projects
                            router's best-effort INSERT OR IGNORE silently
                            drop the row, and the DB-driven execution layer
                            could then never see the project. So, exactly
                            like the execution team's own fixtures
                            (backend/tests/execution_fixtures.py::
                            seed_channel_niche), a minimal channel+niche
                            pair is inserted directly into the sidecar's
                            SQLite DB before the REST flow starts.
1. Create a project      -- POST /api/v1/projects (publish_mode=review_first,
                            Idempotency-Key required by the router). The
                            WebSocket is connected BEFORE creation because
                            project creation now enqueues the stage DAG.
2. Connect the WebSocket -- POST /api/v1/ws-ticket, then
                            ws://127.0.0.1:<port>/api/v1/ws with a
                            ``client.hello`` first frame (ticket +
                            ws_schema_versions [1], subscriptions ["all"]).
                            The socket is connected BEFORE the project is
                            started so no live event can be missed.
3. Start the project     -- POST /api/v1/projects/{id}/pipeline/start
                            (the projects router's only start path;
                            action in {start,pause,resume,cancel}).
4. Poll execution        -- GET /api/v1/jobs/{job_id} (primary: the start
                            response's job id), GET /api/v1/jobs?project_id=
                            and GET /api/v1/projects/{id}, plus
                            GET /api/v1/approvals as gate evidence.

Assertions (state-machine behaviour, not media quality — stub providers
are fine):

* the enqueued pipeline job actually EXECUTES via a runner: its status
  leaves ``queued`` (leased/running/done/succeeded/failed/dead/...);
* at least one ``agent.status_changed`` WS event for this project;
* the project reaches a TERMINAL state or a BLOCKED-AT-GATE state
  (e.g. awaiting Gate A/B approval in review_first mode);
* the sidecar is still healthy at the end (GET /health/live + /health/ready).

Bounded timeouts throughout: execution polling defaults to 75 s
(override with ``YTA_TEST_EXEC_TIMEOUT_S``).

KNOWN BUG (observed 2026-10-23, non-blocking for this acceptance):
``GET /api/v1/projects/{id}`` returns the projects router's in-memory
dict first, so its ``status`` stays ``'draft'`` / ``current_phase``
stays null even after the execution layer has moved the DB row to
``awaiting_review`` at Gate A. Blocked-at-gate evidence is therefore
taken from GET /api/v1/approvals + the jobs list (both DB-backed), and
the DB status is included in failure diagnostics for localisation.

Self-contained: does not depend on tests/integration/conftest.py.
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python"

BOOT_TIMEOUT_S = 25.0
EXEC_TIMEOUT_S = float(os.environ.get("YTA_TEST_EXEC_TIMEOUT_S", "75"))
WS_DRAIN_S = 10.0
POLL_INTERVAL_S = 0.5

JOB_EXECUTED_STATES = {
    "leased",
    "running",
    "done",
    "succeeded",
    "success",
    "completed",
    "failed",
    "dead",
    "cancelled",
    "canceled",
}
JOB_TERMINAL_STATES = {"done", "succeeded", "success", "completed", "failed", "dead", "cancelled", "canceled"}

PROJECT_TERMINAL_STATES = {
    "done",
    "completed",
    "complete",
    "succeeded",
    "success",
    "published",
    "ready",  # schema status: production finished, ready for publish
    "finished",
    "failed",
    "dead",
    "cancelled",
    "canceled",
}
PROJECT_BLOCKED_STATES = {
    "blocked",
    "awaiting_gate_a",
    "awaiting_gate_b",
    "awaiting_gate",
    "awaiting_approval",
    "awaiting_review",
    "pending_approval",
    "gate_a",
    "gate_b",
    "paused_at_gate",
}


# ---------------------------------------------------------------------- #
# Sidecar boot (env contract)
# ---------------------------------------------------------------------- #


class Sidecar:
    def __init__(self, data_dir: Path):
        self.token = "test-token-" + secrets.token_hex(8)
        self.boot_id = "test-boot-" + uuid.uuid4().hex[:12]
        self.data_dir = data_dir
        self.proc: Optional[subprocess.Popen] = None
        self.port: Optional[int] = None
        self._lines: "queue.Queue[str]" = queue.Queue()
        self.output: list[str] = []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
        env = dict(os.environ)
        env.update(
            {
                "YTA_TOKEN": self.token,
                "YTA_PORT": "0",  # main.run() picks a free loopback port
                "YTA_BOOT_ID": self.boot_id,
                "YTA_DATA_DIR": str(self.data_dir),
                "YTA_LOG_LEVEL": "info",
                "PYTHONPATH": str(BACKEND_DIR),
            }
        )
        self.proc = subprocess.Popen(
            [python, "-m", "app.main"],
            cwd=str(BACKEND_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        reader = threading.Thread(target=self._pump, daemon=True)
        reader.start()
        deadline = time.monotonic() + BOOT_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"sidecar exited early rc={self.proc.returncode}; "
                    f"output tail: {self.output[-30:]}"
                )
            try:
                line = self._lines.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                data = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict) and data.get("event") == "listening":
                self.port = int(data["port"])
                return
        raise AssertionError(
            f"sidecar did not print the listening line within "
            f"{BOOT_TIMEOUT_S}s; output tail: {self.output[-30:]}"
        )

    def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            self.output.append(line.rstrip())
            self._lines.put(line)

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


@pytest.fixture(scope="module")
def sidecar(tmp_path_factory):
    sc = Sidecar(tmp_path_factory.mktemp("yta-sidecar-data"))
    sc.start()
    try:
        yield sc
    finally:
        sc.stop()


def _http(sidecar: Sidecar):
    import httpx

    return httpx.Client(
        base_url=sidecar.base_url,
        headers={"X-Local-Token": sidecar.token},
        timeout=10.0,
        trust_env=False,  # loopback only: never honour *_proxy env vars
    )


# ---------------------------------------------------------------------- #
# WebSocket collector
# ---------------------------------------------------------------------- #


class WSCollector:
    """Connects with a REST-minted ticket, records every frame."""

    def __init__(self, sidecar: Sidecar, ticket: str):
        self.sidecar = sidecar
        self.ticket = ticket
        self.frames: list[dict] = []
        self.ack: Optional[dict] = None
        self.error: Optional[BaseException] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self.ack is not None:
                return
            if self.error is not None:
                raise AssertionError(f"websocket connect/hello failed: {self.error!r}")
            time.sleep(0.05)
        raise AssertionError("timed out waiting for system.hello_ack")

    def _run(self) -> None:
        try:
            from websockets.sync.client import connect

            url = f"ws://127.0.0.1:{self.sidecar.port}/api/v1/ws"
            with connect(url, open_timeout=5, proxy=None) as ws:
                self._ws = ws
                ws.send(
                    json.dumps(
                        {
                            "type": "client.hello",
                            "ticket": self.ticket,
                            "ws_schema_versions": [1],
                            "subscriptions": ["all"],
                            # Replay cursor: journaled frames from this
                            # boot are replayed after hello_ack, so an
                            # event emitted just before connect is not lost.
                            "last_boot_id": self.sidecar.boot_id,
                            "last_seq": 0,
                        }
                    )
                )
                ack = json.loads(ws.recv(timeout=5))
                self.ack = ack
                if isinstance(ack, dict):
                    self.frames.append(ack)
                while not self._stop.is_set():
                    try:
                        raw = ws.recv(timeout=1.0)
                    except TimeoutError:
                        continue
                    try:
                        frame = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(frame, dict):
                        self.frames.append(frame)
        except BaseException as exc:  # recorded, asserted by the test body
            if not self._stop.is_set():
                self.error = exc

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def types_seen(self) -> list[str]:
        return [f.get("type", "?") for f in self.frames]

    def agent_status_events_for(self, project_id: str, since_index: int = 0) -> list[dict]:
        out = []
        for frame in self.frames[since_index:]:
            if frame.get("type") != "agent.status_changed":
                continue
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            scoped = frame.get("project_id") or payload.get("project_id")
            # "for this project": explicitly scoped to it, or unscoped
            # (hub topic derivation drops project-less agent frames to
            # 'all' subscribers only) and observed after start.
            if scoped is None or scoped == project_id:
                out.append(frame)
        return out

    def gate_events_for(self, project_id: str) -> list[dict]:
        out = []
        for frame in self.frames:
            ftype = str(frame.get("type") or "")
            if not (ftype.startswith("gate.") or "approval" in ftype):
                continue
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            scoped = frame.get("project_id") or payload.get("project_id")
            if scoped is None or scoped == project_id:
                out.append(frame)
        return out


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


def _seed_channel_niche(sidecar: Sidecar) -> tuple[str, str]:
    """Insert a minimal channel + niche_profile pair into the sidecar DB.

    Mirrors backend/tests/execution_fixtures.py::seed_channel_niche (only
    NOT NULL / CHECK-constrained columns). Returns (channel_id, niche_id).
    """
    import sqlite3
    import time as _time

    suffix = uuid.uuid4().hex[:8]
    niche_id = str(uuid.uuid4())
    channel_id = str(uuid.uuid4())
    now_ms = int(_time.time() * 1000)
    db_path = sidecar.data_dir / "app.db"
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            "INSERT INTO niche_profiles "
            "(id, key, label, source, risk_class, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, 'builtin', 'normal', 1, ?, ?)",
            (niche_id, f"acceptance-{suffix}", "Acceptance Test Niche", now_ms, now_ms),
        )
        conn.execute(
            "INSERT INTO channels "
            "(id, youtube_channel_id, title, default_niche_profile_id, "
            " language, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'en-US', 'active', ?, ?)",
            (
                channel_id,
                f"UC-acceptance-{suffix}",
                "Acceptance Test Channel",
                niche_id,
                now_ms,
                now_ms,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return channel_id, niche_id


def _db_project_status(sidecar: Sidecar, project_id: str) -> Optional[str]:
    """Read-only diagnostic: the projects-table status for this project.

    Diagnostic only (not passing evidence): GET /projects/{id} prefers the
    router's in-memory dict, so the DB row is the execution layer's own
    view and helps localise a failure.
    """
    import sqlite3

    try:
        conn = sqlite3.connect(str(sidecar.data_dir / "app.db"), timeout=5.0)
        try:
            row = conn.execute(
                "SELECT status FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:
        return None


def _project_blocked_evidence(project: dict, approvals_items: list, collector: WSCollector, project_id: str) -> Optional[str]:
    status = str(project.get("status") or "").lower()
    if status in PROJECT_BLOCKED_STATES:
        return f"project status={status!r}"
    # Nested gate / phase hints (graph state shapes vary by team).
    blob = json.dumps(project, default=str).lower()
    for needle in ("awaiting_gate", "awaiting_approval", "awaiting_review", "pending_approval"):
        if needle in blob:
            return f"project payload contains {needle!r}"
    for item in approvals_items:
        ipayload = item if isinstance(item, dict) else {}
        if ipayload.get("project_id") in (None, project_id):
            return f"approval queue non-empty: {ipayload!r}"
    gate_frames = collector.gate_events_for(project_id)
    pending = [f for f in gate_frames if "pending" in str(f.get("type")) or "awaiting" in json.dumps(f, default=str).lower()]
    if pending:
        return f"gate WS event: {pending[0].get('type')!r}"
    return None


# ---------------------------------------------------------------------- #
# Tests
# ---------------------------------------------------------------------- #


def test_sidecar_boots_and_health_is_green(sidecar):
    with _http(sidecar) as http:
        live = http.get("/health/live")  # token-exempt, still fine with header
        assert live.status_code == 200, live.text
        assert live.json().get("status") == "ok"
        ready = http.get("/health/ready")
        assert ready.status_code == 200, ready.text
        assert ready.json().get("db") == "ok", ready.text


def test_project_execution_and_websocket_events(sidecar):
    with _http(sidecar) as http:
        # --- prerequisites ---------------------------------------------- #
        # No channel-creation API exists, and the projects table FKs to
        # channels/niche_profiles: seed the pair in SQLite (see docstring).
        channel_id, niche_id = _seed_channel_niche(sidecar)
        channels = http.get("/api/v1/channels")
        assert channels.status_code == 200, channels.text
        assert any(
            c.get("id") == channel_id for c in channels.json().get("items") or []
        ), f"seeded channel not visible via GET /api/v1/channels: {channels.text}"

        # --- WS ticket + connect BEFORE creation (DAG starts there) ----- #
        ticket_resp = http.post("/api/v1/ws-ticket")
        assert ticket_resp.status_code == 200, (
            f"BLOCKER/BUG: POST /api/v1/ws-ticket expected 200, got "
            f"{ticket_resp.status_code}: {ticket_resp.text}"
        )
        ticket = ticket_resp.json()["ticket"]
        collector = WSCollector(sidecar, ticket)
        collector.start()
        assert collector.ack is not None
        assert collector.ack.get("type") == "system.hello_ack", collector.ack
        frames_before_start = len(collector.frames)

        # --- create project (review_first) ------------------------------ #
        created = http.post(
            "/api/v1/projects",
            headers=_idem(),
            json={
                "channel_id": channel_id,
                "niche_profile_id": niche_id,
                "topic_mode": "manual",
                "topic": "Acceptance test C: how rainbows form",
                "target_minutes": 2,
                "publish_mode": "review_first",
            },
        )
        assert created.status_code == 201, (
            f"BLOCKER/BUG: POST /api/v1/projects expected 201, got "
            f"{created.status_code}: {created.text}"
        )
        project = created.json()
        project_id = project["id"]
        assert project.get("status") == "draft", project

        job_id: Optional[str] = None
        job_statuses: list[str] = []
        project_statuses: list[str] = []
        final_project: dict[str, Any] = dict(project)
        approvals_items: list = []
        blocked_evidence: Optional[str] = None
        try:
            # --- start the project -------------------------------------- #
            started = http.post(
                f"/api/v1/projects/{project_id}/pipeline/start",
                headers=_idem(),
            )
            assert started.status_code == 202, (
                f"BLOCKER/BUG: POST /api/v1/projects/{{id}}/pipeline/start "
                f"expected 202, got {started.status_code}: {started.text} "
                f"(if 404: the start/execution path does not exist in the API)"
            )
            start_body = started.json()
            job_id = start_body.get("job_id")

            # --- poll execution ------------------------------------------ #
            deadline = time.monotonic() + EXEC_TIMEOUT_S
            while time.monotonic() < deadline:
                if job_id:
                    jr = http.get(f"/api/v1/jobs/{job_id}")
                    if jr.status_code == 200:
                        st = str(jr.json().get("status") or "")
                        if st and (not job_statuses or job_statuses[-1] != st):
                            job_statuses.append(st)
                listed = http.get("/api/v1/jobs", params={"project_id": project_id})
                if listed.status_code == 200:
                    for item in listed.json().get("items") or []:
                        st = str(item.get("status") or "")
                        if st and st not in job_statuses:
                            job_statuses.append(st)
                pr = http.get(f"/api/v1/projects/{project_id}")
                if pr.status_code == 200:
                    final_project = pr.json()
                    pst = str(final_project.get("status") or "")
                    if pst and (not project_statuses or project_statuses[-1] != pst):
                        project_statuses.append(pst)
                ar = http.get("/api/v1/approvals")
                if ar.status_code == 200:
                    approvals_items = ar.json().get("items") or []

                blocked_evidence = _project_blocked_evidence(
                    final_project, approvals_items, collector, project_id
                )
                pst = str(final_project.get("status") or "").lower()
                job_done = any(s in JOB_EXECUTED_STATES for s in job_statuses)
                if job_done and (pst in PROJECT_TERMINAL_STATES or blocked_evidence):
                    break
                time.sleep(POLL_INTERVAL_S)

            # Give the socket a bounded drain window for late events.
            drain_deadline = time.monotonic() + WS_DRAIN_S
            while time.monotonic() < drain_deadline:
                if collector.agent_status_events_for(project_id, frames_before_start):
                    break
                if str(final_project.get("status") or "").lower() in PROJECT_TERMINAL_STATES or blocked_evidence:
                    # Terminal/blocked already reached; a short grace only.
                    if time.monotonic() > drain_deadline - WS_DRAIN_S + 3.0:
                        break
                time.sleep(0.25)
        finally:
            collector.stop()

        # --- sidecar must still be alive -------------------------------- #
        live = http.get("/health/live")
        ready = http.get("/health/ready")
        health_ok = live.status_code == 200 and ready.status_code == 200

        ws_types = collector.types_seen()
        agent_events = collector.agent_status_events_for(project_id, frames_before_start)
        final_status = str(final_project.get("status") or "").lower()
        blocked_evidence = blocked_evidence or _project_blocked_evidence(
            final_project, approvals_items, collector, project_id
        )

        diag = (
            f"project_id={project_id} job_id={job_id} "
            f"job_statuses_seen={job_statuses} "
            f"project_statuses_seen={project_statuses} "
            f"final_project_status={final_status!r} "
            f"db_project_status={_db_project_status(sidecar, project_id)!r} "
            f"blocked_evidence={blocked_evidence!r} "
            f"approvals_count={len(approvals_items)} "
            f"ws_types_seen={ws_types} health_ok={health_ok}"
        )

        # 1) Jobs actually execute via the runner.
        assert any(s in JOB_EXECUTED_STATES for s in job_statuses), (
            "BLOCKER: pipeline job never executed — it stayed in "
            f"{job_statuses or 'no observable state'} for {EXEC_TIMEOUT_S:.0f}s. "
            "The projects router enqueues 'pipeline.start' into the SQLite "
            "job queue (POST .../pipeline/start -> 202), but no job runner / "
            "execution layer (backend/app/execution/) consumed it, the "
            "project status never left 'draft', and no agent.status_changed "
            f"event was published. {diag}"
        )
        # 2) At least one agent.status_changed WS event for this project.
        assert agent_events, (
            "BLOCKER: no 'agent.status_changed' WebSocket event observed "
            f"for this project within the execution+drain window. {diag}"
        )
        # 3) Terminal OR blocked-at-gate.
        assert final_status in PROJECT_TERMINAL_STATES or blocked_evidence, (
            "BLOCKER: project reached neither a terminal state "
            f"{sorted(PROJECT_TERMINAL_STATES)} nor a blocked-at-gate state "
            f"(e.g. awaiting Gate A/B approval). {diag}"
        )
        # 4) Sidecar did not crash.
        assert health_ok, f"sidecar health degraded by end of run. {diag}"
