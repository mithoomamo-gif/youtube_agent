"""Comment fetch provider tests — ALL HTTP mocked, zero live network.

Covers ``app.providers.youtube.comments.fetch_video_comments``:

* no usable credential -> ``not_connected`` and ZERO HTTP calls (the
  shared client factory is never even invoked);
* full pagination mapping (top-level + first-level replies) into the
  storage contract;
* read-only by construction: every request is a GET to
  ``/commentThreads`` — no mutating endpoint exists in the module;
* error mapping: commentsDisabled / videoNotFound / quota / auth /
  network / malformed -> typed statuses, comments always empty on
  failure (all-or-nothing, never fabricated);
* quota seam: a supplied ledger records 1 shared-bucket unit per page
  and a pre-flight ceiling refusal makes ZERO HTTP calls;
* the OAuth bearer path and the sync facade.

The client factory (``create_async_client``) is patched in the module
under test, exactly the seam production code uses, backed by
``httpx.MockTransport``.  ALL payloads and credentials below are
SYNTHETIC test fixtures, not real data.
"""

from __future__ import annotations

import asyncio
import urllib.parse

import httpx
import pytest

from app.providers.youtube import comments as provider
from app.providers.youtube.comments import (
    fetch_video_comments,
    fetch_video_comments_sync,
)
from app.providers.youtube.quota import (
    BUCKET_SHARED,
    InMemoryQuotaLedger,
    LedgerEntry,
)

FAKE_API_KEY = "test-youtube-api-key-0000000000"  # SYNTHETIC fixture
VIDEO_ID = "vid12345678"  # SYNTHETIC 11-char video id

# SYNTHETIC page 1: one thread with 2 inline replies (totalReplyCount=3:
# one reply exists beyond the inline window), one bare thread.
PAGE_1 = {
    "kind": "youtube#commentThreadListResponse",
    "nextPageToken": "PAGE2TOKEN",
    "pageInfo": {"totalResults": 2, "resultsPerPage": 100},
    "items": [
        {
            "id": "thread-1",
            "snippet": {
                "videoId": VIDEO_ID,
                "totalReplyCount": 3,
                "canReply": True,
                "isPublic": True,
                "topLevelComment": {
                    "id": "cmt-top-1",
                    "snippet": {
                        "videoId": VIDEO_ID,
                        "authorDisplayName": "Synthetic Viewer One",
                        "authorChannelId": {"value": "UCsynthetic00000000001"},
                        "textOriginal": "Original text wins",
                        "textDisplay": "Original text wins",
                        "likeCount": 42,
                        "publishedAt": "2026-01-01T10:00:00Z",
                        "updatedAt": "2026-01-01T10:00:00Z",
                    },
                },
            },
            "replies": {
                "comments": [
                    {
                        "id": "cmt-reply-1",
                        "snippet": {
                            "videoId": VIDEO_ID,
                            "parentId": "cmt-top-1",
                            "authorDisplayName": "Synthetic Replier",
                            "authorChannelId": {"value": "UCsynthetic00000000002"},
                            "textDisplay": "Reply display only",
                            "likeCount": 5,
                            "publishedAt": "2026-01-01T11:00:00Z",
                        },
                    },
                    {
                        "id": "cmt-reply-2",
                        "snippet": {
                            "videoId": VIDEO_ID,
                            "parentId": "cmt-top-1",
                            "authorDisplayName": "Second Replier",
                            "textOriginal": "Second reply",
                            "likeCount": 0,
                            "publishedAt": "2026-01-01T12:00:00Z",
                        },
                    },
                ]
            },
        },
        {
            "id": "thread-2",
            "snippet": {
                "videoId": VIDEO_ID,
                "totalReplyCount": 0,
                "topLevelComment": {
                    "id": "cmt-top-2",
                    "snippet": {
                        "videoId": VIDEO_ID,
                        "authorDisplayName": "Quiet Viewer",
                        "textOriginal": "No replies here",
                        "likeCount": 1,
                        "publishedAt": "2026-01-02T09:30:00Z",
                    },
                },
            },
        },
    ],
}

# SYNTHETIC page 2: last page (no nextPageToken).
PAGE_2 = {
    "kind": "youtube#commentThreadListResponse",
    "pageInfo": {"totalResults": 1, "resultsPerPage": 100},
    "items": [
        {
            "id": "thread-3",
            "snippet": {
                "videoId": VIDEO_ID,
                "totalReplyCount": 0,
                "topLevelComment": {
                    "id": "cmt-top-3",
                    "snippet": {
                        "videoId": VIDEO_ID,
                        "authorDisplayName": "Last Page Viewer",
                        "textOriginal": "Final comment",
                        "likeCount": 7,
                        "publishedAt": "2026-01-03T08:00:00Z",
                    },
                },
            },
        }
    ],
}


def run(coro):
    return asyncio.run(coro)


class DictSecretProvider:
    """Async ``get_secret(ref)`` provider over a plain dict."""

    def __init__(self, values: dict | None = None, *, raises: bool = False):
        self._values = values or {}
        self._raises = raises
        self.refs: list[str] = []

    async def get_secret(self, ref: str):
        self.refs.append(ref)
        if self._raises:
            raise RuntimeError("credential store unavailable (fixture)")
        return self._values.get(ref)


class FakeTokenManager:
    def __init__(self, token: str | None = "fake-access-token-1234567890"):
        self._token = token
        self.calls: list[str] = []

    async def get_access_token(self, channel_id: str):
        self.calls.append(channel_id)
        if self._token is None:
            raise RuntimeError("not connected (fixture)")
        return self._token


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

    def params_of(self, index: int = 0) -> dict:
        return dict(urllib.parse.parse_qsl(self.requests[index].url.query.decode()))


def paged_handler(request: httpx.Request) -> httpx.Response:
    params = dict(urllib.parse.parse_qsl(request.url.query.decode()))
    if params.get("pageToken") == "PAGE2TOKEN":
        return httpx.Response(200, json=PAGE_2)
    return httpx.Response(200, json=PAGE_1)


@pytest.fixture()
def factory(monkeypatch):
    rec = RecordingFactory(paged_handler)
    monkeypatch.setattr(provider, "create_async_client", rec)
    return rec


def install_handler(monkeypatch, handler) -> RecordingFactory:
    rec = RecordingFactory(handler)
    monkeypatch.setattr(provider, "create_async_client", rec)
    return rec


def with_key() -> DictSecretProvider:
    return DictSecretProvider({provider.API_KEY_REF: FAKE_API_KEY})


# ---------------------------------------------------------------------------
# Credential gate — not_connected + ZERO HTTP
# ---------------------------------------------------------------------------
def test_no_credential_not_connected_and_no_http(factory):
    result = run(
        fetch_video_comments(VIDEO_ID, secret_provider=DictSecretProvider({}))
    )
    assert result.status == "not_connected"
    assert result.reason == "no_youtube_credential"
    assert result.comments == []
    # The key assertion: the client factory was never invoked, so no
    # HTTP request could possibly have been made.
    assert factory.factory_calls == []
    assert factory.requests == []


def test_credential_store_failure_is_not_connected_and_no_http(factory):
    result = run(
        fetch_video_comments(
            VIDEO_ID, secret_provider=DictSecretProvider(raises=True)
        )
    )
    assert result.status == "not_connected"
    assert result.reason == "no_youtube_credential"
    assert factory.factory_calls == []
    assert factory.requests == []


def test_oauth_token_unavailable_is_not_connected_and_no_http(factory):
    manager = FakeTokenManager(token=None)
    result = run(
        fetch_video_comments(
            VIDEO_ID,
            secret_provider=DictSecretProvider({}),
            token_manager=manager,
            oauth_channel_id="UCsynthetic000000000009",
        )
    )
    assert result.status == "not_connected"
    assert result.reason == "oauth_token_unavailable"
    assert factory.factory_calls == []
    assert factory.requests == []


def test_api_key_ref_consulted(factory):
    prov = with_key()
    run(fetch_video_comments(VIDEO_ID, secret_provider=prov))
    assert prov.refs == ["youtube/api_key"]


# ---------------------------------------------------------------------------
# Pagination + mapping
# ---------------------------------------------------------------------------
def test_full_pagination_mapping(factory):
    result = run(fetch_video_comments(VIDEO_ID, secret_provider=with_key()))
    assert result.status == "fetched"
    assert result.reason is None
    assert result.pages_fetched == 2
    assert len(factory.requests) == 2

    # Request shape: commentThreads.list, part=snippet,replies, paging.
    params_1 = factory.params_of(0)
    assert params_1["part"] == "snippet,replies"
    assert params_1["videoId"] == VIDEO_ID
    assert params_1["maxResults"] == "100"
    assert params_1["key"] == FAKE_API_KEY
    assert "pageToken" not in params_1
    assert factory.params_of(1)["pageToken"] == "PAGE2TOKEN"

    by_id = {c.id: c for c in result.comments}
    assert set(by_id) == {
        "cmt-top-1", "cmt-reply-1", "cmt-reply-2", "cmt-top-2", "cmt-top-3",
    }

    top = by_id["cmt-top-1"]
    assert top.video_id == VIDEO_ID
    assert top.author_display_name == "Synthetic Viewer One"
    assert top.author_channel_id == "UCsynthetic00000000001"
    assert top.text == "Original text wins"  # textOriginal preferred
    assert top.like_count == 42
    assert top.reply_count == 3  # totalReplyCount, incl. non-inline reply
    assert top.parent_comment_id is None
    assert top.is_top_level is True
    assert top.published_at == "2026-01-01T10:00:00Z"

    reply = by_id["cmt-reply-1"]
    assert reply.parent_comment_id == "cmt-top-1"
    assert reply.is_top_level is False
    assert reply.text == "Reply display only"  # textDisplay fallback
    assert reply.like_count == 5
    assert reply.reply_count == 0

    assert by_id["cmt-reply-2"].author_channel_id is None
    assert by_id["cmt-top-3"].author_display_name == "Last Page Viewer"


def test_read_only_get_comment_threads_only(factory):
    run(fetch_video_comments(VIDEO_ID, secret_provider=with_key()))
    assert factory.requests, "expected requests"
    for request in factory.requests:
        assert request.method == "GET"
        assert request.url.path.endswith("/commentThreads")


def test_empty_comment_list_is_fetched(monkeypatch):
    install_handler(
        monkeypatch,
        lambda request: httpx.Response(200, json={"items": []}),
    )
    result = run(fetch_video_comments(VIDEO_ID, secret_provider=with_key()))
    assert result.status == "fetched"
    assert result.comments == []
    assert result.pages_fetched == 1


def test_oauth_bearer_used_when_no_api_key(factory):
    manager = FakeTokenManager()
    result = run(
        fetch_video_comments(
            VIDEO_ID,
            secret_provider=DictSecretProvider({}),
            token_manager=manager,
            oauth_channel_id="UCsynthetic000000000009",
        )
    )
    assert result.status == "fetched"
    assert manager.calls == ["UCsynthetic000000000009"]
    request = factory.requests[0]
    assert request.headers.get("Authorization") == (
        "Bearer fake-access-token-1234567890"
    )
    assert "key" not in factory.params_of(0)


# ---------------------------------------------------------------------------
# Error mapping — typed, single attempt per page, comments always empty
# ---------------------------------------------------------------------------
def _error_handler(status_code: int, reason: str):
    body = {"error": {"code": status_code, "errors": [{"reason": reason}]}}
    return lambda request: httpx.Response(status_code, json=body)


def test_comments_disabled_maps_to_typed_status(monkeypatch):
    rec = install_handler(monkeypatch, _error_handler(403, "commentsDisabled"))
    result = run(fetch_video_comments(VIDEO_ID, secret_provider=with_key()))
    assert result.status == "comments_disabled"
    assert result.reason == "comments_disabled"
    assert result.comments == []
    assert len(rec.requests) == 1


def test_video_not_found_maps_to_typed_status(monkeypatch):
    install_handler(monkeypatch, _error_handler(404, "videoNotFound"))
    result = run(fetch_video_comments(VIDEO_ID, secret_provider=with_key()))
    assert result.status == "video_not_found"
    assert result.reason == "video_not_found"
    assert result.comments == []


def test_quota_403_maps_to_quota_exceeded(monkeypatch):
    install_handler(monkeypatch, _error_handler(403, "quotaExceeded"))
    result = run(fetch_video_comments(VIDEO_ID, secret_provider=with_key()))
    assert result.status == "quota_exceeded"
    assert result.reason == "quota_exceeded"
    assert result.comments == []


def test_auth_401_maps_to_auth_failed(monkeypatch):
    install_handler(monkeypatch, _error_handler(401, "authError"))
    result = run(fetch_video_comments(VIDEO_ID, secret_provider=with_key()))
    assert result.status == "auth_failed"
    assert result.reason == "authentication_failed"
    assert result.comments == []


def test_network_error_no_exception_leak(monkeypatch):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused (fixture)", request=request)

    install_handler(monkeypatch, boom)
    result = run(fetch_video_comments(VIDEO_ID, secret_provider=with_key()))
    assert result.status == "fetch_failed"
    assert result.reason == "network_error"
    assert result.comments == []


def test_malformed_second_page_discards_partial_results(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(urllib.parse.parse_qsl(request.url.query.decode()))
        if params.get("pageToken") == "PAGE2TOKEN":
            return httpx.Response(200, json={"items": [{"noSnippet": True}]})
        return httpx.Response(200, json=PAGE_1)

    install_handler(monkeypatch, handler)
    result = run(fetch_video_comments(VIDEO_ID, secret_provider=with_key()))
    assert result.status == "fetch_failed"
    assert result.reason == "malformed_response"
    # All-or-nothing: page 1's comments are NOT carried on failure.
    assert result.comments == []
    assert result.pages_fetched == 1


# ---------------------------------------------------------------------------
# Quota seam
# ---------------------------------------------------------------------------
def test_quota_ledger_records_one_unit_per_page(factory):
    ledger = InMemoryQuotaLedger()
    result = run(
        fetch_video_comments(
            VIDEO_ID, secret_provider=with_key(), quota_ledger=ledger,
            quota_channel_id="chan-internal-1",
        )
    )
    assert result.status == "fetched"
    assert result.quota_units_used == 2  # two pages x 1 unit
    entries = ledger.entries
    assert len(entries) == 2
    for entry in entries:
        assert entry.bucket == BUCKET_SHARED
        assert entry.method == "commentThreads.list"
        assert entry.units == 1
        assert entry.success is True
        assert entry.channel_id == "chan-internal-1"


def test_quota_preflight_refusal_makes_no_http(factory):
    ledger = InMemoryQuotaLedger()
    # Fill the shared bucket right up to the 90% safety ceiling.
    ledger.record(
        LedgerEntry(bucket=BUCKET_SHARED, method="videos.list", units=9000)
    )
    result = run(
        fetch_video_comments(
            VIDEO_ID, secret_provider=with_key(), quota_ledger=ledger,
        )
    )
    assert result.status == "quota_exceeded"
    assert result.reason == "quota_exceeded"
    assert result.comments == []
    assert result.quota_units_used == 0
    assert factory.factory_calls == []
    assert factory.requests == []


def test_no_ledger_means_zero_units_claimed(factory):
    result = run(fetch_video_comments(VIDEO_ID, secret_provider=with_key()))
    assert result.status == "fetched"
    # Never faked: without a ledger nothing is recorded or claimed.
    assert result.quota_units_used == 0


# ---------------------------------------------------------------------------
# Sync facade
# ---------------------------------------------------------------------------
def test_sync_facade(factory):
    result = fetch_video_comments_sync(VIDEO_ID, secret_provider=with_key())
    assert result.status == "fetched"
    assert len(result.comments) == 5
