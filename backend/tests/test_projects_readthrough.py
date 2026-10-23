"""Regression tests for R2-002 — project detail must read through to DB.

Bug: ``GET /api/v1/projects/{id}`` served the projects router's
in-memory creation-time dict first, so it reported ``status='draft'``
and ``current_phase=None`` forever, even after the execution engine
advanced the DB row (e.g. parked it at ``awaiting_review`` at Gate A).
SQLite is the single source of truth; the detail endpoint must re-read
the row on every request.

Flow under test (mirrors production, uses the real engine paths):

1. Seed channel + niche rows directly in the DB (the projects table
   has FKs to both; an unseeded pair makes the router's best-effort
   INSERT silently drop the row — see the Round-2 acceptance test for
   the same seeding rationale).
2. Create the project through ``POST /api/v1/projects`` (which also
   enqueues the real 10-job DAG into the shared DB).
3. Advance it with the execution engine's own handlers
   (``app.execution.handlers.build_registry``): run ``stage.research``
   (DB -> ``in_production`` / phase ``research``), then ``gate.a``
   (review_first, no decision -> ``GatePending``; the gates bridge
   parks the DB row at ``awaiting_review``).
4. ``GET /api/v1/projects/{id}`` after each advance must show the NEW
   DB state — not the creation-time ``draft`` snapshot — while keeping
   the response shape the UI depends on (incl. the response-only
   ``task_dag`` / ``topic_mode`` fields).

App construction + DB access follow tests/test_execution_approvals.py:
the shared session test DB resolved through ``app.db`` (the same
resolver the routers use).
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db as db_module
from app.errors import register_exception_handlers
from app.execution.handlers import build_registry
from app.queue import SQLiteJobQueue
from app.routers import api as api_module

try:  # The runner owns the canonical GatePending (see handlers tests).
    from app.execution.runner import GatePending
except Exception:  # pragma: no cover - fallback if runner unavailable
    from app.execution.handlers import GatePending

NOW_MS = 1_700_000_000_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _build_app() -> FastAPI:
    app = FastAPI(title="YouTube Agent — projects read-through tests")
    register_exception_handlers(app)
    app.include_router(api_module.router)
    return app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(_build_app())


def _conn_factory():
    """Fresh connection to the same default DB the routers read."""
    return db_module.get_connection()


def _init_db() -> None:
    conn = db_module.init_db()
    conn.close()


def _seed_channel_niche() -> tuple[str, str]:
    """Insert the FK parents of a project; return (channel_id, niche_id)."""
    suffix = uuid.uuid4().hex[:12]
    niche_id = f"niche-{suffix}"
    channel_id = f"chan-{suffix}"
    conn = _conn_factory()
    try:
        conn.execute(
            "INSERT INTO niche_profiles (id, key, label, source, "
            "created_at, updated_at) VALUES (?, ?, 'Readthrough Niche', "
            "'builtin', ?, ?)",
            (niche_id, f"readthrough-{suffix}", NOW_MS, NOW_MS),
        )
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title, "
            "language, created_at, updated_at) VALUES (?, ?, "
            "'Readthrough Channel', 'en', ?, ?)",
            (channel_id, f"UC{suffix}", NOW_MS, NOW_MS),
        )
        try:
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()
    return channel_id, niche_id


def _db_project_row(project_id: str) -> dict | None:
    conn = _conn_factory()
    try:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _create_project(client: TestClient) -> dict:
    channel_id, niche_id = _seed_channel_niche()
    resp = client.post(
        "/api/v1/projects",
        json={
            "channel_id": channel_id,
            "niche_profile_id": niche_id,
            "topic_mode": "manual",
            "topic": "Read-through regression topic",
            "target_minutes": 10,
            "publish_mode": "review_first",
        },
        headers={"Idempotency-Key": f"readthrough-{uuid.uuid4().hex}"},
    )
    assert resp.status_code == 201
    return resp.json()


# ---------------------------------------------------------------------------
# R2-002 regression
# ---------------------------------------------------------------------------


def test_project_detail_reads_through_to_db_after_engine_advances(
    client: TestClient,
) -> None:
    _init_db()
    created = _create_project(client)
    project_id = created["id"]
    assert created["status"] == "draft"

    # The row really landed in the DB (seeded FK parents) as a draft,
    # and the detail endpoint agrees before anything advances.
    row = _db_project_row(project_id)
    assert row is not None and row["status"] == "draft"
    before = client.get(f"/api/v1/projects/{project_id}")
    assert before.status_code == 200
    assert before.json()["status"] == "draft"
    assert before.json()["current_phase"] is None

    job_ids = created["task_dag"]["job_ids"]
    assert len(job_ids) == 10
    queue = SQLiteJobQueue(_conn_factory)
    registry = build_registry(_conn_factory)

    # Advance #1 — a real stage handler: DB -> in_production/research.
    research_job = queue.get(job_ids[0])
    assert research_job is not None and research_job["kind"] == "stage.research"
    asyncio.run(registry["stage.research"](research_job))
    row = _db_project_row(project_id)
    assert row is not None and row["status"] == "in_production"
    assert row["current_phase"] == "research"

    mid = client.get(f"/api/v1/projects/{project_id}")
    assert mid.status_code == 200
    assert mid.json()["status"] == "in_production"
    assert mid.json()["current_phase"] == "research"

    # Advance #2 — Gate A with no decision parks the project for review
    # (GatePending is the engine's "parked, not failed" signal; the
    # gates bridge moves the DB row to awaiting_review).
    gate_job = queue.get(job_ids[2])
    assert gate_job is not None and gate_job["kind"] == "gate.a"
    with pytest.raises(GatePending):
        asyncio.run(registry["gate.a"](gate_job))
    row = _db_project_row(project_id)
    assert row is not None and row["status"] == "awaiting_review"

    # THE regression assertion: the detail endpoint must now serve the
    # DB state, not the stale creation-time 'draft' snapshot.
    detail = client.get(f"/api/v1/projects/{project_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "awaiting_review"
    assert body["current_phase"] == "research"  # not the stale None

    # Response shape/fields the React UI depends on are unchanged:
    # DB columns plus the response-only fields from creation.
    assert body["id"] == project_id
    assert body["channel_id"] == created["channel_id"]
    assert body["niche_profile_id"] == created["niche_profile_id"]
    assert body["topic"] == "Read-through regression topic"
    assert body["publish_mode"] == "review_first"
    assert body["topic_mode"] == "manual"
    assert body["task_dag"]["total_agents"] == 35
    assert body["task_dag"]["job_ids"] == job_ids


def test_project_detail_serves_db_only_project(client: TestClient) -> None:
    """A project with no in-memory copy is served from its DB row."""
    _init_db()
    channel_id, niche_id = _seed_channel_niche()
    project_id = f"proj-{uuid.uuid4().hex[:12]}"
    conn = _conn_factory()
    try:
        conn.execute(
            "INSERT INTO projects (id, channel_id, niche_profile_id, "
            "topic, topic_source, status, current_phase, publish_mode, "
            "created_at, updated_at) VALUES (?, ?, ?, 'DB-only topic', "
            "'user', 'in_production', 'scripting', 'review_first', ?, ?)",
            (project_id, channel_id, niche_id, NOW_MS, NOW_MS),
        )
        try:
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()

    resp = client.get(f"/api/v1/projects/{project_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "in_production"
    assert body["current_phase"] == "scripting"


def test_project_detail_unknown_id_is_404(client: TestClient) -> None:
    _init_db()
    resp = client.get(f"/api/v1/projects/no-such-{uuid.uuid4().hex}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "project_not_found"
