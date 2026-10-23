"""Compliance Gate tests (Round 6) — stored checklist, transitions, Gate C.

Covers:
* schema/DDL: runtime ensure creates + seeds the checklist; migration
  0011 carries identical DDL;
* service: derived overall, deliberate logged transitions (who/when),
  evidence required for PASS, actor required, typed refusals;
* pure gate logic: forced-private + public refusal pre-PASS, normal
  behaviour after PASS;
* publishing workflow (Gate C): release() refuses public pre-PASS with
  the typed reason, run() holds the upload private, PASS restores the
  full walk to POST_LIVE_DONE;
* fake-provider path: publish_stage manifest/report disclose the
  forced private; publish finaliser provenance discloses it too.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import db as db_module
from app.compliance import gate as gate_module
from app.compliance import service
from app.compliance.schema import (
    COMPLIANCE_ITEMS_DDL,
    COMPLIANCE_TRANSITIONS_DDL,
    ITEM_KEYS,
    ensure_compliance_schema,
)

ACTOR = "compliance-officer-test"


@pytest.fixture()
def conn(tmp_path):
    connection = db_module.connect(tmp_path / "compliance.db")
    yield connection
    connection.close()


def _pass_all(conn, actor=ACTOR):
    state = None
    for key in ITEM_KEYS:
        state = service.transition_item(
            conn,
            item_key=key,
            to_status="passed",
            actor=actor,
            evidence=f"evidence-for-{key}",
        )
    return state


# ---------------------------------------------------------------------------
# Schema / migration parity
# ---------------------------------------------------------------------------


def test_ensure_creates_and_seeds_checklist(conn):
    state = service.get_state(conn)
    assert [item["key"] for item in state["items"]] == list(ITEM_KEYS)
    assert {item["key"] for item in state["items"]} == {
        "api_audit_status",
        "terms_branding_review",
        "privacy_policy_live",
        "quota_project_verified",
    }
    assert all(item["status"] == "pending" for item in state["items"])
    assert state["overall"] == "not_started"
    assert state["passed"] is False
    assert state["public_publishing_allowed"] is False
    assert state["forced_private"] is True
    assert "forced to Private" in state["effect"]
    # Idempotent: re-ensuring never resets stamps/statuses.
    ensure_compliance_schema(conn)
    assert service.get_state(conn)["overall"] == "not_started"


def test_migration_0011_carries_identical_ddl():
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0011_compliance.py"
    )
    text = migration.read_text(encoding="utf-8")
    assert 'revision: str = "0011"' in text
    assert 'down_revision: Union[str, None] = "0010"' in text

    def _norm(sql: str) -> str:
        return " ".join(sql.split())

    assert _norm(COMPLIANCE_ITEMS_DDL) in _norm(text)
    assert _norm(COMPLIANCE_TRANSITIONS_DDL) in _norm(text)


# ---------------------------------------------------------------------------
# Transitions: logged, evidenced, deliberate
# ---------------------------------------------------------------------------


def test_transition_is_logged_with_who_when(conn):
    state = service.transition_item(
        conn,
        item_key="privacy_policy_live",
        to_status="in_progress",
        actor=ACTOR,
        note="drafting the policy page",
    )
    assert state["overall"] == "in_progress"
    item = next(i for i in state["items"] if i["key"] == "privacy_policy_live")
    assert item["status"] == "in_progress"
    assert item["updated_by"] == ACTOR
    assert isinstance(item["updated_at"], int) and item["updated_at"] > 0
    assert item["note"] == "drafting the policy page"
    assert len(state["history"]) == 1
    entry = state["history"][0]
    assert entry["item_key"] == "privacy_policy_live"
    assert entry["from_status"] == "pending"
    assert entry["to_status"] == "in_progress"
    assert entry["actor"] == ACTOR
    assert isinstance(entry["created_at"], int) and entry["created_at"] > 0


def test_pass_requires_evidence(conn):
    with pytest.raises(service.ComplianceTransitionError) as excinfo:
        service.transition_item(
            conn,
            item_key="api_audit_status",
            to_status="passed",
            actor=ACTOR,
        )
    assert excinfo.value.code == "compliance_evidence_required"
    assert excinfo.value.status_code == 422
    # Nothing was written: no status change, no history row.
    state = service.get_state(conn)
    item = next(i for i in state["items"] if i["key"] == "api_audit_status")
    assert item["status"] == "pending"
    assert state["history"] == []


def test_transition_requires_actor_and_known_item_and_valid_status(conn):
    with pytest.raises(service.ComplianceTransitionError) as e1:
        service.transition_item(
            conn, item_key="api_audit_status", to_status="in_progress", actor="  "
        )
    assert e1.value.code == "compliance_actor_required"
    with pytest.raises(service.ComplianceTransitionError) as e2:
        service.transition_item(
            conn, item_key="nope", to_status="passed", actor=ACTOR, evidence="x"
        )
    assert e2.value.code == "compliance_item_not_found"
    assert e2.value.status_code == 404
    with pytest.raises(service.ComplianceTransitionError) as e3:
        service.transition_item(
            conn, item_key="api_audit_status", to_status="done", actor=ACTOR
        )
    assert e3.value.code == "compliance_status_invalid"
    with pytest.raises(service.ComplianceTransitionError) as e4:
        service.transition_item(
            conn, item_key="api_audit_status", to_status="pending", actor=ACTOR
        )
    assert e4.value.code == "compliance_transition_noop"


def test_overall_pass_only_when_every_item_passes_with_evidence(conn):
    for key in ITEM_KEYS[:-1]:
        state = service.transition_item(
            conn, item_key=key, to_status="passed", actor=ACTOR,
            evidence=f"ref-{key}",
        )
        assert state["overall"] == "in_progress"  # one item still pending
        assert state["public_publishing_allowed"] is False
    state = service.transition_item(
        conn, item_key=ITEM_KEYS[-1], to_status="passed", actor=ACTOR,
        evidence=f"ref-{ITEM_KEYS[-1]}",
    )
    assert state["overall"] == "passed"
    # Re-passing an already-passed item is a no-op and is refused.
    with pytest.raises(service.ComplianceTransitionError) as excinfo:
        service.transition_item(
            conn, item_key=ITEM_KEYS[-1], to_status="passed", actor=ACTOR,
            evidence="again",
        )
    assert excinfo.value.code == "compliance_transition_noop"


def test_full_pass_and_history_accumulates(conn):
    state = None
    for key in ITEM_KEYS:
        state = service.transition_item(
            conn, item_key=key, to_status="passed", actor=ACTOR,
            evidence=f"ref-{key}", note="verified",
        )
    assert state["overall"] == "passed"
    assert state["passed"] is True
    assert state["public_publishing_allowed"] is True
    assert state["forced_private"] is False
    assert len(state["history"]) == len(ITEM_KEYS)
    assert all(h["actor"] == ACTOR for h in state["history"])
    assert all(h["to_status"] == "passed" for h in state["history"])
    assert all(h["evidence"] for h in state["history"])


def test_stored_evidence_can_satisfy_a_later_pass(conn):
    service.transition_item(
        conn, item_key="quota_project_verified", to_status="in_progress",
        actor=ACTOR, evidence="project neat-iterator (verification pending)",
    )
    state = service.transition_item(
        conn, item_key="quota_project_verified", to_status="passed", actor=ACTOR
    )
    item = next(i for i in state["items"] if i["key"] == "quota_project_verified")
    assert item["status"] == "passed"
    assert item["evidence"]


# ---------------------------------------------------------------------------
# Pure gate logic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("overall", ["not_started", "in_progress", None, False])
def test_gate_forces_private_and_refuses_public_pre_pass(overall):
    decision = gate_module.evaluate_visibility("public", overall)
    assert decision["passed"] is False
    assert decision["effective_visibility"] == "private"
    assert decision["forced_private"] is True
    assert decision["public_publish_refused"] is True
    assert decision["reason_code"] == "compliance_not_passed"


def test_gate_honours_requested_visibility_after_pass():
    for requested in ("public", "unlisted", "private"):
        decision = gate_module.evaluate_visibility(requested, "passed")
        assert decision["passed"] is True
        assert decision["effective_visibility"] == requested
        assert decision["forced_private"] is False
        assert decision["reason_code"] is None


# ---------------------------------------------------------------------------
# Publishing workflow — Gate C enforcement
# ---------------------------------------------------------------------------

from app.integration.publish_stage import FakeProvider  # noqa: E402
from app.pipeline.publishing import (  # noqa: E402
    ComplianceGateError,
    InMemoryStore,
    PublishManifest,
    PublishState,
    PublishingWorkflow,
    ReleaseMode,
)

DATA = b"x" * 1024


def _manifest(**over) -> PublishManifest:
    base = dict(
        project_id="proj-compliance",
        channel_id="chan-1",
        master_path="/tmp/master.mp4",
        master_checksum="deadbeefcafe",
        master_size_bytes=len(DATA),
        duration_seconds=60.0,
        resolution_width=1280,
        resolution_height=720,
        thumbnail_path="/tmp/thumb.jpg",
        caption_path="/tmp/caps.srt",
        caption_language="en",
        title="Compliance Test Title",
        description="desc",
        tags=["facts"],
        qc_pass_token="qc-1",
        copyright_pass_token="cr-1",
        policy_pass_token="pol-1",
        licence_ledger_completeness=1.0,
    )
    base.update(over)
    return PublishManifest(**base).freeze()


def _workflow(fake, checker):
    return PublishingWorkflow(
        fake, InMemoryStore(), compliance_checker=checker
    )


def test_workflow_run_holds_upload_private_pre_pass():
    fake = FakeProvider(expected_duration_s=60.0)
    wf = _workflow(fake, lambda: "not_started")
    job = wf.run(_manifest(), DATA)
    # Upload happened, but the public release was refused: the job
    # holds at CHECKS_PASSED and the fake video is still private.
    assert job.state is PublishState.CHECKS_PASSED
    assert job.video_id is not None
    assert job.compliance_forced_private is True
    assert job.compliance_block is not None
    assert job.compliance_block["code"] == "compliance_not_passed"
    assert job.compliance_block["reason"] == "compliance_not_passed"
    assert job.compliance_block["effective_visibility"] == "private"
    assert fake._videos[job.video_id]["privacy"] == "private"
    assert not job.is_live_confirmed


def test_workflow_release_refuses_public_pre_pass_with_typed_reason():
    fake = FakeProvider(expected_duration_s=60.0)
    wf = _workflow(fake, lambda: "in_progress")
    job = wf.run(_manifest(), DATA)
    assert job.state is PublishState.CHECKS_PASSED
    with pytest.raises(ComplianceGateError) as excinfo:
        wf.release(job, _manifest())
    err = excinfo.value
    assert err.code == "compliance_not_passed"
    assert err.reason == "compliance_not_passed"
    assert err.overall == "in_progress"
    # Still private, still held — the refusal changed nothing else.
    assert job.state is PublishState.CHECKS_PASSED
    assert fake._videos[job.video_id]["privacy"] == "private"


def test_workflow_scheduled_publish_is_not_armed_pre_pass():
    from datetime import datetime, timedelta, timezone

    slot = datetime.now(timezone.utc) + timedelta(hours=3)
    fake = FakeProvider(expected_duration_s=60.0)
    wf = _workflow(fake, lambda: "not_started")
    manifest = _manifest(
        release_mode=ReleaseMode.SCHEDULED, target_slot=slot
    )
    job = wf.run(manifest, DATA)
    assert job.state is PublishState.CHECKS_PASSED  # never SCHEDULED
    assert job.armed_slot is None
    assert job.compliance_block["code"] == "compliance_not_passed"
    assert fake._videos[job.video_id]["publish_at"] is None
    assert fake._videos[job.video_id]["privacy"] == "private"


def test_workflow_pass_restores_normal_public_behaviour():
    fake = FakeProvider(expected_duration_s=60.0)
    wf = _workflow(fake, lambda: "passed")
    job = wf.run(_manifest(), DATA)
    assert job.state is PublishState.POST_LIVE_DONE
    assert job.is_live_confirmed
    assert job.compliance_forced_private is False
    assert job.compliance_block is None
    assert fake._videos[job.video_id]["privacy"] == "public"


def test_workflow_without_checker_keeps_legacy_behaviour():
    fake = FakeProvider(expected_duration_s=60.0)
    wf = PublishingWorkflow(fake, InMemoryStore())
    job = wf.run(_manifest(), DATA)
    assert job.state is PublishState.POST_LIVE_DONE


# ---------------------------------------------------------------------------
# Fake-provider publish stage: disclosure in report + manifest
# ---------------------------------------------------------------------------

from app.integration.models import ProjectSpec, make_context  # noqa: E402
from app.integration.publish_stage import finalize_publish  # noqa: E402


def _run_publish_stage(tmp_path: Path, **kwargs):
    master = tmp_path / "final.mp4"
    master.write_bytes(b"fake-master-bytes-for-compliance-test")
    srt = tmp_path / "captions.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:06,000\nHello\n", encoding="utf-8")
    thumb = tmp_path / "thumbnail_1.png"
    thumb.write_bytes(b"fake-thumb")
    spec = ProjectSpec(niche="facts", topic="Ocean depth secrets", minutes=1.0)
    profile = SimpleNamespace(profile_id="facts")
    ctx = make_context(spec, profile, tmp_path / "proj", project_id="comp-test")
    bundle = SimpleNamespace(
        title="Ocean Depth Secrets",
        description="A test description with no attribution requirements.",
        tags=["ocean", "facts"],
        script_text=(
            "the ocean hides many secrets beneath its waves and sailors "
            "have feared the deep trenches for centuries"
        ),
    )
    assemble = SimpleNamespace(
        final_mp4=str(master),
        duration_s=18.0,
        thumbnails=[str(thumb)],
        captions_path=str(srt),
        qc_passed=True,
    )
    return finalize_publish(
        assemble, {"verdict": "PASS", "checks": {}}, bundle, profile, ctx, True,
        **kwargs,
    )


def test_publish_stage_forced_private_disclosed_pre_pass(tmp_path):
    result = _run_publish_stage(tmp_path, compliance_state="not_started")
    assert result.gate_b_passed is True
    assert result.publish_state == "fake_provider_upload_simulated"
    gate = result.compliance_gate
    assert gate["overall"] == "not_started"
    assert gate["forced_private"] is True
    assert gate["effective_visibility"] == "private"
    assert gate["public_publish_refused"] is True
    assert gate["reason_code"] == "compliance_not_passed"
    assert "forced to Private by the Compliance Gate" in gate["disclosure"]
    # The manifest on disk says it too.
    on_disk = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert on_disk["compliance_gate"]["forced_private"] is True
    assert on_disk["compliance_gate"]["reason_code"] == "compliance_not_passed"
    assert on_disk["notes"]["compliance_gate"]["forced_private"] is True
    assert result.notes["fake_provider"]["job_state"] == "CHECKS_PASSED"


def test_publish_stage_pass_restores_normal_simulated_publish(tmp_path):
    result = _run_publish_stage(tmp_path, compliance_state="passed")
    assert result.gate_b_passed is True
    assert result.publish_state == "fake_provider_upload_simulated"
    gate = result.compliance_gate
    assert gate["passed"] is True
    assert gate["forced_private"] is False
    assert gate["effective_visibility"] == "public"
    assert result.notes["fake_provider"]["job_state"] == "POST_LIVE_DONE"


# ---------------------------------------------------------------------------
# Publish finaliser (execution path, fake provider): provenance disclosure
# ---------------------------------------------------------------------------

from app.execution import publish_finaliser  # noqa: E402
from tests.execution_fixtures import make_db, seed_channel_niche, seed_project  # noqa: E402


def _finalise(tmp_path, decision="approved"):
    _db_path, conn_factory = make_db(tmp_path)
    channel_id, niche_id = seed_channel_niche(conn_factory)
    project_id = seed_project(
        conn_factory, channel_id, niche_id, publish_mode="auto",
        status="in_production",
    )
    result = publish_finaliser.finalise_after_gate_b(
        conn_factory, project_id, decision=decision
    )
    conn = conn_factory()
    try:
        project = dict(
            conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        )
        records = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM publish_records WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        ]
    finally:
        conn.close()
    return conn_factory, project_id, result, project, records


def test_finaliser_simulated_publish_discloses_forced_private_pre_pass(tmp_path):
    _factory, _pid, result, project, records = _finalise(tmp_path)
    assert result["finalised"] is True
    assert result["forced_private_by_compliance_gate"] is True
    assert result["compliance_gate"]["reason_code"] == "compliance_not_passed"
    assert len(records) == 1
    assert records[0]["privacy_status"] == "private"
    publish_info = json.loads(project["gate_status_json"])["publish"]
    assert publish_info["forced_private_by_compliance_gate"] is True
    assert publish_info["privacy_status"] == "private"
    assert publish_info["compliance_gate"]["overall"] == "not_started"


def test_finaliser_after_compliance_pass_is_not_forced(tmp_path):
    _db_path, conn_factory = make_db(tmp_path)
    channel_id, niche_id = seed_channel_niche(conn_factory)
    project_id = seed_project(
        conn_factory, channel_id, niche_id, publish_mode="auto",
        status="in_production",
    )
    conn = conn_factory()
    try:
        _pass_all(conn)
    finally:
        conn.close()
    result = publish_finaliser.finalise_after_gate_b(
        conn_factory, project_id, decision="approved"
    )
    assert result["finalised"] is True
    assert result["forced_private_by_compliance_gate"] is False
    assert result["compliance_gate"]["passed"] is True


def test_sqlite_tables_exist_after_ensure(conn):
    ensure_compliance_schema(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "compliance_items" in tables
    assert "compliance_transitions" in tables
    assert isinstance(conn, sqlite3.Connection)
