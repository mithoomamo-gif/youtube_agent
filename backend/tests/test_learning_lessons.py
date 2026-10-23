"""Production lessons tests (Round 5, Learning Loop team).

The production-lessons view (:mod:`app.learning.lessons`) aggregates
the structured reject reasons on approval/gate decision records per
(channel_id, niche) — SEPARATELY from performance learning rules:

* counts by reason with evidence counts, example project ids (ids
  only), gates, and first/last seen;
* threshold behaviour: >= 3 occurrences = lesson, below = observation
  (a reason seen once is listed with count 1, never promoted);
* scope isolation: other channels / niches are invisible, approved
  decisions and reason-less rejects never count;
* separation: the lessons view contains no learning-rule data and
  the consumption seam's suggestions contain no lesson data, even
  when both exist in the same DB for the same scope.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.learning.consumption import build_topic_suggestions
from app.learning.lessons import (
    LESSON_THRESHOLD,
    aggregate_production_lessons,
    production_lessons_for_scope,
)
from app.learning.store import LearningStore
from app.routers.approvals import _APPROVALS_DDL
from tests.execution_fixtures import make_db, seed_channel_niche, seed_project

T0 = 1_700_000_000_000


@pytest.fixture()
def env(tmp_path):
    _db_path, factory = make_db(tmp_path)
    channel_a, niche_a_id = seed_channel_niche(factory, niche_key="facts")
    channel_b, _niche_b_id = seed_channel_niche(factory, niche_key="facts-b")
    conn = factory()
    try:
        conn.execute(_APPROVALS_DDL)
        conn.commit()
    finally:
        conn.close()
    projects_a = [
        seed_project(factory, channel_a, niche_a_id, topic=f"A video {i}")
        for i in range(4)
    ]
    project_b = seed_project(factory, channel_b, _niche_b_id, topic="B video")
    return {
        "factory": factory,
        "channel_a": channel_a,
        "channel_b": channel_b,
        "niche_a_id": niche_a_id,
        "projects_a": projects_a,
        "project_b": project_b,
    }


def _decide(env, project_id, reason, *, decision="rejected", gate="a",
            decided_at=T0, approval_id=None):
    conn = env["factory"]()
    try:
        conn.execute(
            "INSERT INTO approvals (id, project_id, gate, decision,"
            " reason_code, status, created_at, decided_at)"
            " VALUES (?, ?, ?, ?, ?, 'decided', ?, ?)",
            (
                approval_id or uuid.uuid4().hex,
                project_id, gate, decision, reason,
                decided_at, decided_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def seeded(env):
    p = env["projects_a"]
    # weak_hook x4 across 3 projects (one project rejected twice) -> lesson.
    _decide(env, p[0], "weak_hook", decided_at=T0 + 100)
    _decide(env, p[1], "weak_hook", decided_at=T0 + 300)
    _decide(env, p[2], "weak_hook", decided_at=T0 + 200)
    _decide(env, p[0], "weak_hook", gate="b", decided_at=T0 + 400)
    # factual_error x2 -> observation. poor_audio x1 -> observation.
    _decide(env, p[1], "factual_error", decided_at=T0 + 150)
    _decide(env, p[3], "factual_error", decided_at=T0 + 250)
    _decide(env, p[2], "poor_audio", decided_at=T0 + 500)
    # Noise that must never count: an approval, a reason-less reject,
    # and a reject under the OTHER channel with a would-be lesson reason.
    _decide(env, p[0], None, decision="approved", decided_at=T0 + 600)
    _decide(env, p[1], None, decision="rejected", decided_at=T0 + 700)
    _decide(env, env["project_b"], "weak_hook", decided_at=T0 + 800)
    return env


def _view(env, channel=None, niche="facts"):
    conn = env["factory"]()
    try:
        return aggregate_production_lessons(
            conn, channel_id=channel or env["channel_a"], niche=niche
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Aggregation counts + entry shape
# ---------------------------------------------------------------------------


def test_aggregation_counts_and_entry_shape(seeded):
    view = _view(seeded)
    assert view["threshold"] == LESSON_THRESHOLD == 3
    assert view["total_rejected_decisions"] == 7  # 4 + 2 + 1, noise excluded
    by_reason = {e["reason_code"]: e for e in view["entries"]}
    weak = by_reason["weak_hook"]
    assert weak["evidence_count"] == 4
    assert weak["count"] == 4
    # Example refs are project IDS ONLY, distinct, first-seen order.
    assert weak["example_project_ids"] == [
        seeded["projects_a"][0],
        seeded["projects_a"][2],
        seeded["projects_a"][1],
    ]
    assert weak["gates"] == ["a", "b"]
    assert weak["first_seen_at"] == T0 + 100
    assert weak["last_seen_at"] == T0 + 400
    assert by_reason["factual_error"]["evidence_count"] == 2
    assert by_reason["poor_audio"]["evidence_count"] == 1
    # No project content leaks into an entry: only the contract keys.
    assert set(weak) == {
        "reason_code", "evidence_count", "count", "example_project_ids",
        "gates", "first_seen_at", "last_seen_at", "status", "is_lesson",
    }


def test_threshold_splits_lessons_from_observations(seeded):
    view = _view(seeded)
    assert [e["reason_code"] for e in view["lessons"]] == ["weak_hook"]
    assert view["lessons"][0]["status"] == "lesson"
    assert view["lessons"][0]["is_lesson"] is True
    observations = {e["reason_code"]: e for e in view["observations"]}
    assert set(observations) == {"factual_error", "poor_audio"}
    # A reason seen ONCE is listed with count 1 — as an observation,
    # never promoted to a lesson headline.
    assert observations["poor_audio"]["evidence_count"] == 1
    assert observations["poor_audio"]["status"] == "observation"
    assert observations["poor_audio"]["is_lesson"] is False


def test_scope_isolation_other_channel_and_niche(seeded):
    # Other channel (in its own niche scope "facts-b") sees only its
    # own single weak_hook reject.
    other = _view(seeded, channel=seeded["channel_b"], niche="facts-b")
    assert other["total_rejected_decisions"] == 1
    assert other["lessons"] == []
    assert other["observations"][0]["reason_code"] == "weak_hook"
    assert other["observations"][0]["evidence_count"] == 1
    # Same channel, different niche: nothing (niche matches by key or id).
    wrong_niche = _view(seeded, niche="horror")
    assert wrong_niche["total_rejected_decisions"] == 0
    assert wrong_niche["entries"] == []
    # The niche_profile_id spelling resolves to the same scope.
    by_id = _view(seeded, niche=seeded["niche_a_id"])
    assert by_id["total_rejected_decisions"] == 7


def test_empty_scope_and_missing_table_are_honest(env, tmp_path):
    view = _view(env)  # approvals table exists but holds no rows
    assert view["lessons"] == [] and view["observations"] == []
    assert view["total_rejected_decisions"] == 0

    # A DB where the approvals table was never created: honest empty.
    _db_path, factory = make_db(tmp_path / "fresh")
    conn = factory()
    try:
        bare = aggregate_production_lessons(
            conn, channel_id="any", niche="facts"
        )
    finally:
        conn.close()
    assert bare["lessons"] == [] and bare["total_rejected_decisions"] == 0


def test_production_lessons_for_scope_accepts_factory(seeded):
    view = production_lessons_for_scope(
        seeded["factory"], channel_id=seeded["channel_a"], niche="facts"
    )
    assert view["total_rejected_decisions"] == 7
    assert [e["reason_code"] for e in view["lessons"]] == ["weak_hook"]


# ---------------------------------------------------------------------------
# Separation from performance rules
# ---------------------------------------------------------------------------


def test_lessons_and_rules_are_kept_separate(seeded):
    store = LearningStore(seeded["factory"])
    rule = store.create_candidate(
        seeded["channel_a"], "facts", "topic_affinity",
        statement="Ocean depth mysteries win",
        statement_family="ocean-depths", evidence_count=7, confidence=0.75,
    )
    store.activate_rule(
        rule["id"], channel_id=seeded["channel_a"], niche="facts"
    )

    view = _view(seeded)
    # The lessons view carries no rule identity or statement anywhere.
    import json as _json

    blob = _json.dumps(view)
    assert rule["id"] not in blob
    assert "Ocean depth mysteries win" not in blob
    assert "learning_rule" not in blob

    # And the consumption seam never surfaces production lessons.
    result = build_topic_suggestions(
        seeded["channel_a"], "facts", ["Ocean Depth Secrets"],
        conn_factory=seeded["factory"],
    )
    blob2 = _json.dumps(result)
    assert "weak_hook" not in blob2
    assert "production" not in blob2
    assert result["applied_rule_ids"] == [rule["id"]]


# ---------------------------------------------------------------------------
# API surface: production-lessons is its own endpoint/shape
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    # Only this team's router (see test_learning_consumption for why).
    from app.errors import register_exception_handlers
    from app.routers import learning as learning_router_module

    app = FastAPI(title="YouTube Agent — production lessons tests")
    register_exception_handlers(app)
    app.include_router(learning_router_module.router)
    return app


def test_api_production_lessons_endpoint(seeded):
    # The router reads through app.db's default resolver (the shared
    # session DB), so re-seed one scope there with unique ids.
    from app import db as db_module

    db_module.init_db().close()
    suffix = uuid.uuid4().hex[:12]
    channel_id = f"chan-lessons-{suffix}"
    niche_id = f"niche-lessons-{suffix}"
    niche_key = f"lessons-{suffix}"
    project_id = f"proj-lessons-{suffix}"
    now = int(time.time() * 1000)
    conn = db_module.get_connection()
    try:
        conn.execute(
            "INSERT INTO niche_profiles (id, key, label, source,"
            " created_at, updated_at) VALUES (?, ?, 'L', 'builtin', ?, ?)",
            (niche_id, niche_key, now, now),
        )
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title,"
            " language, created_at, updated_at) VALUES (?, ?, 'C',"
            " 'en', ?, ?)",
            (channel_id, f"UC{suffix}", now, now),
        )
        conn.execute(
            "INSERT INTO projects (id, channel_id, niche_profile_id,"
            " topic, topic_source, created_at, updated_at) VALUES"
            " (?, ?, ?, 'T', 'user', ?, ?)",
            (project_id, channel_id, niche_id, now, now),
        )
        from app.routers.approvals import _ensure_approvals_table

        _ensure_approvals_table(conn)
        for i in range(3):
            conn.execute(
                "INSERT INTO approvals (id, project_id, gate, decision,"
                " reason_code, status, created_at, decided_at) VALUES"
                " (?, ?, 'a', 'rejected', 'weak_hook', 'decided', ?, ?)",
                (f"appr-{suffix}-{i}", project_id, now + i, now + i),
            )
        conn.execute(
            "INSERT INTO approvals (id, project_id, gate, decision,"
            " reason_code, status, created_at, decided_at) VALUES"
            " (?, ?, 'a', 'rejected', 'poor_audio', 'decided', ?, ?)",
            (f"appr-{suffix}-once", project_id, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    client = TestClient(_build_app())
    resp = client.get(
        f"/api/v1/channels/{channel_id}/learning/production-lessons",
        params={"niche": niche_key},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Separate shape from rules: lessons/observations, no items/rules.
    assert "items" not in body and "rules" not in body
    assert [e["reason_code"] for e in body["lessons"]] == ["weak_hook"]
    assert body["lessons"][0]["evidence_count"] == 3
    assert body["lessons"][0]["example_project_ids"] == [project_id]
    assert [e["reason_code"] for e in body["observations"]] == ["poor_audio"]
    assert body["observations"][0]["evidence_count"] == 1
    assert body["threshold"] == 3
