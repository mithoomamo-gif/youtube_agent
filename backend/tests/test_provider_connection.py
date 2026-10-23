"""Provider test-connection tests (roadmap section 28) — HTTP mocked.

Covers ``POST /api/v1/providers/{id}/test`` for gemini, pixabay,
elevenlabs and openai end-to-end through the FastAPI router, with all
provider HTTP replaced by ``httpx.MockTransport`` injected via the
router's ``_TEST_TRANSPORT`` hook, plus direct service-level tests of
``app.providers.connection_test.run_provider_test``.

States asserted per provider:

* no key in the secret store -> 200, ``status == "not_configured"``,
  ``ok is False``, and NO HTTP call is made;
* key + mocked success -> ``status == "ok"`` with minimal info
  (model count / search totals / subscription tier);
* key + mocked 401 -> ``status == "error"``, ``category == "invalid_key"``;
* key + mocked 429 -> ``category == "quota_exhausted"``;
* key + connection error -> ``category == "network"``.

The stub detail string must be gone everywhere, and the key value must
never appear in any response (including a provider error body that
echoes it back — redaction must scrub it).
"""

from __future__ import annotations

import asyncio
import os
import tempfile

# Must be set before importing app.* (same pattern as test_routers.py).
os.environ.setdefault(
    "YTA_DATA_DIR", tempfile.mkdtemp(prefix="yta-provider-test-")
)

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import secrets_store
from app.errors import register_exception_handlers
from app.providers.connection_test import run_provider_test
from app.routers import api as api_module
from app.routers import providers as providers_router

try:
    from app import db as _db_module

    _db_module.init_db()
except Exception:
    pass

PROVIDERS_UNDER_TEST = ["gemini", "pixabay", "elevenlabs", "openai"]

KEYS = {
    "gemini": "gemini-test-key-aaaa1111",
    "pixabay": "pixabay-test-key-bbbb2222",
    "elevenlabs": "elevenlabs-test-key-cccc3333",
    "openai": "openai-test-key-dddd4444",
}


def _build_app() -> FastAPI:
    app = FastAPI(title="YouTube Agent — provider test tests")
    register_exception_handlers(app)
    app.include_router(api_module.router)
    return app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(_build_app())


class _FakeKeyring:
    """In-memory keyring stand-in (headless Linux has no real backend)."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def get_password(self, service: str, username: str):
        return self.store.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


@pytest.fixture()
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    fake = _FakeKeyring()
    monkeypatch.setattr(secrets_store, "keyring", fake)
    # Isolate the process-local last-test read model between tests.
    providers_router._LAST_TEST.clear()
    return fake


def _store_key(fake: _FakeKeyring, provider_id: str) -> str:
    key = KEYS[provider_id]
    fake.set_password("YouTubeAgent", f"apikey/{provider_id}", key)
    return key


# ---------------------------------------------------------------------------
# Mock HTTP: one handler factory, behaviour selected by ``mode``.
# ---------------------------------------------------------------------------
def _ok_response(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "generativelanguage.googleapis.com":
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "models/gemini-test-a"},
                    {"name": "models/gemini-test-b"},
                    {"name": "models/gemini-test-c"},
                ]
            },
        )
    if host == "api.openai.com":
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-test-a"}, {"id": "gpt-test-b"}]},
        )
    if host == "pixabay.com":
        return httpx.Response(
            200, json={"totalHits": 42, "total": 42, "hits": [{"id": 1}]}
        )
    if host == "api.elevenlabs.io":
        return httpx.Response(
            200,
            json={
                "tier": "creator",
                "character_count": 1000,
                "character_limit": 121000,
                "next_character_count_reset_unix": 1899999999,
            },
        )
    raise AssertionError(f"unexpected host in mock handler: {host}")


def _make_handler(mode: str, calls: list, echo_key: str | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if mode == "ok":
            return _ok_response(request)
        if mode == "401":
            message = "API key not valid"
            if echo_key:
                # Provider echoing the key back: redaction must scrub it.
                message = f"API key not valid: {echo_key}"
            return httpx.Response(401, json={"error": {"message": message}})
        if mode == "429":
            return httpx.Response(
                429, json={"error": {"message": "quota exceeded for today"}}
            )
        if mode == "network":
            raise httpx.ConnectError("connection refused (mock)", request=request)
        if mode == "timeout":
            raise httpx.ReadTimeout("timed out (mock)", request=request)
        raise AssertionError(f"unknown mock mode {mode!r}")

    return handler


def _install_transport(monkeypatch: pytest.MonkeyPatch, mode: str, calls: list,
                       echo_key: str | None = None) -> None:
    transport = httpx.MockTransport(_make_handler(mode, calls, echo_key))
    monkeypatch.setattr(providers_router, "_TEST_TRANSPORT", transport)


# ---------------------------------------------------------------------------
# Router-level tests, parametrized over the four required providers.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("provider_id", PROVIDERS_UNDER_TEST)
def test_not_configured_when_no_key(
    client: TestClient,
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
) -> None:
    calls: list = []
    _install_transport(monkeypatch, "ok", calls)
    resp = client.post(f"/api/v1/providers/{provider_id}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == provider_id
    assert body["status"] == "not_configured"
    assert body["ok"] is False
    assert body["category"] is None
    assert calls == []  # no key -> no provider call at all
    assert "stub" not in resp.text


@pytest.mark.parametrize("provider_id", PROVIDERS_UNDER_TEST)
def test_ok_on_mocked_success(
    client: TestClient,
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
) -> None:
    key = _store_key(fake_keyring, provider_id)
    calls: list = []
    _install_transport(monkeypatch, "ok", calls)
    resp = client.post(f"/api/v1/providers/{provider_id}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["ok"] is True
    assert body["category"] is None
    assert len(calls) == 1  # exactly ONE cheapest call
    assert "stub" not in resp.text
    assert key not in resp.text
    # Minimal success info per provider.
    if provider_id == "gemini":
        assert body["info"]["model_count"] == 3
    elif provider_id == "openai":
        assert body["info"]["model_count"] == 2
    elif provider_id == "pixabay":
        assert body["info"]["total_hits"] == 42
    elif provider_id == "elevenlabs":
        assert body["info"]["tier"] == "creator"
        assert body["info"]["character_limit"] == 121000

    # Read model: only a passed test may mark the provider connected.
    listing = client.get("/api/v1/providers")
    item = next(p for p in listing.json()["items"] if p["id"] == provider_id)
    assert item["configured"] is True
    assert item["connected"] is True
    assert item["status"] == "ok"
    assert item["last_test"]["status"] == "ok"


@pytest.mark.parametrize("provider_id", PROVIDERS_UNDER_TEST)
def test_invalid_key_on_mocked_401(
    client: TestClient,
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
) -> None:
    key = _store_key(fake_keyring, provider_id)
    calls: list = []
    _install_transport(monkeypatch, "401", calls, echo_key=key)
    resp = client.post(f"/api/v1/providers/{provider_id}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert body["ok"] is False
    assert body["category"] == "invalid_key"
    assert body["detail"]  # provider message kept behind detail
    assert key not in resp.text  # redaction: echoed key is scrubbed
    assert "stub" not in resp.text

    # A failed test must NOT mark the provider connected.
    listing = client.get("/api/v1/providers")
    item = next(p for p in listing.json()["items"] if p["id"] == provider_id)
    assert item["connected"] is False
    assert item["status"] == "error"


@pytest.mark.parametrize("provider_id", PROVIDERS_UNDER_TEST)
def test_quota_exhausted_on_mocked_429(
    client: TestClient,
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
) -> None:
    _store_key(fake_keyring, provider_id)
    calls: list = []
    _install_transport(monkeypatch, "429", calls)
    resp = client.post(f"/api/v1/providers/{provider_id}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert body["ok"] is False
    assert body["category"] == "quota_exhausted"
    assert "stub" not in resp.text


@pytest.mark.parametrize("provider_id", PROVIDERS_UNDER_TEST)
def test_network_on_connection_error(
    client: TestClient,
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
) -> None:
    key = _store_key(fake_keyring, provider_id)
    calls: list = []
    _install_transport(monkeypatch, "network", calls)
    resp = client.post(f"/api/v1/providers/{provider_id}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert body["ok"] is False
    assert body["category"] == "network"
    assert key not in resp.text
    assert "stub" not in resp.text


def test_timeout_category_on_read_timeout(
    client: TestClient,
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store_key(fake_keyring, "gemini")
    calls: list = []
    _install_transport(monkeypatch, "timeout", calls)
    resp = client.post("/api/v1/providers/gemini/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert body["category"] == "timeout"


def test_unknown_provider_still_404(client: TestClient) -> None:
    resp = client.post("/api/v1/providers/nope/test")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "provider_not_found"


# ---------------------------------------------------------------------------
# Service-level tests: run_provider_test with an in-memory secret provider.
# ---------------------------------------------------------------------------
class _DictSecrets:
    def __init__(self, values: dict[str, str | None]):
        self._values = values

    async def get_secret(self, ref: str) -> str | None:
        return self._values.get(ref)


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("provider_id", PROVIDERS_UNDER_TEST)
def test_service_not_configured_makes_no_http_call(provider_id: str) -> None:
    calls: list = []
    transport = httpx.MockTransport(_make_handler("ok", calls))
    result = _run(
        run_provider_test(provider_id, _DictSecrets({}), transport=transport)
    )
    assert result["status"] == "not_configured"
    assert result["ok"] is False
    assert calls == []


def test_service_gemini_list_models_plumbing() -> None:
    """The Gemini test goes through GeminiAdapter.list_models (GET /models
    with the key as the x-goog-api-key header, never in the URL)."""
    calls: list = []
    transport = httpx.MockTransport(_make_handler("ok", calls))
    secrets = _DictSecrets({"apikey/gemini": KEYS["gemini"]})
    result = _run(run_provider_test("gemini", secrets, transport=transport))
    assert result["status"] == "ok"
    assert result["info"]["model_count"] == 3
    assert len(calls) == 1
    request = calls[0]
    assert request.url.path.endswith("/models")
    assert request.headers.get("x-goog-api-key") == KEYS["gemini"]
    assert KEYS["gemini"] not in str(request.url)


def test_service_pixabay_sends_key_only_as_query_param() -> None:
    calls: list = []
    transport = httpx.MockTransport(_make_handler("ok", calls))
    secrets = _DictSecrets({"apikey/pixabay": KEYS["pixabay"]})
    result = _run(run_provider_test("pixabay", secrets, transport=transport))
    assert result["status"] == "ok"
    assert result["info"]["total_hits"] == 42
    assert len(calls) == 1
    params = dict(calls[0].url.params)
    assert params.get("key") == KEYS["pixabay"]
    assert params.get("per_page") == "3"  # minimal search, not a burst
