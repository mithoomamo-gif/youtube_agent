"""Learning consumption seam tests (Round 5, Learning Loop team).

Covers the research consumption seam (:mod:`app.learning.consumption`
+ its integration in :func:`app.integration.stage_bridge.run_research`
+ the ``/learning/*`` API surface):

* consumption returns ONLY same-channel + same-niche ACTIVE rules —
  fixtures seed decoy rules in another channel, another niche, and
  in candidate/superseded states;
* every rule-backed suggestion carries ``why`` provenance (rule ids +
  versions + evidence counts + confidence), and a suggestion with no
  rule support is labelled ``baseline_no_evidence``;
* ranking puts rule-backed suggestions first, by evidence;
* prompt template files are byte-identical before/after suggestion
  generation and a research run — learning travels as DATA and never
  writes to prompt storage;
* the research brief records which rules were applied (ids+versions).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.learning.consumption import (
    BASELINE_BASIS,
    RULE_BASIS,
    build_topic_suggestions,
    fetch_active_rules,
    rank_topic_suggestions,
)
from app.learning.store import LearningStore
from tests.execution_fixtures import make_db, seed_channel_niche

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_ROOT = REPO_ROOT / "prompts"

OCEAN_STATEMENT = "Ocean depth mysteries retain viewers strongly"


def _active(store: LearningStore, channel: str, niche: str, **kwargs) -> dict:
    rule = store.create_candidate(channel, niche, **kwargs)
    activated = store.activate_rule(rule["id"], channel_id=channel, niche=niche)
    assert activated is not None
    return activated


@pytest.fixture()
def env(tmp_path):
    _db_path, factory = make_db(tmp_path)
    channel_a, niche_a_id = seed_channel_niche(factory, niche_key="facts")
    # Channel B only needs to exist (its own niche key is irrelevant —
    # learning scope strings are not FKs); a distinct key avoids the
    # niche_profiles.key UNIQUE constraint.
    channel_b, niche_b_id = seed_channel_niche(factory, niche_key="facts-b")
    store = LearningStore(factory)
    return {
        "factory": factory,
        "store": store,
        "channel_a": channel_a,
        "channel_b": channel_b,
        "niche_a_id": niche_a_id,
    }


@pytest.fixture()
def seeded(env):
    """Scope A/facts active rules + decoys everywhere else."""
    store, a, b = env["store"], env["channel_a"], env["channel_b"]
    ocean = _active(
        store, a, "facts", kind="topic_affinity",
        statement=OCEAN_STATEMENT, statement_family="ocean-depths",
        evidence_count=8, confidence=0.8,
        effect_value=18.0, effect_unit="pct_retention",
        created_from=["snap-1", "snap-2"],
    )
    hook = _active(
        store, a, "facts", kind="hook_pattern",
        statement="Question hooks retain in first 30s",
        statement_family="question-hook", evidence_count=6, confidence=0.7,
    )
    # Decoy: same channel, OTHER niche.
    _active(
        store, a, "horror", kind="topic_affinity",
        statement="Ocean depth mysteries in horror framing",
        statement_family="ocean-horror", evidence_count=99, confidence=0.99,
    )
    # Decoy: OTHER channel, same niche.
    _active(
        store, b, "facts", kind="topic_affinity",
        statement="Ocean depth mysteries on another channel",
        statement_family="ocean-depths", evidence_count=99, confidence=0.99,
    )
    # Decoy: same scope but only a CANDIDATE (never activated).
    store.create_candidate(
        a, "facts", "topic_affinity",
        statement="Volcano eruptions candidate only",
        statement_family="volcano", evidence_count=50, confidence=0.9,
    )
    # Decoy: same scope, SUPERSEDED (v1 replaced by an active v2 in a
    # different family line — the superseded row itself must not read).
    old = store.create_candidate(
        a, "facts", "topic_affinity",
        statement="Desert survival tales old version",
        statement_family="desert", evidence_count=4, confidence=0.5,
    )
    store.activate_rule(old["id"], channel_id=a, niche="facts")
    newer = store.create_candidate(
        a, "facts", "topic_affinity",
        statement="Desert survival tales revised",
        statement_family="desert", evidence_count=5, confidence=0.6,
    )
    store.activate_rule(newer["id"], channel_id=a, niche="facts")
    env.update({"ocean": ocean, "hook": hook, "superseded": old, "desert": newer})
    return env


# ---------------------------------------------------------------------------
# Scope isolation: only same-channel + same-niche ACTIVE rules
# ---------------------------------------------------------------------------


def test_fetch_active_rules_only_same_scope_active(seeded):
    rules = fetch_active_rules(
        seeded["channel_a"], "facts", conn_factory=seeded["factory"]
    )
    ids = {r["rule_id"] for r in rules}
    assert seeded["ocean"]["id"] in ids
    assert seeded["hook"]["id"] in ids
    assert seeded["desert"]["id"] in ids  # the active replacement
    assert seeded["superseded"]["id"] not in ids  # superseded never reads
    # No decoy leaked in: exactly the 3 active scope-A/facts rules.
    assert len(rules) == 3
    assert all(r["version"] >= 1 for r in rules)
    desert = next(r for r in rules if r["rule_id"] == seeded["desert"]["id"])
    assert desert["version"] == seeded["desert"]["version"]


def test_fetch_requires_scope(env):
    with pytest.raises(ValueError):
        fetch_active_rules("", "facts", conn_factory=env["factory"])
    with pytest.raises(ValueError):
        fetch_active_rules(env["channel_a"], "", conn_factory=env["factory"])


def test_build_suggestions_applied_rules_record_ids_and_versions(seeded):
    result = build_topic_suggestions(
        seeded["channel_a"], "facts",
        ["Ocean Depth Secrets Revealed"],
        conn_factory=seeded["factory"],
    )
    applied = {r["rule_id"]: r["version"] for r in result["applied_rules"]}
    assert applied[seeded["ocean"]["id"]] == seeded["ocean"]["version"]
    assert applied[seeded["hook"]["id"]] == seeded["hook"]["version"]
    assert seeded["superseded"]["id"] not in applied
    assert result["applied_rule_ids"] == [r["rule_id"] for r in result["applied_rules"]]
    assert result["rules_considered"] == 3


# ---------------------------------------------------------------------------
# Provenance on suggestions
# ---------------------------------------------------------------------------


def test_rule_backed_suggestion_carries_full_provenance(seeded):
    result = build_topic_suggestions(
        seeded["channel_a"], "facts",
        ["Ocean Depth Secrets Revealed", "Mountain Cooking Basics"],
        conn_factory=seeded["factory"],
    )
    by_topic = {s["topic"]: s for s in result["suggestions"]}
    backed = by_topic["Ocean Depth Secrets Revealed"]
    assert backed["score_basis"] == RULE_BASIS
    assert backed["rank"] == 1
    why = backed["why"]
    assert why["basis"] == RULE_BASIS
    assert why["evidence_count"] == 8
    assert why["confidence"] == pytest.approx(0.8)
    assert len(why["supporting_rules"]) == 1
    support = why["supporting_rules"][0]
    assert support["rule_id"] == seeded["ocean"]["id"]
    assert support["version"] == seeded["ocean"]["version"]
    assert support["evidence_count"] == 8
    assert support["confidence"] == pytest.approx(0.8)
    assert backed["supporting_rule_ids"] == [seeded["ocean"]["id"]]


def test_unsupported_suggestion_is_labelled_baseline_not_dressed_up(seeded):
    result = build_topic_suggestions(
        seeded["channel_a"], "facts",
        ["Mountain Cooking Basics"],
        conn_factory=seeded["factory"],
    )
    (suggestion,) = result["suggestions"]
    assert suggestion["score_basis"] == BASELINE_BASIS
    assert suggestion["why"]["basis"] == BASELINE_BASIS
    assert suggestion["why"]["supporting_rules"] == []
    assert suggestion["why"]["evidence_count"] == 0
    assert suggestion["why"]["confidence"] is None
    assert "no learning-rule evidence" in suggestion["why"]["label"]
    assert suggestion["supporting_rule_ids"] == []


def test_non_topic_kinds_never_claim_topic_support(seeded):
    # The active hook_pattern rule mentions "hooks"; a topic about
    # hooks must still be baseline — only topic_affinity supports.
    result = build_topic_suggestions(
        seeded["channel_a"], "facts",
        ["Question Hooks Masterclass"],
        conn_factory=seeded["factory"],
    )
    (suggestion,) = result["suggestions"]
    assert suggestion["why"]["basis"] == BASELINE_BASIS


def test_ranking_rule_backed_first_by_evidence(env):
    store, a = env["store"], env["channel_a"]
    _active(
        store, a, "facts", kind="topic_affinity",
        statement="Volcano eruptions draw search demand",
        statement_family="volcano", evidence_count=3, confidence=0.5,
    )
    _active(
        store, a, "facts", kind="topic_affinity",
        statement="Ocean trenches draw search demand",
        statement_family="ocean-trenches", evidence_count=9, confidence=0.6,
    )
    result = build_topic_suggestions(
        a, "facts",
        ["Volcano Eruptions Explained", "Ocean Trenches Explained",
         "Unrelated Gardening Tips"],
        conn_factory=env["factory"],
    )
    topics_in_rank_order = [s["topic"] for s in result["suggestions"]]
    assert topics_in_rank_order == [
        "Ocean Trenches Explained",      # evidence 9 beats 3
        "Volcano Eruptions Explained",
        "Unrelated Gardening Tips",      # baseline last
    ]
    assert [s["rank"] for s in result["suggestions"]] == [1, 2, 3]


def test_rank_is_pure_and_dedupes_candidates(seeded):
    rules = fetch_active_rules(
        seeded["channel_a"], "facts", conn_factory=seeded["factory"]
    )
    first = rank_topic_suggestions(
        ["Ocean Depth Secrets", "Ocean Depth Secrets", "  "], rules
    )
    second = rank_topic_suggestions(["Ocean Depth Secrets"], rules)
    assert [s["topic"] for s in first] == ["Ocean Depth Secrets"]
    assert first[0]["why"] == second[0]["why"]


# ---------------------------------------------------------------------------
# Prompt storage is never touched
# ---------------------------------------------------------------------------


def _prompt_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(PROMPTS_ROOT.rglob("*.md")):
        hashes[str(path.relative_to(PROMPTS_ROOT))] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return hashes


def test_suggestion_generation_does_not_modify_prompt_files(seeded):
    before = _prompt_hashes()
    assert before, "expected the real prompts/ tree to exist"
    build_topic_suggestions(
        seeded["channel_a"], "facts",
        ["Ocean Depth Secrets Revealed", "Mountain Cooking Basics"],
        conn_factory=seeded["factory"],
    )
    after = _prompt_hashes()
    assert after == before
    # And no rule statement text landed in any prompt file.
    for path in PROMPTS_ROOT.rglob("*.md"):
        assert OCEAN_STATEMENT not in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Research-stage integration (stage_bridge.run_research)
# ---------------------------------------------------------------------------


def test_run_research_records_applied_rules_and_provenance(seeded, tmp_path):
    from app.integration import stage_bridge

    db_path = next(
        Path(p) for p in [str(tmp_path / "app.db")]
    )
    project = {
        "id": "proj-learning-1",
        "channel_id": seeded["channel_a"],
        "niche_profile_id": seeded["niche_a_id"],
        "topic": "Ocean Depth Secrets",
        "target_minutes": 1,
        "publish_mode": "review_first",
        "db_path": str(db_path),
    }
    payload = {"base_dir": str(tmp_path / "work"), "db_path": str(db_path)}

    result = stage_bridge.run_research(project, payload)
    brief = result["artefact"]
    # The resolved topic is NOT changed by learning.
    assert brief["resolved_topic"] == "Ocean Depth Secrets"

    learning = brief["learning"]
    assert learning["status"] == "ok"
    assert learning["channel_id"] == seeded["channel_a"]
    assert learning["niche"] == "facts"
    applied_ids = {r["rule_id"] for r in learning["applied_rules"]}
    assert seeded["ocean"]["id"] in applied_ids
    assert seeded["superseded"]["id"] not in applied_ids
    assert len(applied_ids) == 3  # decoys (other channel/niche) excluded
    # Top-level visibility record: ids + versions on the brief itself.
    top = {r["rule_id"]: r["version"] for r in brief["applied_learning_rules"]}
    assert top[seeded["ocean"]["id"]] == seeded["ocean"]["version"]
    assert brief["provenance"]["learning_rules_applied"] == [
        r["rule_id"] for r in brief["applied_learning_rules"]
    ]
    # The chosen topic's suggestion carries provenance.
    suggestions = {s["topic"]: s for s in learning["suggestions"]}
    chosen = suggestions["Ocean Depth Secrets"]
    assert chosen["why"]["basis"] == RULE_BASIS
    assert chosen["why"]["supporting_rules"][0]["rule_id"] == seeded["ocean"]["id"]
    # The persisted research report (checkpoint file) carries it too.
    on_disk = json.loads(Path(result["file_path"]).read_text(encoding="utf-8"))
    assert on_disk["learning"]["status"] == "ok"
    assert on_disk["applied_learning_rules"] == brief["applied_learning_rules"]


def test_run_research_without_any_rules_is_honest_baseline(env, tmp_path):
    from app.integration import stage_bridge

    project = {
        "id": "proj-learning-2",
        "channel_id": env["channel_a"],
        "niche_profile_id": env["niche_a_id"],
        "topic": "Ocean Depth Secrets",
        "target_minutes": 1,
        "publish_mode": "review_first",
        "db_path": str(tmp_path / "app.db"),
    }
    payload = {"base_dir": str(tmp_path / "work"), "db_path": str(tmp_path / "app.db")}
    brief = stage_bridge.run_research(project, payload)["artefact"]
    assert brief["learning"]["status"] == "ok"
    assert brief["applied_learning_rules"] == []
    (suggestion,) = [
        s for s in brief["learning"]["suggestions"]
        if s["topic"] == "Ocean Depth Secrets"
    ]
    assert suggestion["why"]["basis"] == BASELINE_BASIS


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    # Only this team's router is mounted: the aggregate
    # app.routers.api pulls in every sibling router, and a sibling
    # landing in flight must not poison these tests.  Aggregate
    # registration itself is asserted by source inspection below.
    from app.errors import register_exception_handlers
    from app.routers import learning as learning_router_module

    app = FastAPI(title="YouTube Agent — learning consumption tests")
    register_exception_handlers(app)
    app.include_router(learning_router_module.router)
    return app


def test_aggregate_api_router_registers_learning_router():
    api_source = (
        Path(__file__).resolve().parent.parent
        / "app" / "routers" / "api.py"
    ).read_text(encoding="utf-8")
    assert "learning," in api_source
    assert "router.include_router(learning.router)" in api_source


@pytest.fixture()
def api_env():
    """Seed the shared session DB (the routers' own resolver)."""
    from app import db as db_module

    db_module.init_db().close()
    suffix = uuid.uuid4().hex[:12]
    channel_id = f"chan-learn-{suffix}"
    niche_id = f"niche-learn-{suffix}"
    niche_key = f"learn-{suffix}"
    now = int(time.time() * 1000)
    conn = db_module.get_connection()
    try:
        conn.execute(
            "INSERT INTO niche_profiles (id, key, label, source,"
            " created_at, updated_at) VALUES (?, ?, 'Learn Niche',"
            " 'builtin', ?, ?)",
            (niche_id, niche_key, now, now),
        )
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title,"
            " language, created_at, updated_at) VALUES (?, ?,"
            " 'Learn Channel', 'en', ?, ?)",
            (channel_id, f"UC{suffix}", now, now),
        )
        conn.commit()
    finally:
        conn.close()
    store = LearningStore(db_module.get_connection)
    rule = _active(
        store, channel_id, niche_key, kind="topic_affinity",
        statement="Ocean depth mysteries win",
        statement_family="ocean-depths", evidence_count=7, confidence=0.75,
    )
    _active(
        store, channel_id, "other-niche", kind="topic_affinity",
        statement="Ocean depth mysteries decoy niche",
        statement_family="ocean-depths", evidence_count=7, confidence=0.75,
    )
    return {"channel_id": channel_id, "niche": niche_key, "rule": rule}


def test_api_rules_endpoint_is_scope_exact(api_env):
    client = TestClient(_build_app())
    resp = client.get(
        f"/api/v1/channels/{api_env['channel_id']}/learning/rules",
        params={"niche": api_env["niche"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["items"][0]["rule_id"] == api_env["rule"]["id"]
    assert body["items"][0]["version"] == api_env["rule"]["version"]
    assert body["items"][0]["evidence_count"] == 7


def test_api_rules_endpoint_requires_niche_and_known_channel(api_env):
    client = TestClient(_build_app())
    missing_niche = client.get(
        f"/api/v1/channels/{api_env['channel_id']}/learning/rules"
    )
    assert missing_niche.status_code == 422
    assert missing_niche.json()["error"]["code"] == "niche_required"
    unknown = client.get(
        "/api/v1/channels/no-such-channel/learning/rules",
        params={"niche": "facts"},
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "channel_not_found"


def test_api_topic_suggestions_carry_provenance(api_env):
    client = TestClient(_build_app())
    resp = client.post(
        f"/api/v1/channels/{api_env['channel_id']}/learning/topic-suggestions",
        json={
            "niche": api_env["niche"],
            "candidates": ["Ocean Depth Secrets", "Gardening Basics"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    by_topic = {s["topic"]: s for s in body["suggestions"]}
    assert by_topic["Ocean Depth Secrets"]["why"]["basis"] == RULE_BASIS
    assert by_topic["Ocean Depth Secrets"]["why"]["supporting_rules"][0][
        "rule_id"
    ] == api_env["rule"]["id"]
    assert by_topic["Gardening Basics"]["why"]["basis"] == BASELINE_BASIS
    assert body["applied_rule_ids"] == [api_env["rule"]["id"]]
