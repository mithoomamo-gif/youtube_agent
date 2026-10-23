"""Comment Engagement REST API tests (Round 5 — Agent 28).

Endpoints under ``/api/v1/channels/{channel_id}/...`` exercised over
the full sidecar app (conftest ``client`` fixture, launch-token set):

* generate draft (201, ``proposed``, provenance present) — with the
  default LLM pinned to ``None`` so generation is the labelled
  deterministic mode and NO network is ever touched;
* approve / reject lifecycle over HTTP, including the 409 on a
  second decision and the not-wired posting boundary fields
  (``posted: false``, ``posting: 'not_wired'``) on every response;
* moderation queue list + local decision (dismiss), with
  ``action_executed: false`` throughout;
* owner isolation: cross-channel draft/flag access answers 404,
  unknown owner channel answers 404 ``channel_not_found``.
"""

from __future__ import annotations

import time
import uuid

import pytest

_SUFFIX = uuid.uuid4().hex[:10]
CHAN_A = f"eng-a-{_SUFFIX}"
CHAN_B = f"eng-b-{_SUFFIX}"
YT_A = f"UCengA{_SUFFIX}xxxx"
YT_B = f"UCengB{_SUFFIX}xxxx"
VIDEO_ID = "engVideo001"
COMMENT_ID = f"UgxEng{_SUFFIX}"


def _seed() -> None:
    from app import db as db_module
    from app.comments.store import CommentsStore

    now = int(time.time() * 1000)
    conn = db_module.get_connection()
    try:
        for chan_id, yt_id, title in (
            (CHAN_A, YT_A, "Engagement Channel A"),
            (CHAN_B, YT_B, "Engagement Channel B"),
        ):
            conn.execute(
                "INSERT OR IGNORE INTO channels (id, youtube_channel_id,"
                " title, language, status, created_at, updated_at)"
                " VALUES (?, ?, ?, 'en', 'active', ?, ?)",
                (chan_id, yt_id, title, now, now),
            )
        conn.commit()
    finally:
        conn.close()

    store = CommentsStore(db_module.get_connection)
    store.upsert_comment(CHAN_A, {
        "id": COMMENT_ID,
        "video_id": VIDEO_ID,
        "author_display_name": "Viewer One",
        "text": "How did you make this video?",
        "like_count": 7,
        "published_at": "2026-10-20T10:00:00Z",
    })
    # The app DB is session-shared: seed the flag once only (the
    # queue test decides it; later tests must not re-add it).
    existing = store.list_moderation_flags(
        CHAN_A, status=None, comment_id=COMMENT_ID
    )
    if not existing:
        store.create_moderation_flag(
            COMMENT_ID, "spam",
            channel_id=CHAN_A,
            reason="heuristic: link-like text",
            provenance={"engine": "deterministic",
                        "spam_method": "heuristics_v1"},
        )


@pytest.fixture(autouse=True)
def _setup(app, monkeypatch):
    # Depends on the conftest `app` fixture so the DB schema exists.
    # Pin the API's default LLM OFF: deterministic labelled drafts,
    # zero network in these tests.
    from app.comments import engagement

    monkeypatch.setattr(engagement, "default_llm", lambda: None)
    _seed()
    yield


def _generate(client) -> dict:
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/comments/{COMMENT_ID}/drafts"
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_generate_draft_via_api(client):
    draft = _generate(client)
    assert draft["status"] == "proposed"
    assert draft["comment_id"] == COMMENT_ID
    assert draft["channel_id"] == CHAN_A
    assert draft["text"]
    assert draft["provenance"]["engine"] == "deterministic"
    assert draft["provenance"]["fallback_reason"] == "llm_not_configured"
    # THE boundary, visible in the API response itself:
    assert draft["posted"] is False
    assert draft["posting"] == "not_wired"


def test_approve_and_reject_via_api(client):
    draft = _generate(client)
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/comment-drafts/{draft['id']}/approve",
        json={"decided_by": "owner"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["decided_by"] == "owner"
    assert body["decided_at"] is not None
    assert body["posted"] is False
    assert body["posting"] == "not_wired"

    # Terminal: a second decision is a 409, not a silent re-decide.
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/comment-drafts/{draft['id']}/reject",
        json={},
    )
    assert resp.status_code == 409, resp.text

    draft2 = _generate(client)
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/comment-drafts/{draft2['id']}/reject",
        json={},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


def test_list_drafts_via_api_scoped(client):
    _generate(client)
    resp = client.get(f"/api/v1/channels/{CHAN_A}/comment-drafts")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(d["comment_id"] == COMMENT_ID for d in items)
    assert all(d["posted"] is False for d in items)
    # Channel B has none of channel A's drafts.
    resp = client.get(f"/api/v1/channels/{CHAN_B}/comment-drafts")
    assert resp.status_code == 200
    assert not any(d["comment_id"] == COMMENT_ID for d in resp.json()["items"])


def test_cross_channel_approve_is_404(client):
    draft = _generate(client)
    resp = client.post(
        f"/api/v1/channels/{CHAN_B}/comment-drafts/{draft['id']}/approve",
        json={},
    )
    assert resp.status_code == 404, resp.text


def test_unknown_channel_is_404(client):
    resp = client.post(
        f"/api/v1/channels/no-such-channel/comments/{COMMENT_ID}/drafts"
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "channel_not_found"


def test_moderation_queue_and_local_decision_via_api(client):
    resp = client.get(f"/api/v1/channels/{CHAN_A}/moderation-queue")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    flag = items[0]
    assert flag["status"] == "pending"
    assert flag["suggested_action_label"] == "review_for_spam"
    assert flag["action_executed"] is False
    assert flag["comment_text"] == "How did you make this video?"

    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/moderation-queue/{flag['id']}/decision",
        json={"decision": "dismiss", "decided_by": "owner"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "dismissed"
    assert body["decision"] == "dismiss"
    assert body["action_executed"] is False
    assert body["youtube_action"] is None

    # Queue is now empty for channel A; deciding again is a 409.
    resp = client.get(f"/api/v1/channels/{CHAN_A}/moderation-queue")
    assert resp.json()["items"] == []
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/moderation-queue/{flag['id']}/decision",
        json={"decision": "mark_reviewed"},
    )
    assert resp.status_code == 409, resp.text
