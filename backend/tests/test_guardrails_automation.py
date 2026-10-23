"""Guardrails on the AUTOMATION path — spot verification (Round 4, W5).

QUESTION
--------
Roadmap promise: a topic containing a blocked term must be REFUSED on
the automation path (schedule -> tick -> project -> production DAG).
This file answers WHERE the refusal happens: at schedule creation, at
the tick / project creation, at the first production stage (research),
or nowhere.

ANSWER (verified 2026-10-23, HEAD e5bf77a): **NOWHERE — DEFECT.**

TRACE (file:line evidence)
--------------------------
Where "blocked terms" live today:

* There is NO blocked-terms guardrail setting at all.  The Settings
  UI's "Budgets & Guardrails" card (src/screens/Settings.tsx:857) has
  a typed backend surface for BUDGETS only; the "Other guardrails"
  block is explicitly local-only — src/screens/Settings.tsx:712-714
  ("The remaining guardrail controls have NO backend field yet, so
  they stay local-only") and :975-977 ("Not saved to backend yet ...
  They are not persisted and are not enforced by the backend") — and
  it contains no blocked-terms control in the first place (only
  review-first, kill switch, pause-at-cap, daily upload cap).
* Backend settings: backend/app/routers/settings.py exposes a generic
  key/value PUT (:150-168, any non-secret key is stored) plus the
  typed budget policy GET|PUT /settings/budget (:116-147;
  app.providers.budget.policy — per-video/daily/monthly caps, warn /
  soft %).  No code anywhere reads a blocked-terms key from settings.
* The only blocked-term list in the codebase is PER-NICHE:
  ``NicheProfile.taboo`` (backend/app/niches/models.py:170-173,
  "Topics / depictions that must never appear in this niche"),
  persisted as ``niche_profiles.taboo_json``
  (backend/app/schema_ddl.py:109).  Its only consumers are:
    - backend/app/integration/content.py:1082-1083 — interpolated
      into the scripting PROMPT ("Taboo — never include: ..."); a
      soft instruction to the LLM, not a gate;
    - backend/app/integration/visuals_stage.py:1520-1524 — copied
      into ``NicheSafetyProfile.taboo_terms`` for the scene-safety
      layer, backend/app/pipeline/scenes/safety.py:267-278, which
      screens each scene's *visual prompt* at the VISUALS stage
      (late in production) and blocks matching SCENES.  The project
      TOPIC is never screened there either.
  ``prompt_matches_taboo`` (safety.py:372-379) — the one helper that
  could screen a raw topic string — has NO production caller (only
  the re-export in app/pipeline/scenes/__init__.py).

Who consults them on the automation path: NOBODY.

* Schedule creation — backend/app/routers/schedules.py:31-44
  (``ScheduleCreate`` has no topic / blocked-term field at all) and
  :161 ``create_schedule``: stores the record; reads no settings,
  no niche taboo.
* Schedule tick / project creation — backend/app/execution/
  scheduler.py:320 ``tick`` -> :368 ``_materialise`` -> :465
  ``_select_topic`` (queue / fixed_list / auto sentinel): the niche
  row IS fetched (:391-395) but only ``words_per_minute`` is used
  (:403, :521); ``taboo_json`` is never read.  The project INSERT
  (:406) is unconditional — the only failure modes are missing
  channel/niche, empty topic queue, empty fixed list.
* First production stage (research) — backend/app/execution/
  handlers.py:430-449: the research handler consults exactly one
  guardrail, ``budget_gate.check_budget_for_project`` (budget caps
  fail closed via GatePending).  No taboo / blocked-term / topic
  screening follows; the stage proceeds (real integration or stub)
  and writes a ``research_brief`` artefact for any topic.
  (The manual path, routers/projects.py:266 ``create_project``, has
  no blocked-term check either.)

TESTS IN THIS FILE
------------------
* ``test_blocked_topic_*`` — the roadmap-promise tests.  They drive
  the real automation seams the existing suites use (SchedulerTick
  over a real SQLite DB + the DAG's ``stage.research`` handler via
  ``build_registry``), with the blocked term configured through the
  REAL blocked-term mechanism that exists (the niche profile's
  ``taboo_json``).  A refusal at ANY point up to and including the
  first stage satisfies them (no project created + a guardrail
  schedule_run failure, OR research fails closed with a guardrail
  reason and writes no research artefact).  **They currently FAIL —
  that failure is the defect reproduction.**  They are deliberately
  NOT xfail: the parent report records them as failing.
* ``test_control_clean_topic_*`` — control: a clean topic on the
  identical path materialises and passes research.  PASSES now and
  must keep passing after any fix (the fix must not break the path).
* ``test_taboo_list_is_real_but_enforced_only_at_scene_level`` and
  ``test_settings_typed_surface_has_no_blocked_terms`` — passing
  characterisation tests pinning the trace above.

DEFECT (reported, not fixed — this worker is test-only)
-------------------------------------------------------
Severity: HIGH.  Unattended scheduled production will happily create
and research a video on a topic the niche profile explicitly lists
as taboo (e.g. "gambling" below); the earliest anything notices is
the visuals stage, and even there only scene *visual prompts* are
screened — the topic/title/script are never checked against the
taboo list anywhere in the codebase, and no user-facing blocked-
terms guardrail setting exists at all.  Mitigations that keep this
from being Critical today: publish is still fake-provider /
review-first by default, and scene-level taboo screening does run
late in the pipeline.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import db as db_module
from app.execution.handlers import build_registry
from app.execution.scheduler import SchedulerTick
from app.queue import SQLiteJobQueue

DAY = 86_400_000

CHAN = "chan-guard"
NICHE = "niche-guard"

BLOCKED_TERM = "gambling"
BLOCKED_TOPIC = "Gambling tricks that always win at the casino"
CLEAN_TOPIC = "How deep sea fish survive crushing pressure"


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


NOW_DT = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
NOW = _ms(NOW_DT)


@pytest.fixture()
def env(tmp_path):
    db_path = tmp_path / "guardrails_test.db"
    conn = db_module.init_db(db_path)
    conn.close()

    def factory():
        return db_module.get_connection(db_path)

    queue = SQLiteJobQueue(factory)
    return SimpleNamespace(path=db_path, factory=factory, queue=queue)


def _seed_channel_niche(env, *, taboo=(BLOCKED_TERM,)):
    """Seed channel + niche, with the blocked term configured via the
    real blocked-term mechanism: ``niche_profiles.taboo_json`` (the
    DB form of ``NicheProfile.taboo``, niches/models.py:170)."""
    conn = env.factory()
    try:
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title, language, "
            "status, created_at, updated_at) VALUES (?, ?, ?, 'en', "
            "'active', ?, ?)",
            (CHAN, f"yt-{CHAN}", "Guardrail Channel", NOW - DAY, NOW - DAY),
        )
        conn.execute(
            "INSERT INTO niche_profiles (id, key, label, source, "
            "words_per_minute, taboo_json, created_at, updated_at) "
            "VALUES (?, ?, ?, 'builtin', 150, ?, ?, ?)",
            (
                NICHE,
                f"key-{NICHE}",
                "Guardrail Niche",
                json.dumps(list(taboo)),
                NOW - DAY,
                NOW - DAY,
            ),
        )
    finally:
        conn.close()


def _seed_schedule(env, *, topic_mode, cadence=None):
    if cadence is None:
        cadence = {"type": "daily", "time": "09:00"}
    conn = env.factory()
    try:
        conn.execute(
            "INSERT INTO schedules (id, name, channel_id, niche_profile_id, "
            "cadence_json, topic_mode, target_minutes, publish_mode, "
            "is_enabled, next_run_at, created_at, updated_at) "
            "VALUES ('sched-guard', 'Guardrail schedule', ?, ?, ?, ?, 12, "
            "'review_first', 1, ?, ?, ?)",
            (
                CHAN,
                NICHE,
                json.dumps(cadence),
                topic_mode,
                NOW - 3_600_000,
                NOW - DAY,
                NOW - DAY,
            ),
        )
    finally:
        conn.close()


def _seed_queue_topic(env, topic):
    conn = env.factory()
    try:
        conn.execute(
            "INSERT INTO topic_queue (id, channel_id, niche_profile_id, "
            "topic, source, priority, status, created_at) VALUES "
            "('tq-guard', ?, ?, ?, 'user', 10, 'pending', ?)",
            (CHAN, NICHE, topic, NOW - DAY),
        )
    finally:
        conn.close()


def _rows(env, sql, params=()):
    conn = env.factory()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _drive_automation_path(env) -> dict:
    """Tick once; if a project was created, run its first DAG stage
    (``stage.research``) through the real handler registry — exactly
    the seams test_execution_scheduler.py / test_execution_handlers.py
    use.  Returns everything the refusal assertions need."""
    summary = SchedulerTick(env.factory, queue=env.queue).tick(now_ms=NOW)
    projects = _rows(env, "SELECT * FROM projects ORDER BY rowid")
    runs = _rows(env, "SELECT * FROM schedule_runs ORDER BY rowid")
    outcome = {
        "summary": summary,
        "projects": projects,
        "runs": runs,
        "research_error": None,
        "research_result": None,
        "research_artefacts": [],
    }
    if not projects:
        return outcome

    project_id = projects[0]["id"]
    jobs = _rows(
        env,
        "SELECT * FROM jobs WHERE project_id = ? AND kind = 'stage.research'",
        (project_id,),
    )
    assert jobs, "tick created a project but no research job — DAG missing"
    job = env.queue.get(jobs[0]["id"])
    registry = build_registry(env.factory)
    try:
        outcome["research_result"] = asyncio.run(registry["stage.research"](job))
    except Exception as exc:  # a fail-closed stage raises; record it
        outcome["research_error"] = f"{type(exc).__name__}: {exc}"
    outcome["research_artefacts"] = _rows(
        env,
        "SELECT * FROM project_artefacts WHERE project_id = ? "
        "AND artefact_type = 'research_brief'",
        (project_id,),
    )
    return outcome


_GUARDRAIL_WORDS = ("guardrail", "taboo", "blocked term", "blocked topic",
                    "blocked_terms", "blocklist", "block list")


def _mentions_guardrail(text: str) -> bool:
    lowered = (text or "").lower()
    return any(w in lowered for w in _GUARDRAIL_WORDS) or BLOCKED_TERM in lowered


def _refusal_point(outcome: dict) -> str | None:
    """Where (if anywhere) the automation path refused the topic.

    Accepts a refusal at the tick (no project + a guardrail-reasoned
    failed/skipped schedule_run) or at the first production stage
    (research raises a guardrail error and writes no artefact)."""
    if not outcome["projects"]:
        for run in outcome["runs"]:
            if run["status"] in ("failed", "skipped", "blocked", "refused"):
                reason = " ".join(
                    str(run.get(k) or "")
                    for k in ("reason", "detail_json")
                )
                if _mentions_guardrail(reason):
                    return f"tick (schedule_run {run['status']}: {reason})"
        return None
    err = outcome["research_error"]
    if err and _mentions_guardrail(err) and not outcome["research_artefacts"]:
        return f"first production stage / research ({err})"
    return None


# ---------------------------------------------------------------------------
# Roadmap-promise tests — CURRENTLY FAILING (defect reproduction)
# ---------------------------------------------------------------------------

def test_blocked_topic_from_queue_is_refused_on_automation_path(env):
    """A queued topic containing the niche's taboo term must be
    refused somewhere on schedule tick -> project -> research."""
    _seed_channel_niche(env)
    _seed_schedule(env, topic_mode="queue")
    _seed_queue_topic(env, BLOCKED_TOPIC)

    outcome = _drive_automation_path(env)

    point = _refusal_point(outcome)
    assert point is not None, (
        "DEFECT: blocked topic was NOT refused anywhere on the "
        "automation path — "
        f"tick summary={outcome['summary']}, "
        f"projects={[(p['id'], p['topic'], p['status']) for p in outcome['projects']]}, "
        f"research_error={outcome['research_error']!r}, "
        f"research_artefacts={len(outcome['research_artefacts'])}"
    )


def test_blocked_topic_in_fixed_list_is_refused_on_automation_path(env):
    """Same promise via fixed_list rotation (cadence_json topics)."""
    _seed_channel_niche(env)
    _seed_schedule(
        env,
        topic_mode="fixed_list",
        cadence={
            "type": "interval",
            "interval_hours": 1,
            "topics": [BLOCKED_TOPIC],
        },
    )

    outcome = _drive_automation_path(env)

    point = _refusal_point(outcome)
    assert point is not None, (
        "DEFECT: blocked fixed_list topic was NOT refused anywhere on "
        "the automation path — "
        f"tick summary={outcome['summary']}, "
        f"projects={[(p['id'], p['topic'], p['status']) for p in outcome['projects']]}, "
        f"research_error={outcome['research_error']!r}, "
        f"research_artefacts={len(outcome['research_artefacts'])}"
    )


# ---------------------------------------------------------------------------
# Control — PASSES now; must keep passing after any fix
# ---------------------------------------------------------------------------

def test_control_clean_topic_proceeds_on_same_path(env):
    """A clean queued topic on the identical path (same niche, same
    taboo list configured) materialises and passes the research stage."""
    _seed_channel_niche(env)
    _seed_schedule(env, topic_mode="queue")
    _seed_queue_topic(env, CLEAN_TOPIC)

    outcome = _drive_automation_path(env)

    assert outcome["summary"]["materialised"] == 1
    assert outcome["summary"]["failed"] == 0
    assert len(outcome["projects"]) == 1
    project = outcome["projects"][0]
    assert project["topic"] == CLEAN_TOPIC
    assert project["topic_source"] == "schedule"
    # First stage proceeds for the clean topic: no error, artefact made.
    assert outcome["research_error"] is None
    assert outcome["research_result"] is not None
    assert outcome["research_result"]["stage"] == "research"
    assert len(outcome["research_artefacts"]) == 1
    # And the queued topic was consumed by the created project.
    tq = _rows(env, "SELECT * FROM topic_queue WHERE id = 'tq-guard'")[0]
    assert tq["status"] == "used"
    assert tq["used_in_project_id"] == project["id"]


# ---------------------------------------------------------------------------
# Characterisation — pin the trace (passing)
# ---------------------------------------------------------------------------

def test_taboo_list_is_real_but_enforced_only_at_scene_level():
    """The niche taboo list exists and IS enforced — but only against
    scene visual prompts at the visuals stage (pipeline/scenes/
    safety.py:267-278), never against a project topic."""
    from app.pipeline.scenes.models import SceneClass, SourceKind
    from app.pipeline.scenes.safety import (
        NicheSafetyProfile,
        SceneSafetyFacts,
        SafetyViolationCode,
        prompt_matches_taboo,
        validate_scene_safety,
    )

    profile = NicheSafetyProfile(niche_id=NICHE, taboo_terms=[BLOCKED_TERM])

    blocked_scene = validate_scene_safety(
        SceneSafetyFacts(
            scene_id="s1",
            scene_class=SceneClass.REAL_WORLD,
            chosen_source=SourceKind.AI_IMAGE,
            visual_prompt="gambling chips stacked on a casino table",
        ),
        profile,
    )
    assert blocked_scene.blocked
    assert SafetyViolationCode.TABOO_TERM in [
        v.code for v in blocked_scene.violations
    ]

    clean_scene = validate_scene_safety(
        SceneSafetyFacts(
            scene_id="s2",
            scene_class=SceneClass.REAL_WORLD,
            chosen_source=SourceKind.AI_IMAGE,
            visual_prompt="deep sea fish glowing in dark water",
        ),
        profile,
    )
    assert not clean_scene.blocked

    # The topic-screening helper exists and works — it simply has no
    # caller on the automation path (see module docstring).
    assert prompt_matches_taboo(BLOCKED_TOPIC, profile) == BLOCKED_TERM
    assert prompt_matches_taboo(CLEAN_TOPIC, profile) is None


def test_settings_typed_surface_has_no_blocked_terms():
    """The settings backend's only typed guardrail surface is the
    budget policy; it carries no blocked-terms field (routers/
    settings.py:116-147), and the generic settings store ships no
    blocked-terms default that anything could consult."""
    from app.providers.budget.policy import policy_from_settings
    from app.routers.settings import BudgetPolicyUpdate

    assert set(BudgetPolicyUpdate.model_fields) == {
        "per_video_cap_usd",
        "daily_cap_usd",
        "monthly_cap_usd",
        "warn_pct",
        "soft_pct",
    }
    policy = policy_from_settings({})
    assert set(policy) == set(BudgetPolicyUpdate.model_fields)
    assert not any("block" in key or "taboo" in key for key in policy)
