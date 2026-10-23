"""Tests for the Round-2 execution runner core (Worker 1).

Covers ``app.execution.flags`` (settings-table control flags),
``app.execution.pools`` (POOL_LIMITS / KIND_TO_POOL / PoolManager) and
``app.execution.runner.JobRunner`` (claim/execute, pool limits, pause &
stop flags, crash recovery, GatePending requeue, failure semantics,
heartbeats).

Each test uses a real tmp SQLite DB built with ``app.db.init_db`` and
drives the async runner with ``asyncio.run`` so no pytest-asyncio mode
assumptions are needed.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from app import db as db_module
from app.queue import SQLiteJobQueue
from app.execution.flags import Flags, read_flags, set_flag
from app.execution.pools import KIND_TO_POOL, POOL_LIMITS, PoolManager, pool_for_kind
from app.execution.runner import GatePending, JobCancelled, JobRunner

SUCCESS = ("done", "succeeded")


@pytest.fixture()
def env(tmp_path):
    db_path = tmp_path / "runner_test.db"
    conn = db_module.init_db(db_path)
    conn.close()
    factory = lambda: db_module.get_connection(db_path)  # noqa: E731
    return SQLiteJobQueue(factory), factory, db_path


class FakeEvents:
    """Recording stand-in for the WS hub / events facade."""

    def __init__(self):
        self.calls: list[tuple] = []

    def publish(self, event_type, payload, project_id=None, job_id=None):
        self.calls.append((event_type, payload, project_id, job_id))
        return {"type": event_type}

    def types(self):
        return [c[0] for c in self.calls]


class BrokenEvents:
    def publish(self, *args, **kwargs):
        raise RuntimeError("hub exploded")


async def wait_until(pred, timeout=10.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return pred()


async def shutdown(runner, task):
    await runner.stop()
    if task is not None:
        await asyncio.wait_for(task, timeout=5)


def status_of(queue, job_id):
    job = queue.get(job_id)
    return job["status"] if job else None


# ----------------------------------------------------------------------
# flags
# ----------------------------------------------------------------------
def test_flags_roundtrip_and_parsing(env):
    _queue, factory, _path = env
    assert read_flags(factory) == Flags(pause_all=False, stop_all=False)

    set_flag(factory, "pause_all", True)
    flags = read_flags(factory)
    assert flags.pause_all is True and flags.stop_all is False

    set_flag(factory, "execution.stop_all", True)
    assert read_flags(factory).stop_all is True

    set_flag(factory, "pause_all", False)
    set_flag(factory, "stop_all", False)
    assert read_flags(factory) == Flags(False, False)

    # Legacy DB with ONLY a bare key (string value) is honoured: remove the
    # namespaced row first, since it takes priority when both exist.
    conn = factory()
    try:
        conn.execute("DELETE FROM settings WHERE key = 'execution.pause_all'")
        conn.execute(
            "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
            ("pause_all", '"true"', 1),
        )
        conn.commit()
    finally:
        conn.close()
    assert read_flags(factory).pause_all is True

    with pytest.raises(ValueError):
        set_flag(factory, "bogus_flag", True)


def test_flags_missing_table_tolerated(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(str(path)).close()  # file exists, no settings table

    def factory():
        return sqlite3.connect(str(path))

    assert read_flags(factory) == Flags(False, False)
    # set_flag creates the table on demand.
    set_flag(factory, "pause_all", True)
    assert read_flags(factory).pause_all is True


# ----------------------------------------------------------------------
# pools
# ----------------------------------------------------------------------
def test_pool_limits_and_mapping():
    assert POOL_LIMITS == {
        "render": 1,
        "tts": 4,
        "llm": 8,
        "downloads": 6,
        "upload": 2,
        "default": 4,
    }
    assert KIND_TO_POOL["stage.render"] == "render"
    assert KIND_TO_POOL["stage.voice"] == "tts"
    for kind in ("stage.research", "stage.script", "stage.package", "stage.qc"):
        assert KIND_TO_POOL[kind] == "llm"
    for kind in ("stage.visuals", "stage.thumbnail"):
        assert KIND_TO_POOL[kind] == "downloads"
    for kind in ("stage.publish", "stage.upload"):
        assert KIND_TO_POOL[kind] == "upload"
    assert KIND_TO_POOL["gate.a"] == "default"
    assert KIND_TO_POOL["gate.b"] == "default"
    assert pool_for_kind("pipeline.run") == "default"
    assert pool_for_kind("pipeline.anything") == "default"
    assert pool_for_kind("totally.unknown") == "default"


def test_pool_manager_blocks_beyond_limit():
    async def scenario():
        pools = PoolManager()
        assert pools.in_use("render") == 0
        async with pools.acquire("render"):
            assert pools.in_use("render") == 1
            acquired = asyncio.Event()

            async def second():
                async with pools.acquire("render"):
                    acquired.set()

            task = asyncio.create_task(second())
            await asyncio.sleep(0.2)
            assert not acquired.is_set()  # render limit is 1
            assert pools.in_use("render") == 1
        await asyncio.wait_for(task, timeout=5)
        assert acquired.is_set()
        assert pools.in_use("render") == 0

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# runner: (a) simple claim + execute to completion
# ----------------------------------------------------------------------
def test_simple_job_completes_with_result(env):
    queue, factory, _path = env

    async def scenario():
        seen = []

        async def handler(job):
            seen.append(job)
            return {"echo": job["payload"]["n"], "ok": True}

        events = FakeEvents()
        runner = JobRunner(
            factory, {"stage.script": handler}, events=events, poll_interval=0.02
        )
        job_id = queue.enqueue("stage.script", {"n": 7})
        task = asyncio.create_task(runner.run())
        try:
            assert await wait_until(lambda: status_of(queue, job_id) in SUCCESS)
            job = queue.get(job_id)
            assert job["result"] == {"echo": 7, "ok": True}
            assert job["lease_owner"] is None
            assert seen and seen[0]["id"] == job_id
            assert seen[0]["payload"] == {"n": 7}
            assert "agent.status_changed" in events.types()
            phase = [c for c in events.calls if c[0] == "pipeline.phase_changed"]
            assert phase and phase[0][1]["phase"] == "stage.script"
        finally:
            await shutdown(runner, task)

    asyncio.run(scenario())


def test_broken_events_hub_never_kills_runner(env):
    queue, factory, _path = env

    async def scenario():
        async def handler(job):
            return {"fine": True}

        runner = JobRunner(
            factory,
            {"stage.script": handler},
            events=BrokenEvents(),
            poll_interval=0.02,
        )
        job_id = queue.enqueue("stage.script", {})
        task = asyncio.create_task(runner.run())
        try:
            assert await wait_until(lambda: status_of(queue, job_id) in SUCCESS)
        finally:
            await shutdown(runner, task)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# runner: (b) pool limit — render pool holds at most 1 concurrent job
# ----------------------------------------------------------------------
def test_render_pool_limit_is_one(env):
    queue, factory, _path = env

    async def scenario():
        state = {"cur": 0, "max": 0}

        async def render(job):
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
            await asyncio.sleep(0.3)
            state["cur"] -= 1
            return {"n": job["payload"]["n"]}

        runner = JobRunner(factory, {"stage.render": render}, poll_interval=0.02)
        ids = [queue.enqueue("stage.render", {"n": n}) for n in (1, 2)]
        task = asyncio.create_task(runner.run())
        try:
            assert await wait_until(
                lambda: all(status_of(queue, j) in SUCCESS for j in ids)
            )
            assert state["max"] == 1
        finally:
            await shutdown(runner, task)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# runner: (c) pause_all in the settings table stops claiming
# ----------------------------------------------------------------------
def test_pause_all_flag_stops_claiming(env):
    queue, factory, _path = env

    async def scenario():
        calls = []

        async def handler(job):
            calls.append(job["id"])
            return {"done": True}

        set_flag(factory, "pause_all", True)
        runner = JobRunner(factory, {"stage.script": handler}, poll_interval=0.02)
        job_id = queue.enqueue("stage.script", {})
        task = asyncio.create_task(runner.run())
        try:
            await asyncio.sleep(0.5)
            assert status_of(queue, job_id) == "queued"
            assert calls == []

            set_flag(factory, "pause_all", False)
            assert await wait_until(lambda: status_of(queue, job_id) in SUCCESS)
            assert calls == [job_id]
        finally:
            await shutdown(runner, task)

    asyncio.run(scenario())


def test_pause_resume_methods_persist(env):
    queue, factory, _path = env

    async def scenario():
        async def handler(job):
            return {"done": True}

        runner = JobRunner(factory, {"stage.script": handler}, poll_interval=0.02)
        job_id = queue.enqueue("stage.script", {})
        task = asyncio.create_task(runner.run())
        try:
            runner.pause()
            assert read_flags(factory).pause_all is True
            await asyncio.sleep(0.4)
            assert status_of(queue, job_id) == "queued"

            runner.resume()
            assert read_flags(factory).pause_all is False
            assert await wait_until(lambda: status_of(queue, job_id) in SUCCESS)
        finally:
            await shutdown(runner, task)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# runner: (d) stop_all cancels in-flight work and ends the loop
# ----------------------------------------------------------------------
def test_stop_all_cancels_inflight_and_stops(env):
    queue, factory, _path = env

    async def scenario():
        state = {"started": False, "cancelled": False}

        async def long_handler(job):
            state["started"] = True
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise
            return {"never": True}

        runner = JobRunner(factory, {"stage.script": long_handler}, poll_interval=0.02)
        job_id = queue.enqueue("stage.script", {})
        task = asyncio.create_task(runner.run())
        try:
            assert await wait_until(lambda: state["started"])
            set_flag(factory, "stop_all", True)
            await asyncio.wait_for(task, timeout=5)  # run() must return
            assert state["cancelled"] is True
            assert status_of(queue, job_id) == "cancelled"
        finally:
            await runner.stop()
            if not task.done():
                task.cancel()

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# runner: (e) crash recovery — expired lease is swept, job executes
# ----------------------------------------------------------------------
def test_crash_recovery_sweeps_expired_lease(env):
    queue, factory, _path = env

    async def scenario():
        async def handler(job):
            return {"recovered": True}

        job_id = queue.enqueue("stage.script", {"x": 1})
        crashed = queue.claim("crashed-owner", lease_seconds=60)
        assert crashed is not None and crashed["id"] == job_id

        # Simulate the crash + lease passage without waiting a real minute.
        conn = factory()
        try:
            conn.execute(
                "UPDATE jobs SET lease_expires_at = ? WHERE id = ?", (1, job_id)
            )
            conn.commit()
        finally:
            conn.close()
        assert status_of(queue, job_id) in ("leased", "running")

        runner = JobRunner(factory, {"stage.script": handler}, poll_interval=0.02)
        task = asyncio.create_task(runner.run())
        try:
            assert await wait_until(lambda: status_of(queue, job_id) in SUCCESS)
            assert queue.get(job_id)["result"] == {"recovered": True}
        finally:
            await shutdown(runner, task)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# runner: (f) GatePending requeue does not consume attempts
# ----------------------------------------------------------------------
def test_gate_pending_requeue_refunds_attempt(env):
    queue, factory, _path = env

    async def scenario():
        state = {"ready": False, "calls": 0}

        async def gated(job):
            state["calls"] += 1
            if not state["ready"]:
                raise GatePending("gate A not approved yet")
            return {"approved": True}

        runner = JobRunner(
            factory,
            {"gate.a": gated},
            poll_interval=0.02,
            gate_requeue_delay_ms=200,
        )
        job_id = queue.enqueue("gate.a", {})
        task = asyncio.create_task(runner.run())
        try:
            # After the first gate-block the job is queued again, and the
            # claim's attempt has been refunded (attempts back to 0).
            assert await wait_until(
                lambda: state["calls"] >= 1 and status_of(queue, job_id) == "queued"
            )
            assert queue.get(job_id)["attempts"] == 0

            state["ready"] = True
            assert await wait_until(lambda: status_of(queue, job_id) in SUCCESS)
            job = queue.get(job_id)
            assert job["result"] == {"approved": True}
            assert job["attempts"] == 1  # exactly one real attempt consumed
            assert state["calls"] >= 2
        finally:
            await shutdown(runner, task)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# runner: (g) failing handler -> dead per queue semantics
# ----------------------------------------------------------------------
def test_failing_handler_goes_dead(env):
    queue, factory, _path = env

    async def scenario():
        async def boom(job):
            raise ValueError("kaboom")

        runner = JobRunner(
            factory,
            {"stage.script": boom},
            poll_interval=0.02,
            error_backoff_base_ms=10,
        )
        job_id = queue.enqueue("stage.script", {}, max_attempts=2)
        task = asyncio.create_task(runner.run())
        try:
            assert await wait_until(lambda: status_of(queue, job_id) == "dead")
            job = queue.get(job_id)
            assert job["attempts"] == 2
            assert job["error_code"] == "ValueError"
            assert "kaboom" in (job["error_message"] or "")
        finally:
            await shutdown(runner, task)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# runner: JobCancelled exception -> job cancelled
# ----------------------------------------------------------------------
def test_job_cancelled_exception_cancels_job(env):
    queue, factory, _path = env

    async def scenario():
        async def canceller(job):
            raise JobCancelled("user cancelled")

        runner = JobRunner(factory, {"stage.script": canceller}, poll_interval=0.02)
        job_id = queue.enqueue("stage.script", {})
        task = asyncio.create_task(runner.run())
        try:
            assert await wait_until(
                lambda: status_of(queue, job_id) == "cancelled"
            )
        finally:
            await shutdown(runner, task)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# runner: heartbeats keep a long job's lease alive (lease_seconds=1)
# ----------------------------------------------------------------------
def test_heartbeat_keeps_lease_alive(env):
    queue, factory, _path = env

    async def scenario():
        captured = {}

        async def slow(job):
            job_id = job["id"]
            captured["initial"] = queue.get(job_id)["lease_expires_at_ms"]
            await asyncio.sleep(2.5)
            captured["later"] = queue.get(job_id)["lease_expires_at_ms"]
            return {"slow": True}

        runner = JobRunner(
            factory, {"stage.script": slow}, poll_interval=0.02, lease_seconds=1
        )
        job_id = queue.enqueue("stage.script", {})
        task = asyncio.create_task(runner.run())
        try:
            assert await wait_until(
                lambda: status_of(queue, job_id) in SUCCESS, timeout=10
            )
            # The lease deadline moved well past the initial 1 s expiry.
            assert captured["later"] > captured["initial"]
            assert queue.get(job_id)["result"] == {"slow": True}
        finally:
            await shutdown(runner, task)

    asyncio.run(scenario())
