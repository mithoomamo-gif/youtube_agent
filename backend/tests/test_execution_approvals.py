"""Approvals persistence tests (Round 2, migration 0002).

Covers:
(a) migration 0002 file sanity + a real alembic upgrade/downgrade
    against a temp database (approvals table + provider_credentials
    last4 column appear, then disappear again on downgrade),
(b) a decision on a pending approval survives a "restart" (the
    approvals module is reloaded, wiping its in-memory decision log,
    and a second app is built) — a second decision is a 409
    ``already_decided`` served from the DB row,
(c) reject without ``reason_code`` is still a 422,
(d) the list endpoint returns pending + decided rows with
    ``artefact_versions`` parsed from JSON,
plus the insert-with-body-fields path, the legacy bare-id in-memory
fallback, and provider last4 persistence.

App construction + Idempotency-Key header patterns follow
tests/test_routers.py; the DB is the shared session test DB resolved
through app.db (the same resolver the routers use).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
import time
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db as db_module
from app import secrets_store
from app.errors import register_exception_handlers
from app.routers import api as api_module
from app.routers import approvals as approvals_module
from app.routers import providers as providers_module

BACKEND_DIR = Path(__file__).resolve().parent.parent
NOW_MS = 1_700_000_000_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _build_app() -> FastAPI:
    app = FastAPI(title="YouTube Agent — approvals persistence tests")
    register_exception_handlers(app)
    app.include_router(api_module.router)
    return app


def _client() -> TestClient:
    return TestClient(_build_app())


def _init_db() -> None:
    conn = db_module.init_db()
    conn.close()


def _seed_project() -> str:
    """Insert niche + channel + project (FK parents of approvals)."""
    suffix = uuid.uuid4().hex[:12]
    niche_id = f"niche-{suffix}"
    channel_id = f"chan-{suffix}"
    project_id = f"proj-{suffix}"
    conn = db_module.get_connection()
    try:
        conn.execute(
            "INSERT INTO niche_profiles (id, key, label, source, "
            "created_at, updated_at) VALUES (?, ?, 'Exec Niche', "
            "'builtin', ?, ?)",
            (niche_id, f"exec-{suffix}", NOW_MS, NOW_MS),
        )
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title, "
            "language, created_at, updated_at) VALUES (?, ?, "
            "'Exec Channel', 'en', ?, ?)",
            (channel_id, f"UC{suffix}", NOW_MS, NOW_MS),
        )
        conn.execute(
            "INSERT INTO projects (id, channel_id, niche_profile_id, "
            "topic, topic_source, created_at, updated_at) VALUES "
            "(?, ?, ?, 'An exec approvals topic', 'user', ?, ?)",
            (project_id, channel_id, niche_id, NOW_MS, NOW_MS),
        )
        try:
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()
    return project_id


def _seed_pending(
    approval_id: str,
    project_id: str,
    gate: str = "a",
    artefact_versions: dict | None = None,
) -> None:
    conn = db_module.get_connection()
    try:
        approvals_module._ensure_approvals_table(conn)
        conn.execute(
            "INSERT INTO approvals (id, project_id, gate, "
            "artefact_versions_json, decision, reason_code, note, "
            "decided_by, status, created_at, decided_at) VALUES "
            "(?, ?, ?, ?, NULL, NULL, NULL, NULL, 'pending', ?, NULL)",
            (
                approval_id,
                project_id,
                gate,
                json.dumps(artefact_versions)
                if artefact_versions is not None
                else None,
                _now_ms(),
            ),
        )
        try:
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()


def _fetch_approval(approval_id: str) -> dict | None:
    conn = db_module.get_connection()
    try:
        approvals_module._ensure_approvals_table(conn)
        row = conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _key() -> dict[str, str]:
    return {"Idempotency-Key": f"exec-key-{uuid.uuid4().hex}"}


# ---------------------------------------------------------------------------
# (a) migration 0002
# ---------------------------------------------------------------------------


def _load_migration_0002():
    path = BACKEND_DIR / "migrations" / "versions" / "0002_approvals.py"
    spec = importlib.util.spec_from_file_location("migration_0002", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0002_metadata() -> None:
    module = _load_migration_0002()
    assert module.revision == "0002"
    assert module.down_revision == "0001"
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_migration_0002_upgrade_and_downgrade(tmp_path, monkeypatch) -> None:
    from alembic import command
    from alembic.config import Config

    db_file = tmp_path / "migrated.db"
    monkeypatch.setenv("YTA_DB_URL", f"sqlite:///{db_file}")
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))

    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(db_file))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "approvals" in tables
        approval_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(approvals)")
        }
        assert approval_cols == {
            "id",
            "project_id",
            "gate",
            "artefact_versions_json",
            "decision",
            "reason_code",
            "note",
            "decided_by",
            "status",
            "created_at",
            "decided_at",
        }
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "idx_approvals_project_gate" in indexes
        assert "idx_approvals_status" in indexes
        credential_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(provider_credentials)")
        }
        assert "last4" in credential_cols
        version = conn.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        # Head is the newest migration: 0011 (Round 5 chain after
        # 0004 Style Lab: 0005 learning_rules, 0006 comments,
        # 0007 analytics, 0008 channel_quality_tier,
        # 0009 calibration_log, 0010 reports, 0011 compliance);
        # this test's subject is 0002's own up/down path.
        assert version == "0011"
    finally:
        conn.close()

    # Downgrade back to 0001: approvals table and last4 column go away,
    # the base provider_credentials table stays.
    command.downgrade(cfg, "0001")
    conn = sqlite3.connect(str(db_file))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "approvals" not in tables
        assert "provider_credentials" in tables
        credential_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(provider_credentials)")
        }
        assert "last4" not in credential_cols
    finally:
        conn.close()

    # And upgrading again is clean (guarded ADD COLUMN path).
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(str(db_file))
    try:
        credential_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(provider_credentials)")
        }
        assert "last4" in credential_cols
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# (b) decision persistence across a restart
# ---------------------------------------------------------------------------


def test_decision_persists_across_restart() -> None:
    _init_db()
    project_id = _seed_project()
    approval_id = f"exec-appr-{uuid.uuid4().hex[:12]}"
    _seed_pending(approval_id, project_id, gate="a")

    client = _client()
    first = client.post(
        f"/api/v1/approvals/{approval_id}/approve", json={}, headers=_key()
    )
    assert first.status_code == 200
    body = first.json()
    assert body["decision"] == "approved"
    assert body["status"] == "decided"
    assert body["decided_at"].endswith("Z")

    # The decision really landed in the DB row (epoch-ms timestamp).
    row = _fetch_approval(approval_id)
    assert row is not None
    assert row["status"] == "decided"
    assert row["decision"] == "approved"
    assert row["decided_by"] == "user"
    assert isinstance(row["decided_at"], int)

    # Simulate a restart: reload the approvals module (fresh in-memory
    # decision log) and build a second app/client around the reloaded
    # router.  Only the DB can remember the decision now.
    reloaded = importlib.reload(approvals_module)
    assert approval_id not in reloaded._DECISIONS
    app2 = FastAPI(title="YouTube Agent — approvals after restart")
    register_exception_handlers(app2)
    app2.include_router(reloaded.router)
    client2 = TestClient(app2)

    second = client2.post(
        f"/api/v1/approvals/{approval_id}/approve", json={}, headers=_key()
    )
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "already_decided"
    assert error["details"]["decision"]["decision"] == "approved"
    assert error["details"]["decision"]["id"] == approval_id


# ---------------------------------------------------------------------------
# (c) reject without reason_code
# ---------------------------------------------------------------------------


def test_reject_without_reason_code_is_422_and_stays_pending() -> None:
    _init_db()
    project_id = _seed_project()
    approval_id = f"exec-appr-{uuid.uuid4().hex[:12]}"
    _seed_pending(approval_id, project_id, gate="b")

    client = _client()
    resp = client.post(
        f"/api/v1/approvals/{approval_id}/reject", json={}, headers=_key()
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "reason_code_required"

    row = _fetch_approval(approval_id)
    assert row is not None
    assert row["status"] == "pending"
    assert row["decision"] is None


# ---------------------------------------------------------------------------
# (d) list endpoint: pending + decided rows
# ---------------------------------------------------------------------------


def test_list_returns_pending_and_decided_rows() -> None:
    _init_db()
    project_id = _seed_project()
    pending_id = f"exec-appr-{uuid.uuid4().hex[:12]}"
    decided_id = f"exec-appr-{uuid.uuid4().hex[:12]}"
    versions = {"script": 3, "outline": 1}
    _seed_pending(pending_id, project_id, gate="a", artefact_versions=versions)
    _seed_pending(decided_id, project_id, gate="b")

    client = _client()
    decided = client.post(
        f"/api/v1/approvals/{decided_id}/reject",
        json={"reason_code": "needs_work", "note": "Tighten the hook"},
        headers=_key(),
    )
    assert decided.status_code == 200
    assert decided.json()["decision"] == "rejected"
    assert decided.json()["reason_code"] == "needs_work"

    listing = client.get("/api/v1/approvals")
    assert listing.status_code == 200
    items = {item["id"]: item for item in listing.json()["items"]}

    pending = items[pending_id]
    assert pending["project_id"] == project_id
    assert pending["gate"] == "a"
    assert pending["status"] == "pending"
    assert pending["decision"] is None
    assert pending["artefact_versions"] == versions
    assert pending["decided_at"] is None

    done = items[decided_id]
    assert done["project_id"] == project_id
    assert done["gate"] == "b"
    assert done["status"] == "decided"
    assert done["decision"] == "rejected"
    assert done["reason_code"] == "needs_work"
    assert done["note"] == "Tighten the hook"
    assert done["decided_by"] == "user"
    assert isinstance(done["decided_at"], int)


# ---------------------------------------------------------------------------
# insert-with-body-fields path + legacy bare-id fallback
# ---------------------------------------------------------------------------


def test_decide_inserts_row_when_body_carries_project_and_gate() -> None:
    _init_db()
    project_id = _seed_project()
    approval_id = f"exec-appr-{uuid.uuid4().hex[:12]}"
    assert _fetch_approval(approval_id) is None

    client = _client()
    resp = client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        json={
            "project_id": project_id,
            "gate": "b",
            "decided_by": "reviewer-x",
        },
        headers=_key(),
    )
    assert resp.status_code == 200

    row = _fetch_approval(approval_id)
    assert row is not None
    assert row["project_id"] == project_id
    assert row["gate"] == "b"
    assert row["status"] == "decided"
    assert row["decision"] == "approved"
    assert row["decided_by"] == "reviewer-x"


def test_bare_id_uses_in_memory_fallback_only() -> None:
    _init_db()
    approval_id = f"exec-bare-{uuid.uuid4().hex[:12]}"

    client = _client()
    first = client.post(
        f"/api/v1/approvals/{approval_id}/approve", json={}, headers=_key()
    )
    assert first.status_code == 200
    # No pending row and no project_id/gate in the body -> no DB row
    # (project_id is NOT NULL/FK), the in-memory log carries it.
    assert _fetch_approval(approval_id) is None

    second = client.post(
        f"/api/v1/approvals/{approval_id}/approve", json={}, headers=_key()
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "already_decided"


# ---------------------------------------------------------------------------
# gates_bridge resume trigger (skipped when the bridge has not landed)
# ---------------------------------------------------------------------------


def test_decision_triggers_gates_bridge_resume() -> None:
    pytest.importorskip("app.execution.gates_bridge")
    _init_db()
    project_id = _seed_project()
    approval_id = f"exec-appr-{uuid.uuid4().hex[:12]}"
    job_id = f"job-{uuid.uuid4().hex[:12]}"
    _seed_pending(approval_id, project_id, gate="a")

    conn = db_module.get_connection()
    try:
        conn.execute(
            "INSERT INTO jobs (id, project_id, agent_key, kind, "
            "payload_json, status, run_after, created_at, updated_at) "
            "VALUES (?, ?, 'gate_a', 'gate.a', ?, 'queued', ?, ?, ?)",
            (
                job_id,
                project_id,
                json.dumps({"project_id": project_id, "stage": "script"}),
                _now_ms() + 60_000,
                NOW_MS,
                NOW_MS,
            ),
        )
        try:
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()

    client = _client()
    resp = client.post(
        f"/api/v1/approvals/{approval_id}/approve", json={}, headers=_key()
    )
    assert resp.status_code == 200

    # The bridge flipped the waiting gate job: approval merged into its
    # payload and run_after zeroed so the runner picks it up at once.
    conn = db_module.get_connection()
    try:
        row = conn.execute(
            "SELECT payload_json, run_after FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    payload = json.loads(row[0])
    assert payload["approval"]["decision"] == "approved"
    assert payload["approval"]["approval_id"] == approval_id
    assert row[1] == 0


# ---------------------------------------------------------------------------
# provider last4 persistence (migration 0002 column)
# ---------------------------------------------------------------------------


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


def test_provider_last4_persisted_to_db(monkeypatch: pytest.MonkeyPatch) -> None:
    _init_db()
    # Bring the shared session DB up to migration 0002 for this column.
    conn = db_module.get_connection()
    try:
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(provider_credentials)")
        }
        if "last4" not in cols:
            conn.execute(
                "ALTER TABLE provider_credentials ADD COLUMN last4 TEXT NULL"
            )
            try:
                conn.commit()
            except Exception:
                pass
    finally:
        conn.close()

    fake = _FakeKeyring()
    monkeypatch.setattr(secrets_store, "keyring", fake)

    client = _client()
    secret_value = "exec-secret-value-5555beef"
    resp = client.put(
        "/api/v1/providers/openai/key", json={"value": secret_value}
    )
    assert resp.status_code == 200
    assert resp.json()["key_last4"] == "beef"
    assert secret_value not in resp.text

    # The hint is in the DB column, not only in process memory: clear
    # the in-process cache and the listing must still serve it.
    providers_module._LAST4.pop("openai", None)
    conn = db_module.get_connection()
    try:
        row = conn.execute(
            "SELECT last4 FROM provider_credentials "
            "WHERE provider = 'openai' AND credential_kind = 'api_key'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "beef"

    listing = client.get("/api/v1/providers")
    openai = next(p for p in listing.json()["items"] if p["id"] == "openai")
    assert openai["configured"] is True
    assert openai["key_last4"] == "beef"
