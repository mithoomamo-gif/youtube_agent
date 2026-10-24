"""Round 5 CLOSURE proofs for R5L-003 + R5-004.

R5L-003 (publish-window): the exact verification reproduction —
a channel with ZERO published videos and ZERO analytics days but
one ACTIVE ``publish_window`` learning rule — must now answer
``eligible: False`` / ``reason: 'not_enough_data'`` from
``GET /api/v1/channels/{id}/learning/publish-window`` because the
endpoint reaches the REAL :class:`PublishWindowService` (roadmap
gate: ≥10 published videos + ≥28 days of analytics).  The GET must
not create or modify any schedule rows (nor any learning rules).

R5-004 (comment drafts): a ``proposed`` draft can be EDITED (text
update with provenance: who / when / previous + new text), and
``discarded`` exists as a terminal state DISTINCT from
``rejected``.  Edit / discard are LOCAL state updates only —
every response still carries ``posted: False`` /
``posting: 'not_wired'`` (the no-post guarantee is unchanged).
"""

from __future__ import annotations

import sqlite3
import time
import uuid

import pytest

from app.comments.store import (
    CommentsStateError,
    CommentsStore,
)
from tests.execution_fixtures import make_db

_SUFFIX = uuid.uuid4().hex[:10]
# The suite shares ONE session DB and niche_profiles.key is UNIQUE.
# This module's profile is private scaffolding (an FK target for its
# schedule row), so it must NOT squat the builtin key 'facts' under a
# private id: the app invariant is builtin key == profile id, and other
# suites (e.g. test_schedules_api) seed/resolve the canonical 'facts'
# row by id. A 'facts'-keyed look-alike row makes their INSERT OR
# IGNORE a silent no-op and their channel FK insert fail. The learning
# rule below still uses the niche STRING 'facts' — that is a store
# scope label, unrelated to niche_profiles.
NICHE_KEY = f"closure-facts-{_SUFFIX}"
PW_CHAN = f"closure-pw-{_SUFFIX}"
CM_CHAN = f"closure-cm-{_SUFFIX}"
CM_COMMENT = f"UgxClosure{_SUFFIX}"
VIDEO_ID = "closureVid1"


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# R5L-003 — publish-window endpoint reaches the real service
# ---------------------------------------------------------------------------


def _seed_publish_window_repro() -> None:
    from app import db as db_module
    from app.learning.store import LearningStore

    now = _now_ms()
    conn = db_module.get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO channels (id, youtube_channel_id,"
            " title, language, status, created_at, updated_at)"
            " VALUES (?, ?, ?, 'en', 'active', ?, ?)",
            (PW_CHAN, f"UCclosure{_SUFFIX}", "Closure PW", now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO niche_profiles (id, key, label,"
            " source, created_at, updated_at)"
            " VALUES (?, ?, 'Facts', 'builtin', ?, ?)",
            (f"np-closure-{_SUFFIX}", NICHE_KEY, now, now),
        )
        conn.commit()
        niche_id = conn.execute(
            "SELECT id FROM niche_profiles WHERE key = ?", (NICHE_KEY,)
        ).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO schedules (id, name, channel_id,"
            " niche_profile_id, topic_mode, publish_mode, is_enabled,"
            " created_at, updated_at) VALUES (?, 'Closure sched', ?, ?,"
            " 'auto', 'review_first', 1, ?, ?)",
            (f"sched-closure-{_SUFFIX}", PW_CHAN, niche_id, now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schedule_runs (id, schedule_id,"
            " status, created_at) VALUES (?, ?, 'created', ?)",
            (f"run-closure-{_SUFFIX}", f"sched-closure-{_SUFFIX}", now),
        )
        conn.commit()
    finally:
        conn.close()

    store = LearningStore(db_module.get_connection)
    if store.list_rule_history(PW_CHAN, "facts"):
        return  # the session DB is shared: seed the rule once only
    rule = store.create_candidate(
        PW_CHAN, "facts", "publish_window",
        "Publishing 12-18 UTC outperforms baseline (n=3)",
        statement_family="publish_window:12_18_utc:positive",
        evidence_count=3, confidence=0.7,
    )
    store.activate_rule(rule["id"], channel_id=PW_CHAN, niche="facts")


def _schedule_rows() -> tuple[list, list]:
    from app import db as db_module

    conn = db_module.get_connection()
    try:
        schedules = [
            tuple(r) for r in conn.execute(
                "SELECT * FROM schedules WHERE channel_id = ?"
                " ORDER BY id", (PW_CHAN,))
        ]
        runs = [
            tuple(r) for r in conn.execute(
                "SELECT * FROM schedule_runs WHERE schedule_id = ?"
                " ORDER BY id", (f"sched-closure-{_SUFFIX}",))
        ]
        return schedules, runs
    finally:
        conn.close()


def _rule_count() -> int:
    from app import db as db_module
    from app.learning.store import LearningStore

    store = LearningStore(db_module.get_connection)
    return len(store.list_rule_history(PW_CHAN, "facts"))


@pytest.fixture(autouse=True)
def _seed_pw(app):
    _seed_publish_window_repro()
    yield


def test_publish_window_repro_zero_data_active_rule_not_eligible(client):
    schedules_before, runs_before = _schedule_rows()
    assert schedules_before and runs_before  # the seed actually landed
    rules_before = _rule_count()
    assert rules_before == 1  # exactly one ACTIVE publish_window rule

    resp = client.get(f"/api/v1/channels/{PW_CHAN}/learning/publish-window")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The gate the fallback used to bypass (R5L-003):
    assert body["eligible"] is False
    assert body["reason"] == "not_enough_data"
    assert body["status"] == "insufficient_data"
    assert body["published_count"] == 0
    assert body["days_of_data"] == 0
    assert body["required"] == {"videos": 10, "days": 28}
    assert body["suggestions"] == []
    assert body["source"] == "publish_window_service"

    # The endpoint is a pure read: no schedule rows created/modified,
    # and no learning rules written either.
    schedules_after, runs_after = _schedule_rows()
    assert schedules_after == schedules_before
    assert runs_after == runs_before
    assert _rule_count() == rules_before


# ---------------------------------------------------------------------------
# R5-004 — draft edit (proposed only, provenance) + discarded state
# ---------------------------------------------------------------------------


@pytest.fixture()
def store_env(tmp_path):
    _db_path, factory = make_db(tmp_path)
    store = CommentsStore(factory)
    channel_id = str(uuid.uuid4())
    ts = _now_ms()
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title,"
            " language, status, created_at, updated_at) VALUES"
            " (?, ?, 'Closure', 'en-US', 'active', ?, ?)",
            (channel_id, f"UC-{uuid.uuid4().hex[:8]}", ts, ts),
        )
        conn.commit()
    finally:
        conn.close()
    store.upsert_comment(channel_id, {
        "id": "c-closure-1",
        "video_id": VIDEO_ID,
        "author_display_name": "Viewer",
        "text": "Great video!",
        "published_at": "2026-10-20T10:00:00Z",
    })
    return {"store": store, "channel_id": channel_id, "factory": factory}


def test_edit_proposed_draft_records_provenance(store_env):
    store = store_env["store"]
    draft = store.create_draft(
        "c-closure-1", "Original reply text",
        provenance={"engine": "deterministic", "model": None,
                    "prompt_version": "comments.reply_draft.v1"},
    )
    edited = store.update_draft_text(
        draft["id"], "Edited reply text", edited_by="owner",
    )
    assert edited is not None
    assert edited["draft_text"] == "Edited reply text"
    assert edited["status"] == "proposed"  # edit is not a decision
    prov = edited["provenance"]
    # Generation provenance preserved; edit appended on top.
    assert prov["engine"] == "deterministic"
    assert prov["prompt_version"] == "comments.reply_draft.v1"
    assert prov["last_edited_by"] == "owner"
    assert isinstance(prov["last_edited_at"], int)
    (edit,) = prov["edits"]
    assert edit["edited_by"] == "owner"
    assert edit["previous_text"] == "Original reply text"
    assert edit["new_text"] == "Edited reply text"
    assert isinstance(edit["edited_at"], int)

    again = store.update_draft_text(
        draft["id"], "Second edit", edited_by="owner",
    )
    assert again["provenance"]["edits"][1]["previous_text"] == (
        "Edited reply text"
    )
    with pytest.raises(ValueError):
        store.update_draft_text(draft["id"], "   ")


def test_edit_terminal_draft_refused(store_env):
    store = store_env["store"]
    draft = store.create_draft("c-closure-1", "Reply")
    store.set_draft_status(draft["id"], "approved")
    with pytest.raises(CommentsStateError):
        store.update_draft_text(draft["id"], "Too late")


def test_discard_is_distinct_terminal_state(store_env):
    store = store_env["store"]
    chan = store_env["channel_id"]

    discarded = store.create_draft("c-closure-1", "Throwaway reply")
    row = store.set_draft_status(
        discarded["id"], "discarded", decided_by="owner",
    )
    assert row is not None and row["status"] == "discarded"
    assert row["decided_at"] is not None

    rejected = store.create_draft("c-closure-1", "Decided-against reply")
    row2 = store.set_draft_status(rejected["id"], "rejected")
    assert row2 is not None and row2["status"] == "rejected"
    assert row["status"] != row2["status"]  # distinct states

    # Discarded is listable/filterable as its own status…
    assert [d["id"] for d in store.list_drafts(chan, status="discarded")] == [
        discarded["id"]
    ]
    # …and terminal: no transition out, no edit, immutable.
    for target in ("proposed", "approved", "rejected", "discarded"):
        with pytest.raises(CommentsStateError):
            store.set_draft_status(discarded["id"], target)
    with pytest.raises(CommentsStateError):
        store.update_draft_text(discarded["id"], "revive attempt")
    assert store.get_draft(discarded["id"])["status"] == "discarded"


def test_existing_db_with_old_check_is_upgraded(store_env):
    """A comment_drafts table created with the OLD status CHECK is
    rebuilt by ensure_comments_schema (rows preserved) so
    'discarded' works on pre-existing databases too."""
    factory = store_env["factory"]
    conn = factory()
    try:
        conn.execute("DROP TABLE comment_drafts")
        conn.execute(
            "CREATE TABLE comment_drafts ("
            " id TEXT PRIMARY KEY,"
            " comment_id TEXT NOT NULL REFERENCES comments (id)"
            "   ON DELETE CASCADE,"
            " channel_id TEXT NOT NULL REFERENCES channels (id)"
            "   ON DELETE RESTRICT,"
            " video_id TEXT NOT NULL,"
            " draft_text TEXT NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'proposed'"
            "   CHECK (status IN ('proposed', 'approved', 'rejected')),"
            " created_by TEXT NOT NULL DEFAULT 'agent28'"
            "   CHECK (created_by IN ('agent28', 'user')),"
            " provenance_json TEXT NULL,"
            " decided_at INTEGER NULL, decided_by TEXT NULL,"
            " created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
        )
        now = _now_ms()
        conn.execute(
            "INSERT INTO comment_drafts (id, comment_id, channel_id,"
            " video_id, draft_text, created_at, updated_at) VALUES"
            " ('legacy-draft', 'c-closure-1', ?, ?, 'legacy', ?, ?)",
            (store_env["channel_id"], VIDEO_ID, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    store = store_env["store"]
    row = store.get_draft("legacy-draft")  # store entry ensures schema
    assert row is not None and row["draft_text"] == "legacy"
    updated = store.set_draft_status("legacy-draft", "discarded")
    assert updated is not None and updated["status"] == "discarded"


# ---------------------------------------------------------------------------
# R5-004 over HTTP — edit + discard endpoints, no-post boundary intact
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _seed_comments(app, monkeypatch):
    from app.comments import engagement

    monkeypatch.setattr(engagement, "default_llm", lambda: None)
    from app import db as db_module
    from app.comments.store import CommentsStore

    now = _now_ms()
    conn = db_module.get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO channels (id, youtube_channel_id,"
            " title, language, status, created_at, updated_at)"
            " VALUES (?, ?, 'Closure Comments', 'en', 'active', ?, ?)",
            (CM_CHAN, f"UCcm{_SUFFIX}", now, now),
        )
        conn.commit()
    finally:
        conn.close()
    store = CommentsStore(db_module.get_connection)
    if store.get_comment(CM_COMMENT) is None:
        store.upsert_comment(CM_CHAN, {
            "id": CM_COMMENT,
            "video_id": VIDEO_ID,
            "author_display_name": "Viewer One",
            "text": "How did you make this?",
            "published_at": "2026-10-20T10:00:00Z",
        })
    yield


def _generate_draft(client) -> dict:
    resp = client.post(
        f"/api/v1/channels/{CM_CHAN}/comments/{CM_COMMENT}/drafts"
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_edit_and_discard_draft_via_api(client):
    draft = _generate_draft(client)
    original_text = draft["text"]

    # Edit while proposed (canonical + engagement spellings share
    # the service): text changes, provenance records the edit.
    resp = client.patch(
        f"/api/v1/channels/{CM_CHAN}/comment-drafts/{draft['id']}",
        json={"draft_text": "Edited via API", "edited_by": "owner"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["draft_text"] == "Edited via API"
    assert body["text"] == "Edited via API"
    assert body["status"] == "proposed"
    assert body["posted"] is False and body["posting"] == "not_wired"
    (edit,) = body["provenance"]["edits"]
    assert edit["edited_by"] == "owner"
    assert edit["previous_text"] == original_text
    assert edit["new_text"] == "Edited via API"

    # Canonical spelling edits too.
    resp = client.patch(
        f"/api/v1/channels/{CM_CHAN}/drafts/{draft['id']}",
        json={"draft_text": "Edited twice"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["draft_text"] == "Edited twice"

    # Discard: distinct terminal status, decided stamps present,
    # still never posted.
    resp = client.post(
        f"/api/v1/channels/{CM_CHAN}/comment-drafts/{draft['id']}/discard",
        json={"decided_by": "owner"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "discarded"
    assert body["decided_at"] is not None
    assert body["posted"] is False and body["posting"] == "not_wired"

    # Terminal: edit after discard -> 409; re-decide -> 409.
    resp = client.patch(
        f"/api/v1/channels/{CM_CHAN}/comment-drafts/{draft['id']}",
        json={"draft_text": "revive"},
    )
    assert resp.status_code == 409, resp.text
    resp = client.post(
        f"/api/v1/channels/{CM_CHAN}/comment-drafts/{draft['id']}/approve",
        json={},
    )
    assert resp.status_code == 409, resp.text

    # Filterable as its own status via the canonical list.
    resp = client.get(
        f"/api/v1/channels/{CM_CHAN}/drafts",
        params={"status": "discarded"},
    )
    assert resp.status_code == 200
    assert any(d["id"] == draft["id"] for d in resp.json()["items"])

    # And the canonical discard spelling works on a fresh draft.
    draft2 = _generate_draft(client)
    resp = client.post(
        f"/api/v1/channels/{CM_CHAN}/drafts/{draft2['id']}/discard",
        json={},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "discarded"
