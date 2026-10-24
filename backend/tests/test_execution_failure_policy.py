"""Fault-injection tests for the Round-4 failure & retry policy.

Covers ``app.execution.failure_policy`` + its wiring in
``app.execution.runner.JobRunner`` and ``app.execution.dag``:

(a) a stage fails once -> retried -> succeeds -> the chain completes;
(b) a stage fails twice -> project parked ``failed`` with
    ``error_summary``, ``project.failed`` persisted (``event_log``)
    AND emitted live, downstream jobs dead / not claimable, and no
    third attempt ever happens;
(c) gate parking (``GatePending`` — the same exception budget-pending
    parks raise) does not consume the retry budget;
(d) scheduler regression: catch_up_once still collapses 5 missed
    slots into exactly 1 project, and a failed slot is recorded once
    and never re-materialises;
(e) restart recovery: persisted attempt counts survive a restart —
    the sweep + retry budget cannot loop.

Each test uses a real tmp SQLite DB (``app.db.init_db``) and drives the
async runner with ``asyncio.run`` like the other execution tests.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.execution import failure_policy
from app.execution.dag import STAGE_KINDS, create_project_dag
from app.execution.runner import GatePending, JobRunner
from app.execution.scheduler import SchedulerTick
from app.queue import SQLiteJobQueue
from tests.execution_fixtures import (
    RecordingHub,
    make_db,
    seed_channel_niche,
    seed_project,
)

SUCCESS = ("done", "succeeded")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path):
    _db_path, factory = make_db(tmp_path)
    channel_id, niche_id = seed_channel_niche(factory)
    project_id = seed_project(
        factory, channel_id, niche_id, publish_mode="auto", status="in_production"
    )
    return {
        "factory": factory,
        "queue": SQLiteJobQueue(factory),
        "project_id": project_id,
        "channel_id": channel_id,
        "niche_id": niche_id,
    }


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


def _event_log_rows(factory, event_type="project.failed"):
    conn = factory()
    try:
        rows = conn.execute(
            "SELECT * FROM event_log WHERE type = ?", (event_type,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


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
    """Registry for all 10 DAG kinds; every handler succeeds unless an
    override (keyed by kind) supplies different behaviour.  ``calls``
    counts invocations per kind."""
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


def _make_runner(env, registry, hub):
    return JobRunner(
        env["factory"],
        registry,
        events=hub,
        poll_interval=0.02,
        error_backoff_base_ms=10,
        gate_requeue_delay_ms=100,
    )


# ---------------------------------------------------------------------------
# policy units: budget + classification
# ---------------------------------------------------------------------------

def test_dag_jobs_carry_retry_once_budget(env):
    ids = create_project_dag(env["queue"], env["project_id"])
    assert len(ids) == 10
    for job_id in ids:
        job = env["queue"].get(job_id)
        assert job["max_attempts"] == failure_policy.STAGE_MAX_ATTEMPTS == 2


def test_retryable_for_caps_dag_jobs_at_one_retry(env):
    job = {"kind": "stage.script", "attempts": 1}
    assert failure_policy.retryable_for(job, ValueError("x")) is True
    job = {"kind": "stage.script", "attempts": 2}
    assert failure_policy.retryable_for(job, ValueError("x")) is False
    # Legacy row with headroom in max_attempts is still capped.
    job = {"kind": "stage.script", "attempts": 2, "max_attempts": 3}
    assert failure_policy.retryable_for(job, ValueError("x")) is False
    # Non-DAG kinds defer to queue semantics.
    job = {"kind": "pipeline.start", "attempts": 2}
    assert failure_policy.retryable_for(job, ValueError("x")) is True


def test_permanent_failure_is_gate_rejection_only():
    class FakeRejection(Exception):
        error_code = "gate_rejected"

    assert failure_policy.is_permanent_failure(FakeRejection("no")) is True
    assert failure_policy.retryable_for(
        {"kind": "gate.a", "attempts": 1}, FakeRejection("no")
    ) is False
    assert failure_policy.is_permanent_failure(ValueError("x")) is False


# ---------------------------------------------------------------------------
# (a) fail once -> retry -> succeed -> chain completes
# ---------------------------------------------------------------------------

def test_stage_fails_once_retried_then_chain_completes(env):
    project_id = env["project_id"]
    create_project_dag(env["queue"], project_id)
    calls: dict[str, int] = {}

    async def flaky_script(job):
        calls["stage.script"] = calls.get("stage.script", 0) + 1
        if calls["stage.script"] == 1:
            raise ValueError("transient boom")
        return {"ok": True, "kind": "stage.script"}

    hub = RecordingHub()
    runner = _make_runner(
        env, _chain_registry(calls, {"stage.script": flaky_script}), hub
    )

    def _all_done():
        jobs = _jobs(env["factory"], project_id)
        return len(jobs) == 10 and all(j["status"] in SUCCESS for j in jobs)

    assert _run_runner_until(runner, _all_done)
    assert calls["stage.script"] == 2  # exactly one retry, then success
    script_job = next(
        j for j in _jobs(env["factory"], project_id) if j["kind"] == "stage.script"
    )
    assert script_job["attempts"] == 2
    project = _project(env["factory"], project_id)
    assert project["status"] != "failed"
    assert project["error_summary"] is None
    assert hub.by_type("project.failed") == []
    assert _event_log_rows(env["factory"]) == []


# ---------------------------------------------------------------------------
# (b) fail twice -> parked terminally, event persisted + live, no 3rd try
# ---------------------------------------------------------------------------

def test_stage_fails_twice_parks_project_terminally(env):
    project_id = env["project_id"]
    ids = create_project_dag(env["queue"], project_id)
    calls: dict[str, int] = {}

    async def always_fail_voice(job):
        calls["stage.voice"] = calls.get("stage.voice", 0) + 1
        raise RuntimeError("tts exploded")

    hub = RecordingHub()
    runner = _make_runner(
        env, _chain_registry(calls, {"stage.voice": always_fail_voice}), hub
    )

    def _parked():
        project = _project(env["factory"], project_id)
        return project is not None and project["status"] == "failed"

    assert _run_runner_until(runner, _parked, timeout=10.0)

    # Exactly two attempts — and still two after a grace period (the
    # runner was stopped above; also assert no queued work remains that
    # a future runner could turn into a third attempt).
    assert calls["stage.voice"] == 2

    project = _project(env["factory"], project_id)
    assert project["status"] == "failed"
    summary = project["error_summary"] or ""
    assert "voice" in summary  # stage named
    assert "tts exploded" in summary  # error carried
    assert "2 attempt" in summary  # attempt count carried

    jobs = {j["kind"]: j for j in _jobs(env["factory"], project_id)}
    # Upstream finished; the failed job is dead with both attempts spent.
    for kind in ("stage.research", "stage.script", "gate.a"):
        assert jobs[kind]["status"] in SUCCESS
    assert jobs["stage.voice"]["status"] == "dead"
    assert jobs["stage.voice"]["attempts"] == 2
    # Downstream chain is dead-lettered — nothing dangling / claimable.
    for kind in (
        "stage.visuals", "stage.render", "stage.thumbnail",
        "stage.qc", "stage.package", "gate.b",
    ):
        assert jobs[kind]["status"] == "dead", (kind, jobs[kind]["status"])
    assert env["queue"].claim("late-worker", kinds=tuple(STAGE_KINDS)) is None
    assert env["queue"].stats().get("queued", 0) == 0

    # Failure event: persisted in event_log AND emitted live.
    rows = _event_log_rows(env["factory"])
    assert len(rows) == 1
    envelope = json.loads(rows[0]["payload_json"])
    payload = envelope["payload"]
    assert envelope["type"] == "project.failed"
    assert payload["project_id"] == project_id
    assert payload["stage"] == "voice"
    assert payload["attempts"] == 2
    assert "tts exploded" in payload["error_summary"]

    live = hub.by_type("project.failed")
    assert len(live) == 1
    assert live[0]["payload"]["project_id"] == project_id
    assert live[0]["payload"]["stage"] == "voice"
    assert live[0]["payload"]["attempts"] == 2

    # A fresh runner over the same DB must not resurrect anything.
    calls.clear()
    hub2 = RecordingHub()
    runner2 = _make_runner(env, _chain_registry(calls), hub2)

    async def _idle():
        task = asyncio.create_task(runner2.run())
        try:
            await asyncio.sleep(0.6)
        finally:
            await runner2.stop()
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(_idle())
    assert calls == {}  # no handler ran — no third attempt, no restart loop
    assert _project(env["factory"], project_id)["status"] == "failed"
    assert len(_event_log_rows(env["factory"])) == 1  # no duplicate event

    _ = ids


def test_park_project_failed_unit(env):
    """Direct policy call: downstream cleanup is DAG-only and the event
    is persisted even with no live events object at all."""
    project_id = env["project_id"]
    queue = env["queue"]
    failed_id = queue.enqueue(
        "stage.render", {"project_id": project_id, "stage": "render"},
        project_id=project_id, max_attempts=2,
    )
    downstream_id = queue.enqueue(
        "stage.qc", {"project_id": project_id, "stage": "qc"},
        depends_on=failed_id, project_id=project_id, max_attempts=2,
    )
    control_id = queue.enqueue(
        "pipeline.start", {"project_id": project_id}, project_id=project_id
    )

    info = failure_policy.park_project_failed(
        env["factory"],
        project_id=project_id,
        job={"id": failed_id, "kind": "stage.render",
             "payload": {"stage": "render"}, "attempts": 2},
        error_code="RuntimeError",
        error_message="render blew up",
        events=None,
    )
    assert info["parked"] is True
    assert info["downstream_dead"] == 1
    project = _project(env["factory"], project_id)
    assert project["status"] == "failed"
    assert "render" in project["error_summary"]
    assert "render blew up" in project["error_summary"]
    assert queue.get(downstream_id)["status"] == "dead"
    # Control jobs are not part of the DAG chain and survive.
    assert queue.get(control_id)["status"] == "queued"
    rows = _event_log_rows(env["factory"])
    assert len(rows) == 1 and rows[0]["type"] == "project.failed"


# ---------------------------------------------------------------------------
# (c) gate / budget parking does not consume retries
# ---------------------------------------------------------------------------

def test_gate_parking_does_not_consume_retry(env):
    project_id = env["project_id"]
    create_project_dag(env["queue"], project_id)
    calls: dict[str, int] = {}
    state = {"parks": 0}

    async def gate_a(job):
        calls["gate.a"] = calls.get("gate.a", 0) + 1
        n = calls["gate.a"]
        if n <= 2:
            # Human gate wait — and the budget-pending park raises this
            # exact exception from the research stage; both are parks.
            state["parks"] += 1
            raise GatePending(f"gate a awaiting approval (park {n})")
        if n == 3:
            raise ValueError("one real failure after parking")
        return {"ok": True, "kind": "gate.a", "decision": "approved"}

    hub = RecordingHub()
    runner = _make_runner(env, _chain_registry(calls, {"gate.a": gate_a}), hub)

    def _all_done():
        jobs = _jobs(env["factory"], project_id)
        return len(jobs) == 10 and all(j["status"] in SUCCESS for j in jobs)

    assert _run_runner_until(runner, _all_done)
    # 2 parks (refunded) + 1 real failure (retry spent) + 1 success.
    assert calls["gate.a"] == 4
    gate_job = next(
        j for j in _jobs(env["factory"], project_id) if j["kind"] == "gate.a"
    )
    assert gate_job["status"] in SUCCESS
    # Only real attempts count: failed claim + successful claim = 2.
    # Had the parks consumed attempts, the single real failure after
    # them would have exhausted the budget and parked the project.
    assert gate_job["attempts"] == 2
    project = _project(env["factory"], project_id)
    assert project["status"] != "failed"
    assert hub.by_type("project.failed") == []


# ---------------------------------------------------------------------------
# (d) scheduler regression: catch_up_once + failed slot never loops
# ---------------------------------------------------------------------------

_HOUR = 3_600_000
_SCHED_NOW = 1_800_000_000_000  # fixed fake clock for scheduler ticks


def _seed_schedule(env, *, cadence, topic_mode="auto", next_run_at):
    conn = env["factory"]()
    try:
        conn.execute(
            "INSERT INTO schedules (id, name, channel_id, niche_profile_id, "
            "cadence_json, topic_mode, target_minutes, publish_mode, "
            "is_enabled, next_run_at, created_at, updated_at) "
            "VALUES ('sched-fp', 'FP schedule', ?, ?, ?, ?, 10, 'auto', 1, "
            "?, ?, ?)",
            (
                env["channel_id"],
                env["niche_id"],
                json.dumps(cadence),
                topic_mode,
                next_run_at,
                _SCHED_NOW - 86_400_000,
                _SCHED_NOW - 86_400_000,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_scheduler_catch_up_once_five_missed_slots_one_project(env):
    _seed_schedule(
        env,
        cadence={"type": "interval", "interval_hours": 1},
        next_run_at=_SCHED_NOW - 5 * _HOUR + _HOUR // 2,
    )
    tick = SchedulerTick(env["factory"], queue=env["queue"])
    summary = tick.tick(now_ms=_SCHED_NOW)
    assert summary["materialised"] == 1
    assert summary["collapsed"] == 4  # 5 missed slots -> 1 project
    conn = env["factory"]()
    try:
        count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        # seed_project created one unrelated project; the tick adds one.
        assert count == 2
        sched = conn.execute(
            "SELECT next_run_at FROM schedules WHERE id = 'sched-fp'"
        ).fetchone()
        assert sched[0] > _SCHED_NOW
    finally:
        conn.close()
    # Second tick at the same clock: nothing re-materialises.
    assert tick.tick(now_ms=_SCHED_NOW)["materialised"] == 0


def test_scheduler_failed_slot_recorded_once_and_advances(env):
    # topic_mode='queue' with an empty topic_queue fails the slot.
    _seed_schedule(
        env,
        cadence={"type": "interval", "interval_hours": 1},
        topic_mode="queue",
        next_run_at=_SCHED_NOW - 2 * _HOUR,
    )
    tick = SchedulerTick(env["factory"], queue=env["queue"])
    first = tick.tick(now_ms=_SCHED_NOW)
    assert first["failed"] == 1 and first["materialised"] == 0
    conn = env["factory"]()
    try:
        runs = conn.execute(
            "SELECT status FROM schedule_runs WHERE schedule_id = 'sched-fp'"
        ).fetchall()
        assert [r[0] for r in runs] == ["failed"]
        sched = conn.execute(
            "SELECT next_run_at FROM schedules WHERE id = 'sched-fp'"
        ).fetchone()
        assert sched[0] is not None and sched[0] > _SCHED_NOW
    finally:
        conn.close()
    # The failed slot must not re-materialise / re-fail forever.
    for _ in range(3):
        again = tick.tick(now_ms=_SCHED_NOW)
        assert again == {"checked": 0, "materialised": 0, "collapsed": 0, "failed": 0}
    conn = env["factory"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM schedule_runs WHERE schedule_id = 'sched-fp'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# (e) restart recovery does not loop
# ---------------------------------------------------------------------------

def test_restart_recovery_preserves_retry_budget(env):
    """Job claimed by a worker that 'crashes'; a fresh runner's startup
    sweep requeues it with its attempt count intact, the handler fails
    its one remaining attempt, and the project parks — total attempts
    stay at 2, never reset."""
    project_id = env["project_id"]
    create_project_dag(env["queue"], project_id)
    queue = env["queue"]

    # First worker claims research (attempts=1) and dies mid-flight.
    claimed = queue.claim("crashed-worker", kinds=("stage.research",))
    assert claimed is not None and claimed["attempts"] == 1
    conn = env["factory"]()
    try:
        conn.execute(
            "UPDATE jobs SET lease_expires_at = 1 WHERE id = ?", (claimed["id"],)
        )
        conn.commit()
    finally:
        conn.close()

    calls: dict[str, int] = {}

    async def failing_research(job):
        calls["stage.research"] = calls.get("stage.research", 0) + 1
        raise RuntimeError("still broken after restart")

    hub = RecordingHub()
    runner = _make_runner(
        env, _chain_registry(calls, {"stage.research": failing_research}), hub
    )

    def _parked():
        project = _project(env["factory"], project_id)
        return project is not None and project["status"] == "failed"

    assert _run_runner_until(runner, _parked)
    # The restarted runner executed the handler exactly once: attempt 2
    # of 2.  A memory-held counter would have allowed two more runs.
    assert calls["stage.research"] == 1
    job = queue.get(claimed["id"])
    assert job["status"] == "dead" and job["attempts"] == 2
    assert "research" in (_project(env["factory"], project_id)["error_summary"] or "")


def test_restart_sweep_dead_letters_and_parks_without_handler(env):
    """Both attempts were burned by crashed workers (no handler ever
    completed a run): the startup sweep dead-letters the job and the
    recovery scan parks the project — no handler execution, no loop."""
    project_id = env["project_id"]
    create_project_dag(env["queue"], project_id)
    queue = env["queue"]

    for owner in ("crashed-1", "crashed-2"):
        claimed = queue.claim(owner, kinds=("stage.research",))
        assert claimed is not None
        conn = env["factory"]()
        try:
            conn.execute(
                "UPDATE jobs SET lease_expires_at = 1 WHERE id = ?",
                (claimed["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        if owner == "crashed-1":
            assert queue.sweep_expired_leases() == 1  # requeue: attempts 1 < 2
    research_id = claimed["id"]
    assert queue.get(research_id)["attempts"] == 2

    calls: dict[str, int] = {}
    hub = RecordingHub()
    runner = _make_runner(env, _chain_registry(calls), hub)

    def _parked():
        project = _project(env["factory"], project_id)
        return project is not None and project["status"] == "failed"

    assert _run_runner_until(runner, _parked)
    assert calls.get("stage.research", 0) == 0  # never executed again
    assert queue.get(research_id)["status"] == "dead"
    project = _project(env["factory"], project_id)
    assert "research" in (project["error_summary"] or "")
    assert len(hub.by_type("project.failed")) == 1
    assert len(_event_log_rows(env["factory"])) == 1
    # Downstream is dead too — a later runner has nothing to claim.
    assert queue.stats().get("queued", 0) == 0


def test_recover_and_park_prefers_originator_on_timestamp_tie(env):
    """Deterministic tie case for the recovery scan's job preference.

    Two dead DAG jobs for one project share IDENTICAL ``created_at`` /
    ``updated_at`` — exactly what one lease sweep produces when it
    dead-letters the originating job and propagates
    ``dependency_failed`` to its downstream in a single transaction.
    On such a tie the scan's ``ORDER BY updated_at, created_at``
    guarantees nothing, so :func:`failure_policy.recover_and_park`
    must still park the project from the *originating*
    (non-``dependency_failed``) job, regardless of row order.

    Both rows are written exactly as the queue writes them: the code
    lives only in the single ``error`` column as ``"code: message"``
    and ``error_code`` stays NULL (the jobs schema has no
    ``error_message`` column, so the queue's split-field path never
    populates ``error_code``).  All timestamps are a fixed constant —
    this test has no wall-clock dependence.
    """
    project_id = env["project_id"]
    tie_ts = 1_700_000_000_000

    def _insert_dead(job_id, kind, stage, error):
        conn = env["factory"]()
        try:
            conn.execute(
                "INSERT INTO jobs (id, project_id, agent_key, kind, "
                "payload_json, status, attempts, max_attempts, error, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'dead', "
                "2, 2, ?, ?, ?)",
                (
                    job_id,
                    project_id,
                    kind,
                    kind,
                    json.dumps({"project_id": project_id, "stage": stage}),
                    error,
                    tie_ts,
                    tie_ts,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    # Originator inserted first, downstream job second: with every
    # ORDER BY key tied, row order is unspecified — in practice the
    # scan walks ``idx_jobs_status_time`` (``updated_at DESC``)
    # backwards for the ASC sort, so the later-inserted downstream row
    # surfaces FIRST.  A first-row-wins scan — or one whose preference
    # guard compares the never-populated ``error_code`` column instead
    # of the ``error`` prefix — then parks from the dependency_failed
    # row instead of the originator.  The fixed preference logic is
    # order-independent, so the assertions below hold either way.
    _insert_dead(
        "job-tie-research", "stage.research", "research",
        "lease_expired: lease expired and attempts exhausted "
        "(worker presumed dead)",
    )
    _insert_dead(
        "job-tie-script", "stage.script", "script",
        "dependency_failed: dependency job failed or died",
    )

    hub = RecordingHub()
    result = failure_policy.recover_and_park(
        env["factory"], queue=None, events=hub
    )
    assert result["parked"] == [project_id]

    project = _project(env["factory"], project_id)
    assert project["status"] == "failed"
    summary = project["error_summary"] or ""
    assert "research" in summary
    assert "lease_expired" in summary
    assert "dependency_failed" not in summary

    live = hub.by_type("project.failed")
    assert len(live) == 1
    assert live[0]["job_id"] == "job-tie-research"
    assert live[0]["payload"]["stage"] == "research"
    assert live[0]["payload"]["error_code"] == "lease_expired"

    rows = _event_log_rows(env["factory"])
    assert len(rows) == 1
    persisted = json.loads(rows[0]["payload_json"])["payload"]
    assert persisted["stage"] == "research"
    assert persisted["error_code"] == "lease_expired"
