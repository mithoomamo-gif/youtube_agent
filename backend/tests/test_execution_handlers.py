"""Tests for app.execution: integration_client, handlers, dag (Round 2 W2).

Builds a real SQLite DB in ``tmp_path`` via ``app.db.init_db`` and seeds
channel / niche_profile / project rows directly against the section-15
schema (see ``app/schema_ddl.py``):

* channels:       id, youtube_channel_id (UNIQUE), title, language,
                  created_at, updated_at are the NOT NULL columns.
* niche_profiles: id, key (UNIQUE), label, source, created_at, updated_at.
* projects:       id, channel_id, niche_profile_id, topic, topic_source,
                  publish_mode, created_at, updated_at.

Handlers are ``async``; tests drive them with ``asyncio.run`` so they do
not depend on pytest-asyncio configuration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import types
import uuid

import pytest

from app import db as db_module
from app.execution import integration_client
from app.execution.dag import STAGE_KINDS, create_project_dag
from app.execution.handlers import GateRejected, StageFailedError, build_registry
from app.queue import SQLiteJobQueue

try:  # Worker 1's runner owns the canonical GatePending.
    from app.execution.runner import GatePending
except Exception:  # runner not landed yet -> handlers' fallback class
    from app.execution.handlers import GatePending


def _now_ms() -> int:
    return int(time.time() * 1000)


def _seed_project(conn, project_id: str, *, publish_mode: str = "review_first"):
    now = _now_ms()
    suffix = uuid.uuid4().hex[:8]
    niche_id = f"niche-{suffix}"
    channel_id = f"chan-{suffix}"
    conn.execute(
        "INSERT INTO niche_profiles (id, key, label, source, created_at,"
        " updated_at) VALUES (?, ?, ?, 'builtin', ?, ?)",
        (niche_id, f"facts-{suffix}", "Facts", now, now),
    )
    conn.execute(
        "INSERT INTO channels (id, youtube_channel_id, title, language,"
        " created_at, updated_at) VALUES (?, ?, ?, 'en', ?, ?)",
        (channel_id, f"UC-{suffix}", "Test Channel", now, now),
    )
    conn.execute(
        "INSERT INTO projects (id, channel_id, niche_profile_id, topic,"
        " topic_source, status, publish_mode, total_cost_micros,"
        " created_at, updated_at)"
        " VALUES (?, ?, ?, 'Test topic', 'user', 'draft', ?, 0, ?, ?)",
        (project_id, channel_id, niche_id, publish_mode, now, now),
    )
    return project_id


@pytest.fixture()
def env(tmp_path):
    db_path = tmp_path / "handlers_test.db"
    init_conn = db_module.init_db(db_path)
    init_conn.close()

    def conn_factory():
        return db_module.connect(db_path)

    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    conn = conn_factory()
    try:
        _seed_project(conn, project_id)
    finally:
        conn.close()
    queue = SQLiteJobQueue(conn_factory)
    return {
        "db_path": db_path,
        "conn_factory": conn_factory,
        "queue": queue,
        "project_id": project_id,
    }


def _project_row(env, project_id=None):
    conn = env["conn_factory"]()
    try:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?",
            (project_id or env["project_id"],),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _run(handler, job):
    return asyncio.run(handler(job))


# ---------------------------------------------------------------------------
# (a) DAG creation
# ---------------------------------------------------------------------------


def test_dag_creates_chained_jobs_in_order_and_is_idempotent(env):
    queue, project_id = env["queue"], env["project_id"]

    ids = create_project_dag(queue, project_id)
    assert len(ids) == 10
    assert len(set(ids)) == 10

    jobs = [queue.get(job_id) for job_id in ids]
    assert [j["kind"] for j in jobs] == STAGE_KINDS
    assert jobs[0]["depends_on"] is None
    for previous, current in zip(jobs, jobs[1:]):
        assert current["depends_on"] == previous["id"]
    for job in jobs:
        assert job["project_id"] == project_id
        assert job["payload"]["project_id"] == project_id
        assert job["payload"]["stage"]

    # Idempotent: a second call returns the same ids, no new rows.
    assert create_project_dag(queue, project_id) == ids
    conn = env["conn_factory"]()
    try:
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        conn.close()
    assert count == 10


# ---------------------------------------------------------------------------
# (b) Stub stage handler
# ---------------------------------------------------------------------------


def test_stub_research_handler_writes_artefact_run_and_phase(env, monkeypatch):
    # Post-Round-4: the real ``app.integration`` DOES export
    # ``run_research`` (the stage bridge), so the handler stub path
    # must be exercised against a FAKE integration module that has
    # no stage function — ``call_stage`` then returns None and the
    # handler falls back to its deterministic stub, exactly the
    # pre-bridge world this test was written for.
    fake = types.ModuleType("app.integration")
    monkeypatch.setitem(sys.modules, "app.integration", fake)

    ids = create_project_dag(env["queue"], env["project_id"])
    registry = build_registry(env["conn_factory"])
    assert set(registry) == set(STAGE_KINDS)

    job = env["queue"].get(ids[0])
    # R4-003: the new stage-failure policy prohibits silent stub
    # substitution on the execution path — a no-result stage now
    # raises StageFailedError unless stub mode is explicitly
    # selected.  This test pins the STUB path, so it now selects
    # stub mode explicitly via the payload (assertions unchanged).
    job["payload"] = {**(job.get("payload") or {}), "stub_mode": True}
    result = _run(registry["stage.research"], job)

    assert result["stub"] is True
    assert result["stage"] == "research"
    assert result["cost_micros"] > 0
    assert result["artefact"]["artefact_type"] == "research_brief"

    project = _project_row(env)
    assert project["current_phase"] == "research"
    assert project["status"] == "in_production"
    assert project["total_cost_micros"] == result["cost_micros"]

    conn = env["conn_factory"]()
    try:
        artefact = conn.execute(
            "SELECT * FROM project_artefacts WHERE project_id = ?",
            (env["project_id"],),
        ).fetchone()
        assert artefact is not None
        assert artefact["artefact_type"] == "research_brief"
        assert artefact["content_hash"]

        run = conn.execute(
            "SELECT * FROM agent_runs WHERE project_id = ?",
            (env["project_id"],),
        ).fetchone()
        assert run is not None
        assert run["status"] == "succeeded"
        assert run["job_id"] == ids[0]
        assert run["cost_micros"] == result["cost_micros"]
    finally:
        conn.close()


def test_stage_handler_missing_project_raises_value_error(env):
    registry = build_registry(env["conn_factory"])
    job = {"id": "no-such-job", "kind": "stage.research",
           "project_id": "no-such-project", "payload": {}}
    with pytest.raises(ValueError):
        _run(registry["stage.research"], job)


def test_cost_ticker_event_emitted(env):
    class FakeEvents:
        def __init__(self):
            self.calls = []

        def publish(self, event_type, payload, project_id=None, job_id=None):
            self.calls.append((event_type, payload, project_id, job_id))

    events = FakeEvents()
    ids = create_project_dag(env["queue"], env["project_id"])
    registry = build_registry(env["conn_factory"], events=events)
    job = env["queue"].get(ids[0])
    result = _run(registry["stage.research"], job)

    tickers = [c for c in events.calls if c[0] == "cost.ticker"]
    assert len(tickers) == 1
    _, payload, project_id, job_id = tickers[0]
    assert project_id == env["project_id"]
    assert job_id == ids[0]
    assert payload["cost_micros"] == result["cost_micros"]
    assert payload["total_cost_micros"] == result["total_cost_micros"]


# ---------------------------------------------------------------------------
# (c)-(e) Gates
# ---------------------------------------------------------------------------


def _gate_job(env, kind="gate.a", payload_extra=None, project_id=None):
    payload = {"project_id": project_id or env["project_id"]}
    if payload_extra:
        payload.update(payload_extra)
    return {
        "id": f"job-{uuid.uuid4().hex[:8]}",
        "kind": kind,
        "project_id": project_id or env["project_id"],
        "payload": payload,
    }


def test_gate_a_review_first_without_approval_raises_pending(env):
    registry = build_registry(env["conn_factory"])
    with pytest.raises(GatePending):
        _run(registry["gate.a"], _gate_job(env))
    project = _project_row(env)
    assert project["status"] == "awaiting_review"
    gate_status = json.loads(project["gate_status_json"])
    assert gate_status["gate_a"] == "pending"


def test_gate_a_approved_payload_completes(env):
    registry = build_registry(env["conn_factory"])
    job = _gate_job(
        env, payload_extra={"approval": {"decision": "approved", "id": "appr-1"}}
    )
    result = _run(registry["gate.a"], job)
    assert result["decision"] == "approved"
    assert result["approval_id"] == "appr-1"
    assert result["gate"] == "a"


def test_gate_a_rejected_payload_raises_gate_rejected(env):
    registry = build_registry(env["conn_factory"])
    job = _gate_job(
        env, payload_extra={"approval": {"decision": "rejected", "id": "appr-2"}}
    )
    with pytest.raises(GateRejected) as excinfo:
        _run(registry["gate.a"], job)
    assert excinfo.value.error_code == "gate_rejected"


def test_gate_auto_mode_auto_approves(env):
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    conn = env["conn_factory"]()
    try:
        _seed_project(conn, project_id, publish_mode="auto")
    finally:
        conn.close()

    registry = build_registry(env["conn_factory"])
    result = _run(registry["gate.b"], _gate_job(env, kind="gate.b",
                                                 project_id=project_id))
    assert result["decision"] == "auto_approved"
    assert result["gate"] == "b"


# ---------------------------------------------------------------------------
# (f) Integration path
# ---------------------------------------------------------------------------


def test_integration_module_is_used_when_stage_exists(env, monkeypatch):
    fake = types.ModuleType("app.integration")

    def run_research(project, payload):
        return {
            "stage": "research",
            "marker": "real-integration",
            "cost_micros": 4242,
            "artefact": {"summary": "real research brief"},
        }

    fake.run_research = run_research
    monkeypatch.setitem(sys.modules, "app.integration", fake)

    assert integration_client.get_integration() is fake

    ids = create_project_dag(env["queue"], env["project_id"])
    registry = build_registry(env["conn_factory"])
    job = env["queue"].get(ids[0])
    result = _run(registry["stage.research"], job)

    assert result.get("marker") == "real-integration"
    assert "stub" not in result  # real results never get the stub flag
    assert result["cost_micros"] == 4242
    # Book-keeping still happens on the real path.
    assert _project_row(env)["current_phase"] == "research"
    conn = env["conn_factory"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM project_artefacts WHERE project_id = ?",
            (env["project_id"],),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_call_stage_returns_none_without_stage_function(env, monkeypatch):
    # Post-Round-4: the real app.integration DOES export run_research
    # (the stage bridge), so "no stage function" must be tested with
    # a module that genuinely lacks it.
    fake = types.ModuleType("app.integration")
    monkeypatch.setitem(sys.modules, "app.integration", fake)
    assert integration_client.call_stage("research", {}, {}) is None
    assert integration_client.call_stage("no_such_stage", {}, {}) is None

    # A missing/unimportable integration package must also degrade
    # to None (None in sys.modules makes the import fail).
    monkeypatch.setitem(sys.modules, "app.integration", None)
    assert integration_client.call_stage("research", {}, {}) is None


# ---------------------------------------------------------------------------
# (g) D2/D1 regressions (Round 4 fix): the script stage in the async
# DAG-handler context must run the REAL content builder via the
# stage bridge — never die on build_content_auto's event-loop guard —
# a live failure must be logged (not silently swallowed) before the
# handler surfaces it (R4-003: StageFailedError — silent stub
# fallback is prohibited on the execution path), and a $0 bundle
# must not suppress the handler's per-stage cost table (D1).
# ---------------------------------------------------------------------------


def _script_job(env, tmp_path):
    ids = create_project_dag(env["queue"], env["project_id"])
    job = env["queue"].get(ids[1])  # stage.script
    assert job["kind"] == "stage.script"
    job["payload"]["base_dir"] = str(tmp_path / "work")
    return job


def _patch_live(monkeypatch, fake_live):
    """Point the stage bridge at ``fake_live`` (an async stand-in for
    content.build_content_live) with no build_content_auto override,
    so the bridge's own run-loop path is what gets exercised."""
    from app.integration import content as content_mod
    from app.integration import stage_bridge

    monkeypatch.setattr(content_mod, "build_content_live", fake_live)
    monkeypatch.setattr(stage_bridge, "build_content_auto", None)
    return content_mod


def test_script_stage_async_context_runs_live_builder(env, monkeypatch, tmp_path):
    calls = {"n": 0}

    async def fake_live(spec, profile, **kwargs):
        calls["n"] += 1
        from app.integration import content as content_mod

        bundle = content_mod.build_content(spec, profile)
        bundle.llm_path = "gemini-live"
        bundle.llm_models = ["fake-model"]
        bundle.llm_cost_usd = 0.011
        return bundle

    _patch_live(monkeypatch, fake_live)

    registry = build_registry(env["conn_factory"])
    result = _run(registry["stage.script"], _script_job(env, tmp_path))

    # (a) The live builder really ran inside the async handler, and
    # its real result — not the handler stub — was recorded.
    assert calls["n"] == 1
    assert "stub" not in result
    assert result["provenance"]["llm_path"] == "gemini-live"
    assert result["artefact"]["script_text"]
    # A non-zero recorded cost stays authoritative (D1, positive arm).
    assert result["cost_micros"] == 11_000
    assert _project_row(env)["current_phase"] == "scripting"


def test_script_stage_live_failure_is_logged_not_masked(
    env, monkeypatch, tmp_path, caplog
):
    async def failing_live(spec, profile, **kwargs):
        raise RuntimeError("fake gemini 429 quota exhausted")

    _patch_live(monkeypatch, failing_live)

    # Suite-order hygiene: test_execution_approvals' migration test
    # runs Alembic, whose fileConfig disables pre-existing loggers
    # (logging.getLogger(...).disabled = True) for the rest of the
    # process — re-enable ours or logger.exception is a no-op here.
    monkeypatch.setattr(
        logging.getLogger("app.execution.handlers"), "disabled", False
    )

    registry = build_registry(env["conn_factory"])
    # R4-003: the new stage-failure policy prohibits silent stub
    # substitution on the execution path — a failed real stage with
    # no explicit stub-mode selection now surfaces as
    # StageFailedError (so the runner retries once, then parks the
    # project failed) instead of falling back to a stub.  This test
    # now pins the new policy; the D2 logging assertions below are
    # unchanged.
    with caplog.at_level(logging.ERROR, logger="app.execution.handlers"):
        with pytest.raises(StageFailedError):
            _run(registry["stage.script"], _script_job(env, tmp_path))

    # (b) The failure is logged with project + stage and the
    # ORIGINAL exception — not masked by build_content_auto's
    # event-loop RuntimeError (the D2 defect's verbatim error).
    assert env["project_id"] in caplog.text
    assert "script" in caplog.text
    assert "fake gemini 429 quota exhausted" in caplog.text
    assert "cannot run inside an active event loop" not in caplog.text


def test_script_stage_no_key_deterministic_bundle_is_not_handler_stub(
    env, monkeypatch, tmp_path
):
    async def no_key_live(spec, profile, **kwargs):
        # Exactly what the real build_content_live returns when no
        # Gemini key resolves: the content layer's own
        # provenance-marked deterministic bundle, $0.
        from app.integration import content as content_mod

        return content_mod.build_content(spec, profile)

    _patch_live(monkeypatch, no_key_live)

    registry = build_registry(env["conn_factory"])
    result = _run(registry["stage.script"], _script_job(env, tmp_path))

    # (c) The deterministic bundle is a REAL bridge result (no
    # handler stub flag) — it is reachable only via this no-key path.
    assert "stub" not in result
    assert result["provenance"]["llm_path"] == "deterministic-stub"
    assert result["artefact"]["script_text"]
    # D1: the bundle's $0 must not suppress the cost table.
    assert result["cost_micros"] == 25_000


def test_resolve_cost_zero_means_no_recorded_cost():
    from app.execution.handlers import STAGE_COST_MICROS, _resolve_cost

    assert _resolve_cost("script", {"cost_micros": 0}) == STAGE_COST_MICROS["script"]
    assert _resolve_cost("script", {"cost_usd": 0.0}) == STAGE_COST_MICROS["script"]
    assert _resolve_cost("script", {"cost_micros": 0, "cost_usd": 0.0}) == 25_000
    assert _resolve_cost("script", {}) == 25_000
    # Non-zero recorded figures still win, in either unit.
    assert _resolve_cost("script", {"cost_micros": 4242}) == 4242
    assert _resolve_cost("script", {"cost_micros": 0, "cost_usd": 0.005}) == 5_000
