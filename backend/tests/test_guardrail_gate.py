"""Guardrail taboo screening (Round 4 fix-pass, D3) — worker tests.

Companion to ``test_guardrails_automation.py`` (the W5 defect
reproduction, whose two ``test_blocked_topic_*`` tests now pass).
This file pins the fix's shape:

* control: a CLEAN topic flows through schedule materialisation AND
  the research stage entry completely unimpeded (no park, no
  approval, no guardrail run record);
* scheduler point: a taboo topic creates no project, consumes no
  queued topic, and is recorded as a ``'failed'`` schedule_run whose
  reason + ``detail_json`` carry the machine-readable
  ``reason_code='guardrail_taboo'`` and the matched term;
* research point: a taboo project created OFF the scheduler path is
  parked fail-closed at research entry — Gate A approval with
  ``reason_code='guardrail_taboo'``, project ``awaiting_review``,
  ``GatePending`` raised, no research artefact written;
* resume: a decided/approved guardrail approval is the explicit
  human override (budget_gate pattern) and research then proceeds;
* matcher units: profile resolution + ``prompt_matches_taboo`` reuse.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import db as db_module
from app.execution.dag import create_project_dag
from app.execution.guardrail_gate import (
    GUARDRAIL_TABOO_REASON,
    match_taboo_term,
    parse_taboo_terms,
)
from app.execution.handlers import build_registry
from app.execution.runner import GatePending
from app.execution.scheduler import SchedulerTick
from app.queue import SQLiteJobQueue

DAY = 86_400_000
CHAN = "chan-gate"
NICHE = "niche-gate"
BLOCKED_TERM = "gambling"
BLOCKED_TOPIC = "Gambling tricks that always win at the casino"
CLEAN_TOPIC = "How deep sea fish survive crushing pressure"

NOW_DT = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
NOW = int(NOW_DT.timestamp() * 1000)


@pytest.fixture()
def env(tmp_path):
    db_path = tmp_path / "guardrail_gate_test.db"
    conn = db_module.init_db(db_path)
    conn.close()

    def factory():
        return db_module.get_connection(db_path)

    return SimpleNamespace(path=db_path, factory=factory, queue=SQLiteJobQueue(factory))


def _seed_channel_niche(env):
    conn = env.factory()
    try:
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title, language, "
            "status, created_at, updated_at) VALUES (?, ?, ?, 'en', "
            "'active', ?, ?)",
            (CHAN, f"yt-{CHAN}", "Gate Channel", NOW - DAY, NOW - DAY),
        )
        conn.execute(
            "INSERT INTO niche_profiles (id, key, label, source, "
            "words_per_minute, taboo_json, created_at, updated_at) "
            "VALUES (?, ?, ?, 'builtin', 150, ?, ?, ?)",
            (
                NICHE,
                f"key-{NICHE}",
                "Gate Niche",
                json.dumps([BLOCKED_TERM]),
                NOW - DAY,
                NOW - DAY,
            ),
        )
    finally:
        conn.close()


def _seed_schedule_and_queue(env, topic):
    conn = env.factory()
    try:
        conn.execute(
            "INSERT INTO schedules (id, name, channel_id, niche_profile_id, "
            "cadence_json, topic_mode, target_minutes, publish_mode, "
            "is_enabled, next_run_at, created_at, updated_at) "
            "VALUES ('sched-gate', 'Gate schedule', ?, ?, ?, 'queue', 12, "
            "'review_first', 1, ?, ?, ?)",
            (
                CHAN,
                NICHE,
                json.dumps({"type": "daily", "time": "09:00"}),
                NOW - 3_600_000,
                NOW - DAY,
                NOW - DAY,
            ),
        )
        conn.execute(
            "INSERT INTO topic_queue (id, channel_id, niche_profile_id, "
            "topic, source, priority, status, created_at) VALUES "
            "('tq-gate', ?, ?, ?, 'user', 10, 'pending', ?)",
            (CHAN, NICHE, topic, NOW - DAY),
        )
    finally:
        conn.close()


def _seed_project_direct(env, project_id="proj-gate", topic=BLOCKED_TOPIC):
    """A project created OFF the automation path (manual / legacy)."""
    conn = env.factory()
    try:
        conn.execute(
            "INSERT INTO projects (id, channel_id, niche_profile_id, topic,"
            " topic_source, status, publish_mode, total_cost_micros,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'user', 'draft', 'review_first', 0, ?, ?)",
            (project_id, CHAN, NICHE, topic, NOW - DAY, NOW - DAY),
        )
    finally:
        conn.close()
    return project_id


def _rows(env, sql, params=()):
    conn = env.factory()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _research_job(env, project_id):
    ids = create_project_dag(env.queue, project_id)
    job = env.queue.get(ids[0])
    assert job["kind"] == "stage.research"
    return job


# ---------------------------------------------------------------------------
# Control (acceptance b): clean topic, both points, unimpeded
# ---------------------------------------------------------------------------


def test_control_clean_topic_flows_through_materialisation_and_research(env):
    _seed_channel_niche(env)
    _seed_schedule_and_queue(env, CLEAN_TOPIC)

    summary = SchedulerTick(env.factory, queue=env.queue).tick(now_ms=NOW)
    assert summary == {"checked": 1, "materialised": 1, "collapsed": 0, "failed": 0}

    projects = _rows(env, "SELECT * FROM projects")
    assert len(projects) == 1
    project = projects[0]
    assert project["topic"] == CLEAN_TOPIC

    # Schedule-run record is a plain 'created' run — no guardrail trace.
    runs = _rows(env, "SELECT * FROM schedule_runs")
    assert len(runs) == 1
    assert runs[0]["status"] == "created"
    assert GUARDRAIL_TABOO_REASON not in (runs[0]["reason"] or "")
    assert GUARDRAIL_TABOO_REASON not in (runs[0]["detail_json"] or "")

    # Research entry: proceeds, writes its artefact, no park/approval.
    job = _research_job(env, project["id"])
    registry = build_registry(env.factory)
    result = asyncio.run(registry["stage.research"](job))
    assert result["stage"] == "research"
    artefacts = _rows(
        env,
        "SELECT * FROM project_artefacts WHERE project_id = ? "
        "AND artefact_type = 'research_brief'",
        (project["id"],),
    )
    assert len(artefacts) == 1
    # No park happened: the approvals table is created lazily by the
    # gate bridge, so it may not even exist on the clean path — either
    # way there must be no guardrail approval for this project.
    tables = {
        r["name"]
        for r in _rows(env, "SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "approvals" in tables:
        assert _rows(
            env,
            "SELECT * FROM approvals WHERE reason_code = ?",
            (GUARDRAIL_TABOO_REASON,),
        ) == []
    after = _rows(env, "SELECT * FROM projects")[0]
    assert after["status"] == "in_production"


# ---------------------------------------------------------------------------
# Scheduler point: visible, machine-readable refusal; nothing consumed
# ---------------------------------------------------------------------------


def test_scheduler_records_guardrail_run_and_consumes_nothing(env):
    _seed_channel_niche(env)
    _seed_schedule_and_queue(env, BLOCKED_TOPIC)

    summary = SchedulerTick(env.factory, queue=env.queue).tick(now_ms=NOW)
    assert summary["materialised"] == 0
    assert summary["failed"] == 1
    assert _rows(env, "SELECT * FROM projects") == []

    runs = _rows(env, "SELECT * FROM schedule_runs")
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "failed"
    assert run["project_id"] is None
    assert GUARDRAIL_TABOO_REASON in run["reason"]
    assert BLOCKED_TERM in run["reason"]
    detail = json.loads(run["detail_json"])
    assert detail["reason_code"] == GUARDRAIL_TABOO_REASON
    assert detail["guardrail"]["matched_taboo_term"] == BLOCKED_TERM
    assert detail["guardrail"]["topic"] == BLOCKED_TOPIC

    # The queued topic was NOT consumed by the refused slot.
    tq = _rows(env, "SELECT * FROM topic_queue WHERE id = 'tq-gate'")[0]
    assert tq["status"] == "pending"
    assert tq["used_in_project_id"] is None


# ---------------------------------------------------------------------------
# Research point: fail-closed park for off-scheduler projects + resume
# ---------------------------------------------------------------------------


def test_research_entry_parks_taboo_project_with_guardrail_approval(env):
    _seed_channel_niche(env)
    project_id = _seed_project_direct(env)
    job = _research_job(env, project_id)
    registry = build_registry(env.factory)

    with pytest.raises(GatePending) as excinfo:
        asyncio.run(registry["stage.research"](job))
    message = str(excinfo.value)
    assert GUARDRAIL_TABOO_REASON in message
    assert BLOCKED_TERM in message

    # No research artefact was written; the project is parked visibly.
    assert _rows(
        env,
        "SELECT * FROM project_artefacts WHERE project_id = ? "
        "AND artefact_type = 'research_brief'",
        (project_id,),
    ) == []
    project = _rows(env, "SELECT * FROM projects WHERE id = ?", (project_id,))[0]
    assert project["status"] == "awaiting_review"

    approvals = _rows(env, "SELECT * FROM approvals WHERE project_id = ?", (project_id,))
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval["gate"] == "a"
    assert approval["status"] == "pending"
    assert approval["reason_code"] == GUARDRAIL_TABOO_REASON
    assert BLOCKED_TERM in approval["note"]
    figures = json.loads(approval["artefact_versions_json"])["guardrail"]
    assert figures["matched_taboo_term"] == BLOCKED_TERM
    assert figures["topic"] == BLOCKED_TOPIC


def test_research_entry_approved_override_resumes(env):
    _seed_channel_niche(env)
    project_id = _seed_project_direct(env)
    job = _research_job(env, project_id)
    registry = build_registry(env.factory)

    with pytest.raises(GatePending):
        asyncio.run(registry["stage.research"](job))

    # The human explicitly approves the guardrail approval (the
    # budget_gate override pattern): research may then proceed.
    conn = env.factory()
    try:
        conn.execute(
            "UPDATE approvals SET decision = 'approved', status = 'decided', "
            "decided_at = ? WHERE project_id = ? AND reason_code = ?",
            (NOW, project_id, GUARDRAIL_TABOO_REASON),
        )
        conn.commit()
    finally:
        conn.close()

    result = asyncio.run(registry["stage.research"](job))
    assert result["stage"] == "research"
    assert len(_rows(
        env,
        "SELECT * FROM project_artefacts WHERE project_id = ? "
        "AND artefact_type = 'research_brief'",
        (project_id,),
    )) == 1


# ---------------------------------------------------------------------------
# Matcher / profile-resolution units
# ---------------------------------------------------------------------------


def test_match_taboo_term_uses_niche_row_taboo_json():
    row = {"id": NICHE, "key": "gate-key", "taboo_json": json.dumps([" gambling "])}
    assert match_taboo_term(BLOCKED_TOPIC, row, niche_id=NICHE) == "gambling"
    assert match_taboo_term(CLEAN_TOPIC, row, niche_id=NICHE) is None
    # No taboo configured -> nothing matches (recorded "none known").
    empty = {"id": NICHE, "key": "gate-key", "taboo_json": None}
    assert match_taboo_term(BLOCKED_TOPIC, empty, niche_id=NICHE) is None
    assert match_taboo_term("", row, niche_id=NICHE) is None


def test_parse_taboo_terms_tolerant_decode():
    assert parse_taboo_terms(None) == []
    assert parse_taboo_terms('["a", "b"]') == ["a", "b"]
    assert parse_taboo_terms(["a", ""]) == ["a"]
    assert parse_taboo_terms("a, b") == ["a", "b"]
    assert parse_taboo_terms("") == []
