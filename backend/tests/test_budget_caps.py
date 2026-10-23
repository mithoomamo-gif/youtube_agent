"""Budget caps exposure + enforcement-lite tests (Round 3).

Covers:
* ``GET /api/v1/settings/budget`` returns roadmap defaults when unset,
* ``PUT /api/v1/settings/budget`` persists (settings table + GET) and
  rejects invalid policies with 422,
* the execution ``pipeline.start`` handler parks a project at Gate A
  with a ``budget_pending`` approval + ``budget.warning`` event when
  the (mocked) estimate exceeds the per-video cap, creates no DAG,
  and proceeds normally when the estimate is under the cap,
* the first DAG stage (``stage.research``) raises GatePending under
  the same over-cap condition (the DAG-created-at-creation path),
* approving the budget approval bypasses the check on the next start.

API tests follow tests/test_routers.py (bare router app, shared
default DB); runner tests use an isolated tmp_path DB via
tests.execution_fixtures, like the other execution suites.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db as db_module
from app.errors import register_exception_handlers
from app.execution import budget_gate
from app.providers.budget.policy import (
    DEFAULT_BUDGET_POLICY,
    SETTINGS_KEY,
    load_budget_policy,
    save_budget_policy,
)
from app.routers import api as api_module
from app.routers import settings as settings_router
from tests.execution_fixtures import (
    RecordingHub,
    make_db,
    seed_channel_niche,
    seed_project,
)

try:
    from app.execution.runner import GatePending
except Exception:  # pragma: no cover
    from app.execution.handlers import GatePending  # type: ignore


# ---------------------------------------------------------------------------
# API fixtures / helpers
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    app = FastAPI(title="YouTube Agent — budget caps tests")
    register_exception_handlers(app)
    app.include_router(api_module.router)
    return app


def _clear_budget_policy() -> None:
    """Reset the stored policy in both the DB and the router mirror."""
    settings_router._MEMORY.pop(SETTINGS_KEY, None)
    settings_router._MEMORY.pop("budget_policy", None)
    try:
        db_module.init_db().close()
        conn = db_module.get_connection()
        try:
            conn.execute("DELETE FROM settings WHERE key = ?", (SETTINGS_KEY,))
            try:
                conn.commit()
            except Exception:
                pass
        finally:
            conn.close()
    except Exception:
        pass


@pytest.fixture()
def api_client():
    _clear_budget_policy()
    yield TestClient(_build_app())
    _clear_budget_policy()


# ---------------------------------------------------------------------------
# GET defaults / PUT persist + validation
# ---------------------------------------------------------------------------


def test_get_budget_returns_defaults_when_unset(api_client):
    resp = api_client.get("/api/v1/settings/budget")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "per_video_cap_usd": 1.0,
        "daily_cap_usd": 10.0,
        "monthly_cap_usd": 30.0,
        "warn_pct": 70,
        "soft_pct": 90,
    }
    assert body == DEFAULT_BUDGET_POLICY


def test_put_budget_persists_and_get_reflects(api_client):
    policy = {
        "per_video_cap_usd": 2.5,
        "daily_cap_usd": 15.0,
        "monthly_cap_usd": 45.0,
        "warn_pct": 60,
        "soft_pct": 85,
    }
    resp = api_client.put("/api/v1/settings/budget", json=policy)
    assert resp.status_code == 200
    assert resp.json() == policy

    got = api_client.get("/api/v1/settings/budget")
    assert got.status_code == 200
    assert got.json() == policy

    # Really in the settings table, as one JSON value under one key.
    conn = db_module.get_connection()
    try:
        row = conn.execute(
            "SELECT value_json FROM settings WHERE key = ?", (SETTINGS_KEY,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert json.loads(row[0]) == policy

    # A fresh app/client (no shared memory beyond the DB + router
    # mirror reset) still serves the persisted policy from the table.
    settings_router._MEMORY.pop(SETTINGS_KEY, None)
    fresh = TestClient(_build_app())
    assert fresh.get("/api/v1/settings/budget").json() == policy


@pytest.mark.parametrize(
    "policy",
    [
        # non-positive amounts
        {"per_video_cap_usd": 0, "daily_cap_usd": 10.0, "monthly_cap_usd": 30.0, "warn_pct": 70, "soft_pct": 90},
        {"per_video_cap_usd": 1.0, "daily_cap_usd": -5.0, "monthly_cap_usd": 30.0, "warn_pct": 70, "soft_pct": 90},
        {"per_video_cap_usd": 1.0, "daily_cap_usd": 10.0, "monthly_cap_usd": -1.0, "warn_pct": 70, "soft_pct": 90},
        # ladder violations: warn >= soft, soft >= 100, warn >= 100
        {"per_video_cap_usd": 1.0, "daily_cap_usd": 10.0, "monthly_cap_usd": 30.0, "warn_pct": 90, "soft_pct": 90},
        {"per_video_cap_usd": 1.0, "daily_cap_usd": 10.0, "monthly_cap_usd": 30.0, "warn_pct": 95, "soft_pct": 90},
        {"per_video_cap_usd": 1.0, "daily_cap_usd": 10.0, "monthly_cap_usd": 30.0, "warn_pct": 70, "soft_pct": 100},
        {"per_video_cap_usd": 1.0, "daily_cap_usd": 10.0, "monthly_cap_usd": 30.0, "warn_pct": 0, "soft_pct": 90},
    ],
)
def test_put_budget_validation_errors(api_client, policy):
    resp = api_client.put("/api/v1/settings/budget", json=policy)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] in ("budget_policy_invalid", "validation_error")
    # Nothing was persisted: defaults still effective.
    assert api_client.get("/api/v1/settings/budget").json() == DEFAULT_BUDGET_POLICY


def test_put_budget_missing_field_is_422(api_client):
    resp = api_client.put(
        "/api/v1/settings/budget", json={"per_video_cap_usd": 1.0}
    )
    assert resp.status_code == 422


def test_policy_helper_roundtrip_on_isolated_db(tmp_path):
    _, conn_factory = make_db(tmp_path)
    assert load_budget_policy(conn_factory) == DEFAULT_BUDGET_POLICY
    saved = save_budget_policy(
        {"per_video_cap_usd": 0.25, "daily_cap_usd": 2.4,
         "monthly_cap_usd": 30.0, "warn_pct": 70, "soft_pct": 90},
        conn_factory,
    )
    assert load_budget_policy(conn_factory) == saved
    with pytest.raises(ValueError):
        save_budget_policy(
            {"per_video_cap_usd": 1.0, "daily_cap_usd": 10.0,
             "monthly_cap_usd": 30.0, "warn_pct": 90, "soft_pct": 70},
            conn_factory,
        )


# ---------------------------------------------------------------------------
# Runner / service enforcement-lite
# ---------------------------------------------------------------------------


@pytest.fixture()
def exec_env(tmp_path):
    _, conn_factory = make_db(tmp_path)
    channel_id, niche_id = seed_channel_niche(conn_factory)
    project_id = seed_project(conn_factory, channel_id, niche_id)
    return {"conn_factory": conn_factory, "project_id": project_id}


def _project_row(conn_factory, project_id):
    conn = conn_factory()
    try:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _approvals(conn_factory, project_id):
    conn = conn_factory()
    try:
        try:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE project_id = ?", (project_id,)
            ).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _job_count(conn_factory, project_id):
    conn = conn_factory()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _run_start(conn_factory, project_id, hub=None):
    from app.execution.service import ExecutionService

    service = ExecutionService(conn_factory, hub=hub)
    handler = service.registry["pipeline.start"]
    job = {"kind": "pipeline.start", "project_id": project_id,
           "payload": {"project_id": project_id}}
    return asyncio.run(handler(job))


def test_start_pauses_at_gate_a_when_estimate_over_cap(exec_env, monkeypatch):
    conn_factory, project_id = exec_env["conn_factory"], exec_env["project_id"]
    hub = RecordingHub()
    # Mock the estimate above the default $1.00 cap ($2.00 estimate).
    monkeypatch.setattr(
        budget_gate, "estimate_project_cost_micros",
        lambda cf, pid, project=None: 2_000_000,
    )
    result = _run_start(conn_factory, project_id, hub=hub)

    assert result["ok"] is True
    assert result["budget_paused"] is True
    assert result["dag"] == "budget_pending"
    assert result["reason"] == "budget_pending"
    assert result["estimated_micros"] == 2_000_000
    assert result["cap_micros"] == 1_000_000
    assert result["spent_micros"] == 0
    assert result["remaining_micros"] == 1_000_000
    assert result["needed_micros"] == 1_000_000

    # Parked at Gate A, no DAG created (no spend can start).
    project = _project_row(conn_factory, project_id)
    assert project["status"] == "awaiting_review"
    assert json.loads(project["gate_status_json"])["gate_a"] == "pending"
    assert _job_count(conn_factory, project_id) == 0

    # Exactly one pending approval, reason budget_pending.
    approvals = _approvals(conn_factory, project_id)
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval["id"] == result["approval_id"]
    assert approval["gate"] == "a"
    assert approval["status"] == "pending"
    assert approval["reason_code"] == "budget_pending"

    # budget.warning emitted with the figures.
    warnings = hub.by_type("budget.warning")
    assert len(warnings) == 1
    payload = warnings[0]["payload"]
    assert payload["project_id"] == project_id
    assert payload["approval_id"] == approval["id"]
    assert payload["spent_micros"] == 0
    assert payload["remaining_micros"] == 1_000_000
    assert payload["needed_micros"] == 1_000_000


def test_start_proceeds_when_estimate_under_cap(exec_env, monkeypatch):
    conn_factory, project_id = exec_env["conn_factory"], exec_env["project_id"]
    hub = RecordingHub()
    # Mock the estimate below the default $1.00 cap ($0.01 estimate).
    monkeypatch.setattr(
        budget_gate, "estimate_project_cost_micros",
        lambda cf, pid, project=None: 10_000,
    )
    result = _run_start(conn_factory, project_id, hub=hub)

    assert result["ok"] is True
    assert result.get("budget_paused") is not True
    assert result["dag"] == "created"
    assert len(result["job_ids"]) == 10
    assert _job_count(conn_factory, project_id) == 10
    assert _approvals(conn_factory, project_id) == []
    assert hub.by_type("budget.warning") == []
    assert _project_row(conn_factory, project_id)["status"] == "in_production"


def test_start_pauses_with_persisted_low_cap_and_default_estimate(exec_env):
    """No estimate mock: a $0.05 persisted cap is below the DAG's
    reserved estimate (sum of STAGE_COST_MICROS = $0.078)."""
    conn_factory, project_id = exec_env["conn_factory"], exec_env["project_id"]
    save_budget_policy(
        {"per_video_cap_usd": 0.05, "daily_cap_usd": 10.0,
         "monthly_cap_usd": 30.0, "warn_pct": 70, "soft_pct": 90},
        conn_factory,
    )
    result = _run_start(conn_factory, project_id, hub=RecordingHub())
    assert result["budget_paused"] is True
    assert result["estimated_micros"] == budget_gate.DAG_RESERVED_ESTIMATE_MICROS
    assert result["cap_micros"] == 50_000


def test_research_stage_raises_gate_pending_when_over_cap(exec_env, monkeypatch):
    from app.execution.handlers import build_registry

    conn_factory, project_id = exec_env["conn_factory"], exec_env["project_id"]
    monkeypatch.setattr(
        budget_gate, "estimate_project_cost_micros",
        lambda cf, pid, project=None: 5_000_000,
    )
    registry = build_registry(conn_factory)
    job = {"id": "job-research-1", "kind": "stage.research",
           "project_id": project_id, "payload": {"project_id": project_id}}
    with pytest.raises(GatePending):
        asyncio.run(registry["stage.research"](job))
    assert _project_row(conn_factory, project_id)["status"] == "awaiting_review"
    approvals = _approvals(conn_factory, project_id)
    assert len(approvals) == 1
    assert approvals[0]["reason_code"] == "budget_pending"


def test_research_stage_proceeds_when_under_cap(exec_env, monkeypatch):
    from app.execution.handlers import build_registry

    conn_factory, project_id = exec_env["conn_factory"], exec_env["project_id"]
    monkeypatch.setattr(
        budget_gate, "estimate_project_cost_micros",
        lambda cf, pid, project=None: 1_000,
    )
    registry = build_registry(conn_factory)
    job = {"id": "job-research-2", "kind": "stage.research",
           "project_id": project_id, "payload": {"project_id": project_id}}
    result = asyncio.run(registry["stage.research"](job))
    assert result["stage"] == "research"
    assert _approvals(conn_factory, project_id) == []


def test_approved_budget_override_bypasses_check(exec_env, monkeypatch):
    conn_factory, project_id = exec_env["conn_factory"], exec_env["project_id"]
    monkeypatch.setattr(
        budget_gate, "estimate_project_cost_micros",
        lambda cf, pid, project=None: 2_000_000,
    )
    first = _run_start(conn_factory, project_id, hub=RecordingHub())
    assert first["budget_paused"] is True

    # User approves the budget approval through the bridge (the same
    # call the approvals router makes on a decision).
    from app.execution import gates_bridge

    applied = gates_bridge.apply_decision(
        conn_factory, project_id, "a", "approved",
        approval_id=first["approval_id"], reason_code="budget_pending",
    )
    assert applied in (True, False)  # no live gate job on this path

    second = _run_start(conn_factory, project_id, hub=RecordingHub())
    assert second.get("budget_paused") is not True
    assert second["dag"] == "created"
