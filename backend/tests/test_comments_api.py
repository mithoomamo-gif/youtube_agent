"""Comments Intelligence REST API tests (Round 5 — Agent 28, REST worker).

Endpoints under ``/api/v1/channels/{channel_id}/...`` exercised over
the full sidecar app (conftest ``client`` fixture, launch-token set),
covering the sync / listing / analysis / canonical drafts /
moderation surface of ``app.routers.comments`` +
``app.comments.api_service`` (the engagement worker's own
``/comment-drafts`` + ``/moderation-queue`` spellings are covered by
``test_comments_engagement_api.py``).

Covered:
- sync: provider boundary patched — ``not_connected`` is a 200 with
  the typed payload AND the persisted ``comment_sync_state``; a
  ``fetched`` page upserts and the comments list endpoint serves it;
  sync-status starts at the honest ``never_synced``;
- comments listing: pagination, flags attached;
- analysis: ``not_analysed`` before any run, ``no_comments`` trigger
  on an empty video, a real deterministic run persists a rollup that
  the GET endpoints then serve (video + channel scopes), and spam
  lands in the moderation queue;
- drafts via the canonical ``/drafts`` paths: generate (201,
  provenance, ``posted: false`` / ``posting: 'not_wired'``),
  approve/reject lifecycle, 409 on a second decision, status filter;
- moderation via ``/moderation``: queue + ``dismiss`` / ``reviewed``
  decisions recorded locally (``action_executed: false``);
- owner isolation: cross-channel access 404s everywhere, unknown
  owner channel 404 ``channel_not_found``.

The default LLM is pinned OFF (labelled deterministic mode) so no
network is ever touched.
"""

from __future__ import annotations

import time
import uuid

import pytest

_SUFFIX = uuid.uuid4().hex[:10]
CHAN_A = f"cmt-a-{_SUFFIX}"
CHAN_B = f"cmt-b-{_SUFFIX}"
YT_A = f"UCcmtA{_SUFFIX}xxxx"
YT_B = f"UCcmtB{_SUFFIX}xxxx"

VIDEO_MAIN = f"mainVid{_SUFFIX[:4]}"     # seeded comments + analysis
VIDEO_SYNC = f"syncVid{_SUFFIX[:4]}"     # fetched-sync target
VIDEO_EMPTY = f"emptyVid{_SUFFIX[:4]}"   # never has comments
VIDEO_NC = f"ncVid{_SUFFIX[:4]}"         # not-connected sync target

C_FAN = f"UgxFan{_SUFFIX}"
C_QUESTION = f"UgxQ{_SUFFIX}"
C_SPAM = f"UgxSpam{_SUFFIX}"
C_B = f"UgxB{_SUFFIX}"


def _seed() -> None:
    from app import db as db_module
    from app.comments.store import CommentsStore

    now = int(time.time() * 1000)
    conn = db_module.get_connection()
    try:
        for chan_id, yt_id, title in (
            (CHAN_A, YT_A, "Comments Channel A"),
            (CHAN_B, YT_B, "Comments Channel B"),
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
        "id": C_FAN, "video_id": VIDEO_MAIN,
        "author_display_name": "Happy Viewer",
        "text": "Amazing video, loved it, great work and superb narration!",
        "like_count": 42, "published_at": "2026-10-20T10:00:00Z",
    })
    store.upsert_comment(CHAN_A, {
        "id": C_QUESTION, "video_id": VIDEO_MAIN,
        "author_display_name": "Curious Viewer",
        "text": "What is the deepest point of the ocean?",
        "like_count": 99, "published_at": "2026-10-20T11:00:00Z",
    })
    store.upsert_comment(CHAN_A, {
        "id": C_SPAM, "video_id": VIDEO_MAIN,
        "author_display_name": "Spammy",
        "text": "FREE FOLLOWERS earn money online visit www.spamxyz.com now",
        "like_count": 0, "published_at": "2026-10-20T12:00:00Z",
    })
    store.upsert_comment(CHAN_B, {
        "id": C_B, "video_id": VIDEO_MAIN,
        "author_display_name": "Other Channel Viewer",
        "text": "This comment belongs to channel B",
        "like_count": 1, "published_at": "2026-10-20T09:00:00Z",
    })


@pytest.fixture(autouse=True)
def _setup(app, monkeypatch):
    # Depends on the conftest `app` fixture so the DB schema exists.
    # Pin the API's default LLM OFF: deterministic labelled analysis
    # + drafts, zero network in these tests.
    from app.comments import engagement

    monkeypatch.setattr(engagement, "default_llm", lambda: None)
    _seed()
    yield


def _patch_fetch(monkeypatch, result_factory) -> None:
    """Replace the provider fetch at the sync service's boundary."""
    import app.comments.service as sync_service

    async def fake_fetch(video_id, **kwargs):
        return result_factory(video_id)

    monkeypatch.setattr(
        sync_service.comments_provider, "fetch_video_comments", fake_fetch
    )


# ---------------------------------------------------------------------------
# Sync — honest not_connected (200 + typed payload, state persisted)
# ---------------------------------------------------------------------------


def test_sync_not_connected_is_200_honest_and_persisted(client, monkeypatch):
    from app.providers.youtube.comments import CommentFetchResult

    _patch_fetch(monkeypatch, lambda vid: CommentFetchResult(
        status="not_connected", video_id=vid,
        reason="no_youtube_credential",
        detail="No usable YouTube credential is configured.",
    ))

    status_before = client.get(
        f"/api/v1/channels/{CHAN_A}/videos/{VIDEO_NC}/comments/sync-status"
    )
    assert status_before.status_code == 200
    assert status_before.json()["status"] == "never_synced"

    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/videos/{VIDEO_NC}/comments/sync"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "not_connected"
    assert body["reason"] == "no_youtube_credential"
    assert body["sync_status"] == "not_connected"
    assert body["fetched_count"] == 0
    assert body["sync_state"]["status"] == "not_connected"

    status_after = client.get(
        f"/api/v1/channels/{CHAN_A}/videos/{VIDEO_NC}/comments/sync-status"
    )
    assert status_after.json()["status"] == "not_connected"
    assert status_after.json()["error_reason"] == "no_youtube_credential"


def test_sync_fetched_upserts_and_lists(client, monkeypatch):
    from app.providers.youtube.comments import (
        CommentFetchResult,
        FetchedComment,
    )

    def make(vid):
        return CommentFetchResult(
            status="fetched", video_id=vid, pages_fetched=1,
            comments=[
                FetchedComment(
                    id=f"UgxSync1{_SUFFIX}", video_id=vid,
                    author_display_name="Sync Viewer",
                    text="Synced comment one, great video",
                    like_count=3, published_at="2026-10-21T08:00:00Z",
                ),
                FetchedComment(
                    id=f"UgxSync2{_SUFFIX}", video_id=vid,
                    author_display_name="Sync Viewer Two",
                    text="Synced comment two",
                    like_count=1, published_at="2026-10-21T09:00:00Z",
                ),
            ],
        )

    _patch_fetch(monkeypatch, make)
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/videos/{VIDEO_SYNC}/comments/sync"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "fetched"
    assert body["inserted_count"] == 2
    assert body["total_stored"] == 2
    assert body["sync_status"] == "synced"

    listing = client.get(
        f"/api/v1/channels/{CHAN_A}/videos/{VIDEO_SYNC}/comments"
    )
    assert listing.status_code == 200
    payload = listing.json()
    assert payload["total"] == 2
    assert len(payload["items"]) == 2
    assert payload["items"][0]["flags"] == []
    assert payload["items"][0]["flag_count"] == 0


def test_sync_unknown_channel_404(client):
    resp = client.post(
        f"/api/v1/channels/no-such-channel/videos/{VIDEO_MAIN}/comments/sync"
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "channel_not_found"
    status = client.get(
        f"/api/v1/channels/no-such-channel/videos/{VIDEO_MAIN}"
        "/comments/sync-status"
    )
    assert status.status_code == 404


# ---------------------------------------------------------------------------
# Comments listing — pagination + owner scoping
# ---------------------------------------------------------------------------


def test_comments_listing_pagination_and_scope(client):
    page1 = client.get(
        f"/api/v1/channels/{CHAN_A}/videos/{VIDEO_MAIN}/comments",
        params={"limit": 2, "offset": 0},
    )
    assert page1.status_code == 200
    body = page1.json()
    assert body["total"] == 3  # only A's comments, never B's
    assert len(body["items"]) == 2
    assert body["next_cursor"] == "2"
    ids = {item["id"] for item in body["items"]}
    assert C_B not in ids

    page2 = client.get(
        f"/api/v1/channels/{CHAN_A}/videos/{VIDEO_MAIN}/comments",
        params={"limit": 2, "offset": 2},
    )
    assert len(page2.json()["items"]) == 1
    assert page2.json()["next_cursor"] is None

    # B asking for the same video id sees only its own comment.
    other = client.get(
        f"/api/v1/channels/{CHAN_B}/videos/{VIDEO_MAIN}/comments"
    )
    assert [i["id"] for i in other.json()["items"]] == [C_B]


# ---------------------------------------------------------------------------
# Analysis — not_analysed / no_comments / persisted rollups
# ---------------------------------------------------------------------------


def test_analysis_not_analysed_state(client):
    video = client.get(
        f"/api/v1/channels/{CHAN_A}/videos/{VIDEO_MAIN}/analysis"
    )
    assert video.status_code == 200
    assert video.json()["status"] == "not_analysed"
    assert video.json()["analysis"] is None

    channel = client.get(f"/api/v1/channels/{CHAN_A}/analysis")
    assert channel.json()["status"] == "not_analysed"
    assert channel.json()["analysis"] is None


def test_analyse_empty_video_is_no_comments(client):
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/videos/{VIDEO_EMPTY}/analyse"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "no_comments"
    assert resp.json()["comment_count"] == 0
    # Nothing was persisted by the no-op.
    after = client.get(
        f"/api/v1/channels/{CHAN_A}/videos/{VIDEO_EMPTY}/analysis"
    )
    assert after.json()["status"] == "not_analysed"


def test_analyse_video_persists_rollup_and_spam_flags(client):
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/videos/{VIDEO_MAIN}/analyse"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "analysed"
    assert body["engine"] == "deterministic"  # LLM pinned off, labelled
    assert body["comment_count"] == 3
    assert body["rollup"]["scope"] == "video"
    assert body["persist_report"]["store_analysis"] is True

    got = client.get(
        f"/api/v1/channels/{CHAN_A}/videos/{VIDEO_MAIN}/analysis"
    )
    payload = got.json()
    assert payload["status"] == "analysed"
    assert payload["analysis"]["rollup"]["comment_count"] == 3
    assert payload["analysis"]["engine"] == "deterministic"
    assert payload["analysis"]["provenance"]["engine"] == "deterministic"

    # The spam comment was flagged by the analysis and carries its
    # flag in the comments listing + the moderation queue.
    listing = client.get(
        f"/api/v1/channels/{CHAN_A}/videos/{VIDEO_MAIN}/comments"
    )
    by_id = {i["id"]: i for i in listing.json()["items"]}
    assert by_id[C_SPAM]["flag_count"] >= 1

    queue = client.get(f"/api/v1/channels/{CHAN_A}/moderation")
    flagged = {i["comment_id"] for i in queue.json()["items"]}
    assert C_SPAM in flagged


def test_analyse_channel_rollup(client):
    resp = client.post(f"/api/v1/channels/{CHAN_A}/analyse")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "analysed"
    assert body["rollup"]["scope"] == "channel"
    # Channel scope aggregates ALL of A's stored comments (the seeded
    # video's 3, plus any earlier test synced into this channel).
    assert body["comment_count"] >= 3
    assert body["rollup"]["comment_count"] == body["comment_count"]

    got = client.get(f"/api/v1/channels/{CHAN_A}/analysis")
    assert got.json()["status"] == "analysed"
    assert got.json()["analysis"]["scope"] == "channel"
    assert (
        got.json()["analysis"]["rollup"]["comment_count"]
        == body["comment_count"]
    )


# ---------------------------------------------------------------------------
# Drafts — canonical /drafts lifecycle
# ---------------------------------------------------------------------------


def _generate_draft(client) -> dict:
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/comments/{C_QUESTION}/drafts"
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_draft_lifecycle_via_canonical_paths(client):
    draft = _generate_draft(client)
    assert draft["status"] == "proposed"
    assert draft["posted"] is False
    assert draft["posting"] == "not_wired"
    assert draft["provenance"]["engine"] == "deterministic"
    assert draft["provenance"]["fallback_reason"] == "llm_not_configured"
    assert draft["draft_text"]

    got = client.get(
        f"/api/v1/channels/{CHAN_A}/drafts/{draft['id']}"
    )
    assert got.status_code == 200
    assert got.json()["id"] == draft["id"]

    approved = client.post(
        f"/api/v1/channels/{CHAN_A}/drafts/{draft['id']}/approve",
        json={"decided_by": "owner"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["posted"] is False
    assert approved.json()["posting"] == "not_wired"
    assert approved.json()["decided_by"] == "owner"

    # Terminal states are immutable: a second decision is a 409.
    again = client.post(
        f"/api/v1/channels/{CHAN_A}/drafts/{draft['id']}/reject", json={}
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "illegal_transition"

    # Status filter reflects the decision.
    listing = client.get(
        f"/api/v1/channels/{CHAN_A}/drafts", params={"status": "approved"}
    )
    assert draft["id"] in [d["id"] for d in listing.json()["items"]]
    proposed = client.get(
        f"/api/v1/channels/{CHAN_A}/drafts", params={"status": "proposed"}
    )
    assert draft["id"] not in [d["id"] for d in proposed.json()["items"]]

    bad_status = client.get(
        f"/api/v1/channels/{CHAN_A}/drafts", params={"status": "bogus"}
    )
    assert bad_status.status_code == 422


def test_draft_reject_via_canonical_path(client):
    draft = _generate_draft(client)
    rejected = client.post(
        f"/api/v1/channels/{CHAN_A}/drafts/{draft['id']}/reject", json={}
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["posted"] is False


def test_draft_unknown_comment_404(client):
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/comments/no-such-comment/drafts"
    )
    assert resp.status_code == 404
    # The generate endpoint is the engagement worker's facade; its
    # error mapping uses the generic 404 code.
    assert resp.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Moderation — canonical /moderation decisions (local recording only)
# ---------------------------------------------------------------------------


def _seed_flag() -> str:
    from app import db as db_module
    from app.comments.store import CommentsStore

    store = CommentsStore(db_module.get_connection)
    existing = store.list_moderation_flags(
        CHAN_A, status=None, comment_id=C_FAN
    )
    if existing:
        return existing[0]["id"]
    flag = store.create_moderation_flag(
        C_FAN, "other", channel_id=CHAN_A,
        reason="needs a human look",
        suggested_action="review",
        provenance={"engine": "deterministic"},
    )
    return flag["id"]


def test_moderation_queue_and_decisions(client):
    flag_id = _seed_flag()
    queue = client.get(f"/api/v1/channels/{CHAN_A}/moderation")
    assert queue.status_code == 200
    mine = [i for i in queue.json()["items"] if i["id"] == flag_id]
    assert len(mine) == 1
    view = mine[0]
    assert view["status"] == "pending"
    assert view["comment_text"]  # flagged comment enriched
    assert view["suggested_action_label"] == "review"
    assert view["action_executed"] is False

    decided = client.post(
        f"/api/v1/channels/{CHAN_A}/moderation/{flag_id}/decision",
        json={"decision": "reviewed", "decided_by": "owner"},
    )
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["status"] == "actioned"  # reviewed == handled by a human
    assert body["action_executed"] is False

    # Already decided: a second decision is a 409.
    again = client.post(
        f"/api/v1/channels/{CHAN_A}/moderation/{flag_id}/decision",
        json={"decision": "dismiss"},
    )
    assert again.status_code == 409


def test_moderation_dismiss_and_invalid_decision(client):
    from app import db as db_module
    from app.comments.store import CommentsStore

    store = CommentsStore(db_module.get_connection)
    flag = store.create_moderation_flag(
        C_QUESTION, "other", channel_id=CHAN_A, reason="check me",
        suggested_action="review",
    )
    bad = client.post(
        f"/api/v1/channels/{CHAN_A}/moderation/{flag['id']}/decision",
        json={"decision": "explode"},
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_moderation_decision"

    dismissed = client.post(
        f"/api/v1/channels/{CHAN_A}/moderation/{flag['id']}/decision",
        json={"decision": "dismiss"},
    )
    assert dismissed.status_code == 200
    assert dismissed.json()["status"] == "dismissed"
    assert dismissed.json()["action_executed"] is False


# ---------------------------------------------------------------------------
# Channel-level aggregates (the Comments screen contract)
# ---------------------------------------------------------------------------


def test_comment_videos_index_and_rollups(client, monkeypatch):
    # Per-video sync rows: the seeded video is known via its stored
    # comments, with an honest sync/analysis state.
    resp = client.get(f"/api/v1/channels/{CHAN_A}/comments/videos")
    assert resp.status_code == 200, resp.text
    rows = {r["video_id"]: r for r in resp.json()["items"]}
    assert VIDEO_MAIN in rows
    assert rows[VIDEO_MAIN]["comment_count"] >= 3
    assert rows[VIDEO_MAIN]["sync_status"] in (
        "never_synced", "not_connected", "synced", "error",
    )
    assert rows[VIDEO_MAIN]["analysis_status"] in (
        "analysed", "not_analysed",
    )
    # B's index never contains A's comment-derived rows beyond B's own.
    rows_b = {r["video_id"] for r in
              client.get(f"/api/v1/channels/{CHAN_B}/comments/videos")
              .json()["items"]}
    assert rows_b <= {VIDEO_MAIN}  # B has its own comment on VIDEO_MAIN

    # Rollups overview serves persisted rows only.
    roll = client.get(f"/api/v1/channels/{CHAN_A}/comments/rollups")
    assert roll.status_code == 200
    payload = roll.json()
    assert payload["status"] in ("analysed", "not_analysed")
    if payload["status"] == "analysed":
        video_ids = {v["video_id"] for v in payload["videos"]}
        assert VIDEO_MAIN in video_ids

    # Channel-level analysis trigger with an explicit video id.
    one = client.post(
        f"/api/v1/channels/{CHAN_A}/comments/analysis",
        json={"video_id": VIDEO_MAIN},
    )
    assert one.status_code == 200, one.text
    assert one.json()["rollup"]["scope"] == "video"

    # ... and without one (whole channel).
    all_ = client.post(
        f"/api/v1/channels/{CHAN_A}/comments/analysis", json={}
    )
    assert all_.status_code == 200
    assert all_.json()["rollup"]["scope"] == "channel"

    after = client.get(f"/api/v1/channels/{CHAN_A}/comments/rollups")
    assert after.json()["status"] == "analysed"
    assert after.json()["channel"]["scope"] == "channel"


def test_channel_level_sync_all_and_single(client, monkeypatch):
    from app.providers.youtube.comments import CommentFetchResult

    _patch_fetch(monkeypatch, lambda vid: CommentFetchResult(
        status="not_connected", video_id=vid,
        reason="no_youtube_credential",
    ))
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/comments/sync", json={}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "not_connected"
    assert body["video_count"] >= 1
    assert all(r["status"] == "not_connected" for r in body["results"])

    single = client.post(
        f"/api/v1/channels/{CHAN_A}/comments/sync",
        json={"video_id": VIDEO_MAIN},
    )
    assert single.status_code == 200
    assert single.json()["status"] == "not_connected"
    assert single.json()["video_id"] == VIDEO_MAIN


# ---------------------------------------------------------------------------
# Cross-channel isolation — everything 404s, nothing leaks
# ---------------------------------------------------------------------------


def test_cross_channel_isolation_404s(client):
    draft = _generate_draft(client)
    flag_id = _seed_flag()

    checks = [
        client.get(f"/api/v1/channels/{CHAN_B}/drafts/{draft['id']}"),
        client.post(
            f"/api/v1/channels/{CHAN_B}/drafts/{draft['id']}/approve",
            json={},
        ),
        client.post(
            f"/api/v1/channels/{CHAN_B}/comments/{C_QUESTION}/drafts"
        ),
        client.post(
            f"/api/v1/channels/{CHAN_B}/moderation/{flag_id}/decision",
            json={"decision": "dismiss"},
        ),
    ]
    for resp in checks:
        assert resp.status_code == 404, resp.text

    # B's own views stay clean of A's data.
    drafts_b = client.get(f"/api/v1/channels/{CHAN_B}/drafts")
    assert all(d["id"] != draft["id"] for d in drafts_b.json()["items"])
    queue_b = client.get(
        f"/api/v1/channels/{CHAN_B}/moderation", params={"status": "pending"}
    )
    assert all(i["id"] != flag_id for i in queue_b.json()["items"])
    analysis_b = client.get(
        f"/api/v1/channels/{CHAN_B}/videos/{VIDEO_MAIN}/analysis"
    )
    assert analysis_b.json()["status"] == "not_analysed"

    # A's rows are untouched by B's attempts.
    got = client.get(f"/api/v1/channels/{CHAN_A}/drafts/{draft['id']}")
    assert got.json()["status"] == "proposed"
