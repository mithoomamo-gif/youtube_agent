"""Learning Loop REST API tests (Round 5 — Learning Loop team).

Covers ``app.routers.learning`` + ``app.learning.service`` over the
landed sibling contracts:

- ``app.learning.store.LearningStore`` (rules / history),
- ``app.calibration.report.build_calibration_report`` (calibration
  summary, migration 0007 ``calibration_log``),
- ``app.learning.lessons.production_lessons_for_scope`` (production
  lessons, exposed by the sibling router at
  ``.../learning/production-lessons``),
- ``app.learning_loop.engine.UpdateEngine`` (manual evaluate
  trigger, bridged to the Learning Store by the service),
- the publish-window seam: the sibling eligibility structure is
  returned verbatim when a sibling module is present; the landed
  sibling is the REAL ``app.publish_window`` service, which
  enforces the roadmap gate (≥10 published videos + ≥28 days of
  analytics) — learned ``publish_window`` rules alone NEVER make a
  channel eligible (R5L-003).

House rules under test: niche is a REQUIRED scope parameter (422
when missing), an unknown channel is a 404 (never 403), and one
channel's learning data never appears under another channel.
"""

from __future__ import annotations

import sys
import time
import types
import uuid

import pytest
from fastapi.testclient import TestClient

_SUFFIX = uuid.uuid4().hex[:10]
CHAN_A = f"lr-a-{_SUFFIX}"
CHAN_B = f"lr-b-{_SUFFIX}"
YT_A = f"UClrA{_SUFFIX}xxxx"
YT_B = f"UClrB{_SUFFIX}xxxx"
UNKNOWN = "no-such-channel"

DAY_MS = 86_400_000


# ---------------------------------------------------------------------------
# Seeding / helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _seed_channels() -> None:
    from app import db as db_module

    now = _now_ms()
    conn = db_module.get_connection()
    try:
        for chan_id, yt_id, title in (
            (CHAN_A, YT_A, "Learning Channel A"),
            (CHAN_B, YT_B, "Learning Channel B"),
        ):
            conn.execute(
                "INSERT OR IGNORE INTO channels (id, youtube_channel_id,"
                " title, language, status, created_at, updated_at)"
                " VALUES (?, ?, ?, 'en', 'active', ?, ?)",
                (chan_id, yt_id, title, now, now),
            )
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _channels(app):
    # Depends on the conftest `app` fixture so the DB schema exists.
    _seed_channels()
    yield


def _store():
    from app import db as db_module
    from app.learning.store import LearningStore

    return LearningStore(db_module.get_connection)


def _niche_profile_id(key: str) -> str:
    from app import db as db_module

    now = _now_ms()
    # The suite shares ONE session DB and niche_profiles.key is UNIQUE.
    # For a BUILTIN key the seeded row must be the canonical builtin
    # profile (id == key, library label/voice — the app invariant in
    # app/niches/builtins.py). Seeding a look-alike row under a private
    # id (the old f"np-{key}") squats the key: other suites that seed
    # or resolve the builtin by id (e.g. test_schedules_api) then get a
    # silent INSERT OR IGNORE no-op followed by an FK failure.
    profile_id = f"np-{key}"
    label = key.replace("_", " ").title()
    voice_style = None
    try:
        from app.niches.builtins import BUILTIN_PROFILES

        builtin = BUILTIN_PROFILES.get(key)
    except Exception:  # pragma: no cover - library always present
        builtin = None
    if builtin is not None:
        profile_id = key
        label = builtin.name
        voice_style = builtin.voice_style
    conn = db_module.get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO niche_profiles (id, key, label,"
            " source, voice_style, created_at, updated_at)"
            " VALUES (?, ?, ?, 'builtin', ?, ?, ?)",
            (profile_id, key, label, voice_style, now, now),
        )
        row = conn.execute(
            "SELECT id FROM niche_profiles WHERE key = ?", (key,)
        ).fetchone()
        assert row is not None
        return str(row[0])
    finally:
        conn.close()


def _seed_project(project_id: str, channel_id: str, niche_key: str,
                  **overrides) -> str:
    from app import db as db_module

    now = _now_ms()
    fields = {
        "id": project_id,
        "channel_id": channel_id,
        "niche_profile_id": _niche_profile_id(niche_key),
        "topic": overrides.pop("topic", "Some topic"),
        "topic_source": "user",
        "status": overrides.pop("status", "draft"),
        "published_at": overrides.pop("published_at", None),
        "target_minutes": overrides.pop("target_minutes", None),
        "created_at": now,
        "updated_at": now,
    }
    fields.update(overrides)
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    conn = db_module.get_connection()
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO projects ({cols}) VALUES ({marks})",
            tuple(fields.values()),
        )
    finally:
        conn.close()
    return project_id


# ---------------------------------------------------------------------------
# 1. Rules: active + candidate, labels visible, scope exact
# ---------------------------------------------------------------------------


def test_rules_lists_active_and_candidate_with_labels(client: TestClient):
    store = _store()
    active = store.create_candidate(
        CHAN_A, "facts", "hook_pattern",
        "Question hooks retain +18% in first 30s",
        statement_family="hook_pattern:question:positive",
        evidence_count=7, confidence=0.75,
    )
    store.activate_rule(active["id"], channel_id=CHAN_A, niche="facts")
    candidate = store.create_candidate(
        CHAN_A, "facts", "topic_affinity",
        "Volcano eruptions may win",
        statement_family="topic_affinity:volcanoes:positive",
        evidence_count=1, confidence=0.3,
    )
    old = store.create_candidate(
        CHAN_A, "facts", "title_pattern",
        "Old title pattern",
        statement_family="title_pattern:numbers:positive",
        evidence_count=9, confidence=0.9,
    )
    store.activate_rule(old["id"], channel_id=CHAN_A, niche="facts")
    store.supersede_rule(
        old["id"], channel_id=CHAN_A, niche="facts",
        statement="New title pattern",
    )

    resp = client.get(
        f"/api/v1/channels/{CHAN_A}/learning/rules",
        params={"niche": "facts"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["channel_id"] == CHAN_A
    assert body["niche"] == "facts"
    by_id = {item["rule_id"]: item for item in body["items"]}

    act = by_id[active["id"]]
    assert act["status"] == "active"
    assert act["evidence_count"] == 7
    assert act["confidence"] == 0.75
    assert act["version"] == 1
    assert act["is_thin_evidence"] is False

    cand = by_id[candidate["id"]]
    assert cand["status"] == "candidate"
    assert cand["evidence_count"] == 1
    # Thin-evidence rules are LABELLED, not hidden (threshold is 5).
    assert cand["is_thin_evidence"] is True

    # The superseded version is history, not a current rule.
    assert old["id"] not in by_id


def test_rules_scope_isolation_and_niche_required(client: TestClient):
    store = _store()
    rule = store.create_candidate(
        CHAN_A, "facts", "duration_band", "8-12m holds attention",
        statement_family="duration_band:8_12m:positive",
        evidence_count=6, confidence=0.6,
    )
    store.activate_rule(rule["id"], channel_id=CHAN_A, niche="facts")

    other_niche = client.get(
        f"/api/v1/channels/{CHAN_A}/learning/rules",
        params={"niche": "horror"},
    )
    assert other_niche.status_code == 200
    assert other_niche.json()["items"] == []

    other_channel = client.get(
        f"/api/v1/channels/{CHAN_B}/learning/rules",
        params={"niche": "facts"},
    )
    assert other_channel.status_code == 200
    assert other_channel.json()["items"] == []

    missing_niche = client.get(f"/api/v1/channels/{CHAN_A}/learning/rules")
    assert missing_niche.status_code == 422
    assert missing_niche.json()["error"]["code"] == "niche_required"

    unknown = client.get(
        f"/api/v1/channels/{UNKNOWN}/learning/rules",
        params={"niche": "facts"},
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "channel_not_found"


# ---------------------------------------------------------------------------
# 2. Rule history: superseded included, newest first, kind required
# ---------------------------------------------------------------------------


def test_rule_history_newest_first_includes_superseded(client: TestClient):
    # A dedicated niche keeps this scope free of other tests' rules.
    store = _store()
    v1 = store.create_candidate(
        CHAN_A, "history", "hook_pattern", "Hook v1",
        statement_family="hook_pattern:story:positive",
        # R5-005 POLICY UPDATE (flagged): was evidence_count=2, but
        # the DECIDED unified policy makes the store itself refuse
        # activation below n>=3 — v1 is seeded at the threshold.
        # n=3 is still inside the n<5 thin display band, so the
        # is_thin_evidence assertion below is unchanged in spirit.
        evidence_count=3, confidence=0.4,
    )
    store.activate_rule(v1["id"], channel_id=CHAN_A, niche="history")
    v2 = store.supersede_rule(
        v1["id"], channel_id=CHAN_A, niche="history", statement="Hook v2",
        evidence_count=4,
    )
    store.supersede_rule(
        v2["id"], channel_id=CHAN_A, niche="history", statement="Hook v3",
        evidence_count=8,
    )
    # Different kind in the same scope: must not appear.
    store.create_candidate(
        CHAN_A, "history", "title_pattern", "Other kind",
        statement_family="title_pattern:q:positive", evidence_count=3,
    )

    resp = client.get(
        f"/api/v1/channels/{CHAN_A}/learning/rules/history",
        params={"niche": "history", "kind": "hook_pattern"},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["version"] for i in items] == [3, 2, 1]
    assert [i["status"] for i in items] == [
        "active", "superseded", "superseded",
    ]
    assert items[-1]["id"] == v1["id"]
    assert items[-1]["is_thin_evidence"] is True  # evidence_count 3 < 5
    assert items[0]["supersedes_id"] == v2["id"]


def test_rule_history_requires_kind_and_validates_it(client: TestClient):
    missing_kind = client.get(
        f"/api/v1/channels/{CHAN_A}/learning/rules/history",
        params={"niche": "facts"},
    )
    assert missing_kind.status_code == 422

    bad_kind = client.get(
        f"/api/v1/channels/{CHAN_A}/learning/rules/history",
        params={"niche": "facts", "kind": "not_a_kind"},
    )
    assert bad_kind.status_code == 422
    assert bad_kind.json()["error"]["code"] == "validation_error"

    cross = client.get(
        f"/api/v1/channels/{CHAN_B}/learning/rules/history",
        params={"niche": "facts", "kind": "hook_pattern"},
    )
    assert cross.status_code == 200
    assert cross.json()["items"] == []

    unknown = client.get(
        f"/api/v1/channels/{UNKNOWN}/learning/rules/history",
        params={"niche": "facts", "kind": "hook_pattern"},
    )
    assert unknown.status_code == 404


# ---------------------------------------------------------------------------
# 3. Calibration summary: D+7 / D+28 bias / MAPE / coverage + labels
# ---------------------------------------------------------------------------


def _seed_calibration(channel_id: str, niche: str, point: str,
                      rows: list[tuple[float, float, float, float]]):
    from app import db as db_module
    from app.calibration.schema import ensure_calibration_schema

    conn = db_module.get_connection()
    try:
        ensure_calibration_schema(conn)
        now = _now_ms()
        for idx, (low, mid, high, actual) in enumerate(rows):
            conn.execute(
                "INSERT INTO calibration_log (id, project_id,"
                " youtube_video_id, channel_id, niche, metric,"
                " snapshot_point, predicted_low, predicted_mid,"
                " predicted_high, actual_value, created_at)"
                " VALUES (?, NULL, ?, ?, ?, 'views', ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex, f"vid-{channel_id}-{point}-{idx}",
                    channel_id, niche, point, low, mid, high, actual, now,
                ),
            )
    finally:
        conn.close()


def test_calibration_summary_values_and_insufficient_labelling(
    client: TestClient,
):
    # D+7: pct errors +10 / -10 / 0 -> bias 0, MAPE 6.66..., all in band.
    _seed_calibration(CHAN_A, "facts", "D+7", [
        (80.0, 100.0, 120.0, 110.0),
        (150.0, 200.0, 250.0, 180.0),
        (90.0, 100.0, 110.0, 100.0),
    ])
    # D+28: a single row -> below MIN_EVIDENCE (3): labelled, no stats.
    _seed_calibration(CHAN_A, "facts", "D+28", [
        (90.0, 100.0, 110.0, 95.0),
    ])

    resp = client.get(
        f"/api/v1/channels/{CHAN_A}/learning/calibration",
        params={"niche": "facts"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["channel_id"] == CHAN_A
    assert body["niche"] == "facts"
    assert len(body["scopes"]) == 1
    scope = body["scopes"][0]
    assert scope["metric"] == "views"

    d7 = scope["points"]["D+7"]
    assert d7["n"] == 3
    assert d7["evidence"] == "sufficient"
    assert d7["signed_bias_pct"] == pytest.approx(0.0)
    assert d7["mape_pct"] == pytest.approx(20.0 / 3.0)
    assert d7["band_coverage"] == pytest.approx(1.0)

    d28 = scope["points"]["D+28"]
    assert d28["n"] == 1
    assert d28["evidence"] == "insufficient_evidence"
    assert d28["label"] == "insufficient evidence"
    assert d28["signed_bias_pct"] is None
    assert d28["mape_pct"] is None
    assert d28["band_coverage"] is None

    assert body["evidence"] == "sufficient"  # D+7 carries real stats


def test_calibration_no_rows_is_labelled_insufficient(client: TestClient):
    resp = client.get(
        f"/api/v1/channels/{CHAN_B}/learning/calibration",
        params={"niche": "facts"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scopes"] == []
    assert body["evidence"] == "insufficient_evidence"
    assert body["label"] == "insufficient evidence"
    assert body["reason"] == "no_calibration_rows"

    missing_niche = client.get(
        f"/api/v1/channels/{CHAN_A}/learning/calibration"
    )
    assert missing_niche.status_code == 422

    unknown = client.get(
        f"/api/v1/channels/{UNKNOWN}/learning/calibration",
        params={"niche": "facts"},
    )
    assert unknown.status_code == 404


# ---------------------------------------------------------------------------
# 4. Production lessons: aggregated reject reasons + observations
# ---------------------------------------------------------------------------


def _seed_rejections(channel_id: str, niche_key: str, project_id: str,
                     reason: str, count: int, gate: str = "a"):
    from app import db as db_module
    from app.routers.approvals import _APPROVALS_DDL

    _seed_project(project_id, channel_id, niche_key, status="failed")
    conn = db_module.get_connection()
    try:
        conn.execute(_APPROVALS_DDL)
        now = _now_ms()
        for idx in range(count):
            conn.execute(
                "INSERT INTO approvals (id, project_id, gate, decision,"
                " reason_code, status, created_at, decided_at)"
                " VALUES (?, ?, ?, 'rejected', ?, 'decided', ?, ?)",
                (
                    uuid.uuid4().hex, project_id, gate, reason,
                    now + idx, now + idx,
                ),
            )
    finally:
        conn.close()


def test_production_lessons_aggregate_reject_reasons(client: TestClient):
    _seed_rejections(CHAN_A, "facts", f"lp-1-{_SUFFIX}", "weak_hook", 3)
    _seed_rejections(CHAN_A, "facts", f"lp-2-{_SUFFIX}", "bad_audio", 1)
    # Another channel's rejects must never leak into A's lessons.
    _seed_rejections(CHAN_B, "facts", f"lp-3-{_SUFFIX}", "weak_hook", 5)

    resp = client.get(
        f"/api/v1/channels/{CHAN_A}/learning/production-lessons",
        params={"niche": "facts"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "approvals.rejected_decisions"
    assert body["total_rejected_decisions"] == 4

    lessons = {e["reason_code"]: e for e in body["lessons"]}
    assert lessons["weak_hook"]["evidence_count"] == 3
    assert lessons["weak_hook"]["status"] == "lesson"
    assert lessons["weak_hook"]["gates"] == ["a"]

    observations = {e["reason_code"]: e for e in body["observations"]}
    assert observations["bad_audio"]["evidence_count"] == 1
    assert observations["bad_audio"]["status"] == "observation"

    other_niche = client.get(
        f"/api/v1/channels/{CHAN_A}/learning/production-lessons",
        params={"niche": "horror"},
    )
    assert other_niche.json()["total_rejected_decisions"] == 0

    missing_niche = client.get(
        f"/api/v1/channels/{CHAN_A}/learning/production-lessons"
    )
    assert missing_niche.status_code == 422

    unknown = client.get(
        f"/api/v1/channels/{UNKNOWN}/learning/production-lessons",
        params={"niche": "facts"},
    )
    assert unknown.status_code == 404


# ---------------------------------------------------------------------------
# 5. Publish window: the real service's gate — an ACTIVE rule alone
#    never makes a data-less channel eligible (R5L-003)
# ---------------------------------------------------------------------------


def test_publish_window_gate_ignores_rules_without_data(client: TestClient):
    """R5L-003: eligibility comes from the REAL PublishWindowService
    (≥10 published videos + ≥28 days of analytics), never from the
    mere existence of an ACTIVE publish_window rule."""
    resp = client.get(f"/api/v1/channels/{CHAN_B}/learning/publish-window")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["eligible"] is False
    assert body["status"] == "insufficient_data"
    assert body["reason"] == "not_enough_data"
    assert body["suggestions"] == []
    assert body["published_count"] == 0
    assert body["days_of_data"] == 0
    assert body["required"] == {"videos": 10, "days": 28}
    assert body["source"] == "publish_window_service"

    store = _store()
    rule = store.create_candidate(
        CHAN_A, "facts", "publish_window",
        "Publishing 12-18 UTC outperforms baseline by 12.0% (shrunk, n=6)",
        statement_family="publish_window:12_18_utc:positive",
        evidence_count=6, confidence=0.8,
    )
    store.activate_rule(rule["id"], channel_id=CHAN_A, niche="facts")

    # The exact R5L-003 reproduction: one ACTIVE rule, but zero
    # published videos and zero analytics days -> NOT eligible.
    resp = client.get(f"/api/v1/channels/{CHAN_A}/learning/publish-window")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["eligible"] is False
    assert body["status"] == "insufficient_data"
    assert body["reason"] == "not_enough_data"
    assert body["suggestions"] == []
    assert body["published_count"] == 0
    assert body["days_of_data"] == 0

    # B still has nothing: A's data never leaks across.
    resp_b = client.get(f"/api/v1/channels/{CHAN_B}/learning/publish-window")
    assert resp_b.json()["eligible"] is False

    unknown = client.get(f"/api/v1/channels/{UNKNOWN}/learning/publish-window")
    assert unknown.status_code == 404


def test_publish_window_sibling_structure_returned_verbatim(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    sentinel = {
        "eligible": False,
        "status": "insufficient_data",
        "reason": "fewer_than_minimum_videos",
        "evidence": {"videos_observed": 2, "minimum_required": 10},
        "custom_nested": {"windows": [{"hour_utc": 15, "lift": 0.2}]},
    }
    fake = types.ModuleType("app.learning.publish_window")
    fake.get_publish_window_suggestion = lambda channel_id: dict(sentinel)
    monkeypatch.setitem(sys.modules, "app.learning.publish_window", fake)

    resp = client.get(f"/api/v1/channels/{CHAN_A}/learning/publish-window")
    assert resp.status_code == 200, resp.text
    # Verbatim: not flattened into a fake suggestion, nothing added.
    assert resp.json() == sentinel


# ---------------------------------------------------------------------------
# 6. Evaluate trigger: runs the engine once for (channel, niche)
# ---------------------------------------------------------------------------


def _seed_published_evidence(channel_id: str, niche_key: str):
    from app import db as db_module

    published_at = _now_ms() - 8 * DAY_MS
    snapshot_at = published_at + 7 * DAY_MS
    conn = db_module.get_connection()
    try:
        for idx, views in enumerate((1000, 2000, 3000)):
            project_id = f"ev-{idx}-{_SUFFIX}"
            _seed_project(
                project_id, channel_id, niche_key,
                topic="Ocean depths", status="published",
                published_at=published_at, target_minutes=10,
            )
            conn.execute(
                "INSERT OR REPLACE INTO analytics_snapshots (id,"
                " project_id, channel_id, youtube_video_id,"
                " snapshot_at, views, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex, project_id, channel_id,
                    f"yt-{project_id}", snapshot_at, views, _now_ms(),
                ),
            )
    finally:
        conn.close()


def test_evaluate_trigger_runs_engine_and_writes_rules(
    client: TestClient,
):
    _seed_published_evidence(CHAN_A, "facts")

    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/learning/evaluate",
        json={"niche": "facts"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["channel_id"] == CHAN_A
    assert body["niche"] == "facts"
    assert body["trigger"] == "manual"
    assert body["snapshot_points"] == ["D+7", "D+28"]

    reports = {r["snapshot_point"]: r for r in body["reports"]}
    d7 = reports["D+7"]
    assert len(d7["videos_evaluated"]) == 3
    outcomes = {(o["kind"], o["action"]) for o in d7["rule_outcomes"]}
    # n=3 same-topic evidence passes the engine's evidence gate.
    assert ("topic_affinity", "created_active") in outcomes
    assert d7["rules_created"] >= 1
    # No snapshots in the D+28 window: an honest empty report.
    assert reports["D+28"]["videos_evaluated"] == []

    # The learned rule is now readable through the rules endpoint.
    rules = client.get(
        f"/api/v1/channels/{CHAN_A}/learning/rules",
        params={"niche": "facts"},
    ).json()
    topic_rules = [
        i for i in rules["items"] if i["kind"] == "topic_affinity"
    ]
    assert any(
        r["status"] == "active" and r["evidence_count"] == 3
        for r in topic_rules
    )


def test_evaluate_trigger_validation_and_scoping(client: TestClient):
    missing_niche = client.post(
        f"/api/v1/channels/{CHAN_A}/learning/evaluate", json={}
    )
    assert missing_niche.status_code == 422
    assert missing_niche.json()["error"]["code"] == "niche_required"

    bad_point = client.post(
        f"/api/v1/channels/{CHAN_A}/learning/evaluate",
        json={"niche": "facts", "snapshot_point": "D+9"},
    )
    assert bad_point.status_code == 422
    assert bad_point.json()["error"]["code"] == "validation_error"

    unknown = client.post(
        f"/api/v1/channels/{UNKNOWN}/learning/evaluate",
        json={"niche": "facts"},
    )
    assert unknown.status_code == 404

    # A trigger for channel B learns nothing and touches nothing of A.
    empty = client.post(
        f"/api/v1/channels/{CHAN_B}/learning/evaluate",
        json={"niche": "facts", "snapshot_point": "D+7"},
    )
    assert empty.status_code == 200
    assert empty.json()["reports"][0]["videos_evaluated"] == []
    assert empty.json()["reports"][0]["rule_outcomes"] == []
