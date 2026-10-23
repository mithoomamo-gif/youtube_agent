"""Style Lab metadata fetch tests — ALL HTTP mocked, zero live network.

Covers ``app.style_lab.metadata.fetch_channel_metadata``:

* no usable credential -> ``metadata_unavailable`` and ZERO HTTP calls
  (the client factory is never even invoked);
* credential + channels.list 200 -> parsed fields, ``status="fetched"``;
* handle-only lookup uses ``forHandle`` (with the ``@`` the API wants);
* a video URL/id -> videos.list mapping incl. tags, and a video alone
  resolves its channel from the video's own ``snippet.channelId``;
* 403 quota -> ``fetch_failed`` with reason ``quota_exceeded``;
* malformed / non-JSON responses -> ``fetch_failed``, no exception leak;
* transport failure -> ``fetch_failed`` with reason ``network_error``;
* OAuth bearer path (no API key, connected-channel token manager);
* the sync facade.

The client factory (``create_async_client``) is patched in the module
under test, exactly the seam production code uses, and backed by
``httpx.MockTransport``.  The fake API key below is a test fixture, not
a real credential.
"""

from __future__ import annotations

import asyncio
import urllib.parse

import httpx
import pytest

from app.style_lab import metadata
from app.style_lab.metadata import fetch_channel_metadata, fetch_channel_metadata_sync

FAKE_API_KEY = "test-youtube-api-key-0000000000"
CHANNEL_ID = "UC1234567890123456789012"  # UC + 22 chars
VIDEO_ID = "vid12345678"  # 11 chars

CHANNEL_PAYLOAD = {
    "kind": "youtube#channelListResponse",
    "items": [
        {
            "id": CHANNEL_ID,
            "snippet": {
                "title": "Test Channel",
                "description": "A channel about tests.",
                "customUrl": "@TestHandle",
            },
            "statistics": {
                "subscriberCount": "1234",
                "viewCount": "987654",
                "videoCount": "42",
                "hiddenSubscriberCount": False,
            },
        }
    ],
}

VIDEO_PAYLOAD = {
    "kind": "youtube#videoListResponse",
    "items": [
        {
            "id": VIDEO_ID,
            "snippet": {
                "title": "A Very Testable Video",
                "description": "Video description here.",
                "channelId": CHANNEL_ID,
                "channelTitle": "Test Channel",
                "tags": ["tests", "metadata", "style-lab"],
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
    """Replacement for ``metadata.create_async_client`` backed by MockTransport."""

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


def ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/channels"):
        return httpx.Response(200, json=CHANNEL_PAYLOAD)
    if request.url.path.endswith("/videos"):
        return httpx.Response(200, json=VIDEO_PAYLOAD)
    return httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})


@pytest.fixture()
def factory(monkeypatch):
    rec = RecordingFactory(ok_handler)
    monkeypatch.setattr(metadata, "create_async_client", rec)
    return rec


def install_handler(monkeypatch, handler) -> RecordingFactory:
    rec = RecordingFactory(handler)
    monkeypatch.setattr(metadata, "create_async_client", rec)
    return rec


def with_key() -> DictSecretProvider:
    return DictSecretProvider({metadata.API_KEY_REF: FAKE_API_KEY})


# ---------------------------------------------------------------------------
# Credential gate
# ---------------------------------------------------------------------------
def test_no_credential_metadata_unavailable_and_no_http(factory):
    result = run(
        fetch_channel_metadata(
            channel_id=CHANNEL_ID,
            secret_provider=DictSecretProvider({}),
        )
    )
    assert result.status == "metadata_unavailable"
    assert result.reason == "no_youtube_credential"
    assert result.external_channel_id == CHANNEL_ID
    # The key assertion: the client factory was never invoked, so no
    # HTTP request could possibly have been made.
    assert factory.factory_calls == []
    assert factory.requests == []


def test_credential_store_failure_is_unavailable_and_no_http(factory):
    result = run(
        fetch_channel_metadata(
            handle="TestHandle",
            secret_provider=DictSecretProvider(raises=True),
        )
    )
    assert result.status == "metadata_unavailable"
    assert result.reason == "no_youtube_credential"
    assert factory.factory_calls == []
    assert factory.requests == []


def test_api_key_ref_consulted(factory):
    provider = with_key()
    run(fetch_channel_metadata(channel_id=CHANNEL_ID, secret_provider=provider))
    assert provider.refs == ["youtube/api_key"]


# ---------------------------------------------------------------------------
# Channel fetch
# ---------------------------------------------------------------------------
def test_channel_by_id_fetched(factory):
    result = run(
        fetch_channel_metadata(channel_id=CHANNEL_ID, secret_provider=with_key())
    )
    assert result.status == "fetched"
    assert result.reason is None
    assert result.external_channel_id == CHANNEL_ID
    assert result.display_name == "Test Channel"
    # Handle normalised to the codebase convention: no leading '@'.
    assert result.handle == "TestHandle"
    assert result.channel_url == f"https://www.youtube.com/channel/{CHANNEL_ID}"
    assert result.subscriber_count == 1234
    assert result.view_count == 987654
    assert result.video_count == 42
    # No video requested -> video fields stay empty.
    assert result.title is None
    assert result.description is None
    assert result.tags == []
    assert result.raw_json is not None
    assert result.raw_json["channels"] == CHANNEL_PAYLOAD

    assert len(factory.requests) == 1
    params = factory.params_of(0)
    assert params["part"] == "snippet,statistics"
    assert params["id"] == CHANNEL_ID
    assert "forHandle" not in params
    assert params["key"] == FAKE_API_KEY


def test_handle_only_lookup_uses_for_handle(factory):
    result = run(
        fetch_channel_metadata(handle="TestHandle", secret_provider=with_key())
    )
    assert result.status == "fetched"
    assert result.external_channel_id == CHANNEL_ID
    params = factory.params_of(0)
    assert params["forHandle"] == "@TestHandle"
    assert "id" not in params


def test_channel_url_handle_form_uses_for_handle(factory):
    result = run(
        fetch_channel_metadata(
            channel_url="https://www.youtube.com/@TestHandle",
            secret_provider=with_key(),
        )
    )
    assert result.status == "fetched"
    assert factory.params_of(0)["forHandle"] == "@TestHandle"


def test_channel_not_found(factory, monkeypatch):
    rec = install_handler(
        monkeypatch,
        lambda request: httpx.Response(200, json={"items": []}),
    )
    result = run(
        fetch_channel_metadata(channel_id=CHANNEL_ID, secret_provider=with_key())
    )
    assert result.status == "fetch_failed"
    assert result.reason == "channel_not_found"
    assert len(rec.requests) == 1


# ---------------------------------------------------------------------------
# Video fetch
# ---------------------------------------------------------------------------
def test_video_url_fetches_video_metadata(factory):
    result = run(
        fetch_channel_metadata(
            channel_id=CHANNEL_ID,
            video_url=f"https://www.youtube.com/watch?v={VIDEO_ID}",
            secret_provider=with_key(),
        )
    )
    assert result.status == "fetched"
    assert result.video_id == VIDEO_ID
    assert result.title == "A Very Testable Video"
    assert result.description == "Video description here."
    assert result.tags == ["tests", "metadata", "style-lab"]
    assert result.raw_json is not None
    assert result.raw_json["videos"] == VIDEO_PAYLOAD

    assert len(factory.requests) == 2
    video_params = factory.params_of(1)
    assert factory.requests[1].url.path.endswith("/videos")
    assert video_params["part"] == "snippet"
    assert video_params["id"] == VIDEO_ID


def test_video_alone_resolves_channel_from_video(factory):
    result = run(
        fetch_channel_metadata(
            video_url=f"https://youtu.be/{VIDEO_ID}",
            secret_provider=with_key(),
        )
    )
    assert result.status == "fetched"
    assert result.external_channel_id == CHANNEL_ID
    assert result.display_name == "Test Channel"
    assert result.title == "A Very Testable Video"
    # Video fetched first, then the channel it belongs to.
    assert factory.requests[0].url.path.endswith("/videos")
    assert factory.requests[1].url.path.endswith("/channels")
    assert factory.params_of(1)["id"] == CHANNEL_ID


# ---------------------------------------------------------------------------
# Failure mapping — never an exception leak, single attempt
# ---------------------------------------------------------------------------
def test_quota_403_fetch_failed(monkeypatch):
    quota_body = {
        "error": {
            "code": 403,
            "errors": [{"reason": "quotaExceeded", "domain": "youtube.quota"}],
        }
    }
    rec = install_handler(
        monkeypatch, lambda request: httpx.Response(403, json=quota_body)
    )
    result = run(
        fetch_channel_metadata(channel_id=CHANNEL_ID, secret_provider=with_key())
    )
    assert result.status == "fetch_failed"
    assert result.reason == "quota_exceeded"
    assert len(rec.requests) == 1  # single attempt, no retry-storm


def test_auth_401_fetch_failed(monkeypatch):
    body = {"error": {"code": 401, "errors": [{"reason": "authError"}]}}
    install_handler(monkeypatch, lambda request: httpx.Response(401, json=body))
    result = run(
        fetch_channel_metadata(channel_id=CHANNEL_ID, secret_provider=with_key())
    )
    assert result.status == "fetch_failed"
    assert result.reason == "authentication_failed"


def test_malformed_response_fetch_failed_no_exception(monkeypatch):
    # 200 but the item has no snippet at all.
    install_handler(
        monkeypatch,
        lambda request: httpx.Response(200, json={"items": [{"id": CHANNEL_ID}]}),
    )
    result = run(
        fetch_channel_metadata(channel_id=CHANNEL_ID, secret_provider=with_key())
    )
    assert result.status == "fetch_failed"
    assert result.reason == "malformed_response"


def test_non_json_response_fetch_failed_no_exception(monkeypatch):
    install_handler(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=b"<html>not json</html>", headers={"content-type": "text/html"}
        ),
    )
    result = run(
        fetch_channel_metadata(channel_id=CHANNEL_ID, secret_provider=with_key())
    )
    assert result.status == "fetch_failed"
    assert result.reason == "invalid_response_json"


def test_network_error_fetch_failed_no_exception(monkeypatch):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused (fixture)", request=request)

    rec = install_handler(monkeypatch, boom)
    result = run(
        fetch_channel_metadata(channel_id=CHANNEL_ID, secret_provider=with_key())
    )
    assert result.status == "fetch_failed"
    assert result.reason == "network_error"
    assert len(rec.requests) == 1


def test_missing_identifier_fetch_failed_after_gate(factory):
    result = run(fetch_channel_metadata(secret_provider=with_key()))
    assert result.status == "fetch_failed"
    assert result.reason == "missing_channel_identifier"
    assert factory.factory_calls == []  # gate/fetch logic made no HTTP call


# ---------------------------------------------------------------------------
# OAuth credential path
# ---------------------------------------------------------------------------
def test_oauth_token_used_when_no_api_key(factory):
    manager = FakeTokenManager()
    result = run(
        fetch_channel_metadata(
            channel_id=CHANNEL_ID,
            secret_provider=DictSecretProvider({}),
            token_manager=manager,
            oauth_channel_id="UComychannel000000000001",
        )
    )
    assert result.status == "fetched"
    assert manager.calls == ["UComychannel000000000001"]
    request = factory.requests[0]
    assert request.headers.get("Authorization") == "Bearer fake-access-token-1234567890"
    assert "key" not in factory.params_of(0)


def test_oauth_token_unavailable_is_metadata_unavailable(factory):
    manager = FakeTokenManager(token=None)
    result = run(
        fetch_channel_metadata(
            channel_id=CHANNEL_ID,
            secret_provider=DictSecretProvider({}),
            token_manager=manager,
            oauth_channel_id="UComychannel000000000001",
        )
    )
    assert result.status == "metadata_unavailable"
    assert result.reason == "oauth_token_unavailable"
    assert factory.factory_calls == []
    assert factory.requests == []


# ---------------------------------------------------------------------------
# Sync facade
# ---------------------------------------------------------------------------
def test_sync_wrapper(factory):
    result = fetch_channel_metadata_sync(
        channel_id=CHANNEL_ID, secret_provider=with_key()
    )
    assert result.status == "fetched"
    assert result.display_name == "Test Channel"
