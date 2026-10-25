"""Auth (launch token / Host / Origin / rate-limit) + health tests.

Self-contained: uses ``app.main.create_app`` when it is importable AND
wires the health routes; otherwise builds a minimal FastAPI app that
mounts LaunchTokenMiddleware + the health router directly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-launch-token-0123456789abcdef"
BASE_URL = "http://127.0.0.1:8000"
AUTH = {"X-Local-Token": TOKEN}
BAD_AUTH = {"X-Local-Token": "definitely-the-wrong-token"}


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        token=TOKEN,
        port=8000,
        boot_id="test-boot-id",
        data_dir="/tmp",
        db_path="/tmp/test.db",
        log_level="INFO",
    )


def build_app():
    """Prefer the real app factory; fall back to a minimal app."""
    try:
        from app.main import create_app

        app = create_app()
        app.state.settings = _settings()
        paths = {getattr(route, "path", "") for route in app.routes}
        if "/health/live" in paths:
            return app
    except Exception:
        pass

    from fastapi import FastAPI

    from app.errors import register_exception_handlers
    from app.middleware import LaunchTokenMiddleware
    from app.routers.health import router as health_router

    app = FastAPI()
    app.state.settings = _settings()
    register_exception_handlers(app)
    app.add_middleware(LaunchTokenMiddleware)
    app.include_router(health_router)
    return app


@pytest.fixture()
def client():
    # Fresh app per test -> fresh in-memory rate-limit state.
    with TestClient(build_app(), base_url=BASE_URL) as c:
        yield c


def test_live_without_token_is_ok_and_exact(client):
    resp = client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ready_without_token_is_401_envelope(client):
    resp = client.get("/health/ready")
    assert resp.status_code == 401
    body = resp.json()
    assert "error" in body
    err = body["error"]
    assert err["code"] == "missing_token"
    assert err["message"]
    assert err["request_id"]
    assert err["retryable"] is False
    assert "details" in err


def test_ready_with_wrong_token_is_401_invalid_token(client):
    resp = client.get("/health/ready", headers=BAD_AUTH)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_token"


def test_ready_with_correct_token_is_not_401(client):
    resp = client.get("/health/ready", headers=AUTH)
    assert resp.status_code != 401
    # 200 when a DB is available, 503 envelope when it is not.
    if resp.status_code == 503:
        assert resp.json()["error"]["code"] == "not_ready"
    else:
        assert resp.status_code == 200
        assert resp.json()["db"] == "ok"


# ---------------------------------------------------------------------------
# G2 / R6-018: Authorization: Bearer <token> is accepted alongside
# X-Local-Token (the Tauri shell's mini_http client sends both).
# Precedence: either valid token authenticates.
# ---------------------------------------------------------------------------

BEARER = {"Authorization": f"Bearer {TOKEN}"}
BAD_BEARER = {"Authorization": "Bearer definitely-the-wrong-token"}  # secret-scan: allow (deliberately fake wrong-token fixture)


def test_ready_with_bearer_token_is_not_401(client):
    resp = client.get("/health/ready", headers=BEARER)
    assert resp.status_code != 401
    if resp.status_code == 503:
        assert resp.json()["error"]["code"] == "not_ready"
    else:
        assert resp.status_code == 200
        assert resp.json()["db"] == "ok"


def test_ready_with_wrong_bearer_token_is_401_invalid_token(client):
    resp = client.get("/health/ready", headers=BAD_BEARER)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_token"


def test_api_path_accepts_bearer_token(client):
    resp = client.get("/api/v1/system/info", headers=BEARER)
    assert resp.status_code != 401


def test_either_header_authenticates_when_both_present(client):
    # Wrong X-Local-Token + valid Bearer -> authenticated.
    resp = client.get("/health/ready", headers={**BAD_AUTH, **BEARER})
    assert resp.status_code != 401
    # Valid X-Local-Token + wrong Bearer -> authenticated.
    resp = client.get("/health/ready", headers={**AUTH, **BAD_BEARER})
    assert resp.status_code != 401


def test_both_headers_wrong_is_401_invalid_token(client):
    resp = client.get("/health/ready", headers={**BAD_AUTH, **BAD_BEARER})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_token"


def test_non_bearer_authorization_is_not_a_token(client):
    # A non-Bearer Authorization header presents no launch token at
    # all: the rejection stays missing_token, not invalid_token.
    resp = client.get(
        "/health/ready", headers={"Authorization": "Basic dGVzdA=="}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_token"


def test_evil_host_is_403(client):
    resp = client.get("/health/ready", headers={**AUTH, "Host": "evil.com"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "host_not_allowed"


def test_post_with_evil_origin_is_403(client):
    resp = client.post(
        "/health/ready",
        headers={**AUTH, "Origin": "https://evil.example"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "origin_not_allowed"


def test_null_origin_is_403(client):
    resp = client.post(
        "/health/ready", headers={**AUTH, "Origin": "null"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "origin_not_allowed"


def test_tauri_origin_is_allowed(client):
    resp = client.post(
        "/health/ready",
        headers={**AUTH, "Origin": "https://tauri.localhost"},
    )
    assert resp.status_code != 403


def test_bad_token_rate_limit_kicks_in(client):
    for _ in range(11):
        client.get("/health/ready", headers=BAD_AUTH)
    resp = client.get("/health/ready", headers=BAD_AUTH)
    assert resp.status_code == 429
    err = resp.json()["error"]
    assert err["code"] == "too_many_bad_tokens"
    assert err["retryable"] is True


def test_request_id_header_on_error_response(client):
    resp = client.get("/health/ready")
    assert resp.status_code == 401
    assert resp.headers.get("x-request-id")
    assert resp.headers["x-request-id"] == resp.json()["error"]["request_id"]


def test_api_path_requires_token(client):
    resp = client.get("/api/v1/system/info")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_token"


# -- CORS for the Tauri webview (patch v14) ------------------------------
# Real-world failure (installer test, 2026-10-25): backend healthy and
# READY on a real Windows install, yet the UI reported "Backend offline"
# because WebView2 blocks cross-origin responses that carry no
# Access-Control-Allow-Origin, and the preflight OPTIONS (which never
# carries the launch token) was rejected 401 by the token check.

TAURI_ORIGIN = "https://tauri.localhost"


def test_preflight_from_tauri_origin_needs_no_token(client):
    resp = client.options(
        "/health/ready",
        headers={
            "Origin": TAURI_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-local-token, authorization",
        },
    )
    assert resp.status_code == 204
    assert resp.headers["access-control-allow-origin"] == TAURI_ORIGIN
    assert "GET" in resp.headers["access-control-allow-methods"]
    assert resp.headers["access-control-allow-private-network"] == "true"
    assert "x-local-token" in resp.headers["access-control-allow-headers"]


def test_preflight_from_evil_origin_is_403_without_acao(client):
    resp = client.options(
        "/health/ready",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 403
    assert "access-control-allow-origin" not in resp.headers


def test_live_with_tauri_origin_carries_acao(client):
    resp = client.get("/health/live", headers={"Origin": TAURI_ORIGIN})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == TAURI_ORIGIN


def test_ready_with_token_and_tauri_origin_carries_acao(client):
    resp = client.get(
        "/health/ready", headers={**AUTH, "Origin": TAURI_ORIGIN}
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == TAURI_ORIGIN


def test_rejection_with_tauri_origin_still_carries_acao(client):
    # No token -> 401, but the webview must be able to READ the error,
    # otherwise it surfaces as an opaque network failure.
    resp = client.get("/health/ready", headers={"Origin": TAURI_ORIGIN})
    assert resp.status_code == 401
    assert resp.headers["access-control-allow-origin"] == TAURI_ORIGIN


def test_no_origin_means_no_acao(client):
    resp = client.get("/health/live")
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers
