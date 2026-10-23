"""Tests for the YouTube Data API provider (roadmap Section 25).

All HTTP is mocked with httpx.MockTransport — no network is touched
except one loopback-listener test that talks to 127.0.0.1 only.

Covered contract points:
- PKCE pair generation (verifier length/charset, S256 challenge).
- Auth URL carries state, code_challenge, access_type=offline,
  prompt=consent.
- Token exchange sends the PKCE verifier.
- Single-flight refresh: two concurrent refreshes = one HTTP request.
- invalid_grant -> NeedsReauth + channel marked needs_reauth.
- Testing-mode 7-day token flag.
- Quota reservation refuses the call that would cross the 90% ceiling
  of the upload bucket (100/day) and of the shared bucket (10,000/day);
  refused calls make NO HTTP request.
- A failed API call still writes a ledger entry.
- 403 quotaExceeded -> QuotaExhausted; 401 -> one refresh + one retry.
- Upload resume honours the server Range header after a 308.
- Forced-private detection when the applied status differs from the
  requested one; insert always sends privacyStatus=private.
- verify_channel: exact-match success, mismatch raises, longUploadsStatus.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import urllib.parse

import httpx
import pytest

from app.providers.youtube import (
    InMemoryQuotaLedger,
    InMemorySecretStore,
    InMemoryUploadSessionStore,
    LedgerEntry,
    QuotaExhausted,
    TokenManager,
    TokenResponse,
    UploadLimitExceeded,
    YouTubeAPIError,
    YouTubeClient,
    build_auth_url,
    exchange_code,
    generate_pkce_pair,
    generate_state,
    pacific_day,
    wait_for_code,
)
from app.providers.youtube.errors import ChannelMismatch, NeedsReauth
from app.providers.youtube.quota import BUCKET_SHARED, BUCKET_UPLOAD
from app.providers.youtube.upload import (
    CHUNK_ALIGNMENT,
    ResumableUploader,
    UploadSessionState,
    compute_idempotency_key,
    validate_metadata,
)

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
CHANNEL_ID = "UCtestchannel123"


def run(coro):
    return asyncio.run(coro)


def make_http(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def seed_token_manager(http: httpx.AsyncClient) -> TokenManager:
    """TokenManager with one connected channel (refresh token stored)."""
    store = InMemorySecretStore()
    tm = TokenManager(
        store, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, http=http
    )
    tm.store_token_response(
        CHANNEL_ID,
        TokenResponse(
            access_token="seed-access-token",
            refresh_token="seed-refresh-token",
            expires_in=3600,
            scope="https://www.googleapis.com/auth/youtube.upload "
            "https://www.googleapis.com/auth/youtube",
        ),
    )
    return tm


# ---------------------------------------------------------------------------
# PKCE + auth URL
# ---------------------------------------------------------------------------


def test_pkce_pair_generation():
    verifier, challenge = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert re.fullmatch(r"[A-Za-z0-9\-._~]+", verifier)
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    assert challenge == expected
    assert "=" not in challenge
    assert challenge != verifier
    verifier2, challenge2 = generate_pkce_pair()
    assert verifier2 != verifier and challenge2 != challenge


def test_generate_state_has_at_least_128_bits():
    state = generate_state()
    # token_urlsafe(32) -> 43 chars carrying 256 bits; >=22 chars ~= 128 bits.
    assert len(state) >= 22
    assert generate_state() != state


def test_auth_url_contents():
    _, challenge = generate_pkce_pair()
    url = build_auth_url(
        client_id=CLIENT_ID,
        redirect_uri="http://127.0.0.1:51234",
        state="state-value-123",
        code_challenge=challenge,
    )
    parsed = urllib.parse.urlsplit(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    qs = urllib.parse.parse_qs(parsed.query)
    assert qs["state"] == ["state-value-123"]
    assert qs["code_challenge"] == [challenge]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == [CLIENT_ID]
    assert qs["redirect_uri"] == ["http://127.0.0.1:51234"]
    scopes = qs["scope"][0].split()
    assert "https://www.googleapis.com/auth/youtube.upload" in scopes
    assert "https://www.googleapis.com/auth/youtube" in scopes


def test_exchange_code_sends_verifier_and_parses_tokens():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["form"] = urllib.parse.parse_qs(request.content.decode())
        return httpx.Response(
            200,
            json={
                "access_token": "ya29.access",
                "refresh_token": "1//refresh",
                "expires_in": 3599,
                "scope": "https://www.googleapis.com/auth/youtube.upload",
                "token_type": "Bearer",
            },
        )

    async def go():
        http = make_http(handler)
        return await exchange_code(
            http,
            code="auth-code-1",
            code_verifier="verifier-abc",
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri="http://127.0.0.1:51234",
        )

    tokens = run(go())
    assert tokens.access_token == "ya29.access"
    assert tokens.refresh_token == "1//refresh"
    assert seen["form"]["grant_type"] == ["authorization_code"]
    assert seen["form"]["code_verifier"] == ["verifier-abc"]
    assert seen["form"]["code"] == ["auth-code-1"]


# ---------------------------------------------------------------------------
# Loopback listener (127.0.0.1 only)
# ---------------------------------------------------------------------------


def test_wait_for_code_loopback_roundtrip():
    async def go():
        captured: dict = {}

        async def drive(redirect_uri: str) -> None:
            captured["uri"] = redirect_uri
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                resp = await client.get(
                    redirect_uri,
                    params={"code": "the-auth-code", "state": "expected-state"},
                )
            captured["status"] = resp.status_code

        code, redirect_uri = await wait_for_code(
            "expected-state", timeout=10.0, on_redirect_uri=drive
        )
        assert code == "the-auth-code"
        assert redirect_uri == captured["uri"]
        assert captured["status"] == 200

    run(go())


def test_wait_for_code_rejects_state_mismatch():
    async def go():
        async def drive(redirect_uri: str) -> None:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                await client.get(
                    redirect_uri,
                    params={"code": "x", "state": "WRONG-state"},
                )

        with pytest.raises(Exception) as excinfo:
            await wait_for_code("expected-state", timeout=10.0, on_redirect_uri=drive)
        assert "state" in str(excinfo.value).lower()

    run(go())


# ---------------------------------------------------------------------------
# Token manager
# ---------------------------------------------------------------------------


def test_single_flight_refresh_collapses_concurrent_calls():
    calls = {"token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2.googleapis.com/token" in str(request.url):
            calls["token"] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": "fresh-access",
                    "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/youtube",
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(404)

    async def go():
        tm = seed_token_manager(make_http(handler))
        # Force both callers to actually need the refresh path.
        tm._cache.clear()
        first, second = await asyncio.gather(
            tm.refresh(CHANNEL_ID), tm.refresh(CHANNEL_ID)
        )
        assert first == "fresh-access"
        assert second == "fresh-access"

    run(go())
    assert calls["token"] == 1


def test_invalid_grant_marks_needs_reauth():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "revoked"},
        )

    async def go():
        tm = seed_token_manager(make_http(handler))
        tm._cache.clear()
        with pytest.raises(NeedsReauth):
            await tm.refresh(CHANNEL_ID)
        status = tm.get_status(CHANNEL_ID)
        assert status.status == "needs_reauth"

    run(go())


def test_testing_mode_token_flag():
    store = InMemorySecretStore()
    tm = TokenManager(store, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    tm.store_token_response(
        CHANNEL_ID,
        TokenResponse(
            access_token="a",
            refresh_token="r",
            expires_in=3600,
            refresh_token_expires_in=604_800,
        ),
    )
    status = tm.get_status(CHANNEL_ID)
    assert status.testing_mode_token is True
    # Status must never carry token values.
    assert not hasattr(status, "refresh_token")
    assert not hasattr(status, "access_token")

    store2 = InMemorySecretStore()
    tm2 = TokenManager(store2, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    tm2.store_token_response(
        CHANNEL_ID,
        TokenResponse(access_token="a", refresh_token="r", expires_in=3600),
    )
    assert tm2.get_status(CHANNEL_ID).testing_mode_token is False


def test_disconnect_revokes_and_deletes_blob():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    async def go():
        http = make_http(handler)
        tm = seed_token_manager(http)
        assert tm.get_status(CHANNEL_ID).status == "connected"
        await tm.disconnect(CHANNEL_ID)
        assert tm.get_status(CHANNEL_ID).status == "not_connected"

    run(go())
    assert any("revoke" in url for url in calls)


# ---------------------------------------------------------------------------
# Quota wrapper
# ---------------------------------------------------------------------------


def _fill_ledger(ledger: InMemoryQuotaLedger, bucket: str, units: int) -> None:
    ledger.record(
        LedgerEntry(
            bucket=bucket,
            method="videos.list",
            units=units,
            day_pt=pacific_day(),
            success=True,
        )
    )


def test_quota_reservation_refuses_upload_bucket_ceiling():
    http_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        http_calls["n"] += 1
        return httpx.Response(200, json={})

    async def go():
        ledger = InMemoryQuotaLedger()
        _fill_ledger(ledger, BUCKET_UPLOAD, 90)  # 90% of 100 = ceiling
        client = YouTubeClient(ledger=ledger, http=make_http(handler))
        with pytest.raises(QuotaExhausted):
            await client.request(
                "videos.insert",
                url="https://www.googleapis.com/upload/youtube/v3/videos",
                http_method="POST",
            )

    run(go())
    assert http_calls["n"] == 0  # refused BEFORE any HTTP call


def test_quota_reservation_allows_up_to_ceiling_then_refuses():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async def go():
        ledger = InMemoryQuotaLedger()
        _fill_ledger(ledger, BUCKET_UPLOAD, 89)
        client = YouTubeClient(ledger=ledger, http=make_http(handler))
        resp = await client.request("videos.insert", http_method="POST")
        assert resp.status_code == 200  # 89 + 1 = 90 == ceiling: allowed
        with pytest.raises(QuotaExhausted):
            await client.request("videos.insert", http_method="POST")

    run(go())


def test_quota_reservation_refuses_shared_bucket_ceiling():
    http_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        http_calls["n"] += 1
        return httpx.Response(200, json={})

    async def go():
        ledger = InMemoryQuotaLedger()
        _fill_ledger(ledger, BUCKET_SHARED, 8_960)
        client = YouTubeClient(ledger=ledger, http=make_http(handler))
        # thumbnails.set costs 50: 8960 + 50 = 9010 > 9000 ceiling.
        with pytest.raises(QuotaExhausted):
            await client.request("thumbnails.set", http_method="POST")
        # A 1-unit read still fits: 8960 + 1 <= 9000.
        resp = await client.request("videos.list")
        assert resp.status_code == 200

    run(go())
    assert http_calls["n"] == 1  # only the allowed read hit the wire


def test_failed_call_still_writes_ledger_entry():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    async def go():
        ledger = InMemoryQuotaLedger()
        client = YouTubeClient(ledger=ledger, http=make_http(handler))
        with pytest.raises(YouTubeAPIError):
            await client.request("videos.list")
        entries = ledger.entries_for(pacific_day())
        assert len(entries) == 1
        assert entries[0].method == "videos.list"
        assert entries[0].units == 1
        assert entries[0].success is False

    run(go())


def test_quota_exceeded_response_raises_quota_exhausted_and_ledgers():
    body = {
        "error": {
            "errors": [{"reason": "quotaExceeded", "domain": "youtube.quota"}],
            "code": 403,
            "message": "quota exceeded",
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=body)

    async def go():
        ledger = InMemoryQuotaLedger()
        client = YouTubeClient(ledger=ledger, http=make_http(handler))
        with pytest.raises(QuotaExhausted):
            await client.request("videos.list")
        assert len(ledger.entries_for(pacific_day())) == 1

    run(go())


def test_upload_limit_exceeded_is_per_channel_signal():
    body = {
        "error": {
            "errors": [{"reason": "uploadLimitExceeded"}],
            "code": 400,
            "message": "upload limit",
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=body)

    async def go():
        ledger = InMemoryQuotaLedger()
        client = YouTubeClient(ledger=ledger, http=make_http(handler))
        with pytest.raises(UploadLimitExceeded) as excinfo:
            await client.request(
                "videos.insert", channel_id=CHANNEL_ID, http_method="POST"
            )
        assert excinfo.value.channel_id == CHANNEL_ID

    run(go())


def test_401_triggers_one_refresh_and_one_retry():
    counts = {"api": 0, "token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "oauth2.googleapis.com/token" in url:
            counts["token"] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": "access-2",
                    "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/youtube",
                    "token_type": "Bearer",
                },
            )
        counts["api"] += 1
        auth = request.headers.get("authorization", "")
        if auth == "Bearer access-2":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(401, json={"error": {"message": "unauthorized"}})

    async def go():
        http = make_http(handler)
        tm = seed_token_manager(http)  # caches "seed-access-token"
        client = YouTubeClient(
            ledger=InMemoryQuotaLedger(), token_manager=tm, http=http
        )
        resp = await client.request("videos.list", channel_id=CHANNEL_ID)
        assert resp.status_code == 200

    run(go())
    assert counts["api"] == 2  # original + exactly one retry
    assert counts["token"] == 1  # exactly one refresh


# ---------------------------------------------------------------------------
# Channel verification
# ---------------------------------------------------------------------------


def _channel_handler(expected_id: str, *, long_uploads: str = "allowed"):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "oauth2.googleapis.com/token" in url:
            return httpx.Response(
                200,
                json={"access_token": "tok", "expires_in": 3600, "token_type": "Bearer"},
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": expected_id,
                        "snippet": {"title": "My Channel"},
                        "status": {"longUploadsStatus": long_uploads},
                    }
                ]
            },
        )

    return handler


def test_verify_channel_exact_match():
    async def go():
        http = make_http(_channel_handler(CHANNEL_ID))
        tm = seed_token_manager(http)
        client = YouTubeClient(
            ledger=InMemoryQuotaLedger(), token_manager=tm, http=http
        )
        info = await client.verify_channel(CHANNEL_ID)
        assert info.channel_id == CHANNEL_ID
        assert info.title == "My Channel"
        assert info.long_uploads_status == "allowed"
        assert info.can_upload_long is True

    run(go())


def test_verify_channel_mismatch_raises():
    async def go():
        http = make_http(_channel_handler("UCsomeoneelse999"))
        tm = seed_token_manager(http)
        client = YouTubeClient(
            ledger=InMemoryQuotaLedger(), token_manager=tm, http=http
        )
        with pytest.raises(ChannelMismatch):
            await client.verify_channel(CHANNEL_ID)

    run(go())


def test_verify_channel_unverified_long_uploads():
    async def go():
        http = make_http(
            _channel_handler(CHANNEL_ID, long_uploads="longUploadsUnspecified")
        )
        tm = seed_token_manager(http)
        client = YouTubeClient(
            ledger=InMemoryQuotaLedger(), token_manager=tm, http=http
        )
        info = await client.verify_channel(CHANNEL_ID)
        assert info.long_uploads_status == "longUploadsUnspecified"
        assert info.can_upload_long is False

    run(go())


# ---------------------------------------------------------------------------
# Upload: validation, resume, forced-private
# ---------------------------------------------------------------------------


def test_validate_metadata_rules():
    ok = validate_metadata(
        title="A title",
        description="desc",
        tags=["a", "b"],
        category_id="22",
        self_declared_made_for_kids=False,
        contains_synthetic_media=True,
    )
    assert ok["category_id"] == "22"
    with pytest.raises(ValueError):
        validate_metadata(
            title="x" * 101,
            description="",
            tags=[],
            category_id="22",
            self_declared_made_for_kids=False,
            contains_synthetic_media=False,
        )
    with pytest.raises(ValueError):
        validate_metadata(
            title="ok",
            description="d" * 5001,
            tags=[],
            category_id="22",
            self_declared_made_for_kids=False,
            contains_synthetic_media=False,
        )
    with pytest.raises(ValueError):
        validate_metadata(
            title="ok",
            description="",
            tags=["t" * 501],
            category_id="22",
            self_declared_made_for_kids=False,
            contains_synthetic_media=False,
        )
    with pytest.raises(ValueError):
        validate_metadata(
            title="ok",
            description="",
            tags=[],
            category_id=None,
            self_declared_made_for_kids=False,
            contains_synthetic_media=False,
        )
    with pytest.raises(ValueError):
        validate_metadata(
            title="ok",
            description="",
            tags=[],
            category_id="22",
            self_declared_made_for_kids=None,
            contains_synthetic_media=False,
        )
    with pytest.raises(ValueError):
        validate_metadata(
            title="ok",
            description="",
            tags=[],
            category_id="22",
            self_declared_made_for_kids=False,
            contains_synthetic_media=None,
        )


SESSION_URI = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=xyz"


def _upload_handler(*, applied_privacy: str, put_script: list):
    """Build a MockTransport handler for the resumable-upload protocol.

    ``put_script`` entries are callables receiving the recorded Content-Range
    and returning an httpx.Response; they are consumed in order.
    """
    recorded: dict = {"posts": [], "put_ranges": []}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and "upload/youtube" in url:
            recorded["posts"].append(json.loads(request.content.decode()))
            return httpx.Response(200, headers={"Location": SESSION_URI})
        if request.method == "PUT":
            recorded["put_ranges"].append(request.headers.get("content-range"))
            step = put_script.pop(0)
            return step(request.headers.get("content-range"))
        return httpx.Response(404)

    return handler, recorded


def _video_resource(privacy: str) -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "id": "video-123",
            "snippet": {"title": "T"},
            "status": {"privacyStatus": privacy},
        },
    )


def test_upload_resume_honours_server_range_and_detects_forced_private(tmp_path):
    total = 131_072 + 262_144 + 100_000  # 493,216 bytes
    file_path = tmp_path / "video.mp4"
    file_path.write_bytes(b"\x07" * total)
    chunk = CHUNK_ALIGNMENT  # 256 KiB chunks keep the script small

    def put1(_range):
        # Server only kept the first 128 KiB of the 256 KiB chunk.
        return httpx.Response(308, headers={"Range": "bytes=0-131071"})

    def put2(_range):
        return httpx.Response(308, headers={"Range": "bytes=0-393215"})

    def put3(_range):
        return _video_resource("private")  # public requested, private applied

    handler, recorded = _upload_handler(
        applied_privacy="private", put_script=[put1, put2, put3]
    )

    async def go():
        http = make_http(handler)
        tm = seed_token_manager(http)
        client = YouTubeClient(
            ledger=InMemoryQuotaLedger(), token_manager=tm, http=http
        )
        uploader = ResumableUploader(
            client, InMemoryUploadSessionStore(), chunk_size=chunk
        )
        return await uploader.upload(
            file_path=file_path,
            channel_id=CHANNEL_ID,
            title="Resume test",
            description="d",
            tags=["x"],
            category_id="22",
            self_declared_made_for_kids=False,
            contains_synthetic_media=True,
            requested_privacy="public",
        )

    result = run(go())
    # Insert ALWAYS sends private, whatever the final intent is.
    assert recorded["posts"][0]["status"]["privacyStatus"] == "private"
    # Resume used the SERVER offset (131072), not the client's assumption.
    assert recorded["put_ranges"][0] == f"bytes 0-262143/{total}"
    assert recorded["put_ranges"][1] == f"bytes 131072-393215/{total}"
    assert recorded["put_ranges"][2] == f"bytes 393216-{total - 1}/{total}"
    assert result.video_id == "video-123"
    assert result.requested_privacy_status == "public"
    assert result.applied_privacy_status == "private"
    assert result.forced_private is True


def test_upload_no_forced_private_when_requested_private(tmp_path):
    file_path = tmp_path / "small.mp4"
    file_path.write_bytes(b"\x01" * 1_000)

    def put_final(_range):
        return _video_resource("private")

    handler, recorded = _upload_handler(
        applied_privacy="private", put_script=[put_final]
    )

    async def go():
        http = make_http(handler)
        tm = seed_token_manager(http)
        client = YouTubeClient(
            ledger=InMemoryQuotaLedger(), token_manager=tm, http=http
        )
        uploader = ResumableUploader(client, InMemoryUploadSessionStore())
        return await uploader.upload(
            file_path=file_path,
            channel_id=CHANNEL_ID,
            title="Private video",
            category_id="22",
            self_declared_made_for_kids=False,
            contains_synthetic_media=False,
            requested_privacy="private",
        )

    result = run(go())
    assert result.forced_private is False
    assert result.applied_privacy_status == "private"


def test_upload_resumes_existing_session_via_status_query(tmp_path):
    file_path = tmp_path / "video.mp4"
    file_path.write_bytes(b"\x03" * 500)
    title = "Already uploaded"
    key = compute_idempotency_key(file_path, CHANNEL_ID, title)

    store = InMemoryUploadSessionStore()
    store.save_session(
        UploadSessionState(
            idempotency_key=key,
            channel_id=CHANNEL_ID,
            session_uri=SESSION_URI,
            total_bytes=500,
            bytes_confirmed=200,
            title=title,
            requested_privacy_status="private",
        )
    )
    posts = {"n": 0}
    status_ranges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts["n"] += 1
            return httpx.Response(200, headers={"Location": SESSION_URI})
        # Status query: upload actually completed on the server.
        status_ranges.append(request.headers.get("content-range", ""))
        return _video_resource("private")

    async def go():
        http = make_http(handler)
        tm = seed_token_manager(http)
        client = YouTubeClient(
            ledger=InMemoryQuotaLedger(), token_manager=tm, http=http
        )
        uploader = ResumableUploader(client, store)
        return await uploader.upload(
            file_path=file_path,
            channel_id=CHANNEL_ID,
            title=title,
            category_id="22",
            self_declared_made_for_kids=False,
            contains_synthetic_media=False,
            requested_privacy="private",
        )

    result = run(go())
    assert status_ranges == ["bytes */500"]  # status query came first
    assert posts["n"] == 0  # no new session was opened
    assert result.video_id == "video-123"
    assert result.forced_private is False
