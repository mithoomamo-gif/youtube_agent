"""Tests for the REAL channel connect flow (Round 3, channels router).

Google's token + channels endpoints are mocked with httpx.MockTransport
(the house style, see tests/providers/test_youtube_api.py); the OAuth
loopback listener is real but talks to 127.0.0.1 only. No token value
may ever appear in an API response.

Covered:
- No client credentials in the secret store -> 409
  ``youtube_client_not_configured`` with the ordered setup-steps payload.
- Credentials present + mocked successful PKCE flow -> tokens stored
  ONLY via the secret store, channel row created in connect state
  ``needs_audit_check`` (audit status unknown), ``channel.updated``
  emitted, and no token material in any response.
- Revalidate with ``invalid_grant`` -> channel flips to ``needs_reauth``.
- Revalidate success -> credential status connected, tokens refreshed
  in the store.
"""

from __future__ import annotations

import json
import time
import urllib.parse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.providers.youtube import InMemorySecretStore, TokenManager, TokenResponse
from app.providers.youtube.oauth import TOKEN_URL
from app.routers import channels as channels_router

CLIENT_ID = "test-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret"
ACCESS_TOKEN = "ya29.test-access-token"
REFRESH_TOKEN = "1//test-refresh-token"
NEW_ACCESS_TOKEN = "ya29.refreshed-access-token"

YT_CONNECT = "UCconnect1234567"
YT_REAUTH = "UCreauth12345678"
YT_OK = "UCokchannel12345"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store_with_client() -> InMemorySecretStore:
    store = InMemorySecretStore()
    store.put(channels_router.CLIENT_ID_REF, CLIENT_ID)
    store.put(channels_router.CLIENT_SECRET_REF, CLIENT_SECRET)
    return store


def _channels_payload(channel_id: str, title: str = "Facts Channel") -> dict:
    return {
        "items": [
            {
                "id": channel_id,
                "snippet": {
                    "title": title,
                    "customUrl": "@factschannel",
                    "country": "PK",
                },
                "status": {"longUploadsStatus": "allowed"},
            }
        ]
    }


def _seed_channel_row(row_id: str, youtube_channel_id: str) -> None:
    from app import db as db_module

    now = int(time.time() * 1000)
    conn = db_module.get_connection()
    try:
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title, language, "
            "status, config_json, created_at, updated_at) "
            "VALUES (?, ?, ?, 'en', 'paused', ?, ?, ?)",
            (
                row_id,
                youtube_channel_id,
                "Seeded Channel",
                json.dumps(
                    {
                        "connect_status": "needs_audit_check",
                        "credential_status": "connected",
                        "audit_status": "unknown",
                    }
                ),
                now,
                now,
            ),
        )
    finally:
        conn.close()


def _get_channel(client: TestClient, youtube_channel_id: str) -> dict | None:
    resp = client.get("/api/v1/channels")
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        if item.get("youtube_channel_id") == youtube_channel_id:
            return item
    return None


def _seed_connected_blob(store: InMemorySecretStore, youtube_channel_id: str) -> None:
    manager = TokenManager(store, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    manager.store_token_response(
        youtube_channel_id,
        TokenResponse(
            access_token="ya29.seed-access",
            refresh_token=REFRESH_TOKEN,
            expires_in=3600,
            scope="https://www.googleapis.com/auth/youtube",
        ),
    )


# ---------------------------------------------------------------------------
# 409: client not configured
# ---------------------------------------------------------------------------


def test_connect_without_client_credentials_returns_409_setup_steps(
    app, client: TestClient
) -> None:
    app.state.youtube_secret_store = InMemorySecretStore()

    resp = client.post("/api/v1/channels/connect", json={"open_browser": False})

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "youtube_client_not_configured"
    details = error["details"]
    assert details["missing_refs"] == [
        "youtube/client_id",
        "youtube/client_secret",
    ]
    steps = details["setup_steps"]
    assert isinstance(steps, list) and len(steps) >= 5
    # Ordered, plain-language steps the UI can render verbatim.
    assert [s["step"] for s in steps] == list(range(1, len(steps) + 1))
    text = " ".join(f"{s['title']} {s['detail']}" for s in steps)
    assert "Google Cloud project" in text
    assert "YouTube Data API v3" in text
    assert "YouTube Analytics API" in text
    assert "Desktop app" in text
    assert "Production" in text
    assert "7 days" in text  # the Testing-mode refresh-token warning
    assert "API key" in text  # the app never needs an API key for uploads


def test_connect_with_only_client_id_returns_409_naming_missing_ref(
    app, client: TestClient
) -> None:
    store = InMemorySecretStore()
    store.put(channels_router.CLIENT_ID_REF, CLIENT_ID)
    app.state.youtube_secret_store = store

    resp = client.post("/api/v1/channels/connect", json={"open_browser": False})

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "youtube_client_not_configured"
    assert error["details"]["missing_refs"] == ["youtube/client_secret"]
    # The configured value itself is never echoed back.
    assert CLIENT_ID not in resp.text


# ---------------------------------------------------------------------------
# Successful mocked flow
# ---------------------------------------------------------------------------


def test_connect_success_stores_tokens_and_creates_channel_needs_audit_check(
    app, client: TestClient
) -> None:
    store = _store_with_client()
    app.state.youtube_secret_store = store
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(TOKEN_URL):
            seen["token_form"] = urllib.parse.parse_qs(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "access_token": ACCESS_TOKEN,
                    "refresh_token": REFRESH_TOKEN,
                    "expires_in": 3599,
                    "scope": (
                        "https://www.googleapis.com/auth/youtube.upload "
                        "https://www.googleapis.com/auth/youtube"
                    ),
                    "token_type": "Bearer",
                },
            )
        if url.startswith(channels_router.CHANNELS_LIST_URL):
            seen["channels_auth"] = request.headers.get("Authorization")
            seen["channels_params"] = urllib.parse.parse_qs(
                urllib.parse.urlsplit(url).query
            )
            return httpx.Response(200, json=_channels_payload(YT_CONNECT))
        return httpx.Response(404)

    app.state.youtube_http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )

    resp = client.post(
        "/api/v1/channels/connect",
        json={"open_browser": False, "channel_id": YT_CONNECT},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "connect_started"
    flow_id = body["flow_id"]
    auth_url = body["auth_url"]
    assert auth_url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(auth_url).query)
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]
    assert qs["code_challenge_method"] == ["S256"]
    redirect_uri = qs["redirect_uri"][0]
    state = qs["state"][0]
    assert redirect_uri.startswith("http://127.0.0.1:")

    # Simulate the browser landing on the loopback listener with the code.
    with httpx.Client(trust_env=False, timeout=10.0) as browser:
        landing = browser.get(
            f"{redirect_uri}/?code=fake-auth-code&state={state}"
        )
    assert landing.status_code == 200

    # Completion is asynchronous: poll the flow status endpoint.
    flow_body: dict = {}
    deadline = time.time() + 10.0
    while time.time() < deadline:
        flow_resp = client.get(f"/api/v1/channels/connect/{flow_id}")
        assert flow_resp.status_code == 200
        flow_body = flow_resp.json()
        if flow_body["status"] not in ("awaiting_callback", "exchanging"):
            break
        time.sleep(0.05)
    assert flow_body["status"] == "needs_audit_check"
    assert flow_body["youtube_channel_id"] == YT_CONNECT
    assert flow_body["credential_status"] == "connected"

    # The exchange used PKCE + the authorization_code grant, and channel
    # discovery used the fresh access token with mine=true.
    form = seen["token_form"]
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["fake-auth-code"]
    assert form["code_verifier"] and len(form["code_verifier"][0]) >= 43
    assert seen["channels_auth"] == f"Bearer {ACCESS_TOKEN}"
    assert seen["channels_params"]["mine"] == ["true"]

    # Tokens went ONLY to the secret store, under the TokenManager ref.
    blob_raw = store.get(f"youtube:channel:{YT_CONNECT}")
    assert blob_raw is not None
    assert json.loads(blob_raw)["refresh_token"] == REFRESH_TOKEN

    # Channel row exists, in needs_audit_check, audit unknown.
    row = _get_channel(client, YT_CONNECT)
    assert row is not None
    assert row["title"] == "Facts Channel"
    assert row["handle"] == "@factschannel"
    assert row["connect_status"] == "needs_audit_check"
    assert row["audit_status"] == "unknown"
    assert row["credential_status"] == "connected"

    # channel.updated was emitted (journalled by the WS hub).
    from app import db as db_module

    conn = db_module.get_connection()
    try:
        events = conn.execute(
            "SELECT payload_json FROM event_log WHERE type = 'channel.updated'"
        ).fetchall()
    finally:
        conn.close()
    envelopes = [json.loads(r[0]) for r in events if r[0]]
    payloads = [e.get("payload", e) for e in envelopes]  # hub journals envelopes
    assert any(
        p.get("youtube_channel_id") == YT_CONNECT
        and p.get("connect_status") == "needs_audit_check"
        for p in payloads
    )

    # No token material in ANY API response seen along the way.
    for text in (resp.text, json.dumps(flow_body), json.dumps(row)):
        assert ACCESS_TOKEN not in text
        assert REFRESH_TOKEN not in text
        assert CLIENT_SECRET not in text


def test_connect_wrong_channel_aborts_and_stores_nothing(
    app, client: TestClient
) -> None:
    store = _store_with_client()
    app.state.youtube_secret_store = store

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(TOKEN_URL):
            return httpx.Response(
                200,
                json={
                    "access_token": ACCESS_TOKEN,
                    "refresh_token": REFRESH_TOKEN,
                    "expires_in": 3599,
                    "scope": "https://www.googleapis.com/auth/youtube",
                },
            )
        if url.startswith(channels_router.CHANNELS_LIST_URL):
            # The consented account owns a DIFFERENT channel than asked.
            return httpx.Response(200, json=_channels_payload("UCsomeoneelse999"))
        return httpx.Response(404)

    app.state.youtube_http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )

    resp = client.post(
        "/api/v1/channels/connect",
        json={"open_browser": False, "channel_id": YT_CONNECT},
    )
    assert resp.status_code == 202
    flow_id = resp.json()["flow_id"]
    qs = urllib.parse.parse_qs(
        urllib.parse.urlsplit(resp.json()["auth_url"]).query
    )
    with httpx.Client(trust_env=False, timeout=10.0) as browser:
        browser.get(
            f"{qs['redirect_uri'][0]}/?code=fake-auth-code&state={qs['state'][0]}"
        )

    flow_body: dict = {}
    deadline = time.time() + 10.0
    while time.time() < deadline:
        flow_body = client.get(f"/api/v1/channels/connect/{flow_id}").json()
        if flow_body["status"] not in ("awaiting_callback", "exchanging"):
            break
        time.sleep(0.05)
    assert flow_body["status"] == "connect_failed"
    assert flow_body["error_code"] == "oauth_flow_failed"
    # Nothing stored, no channel row for either channel.
    assert store.get(f"youtube:channel:{YT_CONNECT}") is None
    assert store.get("youtube:channel:UCsomeoneelse999") is None
    assert _get_channel(client, "UCsomeoneelse999") is None


# ---------------------------------------------------------------------------
# Revalidate
# ---------------------------------------------------------------------------


def test_revalidate_invalid_grant_marks_channel_needs_reauth(
    app, client: TestClient
) -> None:
    store = _store_with_client()
    app.state.youtube_secret_store = store
    _seed_connected_blob(store, YT_REAUTH)
    _seed_channel_row("chan-reauth", YT_REAUTH)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(TOKEN_URL):
            form = urllib.parse.parse_qs(request.content.decode())
            assert form["grant_type"] == ["refresh_token"]
            return httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": "Token has been expired or revoked.",
                },
            )
        return httpx.Response(404)

    app.state.youtube_http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )

    resp = client.post(f"/api/v1/channels/chan-reauth/revalidate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "needs_reauth"
    assert body["connect_status"] == "needs_reauth"
    assert body["credential_status"] == "needs_reauth"
    assert REFRESH_TOKEN not in resp.text

    # The channel row flipped to needs_reauth...
    row = _get_channel(client, YT_REAUTH)
    assert row is not None
    assert row["connect_status"] == "needs_reauth"
    assert row["credential_status"] == "needs_reauth"
    # ...and so did the TokenManager's credential blob in the store.
    status = TokenManager(
        store, client_id=CLIENT_ID, client_secret=CLIENT_SECRET
    ).get_status(YT_REAUTH)
    assert status.status == "needs_reauth"


def test_revalidate_success_refreshes_tokens_and_reports_connected(
    app, client: TestClient
) -> None:
    store = _store_with_client()
    app.state.youtube_secret_store = store
    _seed_connected_blob(store, YT_OK)
    _seed_channel_row("chan-ok", YT_OK)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(TOKEN_URL):
            return httpx.Response(
                200,
                json={
                    "access_token": NEW_ACCESS_TOKEN,
                    "expires_in": 3599,
                    "scope": "https://www.googleapis.com/auth/youtube",
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(404)

    app.state.youtube_http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )

    resp = client.post(f"/api/v1/channels/chan-ok/revalidate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["credential_status"] == "connected"
    # Audit is still unchecked, so the connect state is not upgraded.
    assert body["connect_status"] == "needs_audit_check"
    assert NEW_ACCESS_TOKEN not in resp.text
    assert REFRESH_TOKEN not in resp.text

    row = _get_channel(client, YT_OK)
    assert row is not None
    assert row["credential_status"] == "connected"
    assert row["connect_status"] == "needs_audit_check"


def test_revalidate_unknown_channel_is_404(app, client: TestClient) -> None:
    app.state.youtube_secret_store = _store_with_client()
    resp = client.post("/api/v1/channels/no-such-channel/revalidate")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "channel_not_found"


def test_connect_flow_status_unknown_flow_is_404(app, client: TestClient) -> None:
    resp = client.get("/api/v1/channels/connect/deadbeef")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "connect_flow_not_found"
