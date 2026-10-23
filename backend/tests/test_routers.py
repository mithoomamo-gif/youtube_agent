"""Router tests — self-contained skeleton-surface verification.

Builds a minimal FastAPI app in-process: exception handlers from
app.errors (worker 2) + the aggregate router from app.routers.api.

NOTE — no launch-token middleware is installed here on purpose: the
token middleware is worker 2's module and is mounted by app.main in the
real sidecar; these tests verify router behaviour only, so auth is
bypassed by simply not adding the middleware.

The database is a throwaway SQLite file: YTA_DATA_DIR is pointed at a
temp dir BEFORE any app module is imported, and app.db.init_db (worker
3) initialises the real schema when available.  Routers degrade
gracefully if it is not, which is also exercised implicitly.
"""

from __future__ import annotations

import os
import tempfile

# Must be set before importing app.* so app.db / app.config resolve the
# temp data dir.
os.environ["YTA_DATA_DIR"] = tempfile.mkdtemp(prefix="yta-routers-test-")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import secrets_store
from app.errors import register_exception_handlers
from app.routers import api as api_module

try:  # Initialise the real schema when worker 3's db module is present.
    from app import db as _db_module

    _db_module.init_db()
except Exception:
    pass


def _build_app() -> FastAPI:
    app = FastAPI(title="YouTube Agent — router tests")
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
    return fake


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------


def test_system_info_shape(client: TestClient) -> None:
    resp = client.get("/api/v1/system/info")
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_version"] == 1
    assert body["backend_version"] == "0.1.0"
    assert body["ws_schema"] == 1
    assert body["min_ui_api"] == 1
    assert body["boot_id"]  # present and non-empty
    assert "gates.v1" in body["capabilities"]


def test_system_shutdown(client: TestClient) -> None:
    resp = client.post("/api/v1/system/shutdown")
    assert resp.status_code == 202
    assert resp.json() == {"status": "draining"}


def test_system_capabilities(client: TestClient) -> None:
    resp = client.get("/api/v1/system/capabilities")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["cpu_cores"], int)
    assert body["gpu"] is None


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------


def test_agents_catalogue_has_35(client: TestClient) -> None:
    resp = client.get("/api/v1/agents")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 35
    assert items[0]["id"] == "agent_01"
    assert items[-1]["id"] == "agent_35"
    assert all(a["status"] == "idle" for a in items)


def test_agent_run_unknown_is_404_envelope(client: TestClient) -> None:
    resp = client.get("/api/v1/agents/runs/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "agent_run_not_found"


# ---------------------------------------------------------------------------
# projects + idempotency
# ---------------------------------------------------------------------------


def test_create_project_without_idempotency_key_is_400(
    client: TestClient,
) -> None:
    resp = client.post(
        "/api/v1/projects",
        json={
            "channel_id": "chan-1",
            "niche_profile_id": "facts",
            "topic_mode": "auto",
            "target_minutes": 10,
            "publish_mode": "review_first",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "idempotency_key_required"


def test_create_project_with_key_and_replay(client: TestClient) -> None:
    payload = {
        "channel_id": "chan-1",
        "niche_profile_id": "facts",
        "topic_mode": "manual",
        "topic": "Black holes explained",
        "target_minutes": 12,
        "publish_mode": "review_first",
    }
    headers = {"Idempotency-Key": "test-key-projects-1"}
    resp = client.post("/api/v1/projects", json=payload, headers=headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "draft"
    assert body["topic"] == "Black holes explained"
    assert body["task_dag"]["total_agents"] == 35

    # Same key + same request -> original response replayed, no duplicate.
    replay = client.post("/api/v1/projects", json=payload, headers=headers)
    assert replay.status_code == 201
    assert replay.json()["id"] == body["id"]

    # Same key + different request -> 409 idempotency_conflict.
    changed = dict(payload, topic="A different topic entirely")
    conflict = client.post("/api/v1/projects", json=changed, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_list_projects_shape(client: TestClient) -> None:
    resp = client.get("/api/v1/projects")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["items"], list)
    assert "next_cursor" in body


# ---------------------------------------------------------------------------
# niches (Other path)
# ---------------------------------------------------------------------------


def test_create_niche_short_idea_is_422_envelope(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/niches",
        json={"name": "Space Docs", "idea": "short"},
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"]  # machine-readable code present
    assert error["code"] == "validation_error"


def test_niches_list_has_builtins(client: TestClient) -> None:
    resp = client.get("/api/v1/niches")
    assert resp.status_code == 200
    items = resp.json()["items"]
    builtin_ids = {n["id"] for n in items if n["source"] == "builtin"}
    assert {
        "facts",
        "islamic_stories",
        "horror_stories",
        "history",
        "motivation",
        "tech",
        "crime_stories",
        "health",
    } <= builtin_ids


def test_create_niche_other_persists(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/niches",
        json={
            "name": "Cricket Stories",
            "idea": "Untold dressing-room stories from cricket history",
            "language": "Roman Urdu",
            "tone": "dramatic",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["source"] == "other"
    assert body["status"] == "provisional"
    assert body["name"] == "Cricket Stories"


# ---------------------------------------------------------------------------
# providers + secrets (write-only keys)
# ---------------------------------------------------------------------------


def test_put_provider_key_never_echoes_value(
    client: TestClient, fake_keyring: _FakeKeyring
) -> None:
    secret_value = "test-secret-value-9876wxyz"
    resp = client.put(
        "/api/v1/providers/gemini/key", json={"value": secret_value}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["key_last4"] == "wxyz"
    # The full secret must appear NOWHERE in the response.
    assert secret_value not in resp.text
    # ...but it really was stored in the (fake) credential store.
    assert fake_keyring.get_password("YouTubeAgent", "apikey/gemini") == (
        secret_value
    )

    listing = client.get("/api/v1/providers")
    assert listing.status_code == 200
    gemini = next(
        p for p in listing.json()["items"] if p["id"] == "gemini"
    )
    assert gemini["configured"] is True
    assert gemini["key_last4"] == "wxyz"
    assert secret_value not in listing.text


def test_put_provider_key_unknown_provider_404(
    client: TestClient, fake_keyring: _FakeKeyring
) -> None:
    resp = client.put(
        "/api/v1/providers/nope/key", json={"value": "whatever-1234"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "provider_not_found"


def test_provider_test_without_key_is_not_configured(
    client: TestClient,
) -> None:
    # Real §28 behaviour (the Round-1 stub is gone): with no key stored
    # the endpoint answers 200 + status "not_configured" — NOT ok, NOT
    # an error, and never the old stub detail string.
    resp = client.post("/api/v1/providers/gemini/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "not_configured"
    assert body["ok"] is False
    assert "stub" not in resp.text


# ---------------------------------------------------------------------------
# settings (secrets are not settings)
# ---------------------------------------------------------------------------


def test_put_settings_secret_key_is_422(client: TestClient) -> None:
    resp = client.put(
        "/api/v1/settings", json={"values": {"api_key": "should-not-store"}}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "secret_in_settings"


def test_settings_roundtrip(client: TestClient) -> None:
    resp = client.put(
        "/api/v1/settings", json={"values": {"render_preset": "draft720"}}
    )
    assert resp.status_code == 200
    got = client.get("/api/v1/settings")
    assert got.status_code == 200
    assert got.json()["render_preset"] == "draft720"


# ---------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------


def test_approval_second_decision_is_409(client: TestClient) -> None:
    first = client.post(
        "/api/v1/approvals/appr-1/approve",
        json={},
        headers={"Idempotency-Key": "appr-key-1"},
    )
    assert first.status_code == 200
    assert first.json()["decision"] == "approved"

    second = client.post(
        "/api/v1/approvals/appr-1/approve",
        json={},
        headers={"Idempotency-Key": "appr-key-2"},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "already_decided"


def test_reject_without_reason_code_is_422(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/approvals/appr-2/reject",
        json={},
        headers={"Idempotency-Key": "appr-key-3"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "reason_code_required"


# ---------------------------------------------------------------------------
# remaining list surfaces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/channels",
        "/api/v1/jobs",
        "/api/v1/schedules",
        "/api/v1/publications",
        "/api/v1/logs",
        "/api/v1/usage",
    ],
)
def test_list_endpoints_return_items_shape(client: TestClient, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["items"], list)
    assert "next_cursor" in body


def test_queue_stats_and_snapshot(client: TestClient) -> None:
    stats = client.get("/api/v1/queue/stats")
    assert stats.status_code == 200
    assert "by_status" in stats.json()
    snapshot = client.get("/api/v1/queue/snapshot")
    assert snapshot.status_code == 200
    assert snapshot.json()["today_cost_usd"] == 0.0


def test_usage_today_shape(client: TestClient) -> None:
    resp = client.get("/api/v1/usage/today")
    assert resp.status_code == 200
    body = resp.json()
    assert "date" in body
    assert isinstance(body["totals"], dict)
