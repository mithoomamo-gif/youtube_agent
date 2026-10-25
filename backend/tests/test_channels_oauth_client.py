"""Tests for the OAuth *client* credential endpoints (channels router).

The shipped app reads the Google OAuth client pair from the secret
store (refs ``youtube/client_id`` / ``youtube/client_secret``) but,
until these endpoints landed, had NO write path for them — a real
install could never get past the 409 ``youtube_client_not_configured``
on connect. ``PUT /api/v1/channels/oauth-client`` is that write path;
it is write-only, so no response ever contains either value.

Covered:
- GET status is false with nothing (or only half the pair) stored,
  true once BOTH refs hold non-blank values.
- PUT trims and stores both values via the secret store, returns only
  ``{"client_configured": true}``; blank/missing values are a 422
  ``validation_error`` and store nothing.
- After a PUT, POST /connect no longer 409s — the mocked PKCE flow
  completes end to end using the pair entered through the endpoint.
- DELETE removes both refs (idempotently) and connect 409s again.
"""

from __future__ import annotations

import time
import urllib.parse

import httpx
from fastapi.testclient import TestClient

from app.providers.youtube import InMemorySecretStore
from app.providers.youtube.oauth import TOKEN_URL
from app.routers import channels as channels_router

CLIENT_ID = "entered-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "entered-client-secret-value"
ACCESS_TOKEN = "ya29.entered-access-token"
REFRESH_TOKEN = "1//entered-refresh-token"

YT_ENTERED = "UCentered1234567"

OAUTH_CLIENT_URL = "/api/v1/channels/oauth-client"


def _channels_payload(channel_id: str) -> dict:
    return {
        "items": [
            {
                "id": channel_id,
                "snippet": {
                    "title": "Entered Channel",
                    "customUrl": "@enteredchannel",
                    "country": "PK",
                },
                "status": {"longUploadsStatus": "allowed"},
            }
        ]
    }


# ---------------------------------------------------------------------------
# Status + PUT validation
# ---------------------------------------------------------------------------


def test_oauth_client_status_false_when_nothing_stored(
    app, client: TestClient
) -> None:
    app.state.youtube_secret_store = InMemorySecretStore()

    resp = client.get(OAUTH_CLIENT_URL)

    assert resp.status_code == 200
    assert resp.json() == {"client_configured": False}


def test_oauth_client_status_false_when_only_half_the_pair_stored(
    app, client: TestClient
) -> None:
    store = InMemorySecretStore()
    store.put(channels_router.CLIENT_ID_REF, CLIENT_ID)
    app.state.youtube_secret_store = store

    resp = client.get(OAUTH_CLIENT_URL)

    assert resp.status_code == 200
    assert resp.json() == {"client_configured": False}
    # The stored half of the pair is never echoed back.
    assert CLIENT_ID not in resp.text


def test_put_oauth_client_stores_trimmed_pair_and_never_echoes_secret(
    app, client: TestClient
) -> None:
    store = InMemorySecretStore()
    app.state.youtube_secret_store = store

    resp = client.put(
        OAUTH_CLIENT_URL,
        json={
            "client_id": f"  {CLIENT_ID}  ",
            "client_secret": f"\t{CLIENT_SECRET}\n",
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {"client_configured": True}
    assert CLIENT_SECRET not in resp.text
    # Stored trimmed, under the refs the connect flow reads.
    assert store.get(channels_router.CLIENT_ID_REF) == CLIENT_ID
    assert store.get(channels_router.CLIENT_SECRET_REF) == CLIENT_SECRET

    status = client.get(OAUTH_CLIENT_URL)
    assert status.status_code == 200
    assert status.json() == {"client_configured": True}
    assert CLIENT_SECRET not in status.text


def test_put_oauth_client_rejects_blank_values_and_stores_nothing(
    app, client: TestClient
) -> None:
    store = InMemorySecretStore()
    app.state.youtube_secret_store = store

    cases = [
        ({"client_id": "", "client_secret": CLIENT_SECRET}, "client_id"),
        ({"client_id": "   ", "client_secret": CLIENT_SECRET}, "client_id"),
        ({"client_secret": CLIENT_SECRET}, "client_id"),
        ({"client_id": CLIENT_ID, "client_secret": ""}, "client_secret"),
        ({"client_id": CLIENT_ID, "client_secret": "  "}, "client_secret"),
        ({"client_id": CLIENT_ID}, "client_secret"),
    ]
    for body, field in cases:
        resp = client.put(OAUTH_CLIENT_URL, json=body)
        assert resp.status_code == 422, body
        error = resp.json()["error"]
        assert error["code"] == "validation_error"
        assert error["details"]["field"] == field
        assert CLIENT_SECRET not in resp.text

    assert store.get(channels_router.CLIENT_ID_REF) is None
    assert store.get(channels_router.CLIENT_SECRET_REF) is None
    assert client.get(OAUTH_CLIENT_URL).json() == {"client_configured": False}


# ---------------------------------------------------------------------------
# PUT unblocks connect; DELETE re-blocks it
# ---------------------------------------------------------------------------


def test_connect_uses_oauth_client_entered_via_put(
    app, client: TestClient
) -> None:
    store = InMemorySecretStore()
    app.state.youtube_secret_store = store

    # Before entry: the 409 that stranded real installs.
    blocked = client.post(
        "/api/v1/channels/connect", json={"open_browser": False}
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "youtube_client_not_configured"

    put = client.put(
        OAUTH_CLIENT_URL,
        json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
    )
    assert put.status_code == 200

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
                    "token_type": "Bearer",
                },
            )
        if url.startswith(channels_router.CHANNELS_LIST_URL):
            return httpx.Response(200, json=_channels_payload(YT_ENTERED))
        return httpx.Response(404)

    app.state.youtube_http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )

    resp = client.post(
        "/api/v1/channels/connect",
        json={"open_browser": False, "channel_id": YT_ENTERED},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "connect_started"
    # The authorization URL was built from the pair entered via PUT.
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(body["auth_url"]).query)
    assert qs["client_id"] == [CLIENT_ID]
    assert CLIENT_SECRET not in resp.text

    with httpx.Client(trust_env=False, timeout=10.0) as browser:
        landing = browser.get(
            f"{qs['redirect_uri'][0]}/?code=fake-auth-code&state={qs['state'][0]}"
        )
    assert landing.status_code == 200

    flow_body: dict = {}
    deadline = time.time() + 10.0
    while time.time() < deadline:
        flow_body = client.get(
            f"/api/v1/channels/connect/{body['flow_id']}"
        ).json()
        if flow_body["status"] not in ("awaiting_callback", "exchanging"):
            break
        time.sleep(0.05)
    assert flow_body["status"] == "needs_audit_check"
    assert flow_body["youtube_channel_id"] == YT_ENTERED
    assert CLIENT_SECRET not in str(flow_body)
    assert ACCESS_TOKEN not in str(flow_body)
    assert REFRESH_TOKEN not in str(flow_body)


def test_delete_oauth_client_removes_pair_and_reblocks_connect(
    app, client: TestClient
) -> None:
    store = InMemorySecretStore()
    app.state.youtube_secret_store = store

    put = client.put(
        OAUTH_CLIENT_URL,
        json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
    )
    assert put.status_code == 200
    assert client.get(OAUTH_CLIENT_URL).json() == {"client_configured": True}

    resp = client.delete(OAUTH_CLIENT_URL)
    assert resp.status_code == 200
    assert resp.json() == {"client_configured": False}
    assert CLIENT_SECRET not in resp.text
    assert store.get(channels_router.CLIENT_ID_REF) is None
    assert store.get(channels_router.CLIENT_SECRET_REF) is None
    assert client.get(OAUTH_CLIENT_URL).json() == {"client_configured": False}

    blocked = client.post(
        "/api/v1/channels/connect", json={"open_browser": False}
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "youtube_client_not_configured"

    # Deleting again is a graceful no-op, not an error.
    again = client.delete(OAUTH_CLIENT_URL)
    assert again.status_code == 200
    assert again.json() == {"client_configured": False}
