"""Tests for the Round-4 publish finaliser (Worker 6).

The DAG ends at ``gate.b``; ``app.execution.publish_finaliser`` —
wired into ``ExecutionService`` by wrapping the registry's ``gate.b``
handler — is what turns an approved gate into an exactly-once,
provenance-honest *simulated* publish (fake provider only):

(a) auto mode: gate.b completion -> simulated publish record exists
    (provider fake / simulated), project at the honest terminal
    status ``ready`` (never ``published`` — no real upload happened),
    ``project.publish_simulated`` live event + persisted event_log row;
(b) review_first: NO publish while the gate is parked, publish only
    after the human approval resumes the gate job to completion;
(c) gate.b rejected / blocked: no publish record, and the
    non-publication is visible in the project record;
(d) idempotency: finalise twice + a fresh-service gate replay still
    yields exactly one publish record;
(e) pack tier: a real pack on disk runs the full publish_stage flow
    (frozen manifest written, manifest idempotency key reused).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid
from pathlib import Path

import pytest

from app.execution import gates_bridge, publish_finaliser
from app.execution.dag import create_project_dag
from app.execution.handlers import GateRejected
from app.execution.runner import GatePending
from app.execution.service import ExecutionService
from app.queue import SQLiteJobQueue
from tests.execution_fixtures import (
    RecordingHub,
    make_db,
    seed_channel_niche,
    seed_project,
)


@pytest.fixture()
def env(tmp_path):
    _db_path, conn_factory = make_db(tmp_path)
    channel_id, niche_id = seed_channel_niche(conn_factory)
    return conn_factory, channel_id, niche_id


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _project(conn_factory, project_id: str) -> dict:
    conn = conn_factory()
    try:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return dict(row) if row is not None else {}
    finally:
        conn.close()


def _publish_records(conn_factory, project_id: str) -> list[dict]:
    conn = conn_factory()
    try:
        rows = conn.execute(
            "SELECT * FROM publish_records WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _gate_status(project: dict) -> dict:
    raw = project.get("gate_status_json")
    return json.loads(raw) if raw else {}


def _job_dict(job_id: str, project_id: str, payload_extra: dict | None = None):
    payload = {"project_id": project_id, "stage": "gate_b"}
    if payload_extra:
        payload.update(payload_extra)
    return {
        "id": job_id,
        "kind": "gate.b",
        "project_id": project_id,
        "payload": payload,
    }


def _run_gate_b(service: ExecutionService, job: dict) -> dict:
    return asyncio.run(service.registry["gate.b"](job))


# ---------------------------------------------------------------------------
# (a) auto mode -> simulated publish on gate.b completion
# ---------------------------------------------------------------------------


def test_auto_gate_b_completion_finalises_simulated_publish(env):
    conn_factory, channel_id, niche_id = env
    project_id = seed_project(
        conn_factory, channel_id, niche_id, publish_mode="auto",
        status="in_production",
    )
    hub = RecordingHub()
    service = ExecutionService(conn_factory, hub=hub)

    # Gate A (auto) then Gate B (auto) through the service registry.
    gate_a_job = {
        "id": str(uuid.uuid4()), "kind": "gate.a",
        "project_id": project_id,
        "payload": {"project_id": project_id, "stage": "gate_a"},
    }
    asyncio.run(service.registry["gate.a"](gate_a_job))

    result = _run_gate_b(service, _job_dict(str(uuid.uuid4()), project_id))
    assert result["decision"] == "auto_approved"
    finalisation = result["publish_finalisation"]
    assert finalisation["finalised"] is True
    assert finalisation["already_finalised"] is False
    assert finalisation["provider"] == "fake"
    assert finalisation["simulated"] is True

    # Exactly one publish record, with honest simulated provenance.
    records = _publish_records(conn_factory, project_id)
    assert len(records) == 1
    record = records[0]
    assert record["id"] == finalisation["publish_record_id"]
    assert record["youtube_video_id"].startswith("simulated:fake-video-")
    assert project_id in record["youtube_video_id"]
    assert record["video_url"] is None
    assert record["published_at"] is None
    assert record["privacy_status"] == "private"
    assert record["disclosure_altered_content"] == 1
    assert record["upload_idempotency_key"] == f"simulated-publish:{project_id}"

    # Honest terminal state: 'ready', NOT 'published'; no fabricated
    # real video id / publish time on the project itself.
    project = _project(conn_factory, project_id)
    assert project["status"] == "ready"
    assert project["youtube_video_id"] is None
    assert project["published_at"] is None
    publish_info = _gate_status(project)["publish"]
    assert publish_info["provider"] == "fake"
    assert publish_info["simulated"] is True
    assert publish_info["state"] == "fake_provider_upload_simulated"
    assert publish_info["publish_record_id"] == record["id"]

    # Live event emitted AND a persisted event_log row exists.
    live = hub.by_type("project.publish_simulated")
    assert len(live) == 1
    assert live[0]["payload"]["publish_record_id"] == record["id"]
    conn = conn_factory()
    try:
        rows = conn.execute(
            "SELECT payload_json FROM event_log"
            " WHERE type = 'project.publish_simulated'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    journalled = json.loads(rows[0][0])
    assert journalled["payload"]["publish_record_id"] == record["id"]


# ---------------------------------------------------------------------------
# (b) review_first: publish only after human approval lands
# ---------------------------------------------------------------------------


def test_review_first_publishes_only_after_approval(env):
    conn_factory, channel_id, niche_id = env
    project_id = seed_project(
        conn_factory, channel_id, niche_id, publish_mode="review_first",
        status="in_production",
    )
    queue = SQLiteJobQueue(conn_factory)
    job_ids = create_project_dag(queue, project_id)
    gate_b_job_id = job_ids[-1]
    service = ExecutionService(conn_factory, hub=RecordingHub())

    # First poll: no approval yet -> GatePending, project parked,
    # and crucially NO publish record and NO publish provenance.
    with pytest.raises(GatePending):
        _run_gate_b(service, _job_dict(gate_b_job_id, project_id))
    assert _publish_records(conn_factory, project_id) == []
    project = _project(conn_factory, project_id)
    assert project["status"] == "awaiting_review"
    assert "publish" not in _gate_status(project)

    # Human approves via the approvals flow (payload flip on the job).
    assert gates_bridge.apply_decision(
        conn_factory, project_id, "b", "approved"
    ) is True
    assert _publish_records(conn_factory, project_id) == []

    # The resumed gate job now completes -> finalisation fires.
    conn = conn_factory()
    try:
        row = conn.execute(
            "SELECT payload_json FROM jobs WHERE id = ?", (gate_b_job_id,)
        ).fetchone()
        payload = json.loads(row[0])
    finally:
        conn.close()
    job = {
        "id": gate_b_job_id, "kind": "gate.b",
        "project_id": project_id, "payload": payload,
    }
    result = _run_gate_b(service, job)
    assert result["decision"] == "approved"
    assert result["publish_finalisation"]["finalised"] is True

    records = _publish_records(conn_factory, project_id)
    assert len(records) == 1
    project = _project(conn_factory, project_id)
    assert project["status"] == "ready"
    assert _gate_status(project)["publish"]["simulated"] is True


# ---------------------------------------------------------------------------
# (c) blocked / rejected gates never publish — and say so
# ---------------------------------------------------------------------------


def test_rejected_gate_b_never_publishes_and_records_why(env):
    conn_factory, channel_id, niche_id = env
    project_id = seed_project(
        conn_factory, channel_id, niche_id, publish_mode="review_first",
        status="in_production",
    )
    service = ExecutionService(conn_factory, hub=RecordingHub())

    job = _job_dict(
        str(uuid.uuid4()), project_id,
        {"approval": {"decision": "rejected", "approval_id": "a-1"}},
    )
    with pytest.raises(GateRejected):
        _run_gate_b(service, job)

    assert _publish_records(conn_factory, project_id) == []
    project = _project(conn_factory, project_id)
    assert project["status"] != "ready"
    publish_info = _gate_status(project)["publish"]
    assert publish_info["state"] == "not_published"
    assert publish_info["reason"] == "gate_b_rejected"
    assert "Not published" in (project["error_summary"] or "")


def test_finaliser_refuses_without_approved_gate_decision(env):
    conn_factory, channel_id, niche_id = env
    project_id = seed_project(
        conn_factory, channel_id, niche_id, publish_mode="auto",
        status="blocked",
    )
    # Simulate a Gate B that BLOCKED (e.g. QC WARN policy): the gate
    # status says 'blocked', never approved.
    conn = conn_factory()
    try:
        conn.execute(
            "UPDATE projects SET gate_status_json = ? WHERE id = ?",
            (json.dumps({"gate_a": "approved", "gate_b": "blocked"}),
             project_id),
        )
        conn.commit()
    finally:
        conn.close()

    outcome = publish_finaliser.finalise_after_gate_b(
        conn_factory, project_id, events=RecordingHub()
    )
    assert outcome["finalised"] is False
    assert outcome["reason"] == "gate_b_decision_blocked"
    assert _publish_records(conn_factory, project_id) == []
    project = _project(conn_factory, project_id)
    assert project["status"] == "blocked"  # unchanged by the finaliser
    assert _gate_status(project)["publish"]["state"] == "not_published"


# ---------------------------------------------------------------------------
# (d) exactly-once: duplicate finalise + restart replay
# ---------------------------------------------------------------------------


def test_finalise_is_exactly_once_across_replays_and_restart(env):
    conn_factory, channel_id, niche_id = env
    project_id = seed_project(
        conn_factory, channel_id, niche_id, publish_mode="auto",
        status="in_production",
    )
    hub = RecordingHub()

    first = publish_finaliser.finalise_after_gate_b(
        conn_factory, project_id, decision="auto_approved", events=hub
    )
    second = publish_finaliser.finalise_after_gate_b(
        conn_factory, project_id, decision="auto_approved", events=hub
    )
    assert first["finalised"] is True and first["already_finalised"] is False
    assert second["finalised"] is True and second["already_finalised"] is True
    assert second["publish_record_id"] == first["publish_record_id"]
    assert len(_publish_records(conn_factory, project_id)) == 1
    # The live event fires once — for the actual finalisation only.
    assert len(hub.by_type("project.publish_simulated")) == 1

    # Service restart replay: a brand-new ExecutionService over the
    # same DB sees the gate complete again; still exactly one record.
    service = ExecutionService(conn_factory, hub=RecordingHub())
    result = _run_gate_b(service, _job_dict(str(uuid.uuid4()), project_id))
    assert result["decision"] == "auto_approved"
    assert result["publish_finalisation"]["already_finalised"] is True
    assert len(_publish_records(conn_factory, project_id)) == 1


# ---------------------------------------------------------------------------
# (e) pack tier: real pack on disk -> full publish_stage flow
# ---------------------------------------------------------------------------


def _make_master(pack_dir: Path) -> Path:
    master = pack_dir / "final.mp4"
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=1",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(master),
            ],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0 or not master.is_file():
            raise RuntimeError(proc.stderr[-200:])
    except Exception:
        master.write_bytes(b"fake-master-bytes")
    return master


def test_pack_on_disk_runs_publish_stage_flow(env, tmp_path):
    conn_factory, channel_id, niche_id = env
    project_id = seed_project(
        conn_factory, channel_id, niche_id, publish_mode="auto",
        status="in_production",
    )
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    master = _make_master(pack_dir)
    (pack_dir / "captions.srt").write_text(
        "1\n00:00:00,000 --> 00:00:06,000\nHello\n", encoding="utf-8"
    )

    # Record the finished pack the way the DAG package stage does:
    # a completed stage.package job whose result names the master.
    conn = conn_factory()
    try:
        now = 1_700_000_000_000
        result = {
            "stage": "package",
            "final_mp4": str(master),
            "duration_s": 12.0,
            "qc_passed": True,
            "title": "Pack Tier Title",
        }
        conn.execute(
            "INSERT INTO jobs (id, project_id, agent_key, kind,"
            " payload_json, status, result_json, created_at, updated_at)"
            " VALUES (?, ?, 'stage.package', 'stage.package', '{}',"
            " 'done', ?, ?, ?)",
            (str(uuid.uuid4()), project_id, json.dumps(result), now, now),
        )
        conn.commit()
    finally:
        conn.close()

    service = ExecutionService(conn_factory, hub=RecordingHub())
    gate_a_job = {
        "id": str(uuid.uuid4()), "kind": "gate.a",
        "project_id": project_id,
        "payload": {"project_id": project_id, "stage": "gate_a"},
    }
    asyncio.run(service.registry["gate.a"](gate_a_job))
    result = _run_gate_b(service, _job_dict(str(uuid.uuid4()), project_id))

    finalisation = result["publish_finalisation"]
    assert finalisation["finalised"] is True, finalisation
    assert finalisation["pack_flow"] == "publish_stage"

    # The publish_stage flow froze a manifest into the pack dir and
    # the record reuses the manifest's own idempotency key.
    manifest_path = pack_dir / "publish_manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["publish_state"] == "fake_provider_upload_simulated"
    assert manifest["gate_b"]["passed"] is True

    records = _publish_records(conn_factory, project_id)
    assert len(records) == 1
    assert records[0]["upload_idempotency_key"] == manifest["idempotency_key"]
    assert records[0]["title"] == "Pack Tier Title"
    project = _project(conn_factory, project_id)
    assert project["status"] == "ready"
    assert _gate_status(project)["publish"]["pack_flow"] == "publish_stage"
