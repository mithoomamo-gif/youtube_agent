"""Tests for app.queue.SQLiteJobQueue (SQLite job queue, roadmap §8/§15).

Builds a real SQLite DB in ``tmp_path``.  Prefers ``app.db.init_db`` +
``app.db.connect`` when worker 3's module is importable and functional;
otherwise falls back to creating the ``jobs`` table inline (task-spec
columns) so these tests are self-contained.
"""

import sqlite3

import pytest

from app.queue import SQLiteJobQueue

NOW = 1_700_000_000_000  # fixed epoch-ms used as the fake clock
SUCCESS = ("succeeded", "done")  # s15 says 'done'; task spec says 'succeeded'

# Task-spec jobs table (mirrors section 15 with *_ms column names).
JOBS_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    queue TEXT NOT NULL DEFAULT 'default',
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 0,
    depends_on TEXT NULL,
    run_after_ms INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT NULL,
    lease_expires_at_ms INTEGER NULL,
    heartbeat_at_ms INTEGER NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    idempotency_key TEXT UNIQUE,
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NULL,
    error_code TEXT NULL,
    error_message TEXT NULL,
    project_id TEXT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
)
"""


def _inline_factory(db_file):
    def factory():
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(JOBS_DDL)
        conn.commit()
        return conn

    factory().close()  # create the table up-front
    return factory


def _app_db_factory(db_file):
    """Return a conn_factory backed by app.db, or None if unusable."""
    try:
        from app import db as app_db

        if not (hasattr(app_db, "init_db") and hasattr(app_db, "connect")):
            return None
        try:
            app_db.init_db(str(db_file))
        except TypeError:
            app_db.init_db(db_file)

        def factory():
            try:
                return app_db.connect(str(db_file))
            except TypeError:
                return app_db.connect(db_file)

        probe = factory()
        try:
            probe.execute("SELECT 1 FROM jobs LIMIT 1")
        finally:
            probe.close()
        return factory
    except Exception:
        return None


@pytest.fixture()
def env(tmp_path):
    db_file = tmp_path / "queue_test.db"
    factory = _app_db_factory(db_file) or _inline_factory(db_file)
    return SQLiteJobQueue(factory), factory


@pytest.fixture()
def queue(env):
    return env[0]


def _row_count(factory):
    conn = factory()
    try:
        return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        conn.close()


# ----------------------------------------------------------------------
def test_enqueue_and_claim_sets_lease(queue):
    job_id = queue.enqueue("script_writer", {"topic": "space"}, priority=5)
    assert isinstance(job_id, str) and job_id

    job = queue.claim("worker-1", lease_seconds=60, now_ms=NOW)
    assert job is not None
    assert job["id"] == job_id
    assert job["status"] == "leased"
    assert job["lease_owner"] == "worker-1"
    assert job["lease_expires_at_ms"] == NOW + 60_000
    assert job["heartbeat_at_ms"] == NOW
    assert job["attempts"] == 1
    assert job["payload"] == {"topic": "space"}


def test_second_claim_returns_none_while_leased(queue):
    queue.enqueue("render", {"n": 1})
    assert queue.claim("worker-1", now_ms=NOW) is not None
    assert queue.claim("worker-2", now_ms=NOW) is None


def test_idempotency_same_key_same_id_one_row(env):
    queue, factory = env
    first = queue.enqueue("upload", {"v": 1}, idempotency_key="key-123")
    second = queue.enqueue("upload", {"v": 2}, idempotency_key="key-123")
    assert first == second
    assert _row_count(factory) == 1


def test_heartbeat_extends_lease(queue):
    queue.enqueue("tts", {})
    job = queue.claim("worker-1", lease_seconds=60, now_ms=NOW)
    before = job["lease_expires_at_ms"]

    assert queue.heartbeat(job["id"], "worker-1", lease_seconds=300, now_ms=NOW)
    after = queue.get(job["id"])
    assert after["lease_expires_at_ms"] == NOW + 300_000
    assert after["lease_expires_at_ms"] > before

    # A non-owner cannot heartbeat someone else's lease.
    assert not queue.heartbeat(job["id"], "intruder", lease_seconds=300, now_ms=NOW)


def test_fail_retry_requeues_with_backoff(queue):
    queue.enqueue("research", {}, max_attempts=3)
    job = queue.claim("worker-1", now_ms=NOW)

    status = queue.fail(
        job["id"], "worker-1", "timeout", "provider timed out",
        backoff_base_ms=1000, now_ms=NOW,
    )
    assert status == "queued"
    failed = queue.get(job["id"])
    assert failed["status"] == "queued"
    assert failed["lease_owner"] is None
    assert failed["run_after_ms"] > NOW  # backoff pushed it into the future
    assert failed["error_code"] == "timeout"

    # Not claimable before run_after, claimable after it.
    assert queue.claim("worker-1", now_ms=NOW + 500) is None
    reclaimed = queue.claim("worker-1", now_ms=NOW + 120_000)
    assert reclaimed is not None
    assert reclaimed["id"] == job["id"]
    assert reclaimed["attempts"] == 2


def test_fail_beyond_max_attempts_goes_dead(queue):
    queue.enqueue("upload", {}, max_attempts=2)

    job = queue.claim("w", now_ms=NOW)
    assert queue.fail(job["id"], "w", "e1", "first", now_ms=NOW) == "queued"

    job = queue.claim("w", now_ms=NOW + 120_000)
    assert queue.fail(job["id"], "w", "e2", "second", now_ms=NOW + 120_000) == "dead"

    dead = queue.get(job["id"])
    assert dead["status"] == "dead"
    assert dead["error_code"] == "e2"
    assert queue.claim("w", now_ms=NOW + 10_000_000) is None


def test_fail_non_retryable_goes_dead_immediately(queue):
    queue.enqueue("upload", {}, max_attempts=5)
    job = queue.claim("w", now_ms=NOW)
    assert queue.fail(
        job["id"], "w", "bad_key", "401 invalid", retryable=False, now_ms=NOW
    ) == "dead"
    assert queue.get(job["id"])["status"] == "dead"


def test_dependency_blocks_until_completed(queue):
    a = queue.enqueue("research", {})
    b = queue.enqueue("script_writer", {}, depends_on=a)

    first = queue.claim("w", now_ms=NOW)
    assert first["id"] == a  # B is not claimable yet
    assert queue.claim("w", now_ms=NOW) is None

    assert queue.complete(a, "w", {"brief": "ok"}, now_ms=NOW)
    second = queue.claim("w", now_ms=NOW)
    assert second is not None and second["id"] == b


def test_dependency_dead_marks_dependent_dead(queue):
    a = queue.enqueue("research", {}, max_attempts=1)
    b = queue.enqueue("script_writer", {}, depends_on=a)

    job = queue.claim("w", now_ms=NOW)
    assert job["id"] == a
    queue.fail(a, "w", "boom", "died", retryable=False, now_ms=NOW)

    # Trigger a claim pass (dependency-failure propagation) and inspect B.
    assert queue.claim("w", now_ms=NOW) is None
    dependent = queue.get(b)
    assert dependent["status"] == "dead"
    assert dependent["error_code"] == "dependency_failed"


def test_sweep_expired_lease_requeues_for_other_owner(queue):
    queue.enqueue("render", {})
    job = queue.claim("worker-1", lease_seconds=60, now_ms=NOW)
    assert job is not None

    # Lease still valid -> nothing swept.
    assert queue.sweep_expired_leases(now_ms=NOW + 30_000) == 0
    # Lease expired -> swept back to queued.
    assert queue.sweep_expired_leases(now_ms=NOW + 61_000) == 1
    assert queue.get(job["id"])["status"] == "queued"

    reclaimed = queue.claim("worker-2", now_ms=NOW + 61_000)
    assert reclaimed is not None
    assert reclaimed["lease_owner"] == "worker-2"


def test_sweep_expired_lease_dead_when_attempts_exhausted(queue):
    queue.enqueue("render", {}, max_attempts=1)
    job = queue.claim("worker-1", lease_seconds=60, now_ms=NOW)
    assert queue.sweep_expired_leases(now_ms=NOW + 61_000) == 1
    dead = queue.get(job["id"])
    assert dead["status"] == "dead"
    assert dead["error_code"] == "lease_expired"


def test_complete_stores_result(queue):
    queue.enqueue("thumbnail", {})
    job = queue.claim("w", now_ms=NOW)
    assert queue.start(job["id"], "w", now_ms=NOW)
    assert queue.get(job["id"])["status"] == "running"

    assert queue.complete(job["id"], "w", {"file": "thumb.png"}, now_ms=NOW)
    done = queue.get(job["id"])
    assert done["status"] in SUCCESS
    assert done["result"] == {"file": "thumb.png"}
    assert done["lease_owner"] is None


def test_cancel_and_stats(queue):
    a = queue.enqueue("a_kind", {})
    queue.enqueue("b_kind", {})
    assert queue.cancel(a)
    assert queue.get(a)["status"] == "cancelled"
    assert not queue.cancel(a)  # already terminal

    stats = queue.stats()
    assert stats.get("cancelled") == 1
    assert stats.get("queued") == 1
