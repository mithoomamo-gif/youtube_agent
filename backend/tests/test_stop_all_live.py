"""STOP ALL kill-switch spot verification (Round 4, Worker 4 — test-only).

The roadmap promise under test: ``execution.stop_all`` (see
``app.execution.flags``) is the app's kill switch — it must halt a
*live* production run mid-flight, not merely prevent new starts.

Level of the test: the highest practical one available in the
backend — a real 10-job project DAG (``create_project_dag``) plus a
``stage.publish`` sentinel job chained behind ``gate.b`` (the
execution DAG itself ends at Gate B; ``stage.publish`` is the
recognised upload-pool kind in ``app.execution.pools.KIND_TO_POOL``),
executed by the real :class:`JobRunner` with the real handler
registry (``build_registry`` — deterministic stub stages, no network,
no media).  One stage (``stage.script``) is replaced via the same
registry seam the existing runner tests use, with a deliberately slow
fake that would run for ``SLOW_STAGE_S`` seconds if uninterrupted.

Repair note (Worker W4b, Round 4 verification): as originally
written, the "deterministic stub stages, no network" claim above was
no longer true after the stage-bridge landed — the real
``app.integration`` package exports ``run_voice`` / ``run_visuals`` /
…, so the handlers' real-integration-first path made live network
attempts (edge-TTS, stock downloads) whose latency (observed
~20 s–200 s per stage in this sandbox) blew the 10–20 s chain budgets
in the recovery phase and in the negative-control test, confounding
the kill-switch verdict with integration-stage latency.  The
``_stub_integration`` fixture below restores the documented design —
the same ``sys.modules`` fake-integration seam
``test_execution_handlers.py`` uses — so this file tests the runner /
flags / DAG (its subject), not provider network latency.  The halt
assertions themselves were never the failing part: in every
unrepaired run the failure was at the recovery/control chain wait,
never at a halt assertion.

STOP ALL is triggered through the real mechanism — the persisted
settings-table flag via ``app.execution.flags.set_flag`` (the same
store ``JobRunner.stop_all()`` writes and the runner re-reads every
poll; no HTTP router for execution flags exists in the backend) —
never by calling runner internals.

Documented semantics proved/observed here:

* Halt is *prompt task cancellation*: the in-flight handler receives
  ``asyncio.CancelledError`` and its job row lands in ``cancelled``;
  the runner loop exits.  It is not a graceful stage-boundary wait.
* Jobs downstream of the cancelled stage never start (their handlers
  are never invoked) — including the package/gate.b finalise steps
  and the publish sentinel.
* The halt is recorded at the *job* level.  The ``projects`` row is
  NOT advanced past the last completed stage and is NOT marked
  completed/published; its status stays ``in_production`` (the
  resume-able state — ``pipeline.resume`` requeues cancelled stage
  jobs).  No job is left ``leased``/``running``.
* Clearing the flag does not resurrect the halted runner (it has
  exited); a fresh runner + fresh project runs normally afterwards
  (negative control / recovery, test 3 phases and test 2).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import types

import pytest

from app import db as db_module
from app.execution.dag import STAGE_KINDS, create_project_dag
from app.execution.flags import read_flags, set_flag
from app.execution.handlers import build_registry
from app.execution.runner import JobRunner
from app.queue import SQLiteJobQueue
from tests.execution_fixtures import (
    RecordingHub,
    make_db,
    seed_channel_niche,
    seed_project,
)

SLOW_STAGE_S = 60.0          # uninterrupted, the slow stage takes a minute
HALT_BUDGET_S = 10.0         # runner must be dead well within this
SUCCESS = ("done", "succeeded")
DOWNSTREAM_OF_SCRIPT = [k for k in STAGE_KINDS if k not in ("stage.research", "stage.script")]


@pytest.fixture(autouse=True)
def _stub_integration(monkeypatch):
    """Isolate the integration seam: handlers fall back to their
    deterministic stubs (``integration_client.call_stage`` returns
    ``None`` for a module with no ``run_<stage>`` functions), exactly
    as in ``test_execution_handlers.py``.  Without this, the real
    stage-bridge stages make network attempts whose latency — not
    the kill switch — decides whether the chain budgets expire."""
    fake = types.ModuleType("app.integration")
    monkeypatch.setitem(sys.modules, "app.integration", fake)
    yield


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

async def wait_until(pred, timeout=10.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return pred()


def _status(queue, job_id):
    job = queue.get(job_id)
    return job["status"] if job else None


def _project(factory, project_id):
    conn = factory()
    try:
        conn.row_factory = __import__("sqlite3").Row
        row = conn.execute(
            "SELECT status, current_phase FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _seed_auto_project(factory):
    channel_id, niche_id = seed_channel_niche(factory)
    return seed_project(factory, channel_id, niche_id, publish_mode="auto")


def _select_stub_mode(factory, job_ids):
    """Inject ``stub_mode=True`` into the stored payload of each job.

    R4-003: the new stage-failure policy prohibits silent stub
    substitution on the execution path — with the fake integration
    above returning None for every stage, the DAG would now fail at
    research (StageFailedError) instead of running on the
    deterministic stubs this file's kill-switch assertions are
    about.  These jobs therefore select stub mode explicitly via
    their payloads (assertions unchanged).
    """
    conn = factory()
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


def _enqueue_dag_with_publish(factory, queue, project_id):
    """Real 10-job DAG + a stage.publish sentinel behind gate.b."""
    job_ids = create_project_dag(queue, project_id)
    assert len(job_ids) == len(STAGE_KINDS)
    _select_stub_mode(factory, job_ids)  # R4-003: see helper above
    publish_id = queue.enqueue(
        "stage.publish",
        {"project_id": project_id, "stage": "publish"},
        depends_on=job_ids[-1],
        idempotency_key=f"dag:{project_id}:stage.publish",
        project_id=project_id,
    )
    return job_ids, publish_id


def _recording_registry(factory, hub, started, *, script_handler=None, publish_calls=None):
    """Real registry with every handler wrapped to record invocations.

    ``stage.script`` / ``stage.publish`` may be substituted outright
    (the slow fake / the publish sentinel).
    """
    registry = build_registry(factory, events=hub)
    for kind in list(registry):
        real = registry[kind]

        async def wrapper(job, _kind=kind, _real=real):
            started.append(_kind)
            return await _real(job)

        registry[kind] = wrapper
    if script_handler is not None:
        registry["stage.script"] = script_handler

    async def publish_sentinel(job):
        if publish_calls is not None:
            publish_calls.append(job["id"])
        return {"stage": "publish", "sentinel": True, "uploaded": False}

    registry["stage.publish"] = publish_sentinel
    return registry


# ----------------------------------------------------------------------
# 1) STOP ALL halts a live DAG mid-flight; nothing downstream runs
# ----------------------------------------------------------------------

def test_stop_all_halts_live_dag_and_blocks_publish(tmp_path):
    _db_path, factory = make_db(tmp_path)

    async def scenario():
        queue = SQLiteJobQueue(factory)
        hub = RecordingHub()
        project_id = _seed_auto_project(factory)
        job_ids, publish_id = _enqueue_dag_with_publish(factory, queue, project_id)
        by_kind = dict(zip(STAGE_KINDS, job_ids))

        started: list[str] = []
        publish_calls: list[str] = []
        script_state = {"started": False, "cancelled": False}

        async def slow_script(job):
            script_state["started"] = True
            started.append("stage.script")
            try:
                await asyncio.sleep(SLOW_STAGE_S)
            except asyncio.CancelledError:
                script_state["cancelled"] = True
                raise
            return {"stage": "script", "never": "uninterrupted"}

        registry = _recording_registry(
            factory, hub, started,
            script_handler=slow_script, publish_calls=publish_calls,
        )
        runner = JobRunner(factory, registry, events=hub, poll_interval=0.02)
        task = asyncio.create_task(runner.run())
        try:
            # Mid-flight proof: research really completed, script is
            # demonstrably in progress inside the slow stage.
            assert await wait_until(lambda: script_state["started"], timeout=10)
            assert _status(queue, by_kind["stage.research"]) in SUCCESS
            assert _status(queue, by_kind["stage.script"]) == "running"
            assert runner.inflight_count >= 1
            proj = _project(factory, project_id)
            assert proj["status"] == "in_production"  # research advanced it

            # === Trigger STOP ALL via the real persisted flag ===
            t0 = time.monotonic()
            set_flag(factory, "stop_all", True)
            assert read_flags(factory).stop_all is True
            await asyncio.wait_for(task, timeout=HALT_BUDGET_S)
            halt_s = time.monotonic() - t0

            # Halted promptly: runner loop exited and the in-flight
            # stage was cancelled, a small fraction of the 60 s stage.
            assert halt_s < HALT_BUDGET_S
            assert script_state["cancelled"] is True
            assert runner.is_running is False

            # Persisted state: in-flight job terminal-cancelled, no job
            # left running/leased anywhere in the chain.
            assert _status(queue, by_kind["stage.script"]) == "cancelled"
            all_ids = job_ids + [publish_id]
            assert all(
                _status(queue, j) not in ("running", "leased") for j in all_ids
            )

            # No further stage started after the flag — and even after
            # a grace window, nothing downstream (finalise/package,
            # gate.b, publish/upload sentinel) ever executes.
            await asyncio.sleep(0.5)
            assert [s for s in started if s in DOWNSTREAM_OF_SCRIPT] == []
            assert publish_calls == []
            assert _status(queue, publish_id) not in SUCCESS
            for kind in DOWNSTREAM_OF_SCRIPT:
                assert _status(queue, by_kind[kind]) not in SUCCESS

            # Project row reflects the halt honestly: not advanced past
            # research, never marked complete/published.
            proj = _project(factory, project_id)
            assert proj["current_phase"] == "research"
            assert proj["status"] not in ("completed", "published", "ready")
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            await runner.stop()

        # --- Recovery phase: clearing the flag lets a FRESH runner run
        # a FRESH project end-to-end (the halted runner stays dead —
        # semantics documented in the module docstring).
        set_flag(factory, "stop_all", False)
        assert read_flags(factory).stop_all is False

        project2 = _seed_auto_project(factory)
        job_ids2, publish_id2 = _enqueue_dag_with_publish(factory, queue, project2)
        started2: list[str] = []
        publish_calls2: list[str] = []
        registry2 = _recording_registry(
            factory, hub, started2, publish_calls=publish_calls2
        )
        runner2 = JobRunner(factory, registry2, events=hub, poll_interval=0.02)
        task2 = asyncio.create_task(runner2.run())
        try:
            finished = await wait_until(
                lambda: _status(queue, publish_id2) in SUCCESS, timeout=15
            )
            assert finished, (
                "recovery chain did not finish in 15s: "
                f"{[(k, _status(queue, j)) for k, j in zip(STAGE_KINDS, job_ids2)]}"
                f" publish={_status(queue, publish_id2)}"
            )
            assert all(_status(queue, j) in SUCCESS for j in job_ids2)
            assert publish_calls2 == [publish_id2]
        finally:
            await runner2.stop()
            if not task2.done():
                task2.cancel()

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 2) Negative control: the SAME slow-script run, WITHOUT stop_all,
#    progresses past the halt point and reaches the publish sentinel.
# ----------------------------------------------------------------------

def test_without_stop_all_same_run_progresses_to_publish(tmp_path):
    _db_path, factory = make_db(tmp_path)

    async def scenario():
        queue = SQLiteJobQueue(factory)
        hub = RecordingHub()
        project_id = _seed_auto_project(factory)
        job_ids, publish_id = _enqueue_dag_with_publish(factory, queue, project_id)

        started: list[str] = []
        publish_calls: list[str] = []

        async def slowish_script(job):
            started.append("stage.script")
            await asyncio.sleep(1.0)  # slow, but finite — no flag set
            return {"stage": "script", "stub": True, "artefact": {"stage": "script"}}

        registry = _recording_registry(
            factory, hub, started,
            script_handler=slowish_script, publish_calls=publish_calls,
        )
        runner = JobRunner(factory, registry, events=hub, poll_interval=0.02)
        task = asyncio.create_task(runner.run())
        try:
            finished = await wait_until(
                lambda: _status(queue, publish_id) in SUCCESS, timeout=20
            )
            assert finished, (
                "control chain did not finish in 20s: "
                f"{[(k, _status(queue, j)) for k, j in zip(STAGE_KINDS, job_ids)]}"
                f" publish={_status(queue, publish_id)}"
            )
            # Progressed past exactly where test 1 was halted:
            # every downstream stage ran, in chain order, and the
            # publish sentinel executed exactly once.
            assert all(_status(queue, j) in SUCCESS for j in job_ids)
            assert publish_calls == [publish_id]
            for kind in DOWNSTREAM_OF_SCRIPT:
                assert kind in started
            assert read_flags(factory).stop_all is False
        finally:
            await runner.stop()
            if not task.done():
                task.cancel()

    asyncio.run(scenario())
