"""Round 6 closure (W7) — Gate B package/finaliser alignment.

Regression proof for the frozen-produce smoke divergence
(``outputs/round6/frozen-produce-smoke/SMOKE_REPORT.md``): the
package stage reported ``gate_b_passed=True`` while the publish
finaliser refused the same pack with ``G2_PROVENANCE_FAILED``.

Root causes pinned by these tests:

* provider canonicalisation (``local_plate`` -> ``generated``) lived
  only in the package-stage callers, so the finaliser's re-run of
  the shared finalize judged the raw registry differently;
* the finaliser re-ran the evaluator WITHOUT the project's publish
  mode, WITHOUT the Gate B approval that triggered it, and WITHOUT
  the QC per-check reason codes — so a Round-4 conditional could
  not be recognised / released on the publish path;
* Gate B surfaces carried only a bare bool / the generic strict
  problem code, losing the shared verdict label + typed reasons.

Now there is ONE evaluator (``publish_stage.finalize_publish`` +
``aggregator.evaluate_gate_b``), ONE canonicalisation (inside it),
and ONE labelling (``gate_b_verdict`` / ``gate_b_reason_codes``)
consumed by the package report and the finaliser alike.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.execution import publish_finaliser
from app.integration.models import ProjectSpec, make_context
from app.integration.publish_stage import finalize_publish
from tests.execution_fixtures import (
    RecordingHub,
    make_db,
    seed_channel_niche,
    seed_project,
)

# ---------------------------------------------------------------------------
# Row registries
# ---------------------------------------------------------------------------

#: The smoke registry shape: the app's own local Pillow plates.
PLATE_ROWS = [{
    "asset_id": "scene_0001", "scene_id": "scene_0001",
    "start_s": 0.0, "end_s": 18.0,
    "licence_id": "lic-scene_0001", "provider": "local_plate",
    "content_hash": "abc123", "role": "b_roll",
}]

#: A genuinely provenance-failing registry: G1-complete licence, but
#: a Tier-3 provider — G2 must BLOCK on every evaluation path.
PROVENANCE_FAIL_ROWS = [{
    "asset_id": "scene_0001", "scene_id": "scene_0001",
    "start_s": 0.0, "end_s": 18.0,
    "licence_id": "lic-scene_0001", "provider": "google_images",
    "content_hash": "abc123", "role": "b_roll",
}]

GOOD_ROWS = [{
    "asset_id": "scene_0001", "scene_id": "scene_0001",
    "start_s": 0.0, "end_s": 18.0,
    "licence_id": "lic-scene_0001", "provider": "generated",
    "content_hash": "abc123", "role": "b_roll",
}]


# ---------------------------------------------------------------------------
# Package-stage finalize (direct shared-evaluator harness)
# ---------------------------------------------------------------------------


def _package_finalize(tmp_path: Path, rows, qc_info, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    master = tmp_path / "final.mp4"
    master.write_bytes(b"fake-master-bytes-for-alignment-test")
    srt = tmp_path / "captions.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:06,000\nHello\n", encoding="utf-8")
    thumb = tmp_path / "thumbnail_1.png"
    thumb.write_bytes(b"fake-thumb")
    spec = ProjectSpec(niche="facts", topic="Ocean depth secrets", minutes=1.0)
    ctx = make_context(spec, SimpleNamespace(profile_id="facts"),
                       tmp_path / "proj", project_id="align-test")
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
        qc_passed=(qc_info.get("verdict") == "PASS"),
        licence_segments=rows,
    )
    return finalize_publish(
        assemble, qc_info, bundle, SimpleNamespace(profile_id="facts"),
        ctx, True, **kwargs,
    )


def _qc(verdict, *codes):
    checks = {}
    if codes:
        checks = {"check_0": {"reason_codes": list(codes)}}
    return {"verdict": verdict, "checks": checks}


# ---------------------------------------------------------------------------
# Publish-finaliser harness (pack on disk, DAG job results in the DB)
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path):
    _db_path, conn_factory = make_db(tmp_path)
    channel_id, niche_id = seed_channel_niche(conn_factory)
    return conn_factory, channel_id, niche_id


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


def _seed_pack(conn_factory, project_id, tmp_path, *, rows, qc_summary,
               qc_passed):
    pack_dir = tmp_path / f"pack-{project_id[:8]}"
    pack_dir.mkdir(parents=True, exist_ok=True)
    master = _make_master(pack_dir)
    (pack_dir / "captions.srt").write_text(
        "1\n00:00:00,000 --> 00:00:06,000\nHello\n", encoding="utf-8")
    now = 1_700_000_000_000
    conn = conn_factory()
    try:
        conn.execute(
            "UPDATE projects SET gate_status_json = ? WHERE id = ?",
            (json.dumps({"gate_a": "approved", "gate_b": "approved"}),
             project_id),
        )
        jobs = [
            ("stage.package", {
                "stage": "package", "final_mp4": str(master),
                "duration_s": 18.0, "title": "Alignment Title",
            }, now),
            ("stage.render", {
                "stage": "render", "final_mp4": str(master),
                "duration_s": 18.0, "qc_passed": qc_passed,
                "qc_summary": qc_summary,
                "licence_segments": rows,
                "title": "Alignment Title",
                "script_text": (
                    "the ocean hides many secrets beneath its waves "
                    "and sailors have feared the deep trenches"),
            }, now + 1),
        ]
        for kind, result, ts in jobs:
            conn.execute(
                "INSERT INTO jobs (id, project_id, agent_key, kind,"
                " payload_json, status, result_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, '{}', 'done', ?, ?, ?)",
                (str(uuid.uuid4()), project_id, kind, kind,
                 json.dumps(result), ts, ts),
            )
        conn.commit()
    finally:
        conn.close()
    return pack_dir


def _project(conn_factory, project_id):
    conn = conn_factory()
    try:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row) if row is not None else {}
    finally:
        conn.close()


def _records(conn_factory, project_id):
    conn = conn_factory()
    try:
        rows = conn.execute(
            "SELECT * FROM publish_records WHERE project_id = ?",
            (project_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _publish_info(conn_factory, project_id):
    raw = _project(conn_factory, project_id).get("gate_status_json")
    return (json.loads(raw) if raw else {}).get("publish") or {}


# ---------------------------------------------------------------------------
# 1. Smoke shape: provenance-failing registry — both sides refuse,
#    with the SAME verdict label and the SAME typed reason.
# ---------------------------------------------------------------------------


def test_provenance_failure_package_and_finaliser_agree(env, tmp_path):
    conn_factory, channel_id, niche_id = env

    package = _package_finalize(
        tmp_path / "pkg", PROVENANCE_FAIL_ROWS, _qc("PASS"))
    assert package.gate_b_passed is False
    assert package.gate_b_verdict == "block"
    assert package.gate_b_outcome == "block"
    assert package.publish_state == "manifest_frozen_not_uploaded"
    assert "G2_PROVENANCE_FAILED" in package.gate_b_reason_codes
    assert "VERDICT_NOT_PASS" in package.gate_b_reason_codes
    assert package.manifest["gate_b"]["verdict"] == "block"
    assert "G2_PROVENANCE_FAILED" in package.manifest["gate_b"]["reason_codes"]

    project_id = seed_project(
        conn_factory, channel_id, niche_id, publish_mode="review_first",
        status="in_production")
    _seed_pack(conn_factory, project_id, tmp_path,
               rows=PROVENANCE_FAIL_ROWS,
               qc_summary={"verdict": "PASS", "checks": {}}, qc_passed=True)

    outcome = publish_finaliser.finalise_after_gate_b(
        conn_factory, project_id, decision="approved", events=RecordingHub())
    assert outcome["finalised"] is False
    assert outcome["reason"] == "publish_stage_gate_b_failed"
    # Agreement: same verdict label, same typed reason code.
    assert outcome["gate_b_verdict"] == package.gate_b_verdict == "block"
    assert "G2_PROVENANCE_FAILED" in outcome["gate_b_reason_codes"]
    assert "G2_PROVENANCE_FAILED" in (outcome["detail"] or "")
    assert _records(conn_factory, project_id) == []
    project = _project(conn_factory, project_id)
    assert project["status"] == "blocked"
    assert "G2_PROVENANCE_FAILED" in (project["error_summary"] or "")
    info = _publish_info(conn_factory, project_id)
    assert info["gate_b_verdict"] == "block"
    assert "G2_PROVENANCE_FAILED" in info["gate_b_reason_codes"]


def test_local_plate_smoke_registry_now_agrees_and_passes(env, tmp_path):
    """The actual smoke case: local plates passed at package time and
    were refused by the finaliser.  Both paths must now PASS them —
    the registry is the app's own generated plates (Tier-1)."""
    conn_factory, channel_id, niche_id = env

    package = _package_finalize(tmp_path / "pkg", PLATE_ROWS, _qc("PASS"))
    assert package.gate_b_passed is True
    assert package.gate_b_verdict == "pass"

    project_id = seed_project(
        conn_factory, channel_id, niche_id, publish_mode="review_first",
        status="in_production")
    _seed_pack(conn_factory, project_id, tmp_path, rows=PLATE_ROWS,
               qc_summary={"verdict": "PASS", "checks": {}}, qc_passed=True)

    outcome = publish_finaliser.finalise_after_gate_b(
        conn_factory, project_id, decision="approved", events=RecordingHub())
    assert outcome["finalised"] is True, outcome
    assert len(_records(conn_factory, project_id)) == 1
    assert _project(conn_factory, project_id)["status"] == "ready"
    assert _publish_info(conn_factory, project_id)["gate_b_verdict"] == "pass"


# ---------------------------------------------------------------------------
# 2. Conditional pass: explicitly labelled at package time; the
#    finaliser's behaviour matches the label in both modes.
# ---------------------------------------------------------------------------


def test_conditional_package_labels_and_finaliser_releases_review_first(
        env, tmp_path):
    conn_factory, channel_id, niche_id = env

    # Package time (no Gate B approval exists yet): NOT a bare pass.
    pending = _package_finalize(
        tmp_path / "pending", GOOD_ROWS, _qc("WARN", "BLACK_SEGMENT_BRIEF"),
        mode="review_first")
    assert pending.gate_b_passed is False
    assert pending.gate_b_outcome == "conditional"
    assert pending.gate_b_verdict == "conditional_blocked"
    assert "BLACK_SEGMENT_BRIEF" in pending.gate_b_reason_codes
    assert "CONDITIONAL_APPROVAL_REQUIRED" in pending.gate_b_reason_codes
    assert pending.manifest["gate_b"]["verdict"] == "conditional_blocked"
    assert "BLACK_SEGMENT_BRIEF" in pending.manifest["gate_b"]["reason_codes"]

    # With a recorded approval in review_first: a CONDITIONAL pass,
    # labelled as such — never a bare/strict pass.
    released = _package_finalize(
        tmp_path / "released", GOOD_ROWS, _qc("WARN", "BLACK_SEGMENT_BRIEF"),
        mode="review_first", gate_b_approval=True)
    assert released.gate_b_passed is True
    assert released.gate_b_verdict == "conditional_pass"
    assert released.gate_b_outcome == "conditional"
    assert released.manifest["gate_b"]["verdict"] == "conditional_pass"
    assert released.manifest["gate_b"]["strict_passed"] is False
    assert "BLACK_SEGMENT_BRIEF" in released.gate_b_reason_codes

    # Finaliser: the Gate B approval that triggered finalisation IS
    # the recorded approval — the conditional releases, and the
    # publish provenance carries the conditional label.
    project_id = seed_project(
        conn_factory, channel_id, niche_id, publish_mode="review_first",
        status="in_production")
    _seed_pack(
        conn_factory, project_id, tmp_path, rows=GOOD_ROWS,
        qc_summary={
            "verdict": "WARN",
            "checks": {"black": {"verdict": "WARN",
                                 "reason_codes": ["BLACK_SEGMENT_BRIEF"]}},
        },
        qc_passed=False)
    outcome = publish_finaliser.finalise_after_gate_b(
        conn_factory, project_id, decision="approved", events=RecordingHub())
    assert outcome["finalised"] is True, outcome
    assert len(_records(conn_factory, project_id)) == 1
    info = _publish_info(conn_factory, project_id)
    assert info["gate_b_verdict"] == "conditional_pass"
    assert "BLACK_SEGMENT_BRIEF" in info["gate_b_reason_codes"]


def test_conditional_auto_mode_blocked_on_both_paths(env, tmp_path):
    conn_factory, channel_id, niche_id = env

    package = _package_finalize(
        tmp_path / "pkg", GOOD_ROWS, _qc("WARN", "BLACK_SEGMENT_BRIEF"),
        mode="auto", gate_b_approval=True)
    assert package.gate_b_passed is False
    assert package.gate_b_verdict == "conditional_blocked"
    assert "CONDITIONAL_BLOCKED_AUTO_MODE" in package.gate_b_reason_codes

    project_id = seed_project(
        conn_factory, channel_id, niche_id, publish_mode="auto",
        status="in_production")
    _seed_pack(
        conn_factory, project_id, tmp_path, rows=GOOD_ROWS,
        qc_summary={
            "verdict": "WARN",
            "checks": {"black": {"verdict": "WARN",
                                 "reason_codes": ["BLACK_SEGMENT_BRIEF"]}},
        },
        qc_passed=False)
    outcome = publish_finaliser.finalise_after_gate_b(
        conn_factory, project_id, decision="auto_approved",
        events=RecordingHub())
    assert outcome["finalised"] is False
    assert outcome["gate_b_verdict"] == package.gate_b_verdict
    assert "CONDITIONAL_BLOCKED_AUTO_MODE" in outcome["gate_b_reason_codes"]
    assert _records(conn_factory, project_id) == []


# ---------------------------------------------------------------------------
# 3. Package report surface: the label survives into report facts
#    and report.md — a conditional pass never renders as a bare PASS.
# ---------------------------------------------------------------------------


def test_package_report_surfaces_conditional_label(tmp_path):
    from app.integration.pack import write_report_md
    from app.integration.report_facts import facts_from_publish_manifest

    released = _package_finalize(
        tmp_path / "pkg", GOOD_ROWS, _qc("WARN", "BLACK_SEGMENT_BRIEF"),
        mode="review_first", gate_b_approval=True)
    facts = facts_from_publish_manifest(released.manifest)
    assert facts["gate_b_passed"] is True
    assert facts["gate_b_verdict"] == "conditional_pass"
    assert "BLACK_SEGMENT_BRIEF" in facts["gate_b_reason_codes"]

    report = tmp_path / "report.md"
    write_report_md(
        {
            "title": "Ocean Depth Secrets",
            "gate_a_passed": True,
            "gate_b_passed": True,
            "gate_b_verdict": facts["gate_b_verdict"],
            "gate_b_reason_codes": facts["gate_b_reason_codes"],
            "qc_passed": False,
        },
        report,
    )
    text = report.read_text(encoding="utf-8")
    assert "conditional_pass" in text
    assert "BLACK_SEGMENT_BRIEF" in text
    assert "| Gate B (Pre-Publish) | PASS |" not in text
