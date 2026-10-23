"""Budget reservation release on permanent failure (Round 4 fix pass).

Before this fix, ``budget_gate.release_budget_for_project`` was wired
ONLY into ``pipeline.cancel`` (``ExecutionService._release_budget``,
``reason='pipeline_cancelled'``), so a run that *permanently failed*
— DAG job dead-lettered after its one retry was spent and the
project parked ``failed`` by ``failure_policy.park_project_failed``
— kept its Budget Guard reservation until the Pacific-day bucket
rolled over.

The fix wires the same release delegate into
``JobRunner``'s permanent-failure paths:

* ``JobRunner._run_job`` — after ``queue.fail`` returns ``'dead'``
  (retries exhausted) and the project is parked ``failed``;
* ``JobRunner.run`` startup recovery — for every project
  ``failure_policy.recover_and_park`` parks from the lease sweep.

Pinned semantics here:

* transient (first) failure -> reservation RETAINED into the retry;
* permanent failure -> reservation released immediately
  (``release_reason='run_failed'``), unsettled remainder freed, and
  releasing a second time is a safe no-op (store idempotency);
* PAUSE retains the reservation by design: ``pipeline.pause`` (and
  the runner's global ``pause()``) never release — only cancel did,
  and now permanent failure does too.
"""
from __future__ import annotations

import asyncio
import time

from app.execution import budget_gate, budget_reservations
from app.execution.dag import STAGE_KINDS, create_project_dag
from app.execution.runner import JobRunner
from app.queue import SQLiteJobQueue
from tests.execution_fixtures import (
    RecordingHub,
    make_db,
    seed_channel_niche,
    seed_project,
)


# ---------------------------------------------------------------------------
# Helpers (modelled on test_budget_guard_full / test_execution_failure_policy)
# ---------------------------------------------------------------------------


def _env(tmp_path):
    _, conn_factory = make_db(tmp_path)
    channel_id, niche_id = seed_channel_niche(conn_factory)
    return conn_factory, channel_id, niche_id


def _run_control(conn_factory, project_id, action="start", hub=None):
    from app.execution.service import ExecutionService

    service = ExecutionService(conn_factory, hub=hub)
    handler = service.registry[f"pipeline.{action}"]
    job = {
        "kind": f"pipeline.{action}",
        "project_id": project_id,
        "payload": {"project_id": project_id},
    }
    return asyncio.run(handler(job))


def _project_row(conn_factory, project_id):
    conn = conn_factory()
    try:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _daily(conn_factory):
    return budget_gate.bucket_committed_micros(conn_factory, "daily")


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


def _chain_registry(calls, overrides=None):
    registry = {}
    for kind in STAGE_KINDS:
        if overrides and kind in overrides:
            registry[kind] = overrides[kind]
            continue

        async def handler(job, _kind=kind):
            calls[_kind] = calls.get(_kind, 0) + 1
            return {"ok": True, "kind": _kind}

        registry[kind] = handler
    return registry


def _make_runner(factory, registry, hub):
    return JobRunner(
        factory,
        registry,
        events=hub,
        poll_interval=0.02,
        error_backoff_base_ms=10,
        gate_requeue_delay_ms=100,
    )


# ---------------------------------------------------------------------------
# Permanent failure frees the reservation immediately
# ---------------------------------------------------------------------------


def test_permanent_failure_releases_reservation_immediately(tmp_path):
    conn_factory, channel_id, niche_id = _env(tmp_path)
    project_id = seed_project(
        conn_factory, channel_id, niche_id, topic="fails permanently"
    )

    # Start through the public seam: DAG created + reservation live.
    started = _run_control(conn_factory, project_id, hub=RecordingHub())
    assert started["dag"] == "created"
    reservation = budget_reservations.get_active_reservation(
        conn_factory, project_id
    )
    assert reservation is not None
    reserved = reservation["reserved_micros"]
    assert reserved == budget_gate.DAG_RESERVED_ESTIMATE_MICROS > 0
    assert _daily(conn_factory) == reserved

    calls: dict[str, int] = {}
    active_at_attempt: list[bool] = []

    async def always_fail_voice(job):
        calls["stage.voice"] = calls.get("stage.voice", 0) + 1
        # Was the reservation still held when this attempt began?
        # Attempt 2 only runs if the transient first failure did NOT
        # release it.
        active_at_attempt.append(
            budget_reservations.get_active_reservation(
                conn_factory, project_id
            )
            is not None
        )
        raise RuntimeError("tts exploded")

    hub = RecordingHub()
    runner = _make_runner(
        conn_factory,
        _chain_registry(calls, {"stage.voice": always_fail_voice}),
        hub,
    )

    def _parked():
        project = _project_row(conn_factory, project_id)
        return project is not None and project["status"] == "failed"

    assert _run_runner_until(runner, _parked)

    # Exactly one retry happened, and the reservation survived the
    # transient first failure into that retry...
    assert calls["stage.voice"] == 2
    assert active_at_attempt == [True, True]
    assert _project_row(conn_factory, project_id)["status"] == "failed"

    # ...but the permanent (second) failure released it immediately:
    # no active reservation, the single row is released with the
    # failure reason, and its unsettled remainder no longer commits
    # anything to today's bucket.
    assert (
        budget_reservations.get_active_reservation(conn_factory, project_id)
        is None
    )
    rows = budget_reservations.list_reservations(
        conn_factory, project_id=project_id
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "released"
    assert rows[0]["release_reason"] == "run_failed"
    assert rows[0]["settled_micros"] == 0
    assert rows[0]["remaining_micros"] == 0
    assert rows[0]["committed_micros"] == 0
    assert _daily(conn_factory) == 0

    # Releasing twice is safe (mirrors pipeline.cancel semantics):
    # no active reservation -> no-op, row unchanged.
    assert (
        budget_gate.release_budget_for_project(
            conn_factory, project_id, reason="run_failed"
        )
        is None
    )
    rows = budget_reservations.list_reservations(
        conn_factory, project_id=project_id
    )
    assert len(rows) == 1 and rows[0]["status"] == "released"


def test_restart_recovery_park_releases_reservation(tmp_path):
    """Both attempts burned by crashed workers: the runner's startup
    sweep dead-letters + parks the project, and that permanent
    failure frees the reservation without any handler running."""
    conn_factory, channel_id, niche_id = _env(tmp_path)
    project_id = seed_project(
        conn_factory, channel_id, niche_id,
        topic="crash recovery", status="in_production",
    )
    queue = SQLiteJobQueue(conn_factory)
    create_project_dag(queue, project_id)
    budget_reservations.reserve(conn_factory, project_id, channel_id, 60_000)
    assert (
        budget_reservations.get_active_reservation(conn_factory, project_id)
        is not None
    )

    for owner in ("crashed-1", "crashed-2"):
        claimed = queue.claim(owner, kinds=("stage.research",))
        assert claimed is not None
        conn = conn_factory()
        try:
            conn.execute(
                "UPDATE jobs SET lease_expires_at = 1 WHERE id = ?",
                (claimed["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        if owner == "crashed-1":
            assert queue.sweep_expired_leases() == 1  # requeue: attempt 1 < 2
    assert queue.get(claimed["id"])["attempts"] == 2

    calls: dict[str, int] = {}
    runner = _make_runner(
        conn_factory, _chain_registry(calls), RecordingHub()
    )

    def _parked():
        project = _project_row(conn_factory, project_id)
        return project is not None and project["status"] == "failed"

    assert _run_runner_until(runner, _parked)
    assert calls.get("stage.research", 0) == 0  # never executed again
    assert (
        budget_reservations.get_active_reservation(conn_factory, project_id)
        is None
    )
    rows = budget_reservations.list_reservations(
        conn_factory, project_id=project_id
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "released"
    assert rows[0]["release_reason"] == "run_failed"


# ---------------------------------------------------------------------------
# Pause retains the reservation (by design)
# ---------------------------------------------------------------------------


def test_pipeline_pause_retains_reservation(tmp_path):
    conn_factory, channel_id, niche_id = _env(tmp_path)
    project_id = seed_project(
        conn_factory, channel_id, niche_id, topic="pause me"
    )

    started = _run_control(conn_factory, project_id, hub=RecordingHub())
    assert started["dag"] == "created"
    reservation = budget_reservations.get_active_reservation(
        conn_factory, project_id
    )
    assert reservation is not None
    reserved = reservation["reserved_micros"]
    assert _daily(conn_factory) == reserved

    paused = _run_control(
        conn_factory, project_id, action="pause", hub=RecordingHub()
    )
    assert paused["ok"] is True and paused["action"] == "pause"

    # Pause released nothing: the reservation is still active with
    # its full remainder committed, and the project was not failed.
    still = budget_reservations.get_active_reservation(
        conn_factory, project_id
    )
    assert still is not None
    assert still["id"] == reservation["id"]
    assert still["status"] == "active"
    assert still["remaining_micros"] == reserved
    assert _daily(conn_factory) == reserved
    assert _project_row(conn_factory, project_id)["status"] != "failed"

    # The runner-level global pause is likewise flag-only.
    runner = JobRunner(conn_factory, {}, events=RecordingHub())
    runner.pause()
    try:
        assert (
            budget_reservations.get_active_reservation(
                conn_factory, project_id
            )
            is not None
        )
    finally:
        runner.resume()
