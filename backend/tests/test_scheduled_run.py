"""Tests for the Round 6 one-shot scheduled run (Task Scheduler wake).

Covers ``app.execution.scheduled_run`` (list / dry-run / flags / real
tick) and the ``--scheduled-run`` argument dispatch in ``app.main.run``:

* argument handling triggers the due-schedule path (mocked tick runner);
* ``--dry-run`` lists due schedules without executing anything;
* STOP ALL / pause_all block the run exactly like the other one-shot
  consumers (claimer / analytics poller) honour them;
* a normal launch without the argument is unaffected (the serving path
  runs, the scheduled path is never touched).

DB fixtures mirror ``test_execution_scheduler.py``: a real SQLite DB
in ``tmp_path`` via ``app.db.init_db``, seeded directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import db as db_module
from app.execution import scheduled_run
from app.execution.flags import set_flag
from app.queue import SQLiteJobQueue

HOUR = 3_600_000
DAY = 86_400_000

CHAN = "chan-1"
NICHE = "niche-1"


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


NOW_DT = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
NOW = _ms(NOW_DT)


@pytest.fixture()
def env(tmp_path):
    db_path = tmp_path / "scheduled_run_test.db"
    conn = db_module.init_db(db_path)
    conn.close()

    def factory():
        return db_module.get_connection(db_path)

    return SimpleNamespace(
        path=db_path, factory=factory, queue=SQLiteJobQueue(factory)
    )


def _seed_channel_niche(env):
    conn = env.factory()
    try:
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title, language, "
            "status, created_at, updated_at) VALUES (?, ?, ?, 'en', "
            "'active', ?, ?)",
            (CHAN, f"yt-{CHAN}", "Channel", NOW - DAY, NOW - DAY),
        )
        conn.execute(
            "INSERT INTO niche_profiles (id, key, label, source, "
            "words_per_minute, created_at, updated_at) VALUES (?, ?, ?, "
            "'builtin', 150, ?, ?)",
            (NICHE, f"key-{NICHE}", "Niche", NOW - DAY, NOW - DAY),
        )
    finally:
        conn.close()


def _seed_schedule(env, sid="sched-1", *, cadence=None, next_run_at=None,
                   is_enabled=1):
    if cadence is None:
        cadence = {"type": "daily", "time": "09:00"}
    conn = env.factory()
    try:
        conn.execute(
            "INSERT INTO schedules (id, name, channel_id, niche_profile_id, "
            "cadence_json, topic_mode, target_minutes, publish_mode, "
            "is_enabled, next_run_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'auto', 12, 'review_first', ?, ?, ?, ?)",
            (
                sid,
                f"Schedule {sid}",
                CHAN,
                NICHE,
                json.dumps(cadence),
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


class _FakeTick:
    """Mock scheduler runner: records tick calls, returns a summary."""

    def __init__(self):
        self.calls: list = []

    def tick(self, now_ms=None):
        self.calls.append(now_ms)
        return {"checked": 1, "materialised": 1, "collapsed": 0, "failed": 0}


def _fake_factory(fake):
    return lambda conn_factory: fake


# ---------------------------------------------------------------------------
# Argument handling / dispatch (app.main.run)
# ---------------------------------------------------------------------------


class TestMainDispatch:
    def test_scheduled_run_argument_dispatches(self, monkeypatch):
        import app.main as main_module
        import app.execution.scheduled_run as sr_module

        seen = {}

        def fake_scheduled_main(argv):
            seen["argv"] = argv
            return 7

        monkeypatch.setattr(sr_module, "main", fake_scheduled_main)
        monkeypatch.setattr(
            "sys.argv", ["youtube-agent-backend", "--scheduled-run"]
        )
        with pytest.raises(SystemExit) as excinfo:
            main_module.run()
        assert excinfo.value.code == 7
        assert seen["argv"] == ["--scheduled-run"]

    def test_dry_run_flag_also_dispatches(self, monkeypatch):
        import app.main as main_module
        import app.execution.scheduled_run as sr_module

        seen = {}

        def fake_scheduled_main(argv):
            seen["argv"] = argv
            return 0

        monkeypatch.setattr(sr_module, "main", fake_scheduled_main)
        monkeypatch.setattr(
            "sys.argv",
            ["youtube-agent-backend", "--scheduled-run", "--dry-run"],
        )
        with pytest.raises(SystemExit) as excinfo:
            main_module.run()
        assert excinfo.value.code == 0
        assert seen["argv"] == ["--scheduled-run", "--dry-run"]

    def test_normal_launch_without_argument_unaffected(self, monkeypatch):
        """No flag -> the serving path runs; scheduled main never fires."""
        import app.main as main_module
        import app.execution.scheduled_run as sr_module

        def fail_scheduled_main(argv):  # pragma: no cover - guard
            raise AssertionError("scheduled path must not run")

        monkeypatch.setattr(sr_module, "main", fail_scheduled_main)
        monkeypatch.setattr("sys.argv", ["youtube-agent-backend"])

        from app.config import Settings

        settings = Settings(port=0, token="tok", boot_id="boot")
        monkeypatch.setattr(main_module, "load_settings", lambda: settings)
        monkeypatch.setattr(main_module, "create_app", lambda s: object())

        served = {}

        def fake_uvicorn_run(app, **kwargs):
            served.update(kwargs)

        monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
        main_module.run()  # returns normally (no SystemExit)
        assert served["host"] == "127.0.0.1"
        assert served["port"] > 0  # free port was picked for port=0


# ---------------------------------------------------------------------------
# CLI-level usage handling
# ---------------------------------------------------------------------------


class TestCliUsage:
    def test_dry_run_without_scheduled_run_is_usage_error(self, env, capsys):
        code = scheduled_run.main(
            ["--dry-run"], conn_factory=env.factory, now_ms=NOW
        )
        assert code == scheduled_run.EXIT_USAGE
        assert "--scheduled-run" in capsys.readouterr().err

    def test_missing_flag_is_usage_error(self, env):
        code = scheduled_run.main([], conn_factory=env.factory, now_ms=NOW)
        assert code == scheduled_run.EXIT_USAGE


# ---------------------------------------------------------------------------
# Due listing + dry-run
# ---------------------------------------------------------------------------


class TestDueListing:
    def test_lists_only_due_enabled_schedules(self, env):
        _seed_channel_niche(env)
        _seed_schedule(env, "sched-due", next_run_at=NOW - HOUR)
        _seed_schedule(env, "sched-future", next_run_at=NOW + HOUR)
        _seed_schedule(env, "sched-disabled", next_run_at=NOW - HOUR,
                       is_enabled=0)

        due = scheduled_run.list_due_schedules(env.factory, now_ms=NOW)
        assert [d["id"] for d in due] == ["sched-due"]
        entry = due[0]
        assert entry["due_since_ms"] == NOW - HOUR
        assert entry["overdue_ms"] == HOUR
        assert entry["missed_slots"] == 1
        assert entry["missed_collapsed"] == 0
        assert entry["slot_at_ms"] == NOW - HOUR

    def test_listing_reports_catch_up_collapse(self, env):
        _seed_channel_niche(env)
        # Hourly schedule 5 slots behind (anchor mid-interval).
        anchor = NOW - 5 * HOUR + HOUR // 2
        _seed_schedule(
            env,
            cadence={"type": "interval", "interval_hours": 1},
            next_run_at=anchor,
        )
        due = scheduled_run.list_due_schedules(env.factory, now_ms=NOW)
        assert len(due) == 1
        assert due[0]["missed_slots"] == 5
        assert due[0]["missed_collapsed"] == 4
        assert due[0]["slot_at_ms"] == NOW - HOUR // 2

    def test_dry_run_lists_without_executing(self, env):
        _seed_channel_niche(env)
        _seed_schedule(env, next_run_at=NOW - HOUR)
        fake = _FakeTick()

        report = scheduled_run.run_scheduled_once(
            env.factory, now_ms=NOW, dry_run=True,
            tick_factory=_fake_factory(fake),
        )

        assert report["status"] == "dry_run"
        assert report["summary"] is None
        assert report["due_count"] == 1
        assert report["due"][0]["id"] == "sched-1"
        assert fake.calls == []  # the runner was never invoked
        # Nothing changed in the DB.
        assert _rows(env, "SELECT * FROM projects") == []
        assert _rows(env, "SELECT * FROM schedule_runs") == []
        sched = _rows(env, "SELECT * FROM schedules WHERE id = 'sched-1'")[0]
        assert sched["next_run_at"] == NOW - HOUR
        assert sched["last_run_at"] is None

    def test_main_dry_run_prints_json_and_exits_zero(self, env, capsys):
        _seed_channel_niche(env)
        _seed_schedule(env, next_run_at=NOW - HOUR)
        fake = _FakeTick()

        code = scheduled_run.main(
            ["--scheduled-run", "--dry-run"],
            conn_factory=env.factory,
            now_ms=NOW,
            tick_factory=_fake_factory(fake),
        )
        assert code == scheduled_run.EXIT_OK
        report = json.loads(capsys.readouterr().out)
        assert report["status"] == "dry_run"
        assert report["due_count"] == 1
        assert fake.calls == []


# ---------------------------------------------------------------------------
# Real run + mocked runner
# ---------------------------------------------------------------------------


class TestRunOnce:
    def test_argument_path_invokes_runner_once(self, env):
        """run_scheduled_once triggers the due-schedule path (mock tick)."""
        _seed_channel_niche(env)
        _seed_schedule(env, next_run_at=NOW - HOUR)
        fake = _FakeTick()

        report = scheduled_run.run_scheduled_once(
            env.factory, now_ms=NOW, tick_factory=_fake_factory(fake)
        )

        assert fake.calls == [NOW]
        assert report["status"] == "completed"
        assert report["summary"] == {
            "checked": 1, "materialised": 1, "collapsed": 0, "failed": 0,
        }
        assert report["due_count"] == 1

    def test_real_tick_materialises_due_schedule(self, env):
        """End-to-end with the real SchedulerTick: catch_up_once applies."""
        _seed_channel_niche(env)
        anchor = NOW - 5 * HOUR + HOUR // 2  # 5 hourly slots behind
        _seed_schedule(
            env,
            cadence={"type": "interval", "interval_hours": 1},
            next_run_at=anchor,
        )

        report = scheduled_run.run_scheduled_once(env.factory, now_ms=NOW)

        assert report["status"] == "completed"
        assert report["summary"]["materialised"] == 1
        assert report["summary"]["collapsed"] == 4
        projects = _rows(env, "SELECT * FROM projects")
        assert len(projects) == 1  # one project, not five
        assert projects[0]["schedule_id"] == "sched-1"
        sched = _rows(env, "SELECT * FROM schedules WHERE id = 'sched-1'")[0]
        assert sched["next_run_at"] == NOW + HOUR // 2  # advanced past now
        assert sched["last_run_at"] == NOW

    def test_no_due_schedules_still_invokes_tick_and_reports_zero(self, env):
        _seed_channel_niche(env)
        _seed_schedule(env, next_run_at=NOW + HOUR)
        fake = _FakeTick()

        report = scheduled_run.run_scheduled_once(
            env.factory, now_ms=NOW, tick_factory=_fake_factory(fake)
        )
        assert report["due_count"] == 0
        assert report["due"] == []
        assert fake.calls == [NOW]


# ---------------------------------------------------------------------------
# STOP ALL / pause flags
# ---------------------------------------------------------------------------


class TestFlags:
    @pytest.mark.parametrize("flag", ["pause_all", "stop_all"])
    def test_flag_blocks_run(self, env, flag):
        _seed_channel_niche(env)
        _seed_schedule(env, next_run_at=NOW - HOUR)
        set_flag(env.factory, flag, True)
        fake = _FakeTick()

        report = scheduled_run.run_scheduled_once(
            env.factory, now_ms=NOW, tick_factory=_fake_factory(fake)
        )

        assert report["status"] == "blocked"
        assert report["blocked_by"] == flag
        assert report["flags"][flag] is True
        assert report["summary"] is None
        assert fake.calls == []  # runner never invoked
        assert _rows(env, "SELECT * FROM projects") == []
        sched = _rows(env, "SELECT * FROM schedules WHERE id = 'sched-1'")[0]
        assert sched["next_run_at"] == NOW - HOUR  # not advanced

    def test_stop_all_takes_precedence_in_report(self, env):
        _seed_channel_niche(env)
        _seed_schedule(env, next_run_at=NOW - HOUR)
        set_flag(env.factory, "pause_all", True)
        set_flag(env.factory, "stop_all", True)

        report = scheduled_run.run_scheduled_once(
            env.factory, now_ms=NOW, tick_factory=_fake_factory(_FakeTick())
        )
        assert report["blocked_by"] == "stop_all"

    def test_flag_blocks_even_dry_run_listing_execution_state(self, env):
        """Blocked wins over dry-run: status reports the block, and the
        due list is still shown for diagnosis."""
        _seed_channel_niche(env)
        _seed_schedule(env, next_run_at=NOW - HOUR)
        set_flag(env.factory, "stop_all", True)

        report = scheduled_run.run_scheduled_once(
            env.factory, now_ms=NOW, dry_run=True,
            tick_factory=_fake_factory(_FakeTick()),
        )
        assert report["status"] == "blocked"
        assert report["blocked_by"] == "stop_all"
        assert report["due_count"] == 1
