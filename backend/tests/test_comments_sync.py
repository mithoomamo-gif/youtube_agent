"""Comment sync service tests — mocked HTTP, real SQLite store.

Covers ``app.comments.service.sync_video_comments`` against the real
:class:`app.comments.store.CommentsStore` (in-memory SQLite with the
full app schema, so the channels FK holds):

* successful sync upserts every fetched comment (top-level + replies)
  and marks ``comment_sync_state`` 'synced' with the live count;
* idempotent re-sync: the same SYNTHETIC fixture page twice yields the
  same rows (no duplicates), second sync inserts 0 / updates all, and
  a changed fixture refreshes like/reply counts in place;
* credential-absent -> result + stored state 'not_connected' with the
  gate reason, ZERO HTTP calls, zero comment rows;
* commentsDisabled -> stored state 'error' + error_reason
  'comments_disabled', no rows written;
* videoNotFound -> stored state 'error' + 'video_not_found';
* the sync facade.

ALL payloads and credentials are SYNTHETIC test fixtures.
"""

from __future__ import annotations

import asyncio
import sqlite3
import urllib.parse
from datetime import datetime, timezone

import httpx
import pytest

from app.comments.service import sync_video_comments, sync_video_comments_sync
from app.comments.store import CommentsStore
from app.providers.youtube import comments as provider
from app.schema_ddl import SCHEMA_DDL

FAKE_API_KEY = "test-youtube-api-key-0000000000"  # SYNTHETIC fixture
CHANNEL_ID = "chan-internal-1"
YOUTUBE_CHANNEL_ID = "UCsynthetic000000000001"
VIDEO_ID = "vid12345678"  # SYNTHETIC

# SYNTHETIC single page: 2 top-level (one with 1 inline reply).
PAGE_V1 = {
    "kind": "youtube#commentThreadListResponse",
    "items": [
        {
            "id": "thread-1",
            "snippet": {
                "videoId": VIDEO_ID,
                "totalReplyCount": 1,
                "topLevelComment": {
                    "id": "cmt-1",
                    "snippet": {
                        "authorDisplayName": "Synthetic Viewer",
                        "authorChannelId": {"value": "UCsynthetic00000000002"},
                        "textOriginal": "Great video",
                        "likeCount": 10,
                        "publishedAt": "2026-01-01T10:00:00Z",
                    },
                },
            },
            "replies": {
                "comments": [
                    {
                        "id": "cmt-1-reply-1",
                        "snippet": {
                            "parentId": "cmt-1",
                            "authorDisplayName": "Synthetic Replier",
                            "textOriginal": "Agreed",
                            "likeCount": 2,
                            "publishedAt": "2026-01-01T11:00:00Z",
                        },
                    }
                ]
            },
        },
        {
            "id": "thread-2",
            "snippet": {
                "videoId": VIDEO_ID,
                "totalReplyCount": 0,
                "topLevelComment": {
                    "id": "cmt-2",
                    "snippet": {
                        "authorDisplayName": "Second Viewer",
                        "textOriginal": "Nice",
                        "likeCount": 3,
                        "publishedAt": "2026-01-02T10:00:00Z",
                    },
                },
            },
        },
    ],
}

# SYNTHETIC v2: same comments, changed counters + one NEW comment.
PAGE_V2 = {
    "kind": "youtube#commentThreadListResponse",
    "items": [
        {
            "id": "thread-1",
            "snippet": {
                "videoId": VIDEO_ID,
                "totalReplyCount": 5,
                "topLevelComment": {
                    "id": "cmt-1",
                    "snippet": {
                        "authorDisplayName": "Synthetic Viewer",
                        "authorChannelId": {"value": "UCsynthetic00000000002"},
                        "textOriginal": "Great video",
                        "likeCount": 99,
                        "publishedAt": "2026-01-01T10:00:00Z",
                    },
                },
            },
            "replies": PAGE_V1["items"][0]["replies"],
        },
        PAGE_V1["items"][1],
        {
            "id": "thread-3",
            "snippet": {
                "videoId": VIDEO_ID,
                "totalReplyCount": 0,
                "topLevelComment": {
                    "id": "cmt-3",
                    "snippet": {
                        "authorDisplayName": "Late Viewer",
                        "textOriginal": "Just arrived",
                        "likeCount": 0,
                        "publishedAt": "2026-01-03T10:00:00Z",
                    },
                },
            },
        },
    ],
}


def run(coro):
    return asyncio.run(coro)


class DictSecretProvider:
    def __init__(self, values: dict | None = None):
        self._values = values or {}

    async def get_secret(self, ref: str):
        return self._values.get(ref)


def with_key() -> DictSecretProvider:
    return DictSecretProvider({provider.API_KEY_REF: FAKE_API_KEY})


class RecordingFactory:
    """Replacement for ``comments.create_async_client`` (MockTransport)."""

    def __init__(self, handler):
        self._handler = handler
        self.factory_calls: list[dict] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, **kwargs):
        self.factory_calls.append(kwargs)

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._handler(request)

        transport = kwargs.get("transport") or httpx.MockTransport(handler)
        return httpx.AsyncClient(transport=transport)


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA_DDL)
    now = 1_700_000_000_000
    connection.execute(
        "INSERT INTO channels (id, youtube_channel_id, title, language,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (CHANNEL_ID, YOUTUBE_CHANNEL_ID, "Synthetic Channel", "en-US", now, now),
    )
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture()
def store(conn):
    return CommentsStore(conn)


def install(monkeypatch, handler) -> RecordingFactory:
    rec = RecordingFactory(handler)
    monkeypatch.setattr(provider, "create_async_client", rec)
    return rec


def page_handler(payload):
    return lambda request: httpx.Response(200, json=payload)


# ---------------------------------------------------------------------------
# Successful sync
# ---------------------------------------------------------------------------
def test_sync_success_upserts_and_marks_synced(store, monkeypatch):
    install(monkeypatch, page_handler(PAGE_V1))
    result = run(
        sync_video_comments(
            CHANNEL_ID, VIDEO_ID, store=store, secret_provider=with_key()
        )
    )
    assert result.status == "fetched"
    assert result.fetched_count == 3
    assert result.inserted_count == 3
    assert result.updated_count == 0
    assert result.total_stored == 3
    assert result.sync_state_status == "synced"

    assert store.count_comments(CHANNEL_ID, VIDEO_ID) == 3
    top = store.get_comment("cmt-1", channel_id=CHANNEL_ID)
    assert top is not None
    assert top["author_display_name"] == "Synthetic Viewer"
    assert top["text"] == "Great video"
    assert top["like_count"] == 10
    assert top["reply_count"] == 1
    assert top["is_top_level"] is True
    expected_ms = int(
        datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc).timestamp() * 1000
    )
    assert top["published_at"] == expected_ms

    replies = store.list_replies(CHANNEL_ID, "cmt-1")
    assert [r["id"] for r in replies] == ["cmt-1-reply-1"]
    assert replies[0]["is_top_level"] is False

    state = store.get_sync_state(CHANNEL_ID, VIDEO_ID)
    assert state["status"] == "synced"
    assert state["comment_count"] == 3
    assert state["error_reason"] is None
    assert state["last_synced_at"] is not None


def test_sync_is_idempotent_and_refreshes_counts(store, monkeypatch):
    install(monkeypatch, page_handler(PAGE_V1))
    first = run(
        sync_video_comments(
            CHANNEL_ID, VIDEO_ID, store=store, secret_provider=with_key()
        )
    )
    assert first.inserted_count == 3
    rows_after_first = store.list_comments(CHANNEL_ID, VIDEO_ID)
    assert len(rows_after_first) == 3

    # Same fixture again: same rows, nothing inserted, all refreshed.
    second = run(
        sync_video_comments(
            CHANNEL_ID, VIDEO_ID, store=store, secret_provider=with_key()
        )
    )
    assert second.inserted_count == 0
    assert second.updated_count == 3
    assert second.total_stored == 3
    assert store.count_comments(CHANNEL_ID, VIDEO_ID) == 3
    assert {r["id"] for r in store.list_comments(CHANNEL_ID, VIDEO_ID)} == {
        r["id"] for r in rows_after_first
    }

    # Changed fixture: counters update in place, only the new one inserts.
    install(monkeypatch, page_handler(PAGE_V2))
    third = run(
        sync_video_comments(
            CHANNEL_ID, VIDEO_ID, store=store, secret_provider=with_key()
        )
    )
    assert third.inserted_count == 1
    assert third.updated_count == 3
    assert third.total_stored == 4
    top = store.get_comment("cmt-1", channel_id=CHANNEL_ID)
    assert top is not None
    assert top["like_count"] == 99
    assert top["reply_count"] == 5
    state = store.get_sync_state(CHANNEL_ID, VIDEO_ID)
    assert state["status"] == "synced"
    assert state["comment_count"] == 4


# ---------------------------------------------------------------------------
# Typed failure states — no rows, no fabrication
# ---------------------------------------------------------------------------
def test_sync_not_connected_zero_http_and_state(store, monkeypatch):
    factory = install(monkeypatch, page_handler(PAGE_V1))
    result = run(
        sync_video_comments(
            CHANNEL_ID, VIDEO_ID, store=store,
            secret_provider=DictSecretProvider({}),
        )
    )
    assert result.status == "not_connected"
    assert result.reason == "no_youtube_credential"
    assert result.sync_state_status == "not_connected"
    assert result.fetched_count == 0
    # Zero HTTP: the shared factory was never invoked.
    assert factory.factory_calls == []
    assert factory.requests == []
    assert store.count_comments(CHANNEL_ID, VIDEO_ID) == 0
    state = store.get_sync_state(CHANNEL_ID, VIDEO_ID)
    assert state["status"] == "not_connected"
    assert state["error_reason"] == "no_youtube_credential"
    assert state["comment_count"] == 0
    assert state["last_synced_at"] is None


def test_sync_comments_disabled_error_state(store, monkeypatch):
    body = {"error": {"code": 403, "errors": [{"reason": "commentsDisabled"}]}}
    install(monkeypatch, lambda request: httpx.Response(403, json=body))
    result = run(
        sync_video_comments(
            CHANNEL_ID, VIDEO_ID, store=store, secret_provider=with_key()
        )
    )
    assert result.status == "comments_disabled"
    assert result.sync_state_status == "error"
    assert store.count_comments(CHANNEL_ID, VIDEO_ID) == 0
    state = store.get_sync_state(CHANNEL_ID, VIDEO_ID)
    assert state["status"] == "error"
    assert state["error_reason"] == "comments_disabled"
    assert state["last_synced_at"] is None


def test_sync_video_not_found_error_state(store, monkeypatch):
    body = {"error": {"code": 404, "errors": [{"reason": "videoNotFound"}]}}
    install(monkeypatch, lambda request: httpx.Response(404, json=body))
    result = run(
        sync_video_comments(
            CHANNEL_ID, VIDEO_ID, store=store, secret_provider=with_key()
        )
    )
    assert result.status == "video_not_found"
    assert result.sync_state_status == "error"
    state = store.get_sync_state(CHANNEL_ID, VIDEO_ID)
    assert state["status"] == "error"
    assert state["error_reason"] == "video_not_found"


def test_sync_failure_after_success_keeps_stored_rows(store, monkeypatch):
    install(monkeypatch, page_handler(PAGE_V1))
    run(
        sync_video_comments(
            CHANNEL_ID, VIDEO_ID, store=store, secret_provider=with_key()
        )
    )
    body = {"error": {"code": 403, "errors": [{"reason": "commentsDisabled"}]}}
    install(monkeypatch, lambda request: httpx.Response(403, json=body))
    result = run(
        sync_video_comments(
            CHANNEL_ID, VIDEO_ID, store=store, secret_provider=with_key()
        )
    )
    assert result.status == "comments_disabled"
    # Previously synced data is untouched; count in state is preserved.
    assert store.count_comments(CHANNEL_ID, VIDEO_ID) == 3
    assert result.total_stored == 3
    state = store.get_sync_state(CHANNEL_ID, VIDEO_ID)
    assert state["status"] == "error"
    assert state["comment_count"] == 3
    assert state["last_synced_at"] is not None  # from the earlier success


# ---------------------------------------------------------------------------
# Sync facade
# ---------------------------------------------------------------------------
def test_sync_facade(store, monkeypatch):
    install(monkeypatch, page_handler(PAGE_V1))
    result = sync_video_comments_sync(
        CHANNEL_ID, VIDEO_ID, store=store, secret_provider=with_key()
    )
    assert result.status == "fetched"
    assert result.total_stored == 3
