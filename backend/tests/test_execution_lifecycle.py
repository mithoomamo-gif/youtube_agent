"""Execution lifecycle tests (Round 2, Worker 6).

Covers the wiring owned by ``app.execution.service``:

(a) ``POST /projects`` enqueues the real 10-job DAG
    (projects-router edit) and reports ``task_dag.status == 'queued'``
    with the DAG job ids.
(b) End-to-end: an ``auto`` project runs the full chain through
    :class:`ExecutionService` + the real JobRunner/stub handlers until
    every stage job — including ``gate.b`` — is done and the project
    row has progressed; a ``review_first`` project instead parks at
    Gate A with ``status='awaiting_review'``.
(c) ``pipeline.cancel`` (enqueued exactly like the REST endpoint does)
    is executed by the service's control handler: all queued stage
    jobs are cancelled and the project is marked ``cancelled``.
(d) Service lifecycle/attach degradation behaviour.

App construction follows tests/conftest.py + tests/test_routers.py:
the API test builds the bare router app (no launch-token middleware,
as in test_routers) against the shared default DB the routers resolve
via ``app.db.get_connection()``; service tests use an isolated
``tmp_path`` DB with an explicit conn_factory.

Sibling-module guard: DAG/runner-dependent tests ``xfail`` (never
fake-pass) when the sibling module is genuinely absent at runtime.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import tempfile
import time
import types
import uuid

# Same bootstrap as test_routers.py: a data dir must exist before any
# app module resolves settings/db paths (conftest also does this).
os.environ.setdefault("YTA_DATA_DIR", tempfile.mkdtemp(prefix="yta-exec-test-"))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db as db_module
from app.queue import SQLiteJobQueue

SUCCESS = ("done", "succeeded")
POLL_TIMEOUT_S = 10.0
POLL_INTERVAL_S = 0.05


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _require(module_name: str, attr: str | None = None):
    """Import a sibling module/attr or xfail the calling test."""
    try:
        module = importlib.import_module(module_name)
        if attr is not None:
            getattr(module, attr)
        return module
    except Exception as exc:  # sibling not landed yet — never fake it
        pytest.xfail(f"{module_name} not available yet: {exc!r}")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _seed_channel_niche(conn, suffix: str) -> tuple[str, str]:
    now = _now_ms()
    channel_id = f"chan-{suffix}"
    niche_id = f"niche-{suffix}"
    conn.execute(
        "INSERT OR IGNORE INTO niche_profiles "
        "(id, key, label, source, created_at, updated_at) "
        "VALUES (?, ?, ?, 'builtin', ?, ?)",
        (niche_id, f"key-{suffix}", f"Niche {suffix}", now, now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO channels "
        "(id, youtube_channel_id, title, language, created_at, updated_at) "
        "VALUES (?, ?, ?, 'en', ?, ?)",
        (channel_id, f"yt-{suffix}", f"Channel {suffix}", now, now),
    )
    return channel_id, niche_id


def _seed_project(conn_factory, *, publish_mode: str) -> str:
    suffix = uuid.uuid4().hex[:10]
    project_id = f"proj-{suffix}"
    conn = conn_factory()
    try:
        channel_id, niche_id = _seed_channel_niche(conn, suffix)
        now = _now_ms()
        conn.execute(
            "INSERT INTO projects "
            "(id, channel_id, niche_profile_id, topic, topic_source, "
            " publish_mode, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'Lifecycle test topic', 'agent', ?, 'draft', ?, ?)",
            (project_id, channel_id, niche_id, publish_mode, now, now),
        )
        try:
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()
    return project_id


def _jobs_for(conn_factory, project_id: str) -> list[dict]:
    conn = conn_factory()
    try:
        rows = conn.execute(
            "SELECT kind, status FROM jobs WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _project_row(conn_factory, project_id: str) -> dict | None:
    conn = conn_factory()
    try:
        row = conn.execute(
            "SELECT status, current_phase FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


@pytest.fixture()
def tmp_conn_factory(tmp_path):
    db_path = tmp_path / "exec_test.db"
    db_module.init_db(db_path).close()
    return lambda: db_module.get_connection(db_path)


def _make_service(conn_factory, **overrides):
    from app.execution.service import ExecutionService

    kwargs = {
        "runner_kwargs": {"poll_interval": 0.02},
        # Keep the scheduler loop out of these tests: first tick is far
        # beyond the test duration (scheduler fallback is covered by
        # the lifecycle test's start/stop).
        "scheduler_initial_delay_s": 3600.0,
    }
    kwargs.update(overrides)
    return ExecutionService(conn_factory, **kwargs)


async def _run_until(service, predicate, timeout: float = POLL_TIMEOUT_S) -> bool:
    await service.start()
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            await asyncio.sleep(POLL_INTERVAL_S)
        return predicate()
    finally:
        await service.stop()


# ---------------------------------------------------------------------------
# (a) POST /projects creates the DAG
# ---------------------------------------------------------------------------


def test_create_project_enqueues_dag() -> None:
    _require("app.execution.dag", "create_project_dag")
    from app.errors import register_exception_handlers
    from app.routers import api as api_module

    # The routers resolve their DB via app.db defaults — seed FK parents
    # there so _persist_project + DAG FK references succeed.
    db_module.init_db().close()
    conn_factory = lambda: db_module.get_connection()  # noqa: E731
    suffix = uuid.uuid4().hex[:10]
    conn = conn_factory()
    try:
        channel_id, niche_id = _seed_channel_niche(conn, suffix)
        try:
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()

    app = FastAPI(title="YouTube Agent — execution lifecycle tests")
    register_exception_handlers(app)
    app.include_router(api_module.router)
    client = TestClient(app)

    payload = {
        "channel_id": channel_id,
        "niche_profile_id": niche_id,
        "topic_mode": "manual",
        "topic": "DAG lifecycle topic",
        "target_minutes": 10,
        "publish_mode": "auto",
    }
    resp = client.post(
        "/api/v1/projects",
        json=payload,
        headers={"Idempotency-Key": f"exec-dag-{suffix}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    project_id = body["id"]

    # Enriched task_dag replaces the not_started stub.
    assert body["task_dag"]["status"] == "queued"
    assert body["task_dag"]["total_agents"] == 35
    job_ids = body["task_dag"]["job_ids"]
    assert len(job_ids) == 10

    # …and the 10 DAG jobs really are in the jobs table.
    jobs = _jobs_for(conn_factory, project_id)
    assert len(jobs) == 10
    assert {j["kind"] for j in jobs} == {
        "stage.research",
        "stage.script",
        "gate.a",
        "stage.voice",
        "stage.visuals",
        "stage.render",
        "stage.thumbnail",
        "stage.qc",
        "stage.package",
        "gate.b",
    }
    assert all(j["status"] == "queued" for j in jobs)


# ---------------------------------------------------------------------------
# (b) end-to-end execution via ExecutionService
# ---------------------------------------------------------------------------


def test_auto_project_runs_full_chain(tmp_conn_factory, monkeypatch) -> None:
    _require("app.execution.runner", "JobRunner")
    _require("app.execution.handlers", "build_registry")
    _require("app.execution.dag", "create_project_dag")
    from app.execution.dag import create_project_dag

    # Seam isolation (same sys.modules fake-integration pattern as
    # test_stop_all_live.py's _stub_integration fixture): the real
    # app.integration exports run_<stage> functions, so without this
    # the handlers take their real-integration-first path and make
    # live network attempts (edge-TTS, stock downloads) whose latency
    # decides whether the 10s chain budget expires.  A fake module
    # with no run_<stage> functions makes integration_client return
    # None, so handlers fall back to their deterministic stubs and
    # this test exercises the chain semantics, fast and offline.
    fake = types.ModuleType("app.integration")
    monkeypatch.setitem(sys.modules, "app.integration", fake)

    project_id = _seed_project(tmp_conn_factory, publish_mode="auto")
    queue = SQLiteJobQueue(tmp_conn_factory)
    job_ids = create_project_dag(queue, project_id)
    assert len(job_ids) == 10

    # R4-003: the new stage-failure policy prohibits silent stub
    # substitution on the execution path — with the fake integration
    # above returning None for every stage, the chain would now fail
    # at research and park the project 'failed'.  This test pins the
    # stub-driven chain semantics, so its DAG jobs now select stub
    # mode explicitly via their stored payloads (assertions
    # unchanged).
    conn = tmp_conn_factory()
    try:
        for job_id in job_ids:
            row = conn.execute(
                "SELECT payload_json FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            payload = json.loads(row[0] or "{}")
            payload["stub_mode"] = True
            conn.execute(
                "UPDATE jobs SET payload_json = ? WHERE id = ?",
                (json.dumps(payload), job_id),
            )
        try:
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()

    service = _make_service(tmp_conn_factory)

    def _all_done() -> bool:
        jobs = _jobs_for(tmp_conn_factory, project_id)
        return len(jobs) == 10 and all(j["status"] in SUCCESS for j in jobs)

    completed = asyncio.run(_run_until(service, _all_done))
    assert completed, (
        f"chain did not finish in {POLL_TIMEOUT_S}s: "
        f"{_jobs_for(tmp_conn_factory, project_id)}"
    )

    jobs = _jobs_for(tmp_conn_factory, project_id)
    gate_b = next(j for j in jobs if j["kind"] == "gate.b")
    assert gate_b["status"] in SUCCESS

    project = _project_row(tmp_conn_factory, project_id)
    assert project is not None
    assert project["status"] != "draft"  # progressed into production
    assert project["current_phase"] == "packaging"  # final DAG phase

    assert service.status() == {
        "running": False,
        "runner_alive": False,
        "scheduler_alive": False,
    }


def test_review_first_project_parks_at_gate_a(tmp_conn_factory) -> None:
    _require("app.execution.runner", "JobRunner")
    _require("app.execution.handlers", "build_registry")
    _require("app.execution.dag", "create_project_dag")
    from app.execution.dag import create_project_dag

    project_id = _seed_project(tmp_conn_factory, publish_mode="review_first")
    queue = SQLiteJobQueue(tmp_conn_factory)
    create_project_dag(queue, project_id)

    service = _make_service(tmp_conn_factory)

    def _awaiting_review() -> bool:
        row = _project_row(tmp_conn_factory, project_id)
        return row is not None and row["status"] == "awaiting_review"

    parked = asyncio.run(_run_until(service, _awaiting_review))
    assert parked, (
        f"project did not park at Gate A: "
        f"{_project_row(tmp_conn_factory, project_id)} / "
        f"{_jobs_for(tmp_conn_factory, project_id)}"
    )

    # Gate A itself has NOT passed — the chain is parked, not finished.
    jobs = _jobs_for(tmp_conn_factory, project_id)
    gate_a = next(j for j in jobs if j["kind"] == "gate.a")
    assert gate_a["status"] not in SUCCESS


# ---------------------------------------------------------------------------
# (c) pipeline.cancel via the service control handler
# ---------------------------------------------------------------------------


def test_pipeline_cancel_control_handler(tmp_conn_factory) -> None:
    from app.execution.service import ExecutionService

    project_id = _seed_project(tmp_conn_factory, publish_mode="auto")
    queue = SQLiteJobQueue(tmp_conn_factory)

    try:
        dag = importlib.import_module("app.execution.dag")
        stage_ids = dag.create_project_dag(queue, project_id)
    except Exception:
        # DAG builder genuinely absent: stand up equivalent queued
        # stage jobs by hand so the control handler is still exercised.
        stage_ids = [
            queue.enqueue(
                kind,
                {"project_id": project_id, "stage": kind},
                project_id=project_id,
            )
            for kind in ("stage.research", "stage.script", "stage.voice")
        ]
    assert stage_ids

    # Enqueue the control job exactly like the REST pipeline endpoint.
    queue.enqueue(
        "pipeline.cancel",
        {"project_id": project_id, "action": "cancel"},
        queue="default",
        idempotency_key=f"test-cancel-{project_id}",
        project_id=project_id,
    )

    service = ExecutionService(tmp_conn_factory)
    handler = service.registry["pipeline.cancel"]

    async def _dispatch() -> dict:
        # Simulate the runner: claim only the control job, dispatch it
        # through the merged registry, complete it.
        job = queue.claim(
            "test-owner", kinds=("pipeline.cancel",), lease_seconds=60
        )
        assert job is not None and job["kind"] == "pipeline.cancel"
        outcome = handler(job)
        result = await outcome if asyncio.iscoroutine(outcome) else outcome
        assert queue.complete(job["id"], "test-owner", result)
        return result

    result = asyncio.run(_dispatch())
    assert result["ok"] is True
    assert result["cancelled"] == len(stage_ids)

    jobs = _jobs_for(tmp_conn_factory, project_id)
    stage_jobs = [j for j in jobs if not j["kind"].startswith("pipeline.")]
    assert len(stage_jobs) == len(stage_ids)
    assert all(j["status"] == "cancelled" for j in stage_jobs)

    project = _project_row(tmp_conn_factory, project_id)
    assert project is not None and project["status"] == "cancelled"


def test_pipeline_pause_then_resume_requeues(tmp_conn_factory) -> None:
    """Pause cancels queued stage jobs (status untouched); resume
    requeues the cancelled chain so it can run again."""
    _require("app.execution.dag", "create_project_dag")
    from app.execution.dag import create_project_dag
    from app.execution.service import ExecutionService

    project_id = _seed_project(tmp_conn_factory, publish_mode="auto")
    queue = SQLiteJobQueue(tmp_conn_factory)
    create_project_dag(queue, project_id)

    service = ExecutionService(tmp_conn_factory)

    async def _call(kind: str) -> dict:
        handler = service.registry[kind]
        job = {"kind": kind, "payload": {"project_id": project_id},
               "project_id": project_id}
        outcome = handler(job)
        return await outcome if asyncio.iscoroutine(outcome) else outcome

    paused = asyncio.run(_call("pipeline.pause"))
    assert paused["cancelled"] == 10
    assert all(
        j["status"] == "cancelled"
        for j in _jobs_for(tmp_conn_factory, project_id)
    )
    # Pause deliberately leaves the project status alone.
    assert _project_row(tmp_conn_factory, project_id)["status"] == "draft"

    resumed = asyncio.run(_call("pipeline.resume"))
    assert resumed["dag"] == "resumed"
    assert all(
        j["status"] == "queued"
        for j in _jobs_for(tmp_conn_factory, project_id)
    )


# ---------------------------------------------------------------------------
# (d) service lifecycle + attach degradation
# ---------------------------------------------------------------------------


def test_service_start_stop_status(tmp_conn_factory) -> None:
    from app.execution.service import ExecutionService

    service = ExecutionService(
        tmp_conn_factory,
        scheduler_initial_delay_s=0.05,
        scheduler_interval_s=0.1,
    )
    assert service.status() == {
        "running": False,
        "runner_alive": False,
        "scheduler_alive": False,
    }
    # Control handlers are always present, even if a sibling is missing.
    for kind in (
        "pipeline.start",
        "pipeline.pause",
        "pipeline.resume",
        "pipeline.cancel",
    ):
        assert kind in service.registry

    async def _cycle() -> dict:
        await service.start()
        await asyncio.sleep(0.2)  # let the scheduler loop tick once
        mid = service.status()
        await service.stop()
        return mid

    mid = asyncio.run(_cycle())
    assert mid["running"] is True
    assert mid["scheduler_alive"] is True
    assert service.status() == {
        "running": False,
        "runner_alive": False,
        "scheduler_alive": False,
    }


def test_create_app_attaches_execution_service(app, monkeypatch) -> None:
    """create_app wires app.state.execution (dormant by default)."""
    monkeypatch.delenv("YTA_EXECUTION", raising=False)
    monkeypatch.delenv("YTA_EXECUTION_AUTOSTART", raising=False)
    from app.execution.service import ExecutionService

    service = getattr(app.state, "execution", None)
    assert isinstance(service, ExecutionService)
    # Autostart is off in create_app: attached but not running.
    assert service.status()["running"] is False
