"""Tests for app.execution.scheduler (SchedulerTick + next_occurrence).

Builds a real SQLite DB in ``tmp_path`` via ``app.db.init_db`` and seeds
channels / niche profiles / schedules directly, mirroring how Round 1
tests seed their fixtures.  When Worker 2's ``app.execution.dag`` module
is absent the scheduler falls back to a single ``pipeline.start`` job —
the job assertions therefore accept either shape (any job row for the
project, and specifically a ``pipeline.start`` job when the DAG module is
not importable).
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import db as db_module
from app.execution.scheduler import SchedulerTick, next_occurrence, parse_cadence
from app.queue import SQLiteJobQueue

HOUR = 3_600_000
DAY = 86_400_000

CHAN = "chan-1"
NICHE = "niche-1"


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# Friday 2026-10-23 12:00 UTC — a fixed "now" for every tick test.
NOW_DT = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
NOW = _ms(NOW_DT)
assert NOW_DT.weekday() == 4  # Friday


def _at(hour: int, minute: int = 0, day: int = 23) -> int:
    return _ms(datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc))


@pytest.fixture()
def env(tmp_path):
    db_path = tmp_path / "scheduler_test.db"
    conn = db_module.init_db(db_path)
    conn.close()

    def factory():
        return db_module.get_connection(db_path)

    queue = SQLiteJobQueue(factory)
    return SimpleNamespace(path=db_path, factory=factory, queue=queue)


def _seed_channel_niche(env, chan=CHAN, niche=NICHE):
    conn = env.factory()
    try:
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title, language, "
            "status, created_at, updated_at) VALUES (?, ?, ?, 'en', "
            "'active', ?, ?)",
            (chan, f"yt-{chan}", f"Channel {chan}", NOW - DAY, NOW - DAY),
        )
        conn.execute(
            "INSERT INTO niche_profiles (id, key, label, source, "
            "words_per_minute, created_at, updated_at) VALUES (?, ?, ?, "
            "'builtin', 150, ?, ?)",
            (niche, f"key-{niche}", f"Niche {niche}", NOW - DAY, NOW - DAY),
        )
    finally:
        conn.close()


def _seed_schedule(
    env,
    sid="sched-1",
    *,
    chan=CHAN,
    niche=NICHE,
    cadence=None,
    topic_mode="auto",
    next_run_at=None,
    is_enabled=1,
    target_minutes=12,
    publish_mode="review_first",
):
    if cadence is None:
        cadence = {"type": "daily", "time": "09:00"}
    conn = env.factory()
    try:
        conn.execute(
            "INSERT INTO schedules (id, name, channel_id, niche_profile_id, "
            "cadence_json, topic_mode, target_minutes, publish_mode, "
            "is_enabled, next_run_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                f"Schedule {sid}",
                chan,
                niche,
                json.dumps(cadence) if not isinstance(cadence, str) else cadence,
                topic_mode,
                target_minutes,
                publish_mode,
                is_enabled,
                next_run_at,
                NOW - DAY,
                NOW - DAY,
            ),
        )
    finally:
        conn.close()
    return sid


def _rows(env, sql, params=()):
    conn = env.factory()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _schedule(env, sid):
    rows = _rows(env, "SELECT * FROM schedules WHERE id = ?", (sid,))
    return rows[0] if rows else None


def _projects(env):
    # Order by rowid (insertion order): created_at is wall-clock ms and two
    # projects created inside one millisecond would tie-break on random
    # uuid ids, making the topic order flaky.
    return _rows(env, "SELECT * FROM projects ORDER BY rowid")


def _runs(env, sid=None):
    # Order by rowid (insertion order), same as _projects: every run in a
    # test is stamped created_at=NOW (the tick's fixed "now"), so ordering
    # by created_at is a tie with no guaranteed order.
    if sid:
        return _rows(
            env,
            "SELECT * FROM schedule_runs WHERE schedule_id = ? "
            "ORDER BY rowid",
            (sid,),
        )
    return _rows(env, "SELECT * FROM schedule_runs ORDER BY rowid")


def _jobs_for_project(env, project_id):
    return _rows(env, "SELECT * FROM jobs WHERE project_id = ?", (project_id,))


# ---------------------------------------------------------------------------
# (f) next_occurrence unit tests
# ---------------------------------------------------------------------------

class TestNextOccurrence:
    def test_interval(self):
        t = _at(12)
        assert next_occurrence({"type": "interval", "interval_hours": 6}, t) == t + 6 * HOUR
        assert next_occurrence({"type": "interval", "interval_hours": 1}, t) == t + HOUR
        assert next_occurrence({"type": "interval", "interval_hours": 0.5}, t) == t + HOUR // 2

    def test_interval_invalid(self):
        t = _at(12)
        assert next_occurrence({"type": "interval", "interval_hours": 0}, t) is None
        assert next_occurrence({"type": "interval", "interval_hours": -2}, t) is None
        assert next_occurrence({"type": "interval"}, t) is None
        assert next_occurrence({"type": "interval", "interval_hours": "6"}, t) is None

    def test_daily_same_day_and_next_day(self):
        cadence = {"type": "daily", "time": "09:00"}
        assert next_occurrence(cadence, _at(8)) == _at(9)          # later today
        assert next_occurrence(cadence, _at(10)) == _at(9, day=24)  # tomorrow
        assert next_occurrence(cadence, _at(9) - 1) == _at(9)       # 1ms before

    def test_daily_exact_boundary_rolls_to_next_day(self):
        cadence = {"type": "daily", "time": "09:00"}
        # Strictly after: exactly at 09:00 -> next day 09:00.
        assert next_occurrence(cadence, _at(9)) == _at(9, day=24)
        midnight = {"type": "daily", "time": "00:00"}
        assert next_occurrence(midnight, _at(0)) == _at(0, day=24)

    def test_weekly(self):
        # 2026-10-23 is a Friday (weekday 4); 2026-10-26 is the next Monday.
        friday_9am = {"type": "weekly", "weekday": 4, "time": "09:00"}
        assert next_occurrence(friday_9am, _at(8)) == _at(9)  # later today
        monday_9am = {"type": "weekly", "weekday": 0, "time": "09:00"}
        assert next_occurrence(monday_9am, _at(8)) == _at(9, day=26)
        # A time already passed today on the target weekday -> next week.
        friday_7am = {"type": "weekly", "weekday": 4, "time": "07:00"}
        assert next_occurrence(friday_7am, _at(8)) == _at(7, day=30)

    def test_weekly_exact_boundary_rolls_a_week(self):
        monday_9am = {"type": "weekly", "weekday": 0, "time": "09:00"}
        mon = _at(9, day=26)  # Monday 2026-10-26 09:00 exactly
        assert next_occurrence(monday_9am, mon) == mon + 7 * DAY

    def test_unknown_and_invalid(self):
        t = _at(12)
        assert next_occurrence({}, t) is None
        assert next_occurrence({"type": "monthly"}, t) is None
        assert next_occurrence({"type": "daily", "time": "25:00"}, t) is None
        assert next_occurrence({"type": "daily", "time": "9am"}, t) is None
        assert next_occurrence({"type": "daily"}, t) is None
        assert next_occurrence({"type": "weekly", "weekday": 7, "time": "09:00"}, t) is None
        assert next_occurrence({"type": "weekly", "weekday": -1, "time": "09:00"}, t) is None
        assert next_occurrence(None, t) is None
        assert next_occurrence("daily", t) is None

    def test_parse_cadence_tolerant(self):
        assert parse_cadence('{"type": "daily", "time": "09:00"}') == {
            "type": "daily",
            "time": "09:00",
        }
        assert parse_cadence({"type": "interval", "interval_hours": 2}) == {
            "type": "interval",
            "interval_hours": 2,
        }
        assert parse_cadence("not json") is None
        assert parse_cadence("[1, 2]") is None
        assert parse_cadence(None) is None
        assert parse_cadence("") is None


# ---------------------------------------------------------------------------
# (a) due daily schedule materialises exactly once
# ---------------------------------------------------------------------------

def test_due_daily_schedule_materialises(env):
    _seed_channel_niche(env)
    _seed_schedule(env, cadence={"type": "daily", "time": "09:00"},
                   next_run_at=_at(9))  # due today 09:00, now is 12:00

    tick = SchedulerTick(env.factory, queue=env.queue)
    summary = tick.tick(now_ms=NOW)

    assert summary["checked"] == 1
    assert summary["materialised"] == 1
    assert summary["failed"] == 0
    assert summary["collapsed"] == 0

    projects = _projects(env)
    assert len(projects) == 1
    project = projects[0]
    assert project["status"] == "in_production"
    assert project["topic_source"] == "schedule"
    assert project["topic"] == f"auto:{NICHE}"  # auto sentinel
    assert project["schedule_id"] == "sched-1"
    assert project["channel_id"] == CHAN
    assert project["niche_profile_id"] == NICHE
    assert project["target_minutes"] == 12
    assert project["target_words"] == 12 * 150
    assert project["publish_mode"] == "review_first"

    runs = _runs(env, "sched-1")
    assert len(runs) == 1
    assert runs[0]["status"] == "created"
    assert runs[0]["project_id"] == project["id"]
    detail = json.loads(runs[0]["detail_json"])
    assert detail["missed_collapsed"] == 0
    assert detail["slot_at"] == _at(9)

    # DAG-or-fallback: at least one job exists for the project; without
    # Worker 2's dag module it is exactly the pipeline.start fallback.
    jobs = _jobs_for_project(env, project["id"])
    assert len(jobs) >= 1
    if importlib.util.find_spec("app.execution.dag") is None:
        assert len(jobs) == 1
        assert jobs[0]["kind"] == "pipeline.start"
        assert json.loads(jobs[0]["payload_json"]) == {"project_id": project["id"]}

    sched = _schedule(env, "sched-1")
    assert sched["next_run_at"] == _at(9, day=24)  # advanced into the future
    assert sched["next_run_at"] > NOW
    assert sched["last_run_at"] == NOW


# ---------------------------------------------------------------------------
# (b) catch_up_once collapses missed slots
# ---------------------------------------------------------------------------

def test_catch_up_once_collapses_missed_slots(env):
    _seed_channel_niche(env)
    # Anchor 5 slots back with now mid-interval: missed slots at
    # -4.5h, -3.5h, -2.5h, -1.5h, -0.5h -> 5 missed, latest materialised,
    # 4 collapsed.
    anchor = NOW - 5 * HOUR + HOUR // 2
    _seed_schedule(env, cadence={"type": "interval", "interval_hours": 1},
                   next_run_at=anchor)

    tick = SchedulerTick(env.factory, queue=env.queue)
    summary = tick.tick(now_ms=NOW)

    assert summary["materialised"] == 1
    assert summary["collapsed"] == 4
    assert len(_projects(env)) == 1

    runs = _runs(env, "sched-1")
    assert len(runs) == 1
    assert runs[0]["status"] == "created"
    detail = json.loads(runs[0]["detail_json"])
    assert detail["missed_collapsed"] == 4
    assert detail["slot_at"] == NOW - HOUR // 2  # the LATEST missed slot

    sched = _schedule(env, "sched-1")
    assert sched["next_run_at"] == NOW + HOUR // 2  # first slot after now
    assert sched["last_run_at"] == NOW


# ---------------------------------------------------------------------------
# (c) not-yet-due and (d) disabled schedules are untouched
# ---------------------------------------------------------------------------

def test_not_yet_due_schedule_untouched(env):
    _seed_channel_niche(env)
    future = NOW + HOUR
    _seed_schedule(env, next_run_at=future)

    summary = SchedulerTick(env.factory, queue=env.queue).tick(now_ms=NOW)

    assert summary == {"checked": 0, "materialised": 0, "collapsed": 0, "failed": 0}
    assert _projects(env) == []
    assert _runs(env) == []
    sched = _schedule(env, "sched-1")
    assert sched["next_run_at"] == future
    assert sched["last_run_at"] is None


def test_disabled_schedule_untouched(env):
    _seed_channel_niche(env)
    past = NOW - HOUR
    _seed_schedule(env, next_run_at=past, is_enabled=0)

    summary = SchedulerTick(env.factory, queue=env.queue).tick(now_ms=NOW)

    assert summary["checked"] == 0
    assert summary["materialised"] == 0
    assert _projects(env) == []
    assert _runs(env) == []
    sched = _schedule(env, "sched-1")
    assert sched["next_run_at"] == past  # not advanced, not run
    assert sched["last_run_at"] is None


def test_soft_deleted_schedule_untouched(env):
    _seed_channel_niche(env)
    _seed_schedule(env, next_run_at=NOW - HOUR)
    conn = env.factory()
    try:
        conn.execute(
            "UPDATE schedules SET deleted_at = ? WHERE id = 'sched-1'", (NOW,)
        )
    finally:
        conn.close()

    summary = SchedulerTick(env.factory, queue=env.queue).tick(now_ms=NOW)
    assert summary["materialised"] == 0
    assert _projects(env) == []


# ---------------------------------------------------------------------------
# (e) queue topic_mode consumes the pending topic
# ---------------------------------------------------------------------------

def test_queue_topic_mode_consumes_highest_priority(env):
    _seed_channel_niche(env)
    _seed_schedule(env, topic_mode="queue", next_run_at=NOW - HOUR)
    conn = env.factory()
    try:
        for tid, topic, prio in (
            ("tq-low", "Low priority topic", 5),
            ("tq-high", "High priority topic", 10),
            ("tq-other-niche", "Other niche topic", 99),
        ):
            niche = NICHE if tid != "tq-other-niche" else "niche-2"
            if tid == "tq-other-niche":
                conn.execute(
                    "INSERT INTO niche_profiles (id, key, label, source, "
                    "created_at, updated_at) VALUES ('niche-2', 'key-2', "
                    "'N2', 'builtin', ?, ?)",
                    (NOW - DAY, NOW - DAY),
                )
            conn.execute(
                "INSERT INTO topic_queue (id, channel_id, niche_profile_id, "
                "topic, source, priority, status, created_at) VALUES "
                "(?, ?, ?, ?, 'user', ?, 'pending', ?)",
                (tid, CHAN, niche, topic, prio, NOW - DAY),
            )
    finally:
        conn.close()

    summary = SchedulerTick(env.factory, queue=env.queue).tick(now_ms=NOW)

    assert summary["materialised"] == 1
    projects = _projects(env)
    assert len(projects) == 1
    assert projects[0]["topic"] == "High priority topic"
    assert projects[0]["topic_source"] == "schedule"

    tq = {r["id"]: r for r in _rows(env, "SELECT * FROM topic_queue")}
    assert tq["tq-high"]["status"] == "used"
    assert tq["tq-high"]["used_in_project_id"] == projects[0]["id"]
    assert tq["tq-low"]["status"] == "pending"
    assert tq["tq-other-niche"]["status"] == "pending"


# ---------------------------------------------------------------------------
# fixed_list rotation + failure paths (extra coverage)
# ---------------------------------------------------------------------------

def test_fixed_list_rotation(env):
    _seed_channel_niche(env)
    cadence = {
        "type": "interval",
        "interval_hours": 1,
        "topics": ["First topic", "Second topic"],
    }
    _seed_schedule(env, cadence=cadence, topic_mode="fixed_list",
                   next_run_at=NOW - HOUR)
    tick = SchedulerTick(env.factory, queue=env.queue)

    tick.tick(now_ms=NOW)
    # Pin the starting index immediately: the first-ever created run must
    # take topics[0] (an off-by-one here is the R2-004 swap signature).
    assert [p["topic"] for p in _projects(env)] == ["First topic"]

    # Force the schedule due again; rotation advances by prior runs count.
    conn = env.factory()
    try:
        conn.execute(
            "UPDATE schedules SET next_run_at = ? WHERE id = 'sched-1'",
            (NOW - HOUR,),
        )
    finally:
        conn.close()
    tick.tick(now_ms=NOW)

    topics = [p["topic"] for p in _projects(env)]
    assert topics == ["First topic", "Second topic"]


def test_fixed_list_rotation_ignores_failed_runs(env):
    """R2-004 regression: a prior 'failed' run must not advance rotation.

    _record_failure writes a schedule_runs row (status='failed', no
    project) for every broken slot.  The old rotation counted ALL runs,
    so one failed tick shifted the index by one and the first topic came
    out as "Second topic" — the exact swap seen in full-suite runs.
    Rotation must count only runs that actually consumed a topic
    (status='created'), and must wrap by that count.
    """
    _seed_channel_niche(env)
    cadence = {
        "type": "interval",
        "interval_hours": 1,
        "topics": ["First topic", "Second topic"],
    }
    _seed_schedule(env, cadence=cadence, topic_mode="fixed_list",
                   next_run_at=NOW - HOUR)
    # Seed the row _record_failure would have written for an earlier
    # broken slot (e.g. the channel was missing at that tick).
    conn = env.factory()
    try:
        conn.execute(
            "INSERT INTO schedule_runs (id, schedule_id, project_id, "
            "status, reason, detail_json, started_at, finished_at, "
            "created_at) VALUES ('run-failed-0', 'sched-1', NULL, "
            "'failed', 'channel not found: chan-1', ?, ?, ?, ?)",
            (json.dumps({"error": "channel not found: chan-1"}),
             NOW - DAY, NOW - DAY, NOW - DAY),
        )
    finally:
        conn.close()

    tick = SchedulerTick(env.factory, queue=env.queue)
    for expected in (
        ["First topic"],
        ["First topic", "Second topic"],
        # Third created run wraps around by the created-run count.
        ["First topic", "Second topic", "First topic"],
    ):
        summary = tick.tick(now_ms=NOW)
        assert summary["materialised"] == 1
        assert [p["topic"] for p in _projects(env)] == expected
        conn = env.factory()
        try:
            conn.execute(
                "UPDATE schedules SET next_run_at = ? WHERE id = 'sched-1'",
                (NOW - HOUR,),
            )
        finally:
            conn.close()

    # The seeded failure is still in the history, untouched, and the
    # created runs are exactly the three projects, in rowid order.
    runs = _runs(env, "sched-1")
    assert [r["status"] for r in runs] == [
        "failed", "created", "created", "created",
    ]


def test_missing_channel_fails_one_schedule_others_continue(env):
    _seed_channel_niche(env)
    # A schedule pointing at a channel that does not exist can only be
    # seeded with FK enforcement off (its own FK would reject the row).
    _seed_schedule(env, "sched-good", next_run_at=NOW - HOUR)
    conn = sqlite3.connect(str(env.path))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO schedules (id, name, channel_id, niche_profile_id, "
            "cadence_json, topic_mode, publish_mode, is_enabled, "
            "next_run_at, created_at, updated_at) VALUES ('sched-bad', "
            "'Bad', 'chan-missing', ?, ?, 'auto', 'review_first', 1, ?, ?, ?)",
            (NICHE, json.dumps({"type": "daily", "time": "09:00"}),
             NOW - 2 * HOUR, NOW - DAY, NOW - DAY),
        )
        conn.commit()
    finally:
        conn.close()

    summary = SchedulerTick(env.factory, queue=env.queue).tick(now_ms=NOW)

    assert summary["checked"] == 2
    assert summary["materialised"] == 1
    assert summary["failed"] == 1
    assert len(_projects(env)) == 1
    bad_runs = _runs(env, "sched-bad")
    assert len(bad_runs) == 1
    assert bad_runs[0]["status"] == "failed"
    assert bad_runs[0]["project_id"] is None
    assert "chan-missing" in (bad_runs[0]["reason"] or "")


def test_events_emitted_on_materialise(env):
    _seed_channel_niche(env)
    _seed_schedule(env, next_run_at=NOW - HOUR)

    published = []

    class FakeHub:
        def publish(self, event_type, payload=None, **kwargs):
            published.append((event_type, payload, kwargs))

    tick = SchedulerTick(env.factory, queue=env.queue, events=FakeHub())
    summary = tick.tick(now_ms=NOW)

    assert summary["materialised"] == 1
    assert len(published) == 1
    event_type, payload, kwargs = published[0]
    assert event_type == "schedule.materialised"
    assert payload["schedule_id"] == "sched-1"
    assert payload["project_id"] == _projects(env)[0]["id"]
    assert kwargs.get("project_id") == payload["project_id"]


def test_broken_events_hub_does_not_break_tick(env):
    _seed_channel_niche(env)
    _seed_schedule(env, next_run_at=NOW - HOUR)

    class ExplodingHub:
        def publish(self, *args, **kwargs):
            raise RuntimeError("hub down")

    summary = SchedulerTick(
        env.factory, queue=env.queue, events=ExplodingHub()
    ).tick(now_ms=NOW)
    assert summary["materialised"] == 1


def test_weekly_schedule_tick(env):
    _seed_channel_niche(env)
    # Friday 07:00 slot, now Friday 12:00 -> next is next Friday 07:00.
    _seed_schedule(
        env,
        cadence={"type": "weekly", "weekday": 4, "time": "07:00"},
        next_run_at=_at(7),
    )
    summary = SchedulerTick(env.factory, queue=env.queue).tick(now_ms=NOW)
    assert summary["materialised"] == 1
    assert _schedule(env, "sched-1")["next_run_at"] == _at(7, day=30)


# ---------------------------------------------------------------------------
# (g) second tick immediately after -> no duplicate
# ---------------------------------------------------------------------------

def test_second_tick_no_duplicate(env):
    _seed_channel_niche(env)
    _seed_schedule(env, next_run_at=_at(9))
    tick = SchedulerTick(env.factory, queue=env.queue)

    first = tick.tick(now_ms=NOW)
    second = tick.tick(now_ms=NOW)

    assert first["materialised"] == 1
    assert second == {"checked": 0, "materialised": 0, "collapsed": 0, "failed": 0}
    assert len(_projects(env)) == 1
    assert len(_runs(env, "sched-1")) == 1
    jobs = _jobs_for_project(env, _projects(env)[0]["id"])
    assert len(jobs) >= 1  # and not duplicated by the second tick
    if importlib.util.find_spec("app.execution.dag") is None:
        assert len(jobs) == 1
