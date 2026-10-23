"""Comments store: upsert idempotency, sync state, state machines,
owner-channel isolation.

Two owner channels (A and B) are seeded in the real section-15 schema;
every isolation assertion checks that channel B cannot list, read or
mutate channel A's comments / drafts / flags / analyses.
"""

from __future__ import annotations

import time
import uuid

import pytest

from app.comments.store import (
    CommentsNotFoundError,
    CommentsScopeError,
    CommentsStateError,
    CommentsStore,
)
from tests.execution_fixtures import make_db

VIDEO_A = "videoAAA111"
VIDEO_B = "videoBBB222"


def _seed_channel(factory, label: str) -> str:
    channel_id = str(uuid.uuid4())
    ts = int(time.time() * 1000)
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title, language,"
            " status, created_at, updated_at) VALUES (?, ?, ?, 'en-US',"
            " 'active', ?, ?)",
            (channel_id, f"UC-owner-{label}-{uuid.uuid4().hex[:8]}",
             f"Owner {label}", ts, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return channel_id


@pytest.fixture()
def env(tmp_path):
    _db_path, factory = make_db(tmp_path)
    return {
        "factory": factory,
        "store": CommentsStore(factory),
        "channel_a": _seed_channel(factory, "A"),
        "channel_b": _seed_channel(factory, "B"),
    }


def _comment(cid="UgxAAA111", **overrides):
    base = {
        "id": cid,
        "video_id": VIDEO_A,
        "author_display_name": "Viewer One",
        "author_channel_id": "UCviewer1",
        "text": "Great video!",
        "like_count": 3,
        "reply_count": 1,
        "published_at": "2026-10-20T10:00:00Z",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Upsert idempotency
# ---------------------------------------------------------------------------


def test_upsert_comment_insert_and_get(env):
    store = env["store"]
    row = store.upsert_comment(env["channel_a"], _comment())
    assert row["id"] == "UgxAAA111"
    assert row["channel_id"] == env["channel_a"]
    assert row["video_id"] == VIDEO_A
    assert row["like_count"] == 3
    assert row["is_top_level"] is True
    # RFC3339 published_at converted to epoch ms.
    from datetime import datetime, timezone

    expected_ms = int(
        datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc).timestamp()
        * 1000
    )
    assert row["published_at"] == expected_ms
    got = store.get_comment("UgxAAA111")
    assert got is not None and got["text"] == "Great video!"


def test_upsert_comment_idempotent_updates_counts(env):
    store = env["store"]
    first = store.upsert_comment(env["channel_a"], _comment())
    second = store.upsert_comment(
        env["channel_a"],
        _comment(like_count=42, reply_count=7, text="Great video! (edited)"),
    )
    assert second["id"] == first["id"]
    assert second["created_at"] == first["created_at"]
    assert second["like_count"] == 42
    assert second["reply_count"] == 7
    assert second["text"] == "Great video! (edited)"
    assert second["fetched_at"] >= first["fetched_at"]
    assert store.count_comments(env["channel_a"]) == 1
    assert store.count_comments(env["channel_a"], VIDEO_A) == 1


def test_upsert_comments_bulk_and_replies(env):
    store = env["store"]
    rows = store.upsert_comments(
        env["channel_a"],
        [
            _comment("c-parent"),
            _comment("c-reply", parent_comment_id="c-parent", reply_count=0),
        ],
        video_id=VIDEO_A,
    )
    assert [r["id"] for r in rows] == ["c-parent", "c-reply"]
    reply = store.get_comment("c-reply")
    assert reply is not None and reply["is_top_level"] is False
    replies = store.list_replies(env["channel_a"], "c-parent")
    assert [r["id"] for r in replies] == ["c-reply"]
    top = store.list_comments(env["channel_a"], VIDEO_A, top_level_only=True)
    assert [r["id"] for r in top] == ["c-parent"]
    # Re-running the same bulk sync duplicates nothing.
    store.upsert_comments(
        env["channel_a"], [_comment("c-parent"), _comment("c-reply")],
        video_id=VIDEO_A,
    )
    assert store.count_comments(env["channel_a"], VIDEO_A) == 2


def test_upsert_comment_requires_id_and_video(env):
    store = env["store"]
    with pytest.raises(ValueError):
        store.upsert_comment(env["channel_a"], {"video_id": VIDEO_A})
    with pytest.raises(ValueError):
        store.upsert_comment(env["channel_a"], {"id": "x1"})


def test_upsert_comment_cross_channel_raises_scope(env):
    store = env["store"]
    store.upsert_comment(env["channel_a"], _comment())
    with pytest.raises(CommentsScopeError):
        store.upsert_comment(env["channel_b"], _comment())
    # The original row is untouched and still owned by A.
    row = store.get_comment("UgxAAA111", channel_id=env["channel_a"])
    assert row is not None


# ---------------------------------------------------------------------------
# Sync state
# ---------------------------------------------------------------------------


def test_sync_state_never_synced_default(env):
    store = env["store"]
    state = store.get_sync_state(env["channel_a"], VIDEO_A)
    assert state["status"] == "never_synced"
    assert state["exists"] is False
    assert state["comment_count"] == 0
    assert state["last_synced_at"] is None
    assert store.list_sync_states(env["channel_a"]) == []


def test_sync_state_set_get_and_transitions(env):
    store = env["store"]
    state = store.set_sync_state(
        env["channel_a"], VIDEO_A, "not_connected",
        error_reason="channel not connected",
    )
    assert state["status"] == "not_connected"
    assert state["error_reason"] == "channel not connected"
    assert state["exists"] is True

    state = store.set_sync_state(
        env["channel_a"], VIDEO_A, "synced", comment_count=12
    )
    assert state["status"] == "synced"
    assert state["comment_count"] == 12
    assert state["last_synced_at"] is not None
    assert state["error_reason"] is None  # cleared on success

    state = store.set_sync_state(
        env["channel_a"], VIDEO_A, "error", error_reason="quota exceeded"
    )
    assert state["status"] == "error"
    assert state["error_reason"] == "quota exceeded"
    assert state["comment_count"] == 12  # preserved when not given

    listed = store.list_sync_states(env["channel_a"])
    assert len(listed) == 1 and listed[0]["video_id"] == VIDEO_A
    assert store.list_sync_states(env["channel_b"]) == []

    with pytest.raises(ValueError):
        store.set_sync_state(env["channel_a"], VIDEO_A, "bogus")


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------


def test_analysis_create_list_latest(env):
    store = env["store"]
    a1 = store.create_analysis(
        env["channel_a"], engine="deterministic", scope="video",
        video_id=VIDEO_A, rollup={"questions": 2}, comment_count=5,
    )
    assert a1["engine"] == "deterministic"
    assert a1["rollup"] == {"questions": 2}
    a2 = store.create_analysis(
        env["channel_a"], engine="llm", scope="video", video_id=VIDEO_A,
        rollup={"sentiment": "positive"}, model_id="gemini-test",
        comment_count=9, provenance={"agent": "agent28"},
    )
    assert a2["model_id"] == "gemini-test"
    latest = store.latest_analysis(
        env["channel_a"], scope="video", video_id=VIDEO_A
    )
    assert latest is not None and latest["id"] == a2["id"]
    assert len(store.list_analyses(env["channel_a"], video_id=VIDEO_A)) == 2
    assert store.list_analyses(env["channel_b"]) == []
    assert store.get_analysis(a2["id"], channel_id=env["channel_b"]) is None

    with pytest.raises(ValueError):
        store.create_analysis(env["channel_a"], engine="magic")
    with pytest.raises(ValueError):
        store.create_analysis(env["channel_a"], engine="llm", scope="video")


# ---------------------------------------------------------------------------
# Drafts — guarded state machine
# ---------------------------------------------------------------------------


def _draft(env):
    store = env["store"]
    store.upsert_comment(env["channel_a"], _comment())
    return store.create_draft("UgxAAA111", "Thanks for watching!")


def test_draft_create_defaults_and_scope_copy(env):
    draft = _draft(env)
    assert draft["status"] == "proposed"
    assert draft["created_by"] == "agent28"
    assert draft["channel_id"] == env["channel_a"]
    assert draft["video_id"] == VIDEO_A
    assert draft["decided_at"] is None


def test_draft_state_machine_happy_paths(env):
    store = env["store"]
    draft = _draft(env)
    approved = store.set_draft_status(
        draft["id"], "approved", decided_by="user"
    )
    assert approved is not None
    assert approved["status"] == "approved"
    assert approved["decided_at"] is not None
    assert approved["decided_by"] == "user"

    store.upsert_comment(env["channel_a"], _comment("c-second"))
    draft2 = store.create_draft("c-second", "Another reply")
    rejected = store.set_draft_status(draft2["id"], "rejected")
    assert rejected is not None and rejected["status"] == "rejected"


@pytest.mark.parametrize("terminal", ["approved", "rejected"])
@pytest.mark.parametrize("target", ["proposed", "approved", "rejected"])
def test_draft_terminal_states_immutable(env, terminal, target):
    store = env["store"]
    draft = _draft(env)
    store.set_draft_status(draft["id"], terminal)
    with pytest.raises(CommentsStateError):
        store.set_draft_status(draft["id"], target)
    row = store.get_draft(draft["id"])
    assert row is not None and row["status"] == terminal


def test_draft_noop_and_bad_status_rejected(env):
    store = env["store"]
    draft = _draft(env)
    with pytest.raises(CommentsStateError):
        store.set_draft_status(draft["id"], "proposed")
    with pytest.raises(ValueError):
        store.set_draft_status(draft["id"], "bogus")
    assert store.set_draft_status("no-such-draft", "approved") is None


def test_draft_unknown_comment_and_scope(env):
    store = env["store"]
    with pytest.raises(CommentsNotFoundError):
        store.create_draft("no-such-comment", "hi")
    store.upsert_comment(env["channel_a"], _comment())
    with pytest.raises(CommentsScopeError):
        store.create_draft(
            "UgxAAA111", "hi", channel_id=env["channel_b"]
        )


# ---------------------------------------------------------------------------
# Moderation flags — guarded, human-decision only
# ---------------------------------------------------------------------------


def test_flag_create_and_decide(env):
    store = env["store"]
    store.upsert_comment(env["channel_a"], _comment())
    flag = store.create_moderation_flag(
        "UgxAAA111", "spam", reason="link spam",
        suggested_action="review",
    )
    assert flag["status"] == "pending"
    assert flag["flag_kind"] == "spam"
    assert flag["channel_id"] == env["channel_a"]
    assert flag["suggested_action"] == "review"

    decided = store.set_moderation_flag_status(
        flag["id"], "dismissed", decided_by="user"
    )
    assert decided is not None and decided["status"] == "dismissed"
    assert decided["decided_at"] is not None
    # Terminal: cannot be re-decided.
    with pytest.raises(CommentsStateError):
        store.set_moderation_flag_status(flag["id"], "actioned")
    with pytest.raises(ValueError):
        store.create_moderation_flag("UgxAAA111", "bogus-kind")


# ---------------------------------------------------------------------------
# Cross-channel isolation across every surface
# ---------------------------------------------------------------------------


def test_cross_channel_isolation(env):
    store = env["store"]
    a, b = env["channel_a"], env["channel_b"]
    store.upsert_comment(a, _comment())
    store.upsert_comment(b, _comment("c-b", video_id=VIDEO_B))
    store.set_sync_state(a, VIDEO_A, "synced", comment_count=1)
    draft = store.create_draft("UgxAAA111", "reply for A")
    flag = store.create_moderation_flag("UgxAAA111", "abuse")
    analysis = store.create_analysis(
        a, engine="deterministic", scope="video", video_id=VIDEO_A,
        rollup={},
    )

    # Lists are always scoped.
    assert [c["id"] for c in store.list_comments(b)] == ["c-b"]
    assert store.list_drafts(b) == []
    assert store.list_moderation_flags(b) == []
    assert store.list_analyses(b) == []
    assert store.list_sync_states(b) == []
    assert store.get_sync_state(b, VIDEO_A)["status"] == "never_synced"

    # Scoped reads look like "not found" from B.
    assert store.get_comment("UgxAAA111", channel_id=b) is None
    assert store.get_draft(draft["id"], channel_id=b) is None
    assert store.get_moderation_flag(flag["id"], channel_id=b) is None
    assert store.get_analysis(analysis["id"], channel_id=b) is None

    # Scoped writes raise.
    with pytest.raises(CommentsScopeError):
        store.set_draft_status(draft["id"], "approved", channel_id=b)
    with pytest.raises(CommentsScopeError):
        store.set_moderation_flag_status(flag["id"], "actioned", channel_id=b)

    # A's rows are untouched by B's failed attempts.
    assert store.get_draft(draft["id"])["status"] == "proposed"
    assert store.get_moderation_flag(flag["id"])["status"] == "pending"
