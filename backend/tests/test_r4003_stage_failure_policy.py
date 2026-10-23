"""R4-003 regression tests: execution-path stage failures must NOT
silently fall back to stub artefacts.

Policy under test (decided for R4-003):

* On the execution path (``app.execution.handlers`` stage handlers
  driven by the job runner), a real-stage exception — or a
  no-result/``None`` from the stage bridge — must surface to the
  runner's existing failure policy: the handler writes a ``failed``
  ``agent_runs`` row (with the exception summary) and raises
  ``app.execution.handlers.StageFailedError``; the runner then retries
  the job exactly once and, on the second failure, parks the project
  ``status='failed'`` with ``error_summary`` + a persisted/live
  ``project.failed`` event (``failure_policy.park_project_failed``).
  NO stub artefacts are written on this path.
* Stub substitution remains legitimate ONLY for jobs that explicitly
  select stub mode: ``payload["stub_mode"]`` truthy, OR
  ``project["stub_mode"]`` truthy, OR the project config (``config``
  dict / ``config_json``) carrying a truthy ``"stub_mode"`` /
  ``"offline"`` — see ``handlers._stub_mode_selected(project,
  payload)``.  Stub-selected jobs behave exactly as before: the result
  carries ``"stub": True``, the ``agent_runs`` row is ``succeeded``
  with the stub provenance in ``output_ref_json``, and the artefact
  uses the ``stub://`` path.
* The produce path (``app.integration.produce.produce_video``) is a
  separate entry point and is untouched by this policy: its offline
  deterministic-stub provenance keeps working exactly as before (also
  already proven by ``tests/integration/test_produce_glue.py::
  test_spec_to_result_shape`` and ``tests/integration/
  test_stage_bridge.py::test_produce_video_monolith_still_passes``;
  test (c) below pins it again from this file, self-contained).

Harness patterns follow ``tests/test_execution_failure_policy.py``
and ``tests/execution_fixtures.py``: a real tmp SQLite DB, the real
10-job DAG, the real handler registry + ``JobRunner``, with the
integration stage substituted by monkeypatching
``app.execution.integration_client.call_stage``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.execution import integration_client
from app.execution.dag import STAGE_KINDS, create_project_dag
from app.execution.handlers import build_registry
from app.execution.runner import JobRunner
from app.queue import SQLiteJobQueue
from tests.execution_fixtures import (
    RecordingHub,
    make_db,
    seed_channel_niche,
    seed_project,
)

try:  # R4-003 contract surface — landed by the implementation worker.
    from app.execution.handlers import StageFailedError
except Exception:  # impl not landed yet: tests below fail explicitly
    StageFailedError = None  # type: ignore[assignment]

SUCCESS = ("done", "succeeded")
BOOM = "r4003 boom: research provider exploded"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path):
    _db_path, factory = make_db(tmp_path)
    channel_id, niche_id = seed_channel_niche(factory)
    return {
        "factory": factory,
        "queue": SQLiteJobQueue(factory),
        "channel_id": channel_id,
        "niche_id": niche_id,
    }


def _seed_auto_project(env, *, status="in_production"):
    return seed_project(
        env["factory"], env["channel_id"], env["niche_id"],
        publish_mode="auto", status=status,
    )


def _project(factory, project_id):
    conn = factory()
    try:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _jobs(factory, project_id):
    conn = factory()
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE project_id = ? ORDER BY created_at, rowid",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _artefacts(factory, project_id):
    conn = factory()
    try:
        rows = conn.execute(
            "SELECT * FROM project_artefacts WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _agent_runs(factory, project_id, *, agent_key=None):
    conn = factory()
    try:
        if agent_key is not None:
            rows = conn.execute(
                "SELECT * FROM agent_runs WHERE project_id = ? "
                "AND agent_key = ? ORDER BY created_at, rowid",
                (project_id, agent_key),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agent_runs WHERE project_id = ? "
                "ORDER BY created_at, rowid",
                (project_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _event_log_rows(factory, event_type="project.failed"):
    conn = factory()
    try:
        rows = conn.execute(
            "SELECT * FROM event_log WHERE type = ?", (event_type,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _run_summary_text(run_row) -> str:
    """All summary-bearing text of an agent_runs row, concatenated."""
    parts = []
    for col in ("error", "output_ref_json", "decision_log", "input_ref_json"):
        value = run_row.get(col)
        if value:
            parts.append(str(value))
    return "\n".join(parts)


async def _wait_until(pred, timeout=10.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return pred()


def _run_runner_until(runner, pred, timeout=10.0):
    async def scenario():
        task = asyncio.create_task(runner.run())
        try:
            return await _wait_until(pred, timeout=timeout)
        finally:
            await runner.stop()
            await asyncio.wait_for(task, timeout=5)

    return asyncio.run(scenario())


def _make_runner(env, hub):
    registry = build_registry(env["factory"], events=hub)
    return JobRunner(
        env["factory"],
        registry,
        events=hub,
        poll_interval=0.02,
        error_backoff_base_ms=10,
        gate_requeue_delay_ms=100,
    )


def _select_stub_mode_on_dag_jobs(env, project_id):
    """Inject ``stub_mode=True`` into every DAG job's stored payload.

    ``create_project_dag`` builds payloads of exactly
    ``{project_id, stage}``; explicit stub selection (the R4-003
    contract's payload arm) is a payload key, so the stored
    ``payload_json`` is updated in place — the runner then claims jobs
    whose payload selects stub mode, exactly like a producer that
    enqueued them that way.
    """
    conn = env["factory"]()
    try:
        rows = conn.execute(
            "SELECT id, payload_json FROM jobs WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row[1] or "{}")
            payload["stub_mode"] = True
            conn.execute(
                "UPDATE jobs SET payload_json = ? WHERE id = ?",
                (json.dumps(payload), row[0]),
            )
        try:
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()


def _require_stage_failed_error():
    assert StageFailedError is not None, (
        "R4-003 contract: app.execution.handlers.StageFailedError "
        "does not exist — the implementation has not landed"
    )
    return StageFailedError


# ---------------------------------------------------------------------------
# Contract unit: stub-mode selection (_stub_mode_selected)
# ---------------------------------------------------------------------------

def test_stub_mode_selected_contract():
    from app.execution import handlers

    selector = getattr(handlers, "_stub_mode_selected", None)
    assert callable(selector), (
        "R4-003 contract: handlers._stub_mode_selected(project, "
        "payload) does not exist"
    )
    # Payload arm.
    assert selector({}, {"stub_mode": True}) is True
    assert selector({}, {"stub_mode": 1}) is True
    # Project arm.
    assert selector({"stub_mode": True}, {}) is True
    # Project config arms: config dict, config_json (JSON text).
    assert selector({"config": {"stub_mode": True}}, {}) is True
    assert selector({"config": {"offline": True}}, {}) is True
    assert selector({"config_json": '{"stub_mode": true}'}, {}) is True
    assert selector({"config_json": '{"offline": true}'}, {}) is True
    assert selector({"config_json": {"stub_mode": True}}, {}) is True
    assert selector({"config_json": {"offline": 1}}, {}) is True
    # Nothing selected -> not stub mode.
    assert not selector({}, {})
    assert not selector({"stub_mode": False}, {"stub_mode": False})
    assert not selector({"config": {"stub_mode": False}}, {})
    assert not selector({"config_json": '{"offline": false}'}, {})
    assert not selector({"topic": "x"}, {"stage": "research"})


# ---------------------------------------------------------------------------
# (a) Execution-path stage raises -> retry once -> parked failed,
#     no stub artefacts, failed agent_runs rows
# ---------------------------------------------------------------------------

def test_execution_stage_exception_retries_once_then_parks_failed(env, monkeypatch):
    _require_stage_failed_error()
    project_id = _seed_auto_project(env)
    ids = create_project_dag(env["queue"], project_id)
    assert len(ids) == 10

    calls = {"n": 0}

    def exploding_stage(stage, project, payload):
        calls["n"] += 1
        raise RuntimeError(BOOM)

    monkeypatch.setattr(integration_client, "call_stage", exploding_stage)

    hub = RecordingHub()
    runner = _make_runner(env, hub)

    def _parked():
        project = _project(env["factory"], project_id)
        return project is not None and project["status"] == "failed"

    assert _run_runner_until(runner, _parked, timeout=10.0)

    # Attempted twice total: one initial attempt + exactly one retry.
    assert calls["n"] == 2
    jobs = {j["kind"]: j for j in _jobs(env["factory"], project_id)}
    assert jobs["stage.research"]["status"] == "dead"
    assert jobs["stage.research"]["attempts"] == 2

    # Project parked failed with a non-empty error_summary naming the
    # failure; it did NOT advance to a completion state.
    project = _project(env["factory"], project_id)
    assert project["status"] == "failed"
    assert project["status"] not in ("ready", "in_production", "published")
    assert project["current_phase"] != "packaging"
    summary = project["error_summary"] or ""
    assert summary.strip()
    assert "research" in summary
    assert "research provider exploded" in summary

    # project.failed emitted live AND persisted in event_log.
    live = hub.by_type("project.failed")
    assert len(live) == 1
    assert live[0]["payload"]["project_id"] == project_id
    rows = _event_log_rows(env["factory"])
    assert len(rows) == 1
    envelope = json.loads(rows[0]["payload_json"])
    assert envelope["type"] == "project.failed"
    assert envelope["payload"]["project_id"] == project_id

    # NO stub artefacts — and no artefact at all for the failed stage
    # (research is the first stage, so no artefacts whatsoever).
    artefacts = _artefacts(env["factory"], project_id)
    assert artefacts == []
    assert not any(
        str(a["file_path"]).startswith("stub://") for a in artefacts
    )

    # agent_runs: one 'failed' row per attempt (2), none 'succeeded',
    # each carrying the exception summary.
    runs = _agent_runs(env["factory"], project_id, agent_key="stage.research")
    assert len(runs) == 2, runs
    assert all(r["status"] == "failed" for r in runs)
    assert not any(r["status"] == "succeeded" for r in runs)
    for run in runs:
        assert "research provider exploded" in _run_summary_text(run)
    assert sorted(int(r["attempt"]) for r in runs) == [1, 2]

    # Downstream never succeeded; the chain is dead, not completed.
    for kind in STAGE_KINDS:
        if kind == "stage.research":
            continue
        assert jobs[kind]["status"] == "dead", (kind, jobs[kind]["status"])
    assert env["queue"].claim("late-worker", kinds=tuple(STAGE_KINDS)) is None


def test_handler_stage_exception_raises_stage_failed_and_writes_failed_run(
    env, monkeypatch, caplog
):
    """Handler level (single attempt): an integration exception is
    raised as StageFailedError after the failed agent_runs row is
    written — never converted into a stub success."""
    exc_type = _require_stage_failed_error()
    project_id = _seed_auto_project(env, status="draft")
    ids = create_project_dag(env["queue"], project_id)
    job = env["queue"].get(ids[0])
    assert job["kind"] == "stage.research"

    def exploding_stage(stage, project, payload):
        raise RuntimeError(BOOM)

    monkeypatch.setattr(integration_client, "call_stage", exploding_stage)
    # Alembic's fileConfig can disable this logger process-wide when
    # sibling suites ran first (see test_execution_handlers.py).
    monkeypatch.setattr(
        logging.getLogger("app.execution.handlers"), "disabled", False
    )

    registry = build_registry(env["factory"])
    with caplog.at_level(logging.ERROR, logger="app.execution.handlers"):
        with pytest.raises(exc_type) as excinfo:
            asyncio.run(registry["stage.research"](job))
    assert "research provider exploded" in str(excinfo.value)

    runs = _agent_runs(env["factory"], project_id, agent_key="stage.research")
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert "research provider exploded" in _run_summary_text(runs[0])
    assert _artefacts(env["factory"], project_id) == []
    project = _project(env["factory"], project_id)
    assert project["status"] == "draft"  # not advanced by a failed stage
    assert project["current_phase"] is None


# ---------------------------------------------------------------------------
# Focused unit: stage returns None WITHOUT stub mode -> same failure
# at handler level, and the None path is LOGGED (the old code logged
# nothing there)
# ---------------------------------------------------------------------------

def test_handler_stage_none_without_stub_mode_fails_and_is_logged(
    env, monkeypatch, caplog
):
    exc_type = _require_stage_failed_error()
    project_id = _seed_auto_project(env, status="draft")
    ids = create_project_dag(env["queue"], project_id)
    job = env["queue"].get(ids[0])
    assert job["kind"] == "stage.research"
    assert not (job.get("payload") or {}).get("stub_mode")

    monkeypatch.setattr(
        integration_client, "call_stage", lambda stage, project, payload: None
    )
    monkeypatch.setattr(
        logging.getLogger("app.execution.handlers"), "disabled", False
    )

    registry = build_registry(env["factory"])
    with caplog.at_level(logging.DEBUG, logger="app.execution.handlers"):
        with pytest.raises(exc_type):
            asyncio.run(registry["stage.research"](job))

    # Failed run recorded (not a succeeded stub run), no artefacts.
    runs = _agent_runs(env["factory"], project_id, agent_key="stage.research")
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert not any(r["status"] == "succeeded" for r in runs)
    assert _artefacts(env["factory"], project_id) == []
    project = _project(env["factory"], project_id)
    assert project["status"] == "draft"
    assert project["current_phase"] is None

    # Something was LOGGED for the None path (WARNING or above, naming
    # the stage / project) — the old code logged nothing here.
    noisy = [
        r for r in caplog.records
        if r.name == "app.execution.handlers" and r.levelno >= logging.WARNING
    ]
    assert noisy, (
        "R4-003: the no-result/None path without stub mode must be "
        "logged, not silent"
    )
    assert "research" in caplog.text


# ---------------------------------------------------------------------------
# (b) Explicit stub mode: job completes with stub provenance, exactly
#     as before — both for a None stage result (runner, full DAG) and
#     for a raising stage (handler level)
# ---------------------------------------------------------------------------

def test_explicit_stub_mode_execution_completes_with_stub_provenance(
    env, monkeypatch
):
    project_id = _seed_auto_project(env)
    ids = create_project_dag(env["queue"], project_id)
    _select_stub_mode_on_dag_jobs(env, project_id)

    # Integration stage absent / returns no result.
    monkeypatch.setattr(
        integration_client, "call_stage", lambda stage, project, payload: None
    )

    hub = RecordingHub()
    runner = _make_runner(env, hub)

    def _all_done():
        jobs = _jobs(env["factory"], project_id)
        return len(jobs) == 10 and all(j["status"] in SUCCESS for j in jobs)

    assert _run_runner_until(runner, _all_done, timeout=10.0)

    project = _project(env["factory"], project_id)
    assert project["status"] != "failed"
    assert hub.by_type("project.failed") == []
    assert _event_log_rows(env["factory"]) == []

    # The research job COMPLETED and its stored result is the stub.
    research_job = env["queue"].get(ids[0])
    result = research_job["result"]
    assert isinstance(result, dict)
    assert result["stub"] is True
    assert result["stage"] == "research"

    # agent_runs 'succeeded' row records the stub provenance.
    runs = _agent_runs(env["factory"], project_id, agent_key="stage.research")
    assert len(runs) == 1
    assert runs[0]["status"] == "succeeded"
    output_ref = runs[0]["output_ref_json"] or ""
    assert "stub" in output_ref.lower()
    parsed = json.loads(output_ref)
    assert parsed.get("stub") is True

    # Artefact uses the stub:// path, as before.
    artefacts = _artefacts(env["factory"], project_id)
    research_arts = [a for a in artefacts if a["artefact_type"] == "research_brief"]
    assert len(research_arts) == 1
    assert str(research_arts[0]["file_path"]).startswith("stub://")


def test_explicit_stub_mode_handler_still_falls_back_on_exception(
    env, monkeypatch
):
    """Stub-selected jobs behave exactly as before (contract): even a
    raising real stage degrades to the stub result instead of raising
    StageFailedError."""
    project_id = _seed_auto_project(env, status="draft")
    ids = create_project_dag(env["queue"], project_id)
    job = env["queue"].get(ids[0])
    job["payload"] = dict(job.get("payload") or {})
    job["payload"]["stub_mode"] = True

    def exploding_stage(stage, project, payload):
        raise RuntimeError(BOOM)

    monkeypatch.setattr(integration_client, "call_stage", exploding_stage)

    registry = build_registry(env["factory"])
    result = asyncio.run(registry["stage.research"](job))
    assert result["stub"] is True
    assert result["stage"] == "research"
    runs = _agent_runs(env["factory"], project_id, agent_key="stage.research")
    assert len(runs) == 1 and runs[0]["status"] == "succeeded"
    artefacts = _artefacts(env["factory"], project_id)
    assert len(artefacts) == 1
    assert str(artefacts[0]["file_path"]).startswith("stub://")


# ---------------------------------------------------------------------------
# (c) Produce path untouched: the offline produce flow still completes
#     with its deterministic-stub provenance.  Self-contained version
#     of the pattern in tests/integration/test_produce_glue.py (whose
#     stage_fakes fixture is scoped to that directory): every produce
#     stage is faked with a contract-shaped result, so this runs with
#     no network / TTS / ffmpeg.  Existing proof of the same thing:
#     test_produce_glue.py::test_spec_to_result_shape and
#     test_stage_bridge.py::test_produce_video_monolith_still_passes.
# ---------------------------------------------------------------------------

def _produce_fakes(tmp_path):
    bundle = SimpleNamespace(
        title="Test Title: Ocean Secrets",
        hook="A hook that is definitely long enough to matter.",
        chapters=[SimpleNamespace(index=1, title="Hook", text="Chapter text.")],
        script_text="Chapter text.",
        narration_text="Chapter text.",
        thumbnail_text="Ocean Secrets",
        description="A test description.",
        tags=["test", "ocean"],
        claims=[],
    )
    voice = SimpleNamespace(
        narration_wav="/nonexistent/narration.wav",
        duration_s=55.0,
        path_used="stub",
        voice_id="stub-voice",
        chapter_files=[],
        music_wav=None,
        mixed_master_wav="/nonexistent/master.wav",
        ledger_rows=[{
            "asset_id": "voice-1", "scene_id": "all",
            "source": "own/generated: stub voice", "licence": "own work",
            "kind": "voice", "local_path": "/nonexistent/narration.wav",
            "notes": "stub",
        }],
    )
    visuals = SimpleNamespace(
        scene_manifest=[],
        asset_files=[],
        ledger_rows=[{
            "asset_id": "plate-1", "scene_id": "1",
            "source": "own/generated: PIL plate", "licence": "own work",
            "kind": "image", "local_path": "/nonexistent/plate.png",
            "notes": "",
        }],
        total_duration_s=55.0,
    )
    final = tmp_path / "final.mp4"
    final.write_bytes(b"fake-mp4")
    srt = tmp_path / "captions.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8"
    )
    thumbs = []
    for i in range(3):
        thumb = tmp_path / f"thumbnail_{i + 1}.png"
        thumb.write_bytes(b"fake-png")
        thumbs.append(str(thumb))
    assemble = SimpleNamespace(
        final_mp4=str(final),
        duration_s=55.0,
        captions_srt=str(srt),
        subtitle_path_used="whisper-stub",
        thumbnails=thumbs,
        qc_passed=True,
        qc_summary="all good",
        loudness_lufs=-14.2,
        render_path_used="ffmpeg-local",
        ledger_rows=[],
    )
    publish = SimpleNamespace(
        gate_b_passed=True,
        manifest={"video_id": "fake-1", "state": "ready"},
        manifest_path=None,
        publish_state="ready_for_review",
        tokens=[],
    )
    return SimpleNamespace(
        bundle=bundle, voice=voice, visuals=visuals,
        assemble=assemble, publish=publish,
    )


def test_produce_path_stub_provenance_unaffected(monkeypatch, tmp_path):
    from app.integration import produce
    from app.integration.models import ProjectSpec

    fakes = _produce_fakes(tmp_path)
    monkeypatch.setattr(produce, "build_content", lambda *a: fakes.bundle)
    monkeypatch.setattr(produce, "run_gate_a", lambda *a: (True, []))
    monkeypatch.setattr(produce, "synthesize_voice", lambda *a: fakes.voice)
    monkeypatch.setattr(
        produce, "plan_and_generate_visuals", lambda *a: fakes.visuals
    )
    monkeypatch.setattr(produce, "assemble_project", lambda *a: fakes.assemble)
    monkeypatch.setattr(produce, "finalize_publish", lambda *a: fakes.publish)

    result = produce.produce_video(
        ProjectSpec(niche="facts", topic="Ocean depth secrets", minutes=1.0),
        tmp_path / "out",
    )

    # The offline produce flow completes end-to-end with its own
    # deterministic-stub provenance — no execution stub_mode selection
    # is involved (or needed) on this path.
    assert result.final_mp4 == fakes.assemble.final_mp4
    assert result.gate_a_passed is True
    assert result.gate_b_passed is True
    assert result.publish_state == "ready_for_review"
    assert result.paths_used["llm"] == "deterministic-stub"
    assert result.paths_used["tts"] == "stub"
    assert result.paths_used["publish"] == "fake-provider"
    assert Path(result.licence_ledger_csv).is_file()
    assert Path(result.report_md).is_file()

    # Thin guard: the execution-path stub_mode payload key has no
    # produce-path counterpart — ProjectSpec does not even accept it,
    # so execution payload keys cannot leak into a produce run.
    assert "stub_mode" not in ProjectSpec.model_fields
