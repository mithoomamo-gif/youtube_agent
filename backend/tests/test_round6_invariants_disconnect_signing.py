"""ROUND 6 — Invariant 5 + Invariant 3 proof pack.

Invariant 5: *disconnect = revoke remotely first, delete locally
second.*

* The primitive is PROVEN: ``TokenManager.disconnect``
  (``app.providers.youtube.tokens``) attempts Google's revoke
  endpoint BEFORE deleting the local credential blob — ordering is
  asserted with a recording store + mock transport.
* The guarantee is WEAKER than the invariant's strict reading
  (characterisation ``__gap_r6_f3``): local deletion is unconditional
  (``finally``) — it happens even when the revoke call fails. That is
  a deliberate, code-documented product decision ("the user asked to
  disconnect this machine"), but it is NOT "delete only after a
  successful revoke".
* The API layer is CLOSED (R6-F2, closure pass): ``DELETE
  /api/v1/channels/{channel_id}`` now invokes
  ``TokenManager.disconnect`` for the channel's stored credential
  (revoke at Google first, local secret deletion second) BEFORE
  soft-marking the channel row ``disconnected``. A recording-store +
  mock-transport test proves the ordering end-to-end through the
  route users actually invoke, that the stored secret is gone after
  the DELETE, and that a channel with no stored credential still
  disconnects gracefully (no HTTP, no error).

Invariant 3: *no unsigned code executes* (installer / sidecar /
updates). Per the Round-6 brief this is the CI/signing domain: these
tests verify the configuration POINTS exist and pin their current
state; they do not (and cannot, from this VM) implement signing.
The remaining gap is documented in ``docs/ROUND6_INVARIANTS.md``
(R6-F6): the Tauri updater ``pubkey`` in ``tauri.conf.json`` is still
empty, so update-signature verification is not yet functional even
though every other config point (endpoints, externalBin sidecar,
PyInstaller spec, CI signing-by-secret-name) is in place.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sqlite3
import time
from pathlib import Path

import httpx
import pytest

from app.providers.youtube import InMemorySecretStore, TokenManager, TokenResponse
from app.providers.youtube.oauth import CONNECT_SCOPES, REVOKE_URL

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_ID = "UCr6channel00000000000"
REF = f"youtube:channel:{CHANNEL_ID}"


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# (d) TokenManager.disconnect — order + semantics
# ---------------------------------------------------------------------------


class RecordingStore(InMemorySecretStore):
    def __init__(self, events: list) -> None:
        super().__init__()
        self._events = events

    def delete(self, ref: str) -> None:
        self._events.append(("delete", ref))
        super().delete(ref)


def _manager(events: list, handler) -> tuple[TokenManager, RecordingStore]:
    store = RecordingStore(events)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tm = TokenManager(
        store, client_id="r6-client-id", client_secret="r6-client-secret",
        http=http,
    )
    tm.store_token_response(
        CHANNEL_ID,
        TokenResponse(
            access_token="seed-access-token",
            refresh_token="seed-refresh-token",
            expires_in=3600,
            scope=" ".join(CONNECT_SCOPES),
        ),
    )
    return tm, store


def test_disconnect_revokes_remotely_before_deleting_locally():
    events: list = []
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        events.append(("revoke", str(request.url)))
        seen["url"] = str(request.url)
        return httpx.Response(200)

    tm, store = _manager(events, handler)
    run(tm.disconnect(CHANNEL_ID))
    assert seen["url"] == REVOKE_URL
    assert [kind for kind, _ in events] == ["revoke", "delete"]
    assert events[1] == ("delete", REF)
    assert store.get(REF) is None
    assert tm.get_status(CHANNEL_ID).status == "not_connected"


def test_disconnect_deletes_locally_even_when_revoke_fails__gap_r6_f3():
    """GAP R6-F3 (characterisation): when the revoke call FAILS the
    local blob is still deleted — deletion is unconditional
    (``finally`` in ``TokenManager.disconnect``), not gated on revoke
    success as the strict invariant reading requires. The order
    (revoke attempted first) does hold. If the semantics are ever
    tightened to delete-only-after-successful-revoke, flip this test
    and close the finding in docs/ROUND6_INVARIANTS.md."""
    events: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append(("revoke", str(request.url)))
        raise httpx.ConnectError("network down", request=request)

    tm, store = _manager(events, handler)
    run(tm.disconnect(CHANNEL_ID))  # must not raise
    assert [kind for kind, _ in events] == ["revoke", "delete"]
    assert store.get(REF) is None


def test_disconnect_deletes_locally_on_revoke_http_500__gap_r6_f3():
    events: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append(("revoke", str(request.url)))
        return httpx.Response(500)

    tm, store = _manager(events, handler)
    run(tm.disconnect(CHANNEL_ID))
    assert [kind for kind, _ in events] == ["revoke", "delete"]
    assert store.get(REF) is None


def test_disconnect_without_stored_grant_is_a_noop():
    events: list = []
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    tm, _store = _manager(events, handler)
    run(tm.disconnect("UCnever-connected-000000"))
    assert calls == []  # nothing to revoke -> no HTTP at all


# ---------------------------------------------------------------------------
# (d) The route users actually invoke — R6-F2 closure proof
# ---------------------------------------------------------------------------


def _seed_channel_row(
    settings, channel_pk: str, youtube_channel_id: str = CHANNEL_ID
) -> None:
    now_ms = int(time.time() * 1000)
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title,"
            " language, status, created_at, updated_at)"
            " VALUES (?, ?, ?, 'en', 'active', ?, ?)",
            (channel_pk, youtube_channel_id, "R6 Channel", now_ms, now_ms),
        )
        conn.commit()
    finally:
        conn.close()


def _read_channel_row(settings, channel_pk: str):
    conn = sqlite3.connect(settings.db_path)
    try:
        return conn.execute(
            "SELECT status, deleted_at FROM channels WHERE id = ?",
            (channel_pk,),
        ).fetchone()
    finally:
        conn.close()


def test_router_disconnect_revokes_then_deletes_secret_and_soft_deletes(
    app, client, settings
):
    """R6-F2 (CLOSED): ``DELETE /api/v1/channels/{id}`` invokes
    ``TokenManager.disconnect`` for the channel's stored credential:
    the revoke call at Google is recorded BEFORE the local secret
    deletion (single ordered event log), the stored secret is gone
    after the DELETE, and the channel row is soft-deleted exactly as
    before (status ``disconnected`` + ``deleted_at`` set)."""
    from app.routers import channels as channels_router

    events: list = []

    class _RouteRecordingStore(InMemorySecretStore):
        def delete(self, ref: str) -> None:
            events.append(("delete", ref))
            super().delete(ref)

    store = _RouteRecordingStore()
    store.put(channels_router.CLIENT_ID_REF, "r6-client-id")
    store.put(channels_router.CLIENT_SECRET_REF, "r6-client-secret")
    seeder = TokenManager(
        store, client_id="r6-client-id", client_secret="r6-client-secret"
    )
    seeder.store_token_response(
        CHANNEL_ID,
        TokenResponse(
            access_token="seed-access-token",
            refresh_token="seed-refresh-token",
            expires_in=3600,
            scope=" ".join(CONNECT_SCOPES),
        ),
    )
    assert store.get(REF) is not None
    events.clear()  # seeding performs no delete; be explicit anyway

    def handler(request: httpx.Request) -> httpx.Response:
        events.append(("revoke", str(request.url)))
        return httpx.Response(200)

    app.state.youtube_secret_store = store
    app.state.youtube_http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )

    channel_pk = "r6-channel-pk"
    _seed_channel_row(settings, channel_pk)

    resp = client.delete(f"/api/v1/channels/{channel_pk}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "disconnected"

    # Revoke at Google FIRST, local secret deletion SECOND.
    assert events == [("revoke", REVOKE_URL), ("delete", REF)]
    # The stored credential is gone after the DELETE.
    assert store.get(REF) is None

    # The channel row is soft-deleted as before.
    row = _read_channel_row(settings, channel_pk)
    assert row is not None and row[0] == "disconnected" and row[1] is not None


def test_router_disconnect_without_stored_credential_is_graceful(
    app, client, settings
):
    """R6-F2 graceful case: a channel with NO stored credential still
    disconnects — the primitive is a no-op (no revoke HTTP at all)
    and the row is soft-deleted; the route must not 500."""
    from app.routers import channels as channels_router

    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    store = InMemorySecretStore()
    store.put(channels_router.CLIENT_ID_REF, "r6-client-id")
    store.put(channels_router.CLIENT_SECRET_REF, "r6-client-secret")
    app.state.youtube_secret_store = store
    app.state.youtube_http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )

    channel_pk = "r6-channel-pk-nocred"
    _seed_channel_row(
        settings, channel_pk, youtube_channel_id="UCr6nocred00000000000"
    )

    resp = client.delete(f"/api/v1/channels/{channel_pk}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "disconnected"
    assert calls == []  # nothing stored -> nothing to revoke -> no HTTP
    row = _read_channel_row(settings, channel_pk)
    assert row is not None and row[0] == "disconnected" and row[1] is not None


def test_token_manager_disconnect_has_a_route_caller():
    """Source-level pin for the R6-F2 closure: the channels router
    (the route users actually invoke) calls ``.disconnect(`` — the
    primitive is no longer orphaned."""
    app_root = REPO_ROOT / "backend" / "app"
    callers = []
    for path in app_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if ".disconnect(" in line and "def disconnect" not in line:
                callers.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}:{lineno}"
                )
    assert any(
        c.startswith("backend/app/routers/channels.py") for c in callers
    ), f"channels router no longer calls disconnect(): {callers}"


# ---------------------------------------------------------------------------
# (e) Invariant 3 — signing / updater configuration points
# ---------------------------------------------------------------------------


def _tauri_conf() -> dict:
    return json.loads(
        (REPO_ROOT / "src-tauri" / "tauri.conf.json").read_text(
            encoding="utf-8"
        )
    )


def test_tauri_updater_and_bundle_config_points_exist():
    conf = _tauri_conf()
    assert conf["bundle"]["active"] is True
    # The Python sidecar ships as a Tauri externalBin.
    assert conf["bundle"]["externalBin"] == ["binaries/agent-backend"]
    updater = conf["plugins"]["updater"]
    endpoints = updater["endpoints"]
    assert isinstance(endpoints, list) and len(endpoints) >= 1
    assert all(e.startswith("https://") for e in endpoints)
    assert "pubkey" in updater, "updater pubkey config point must exist"
    assert updater["windows"]["installMode"] == "passive"


def test_updater_pubkey_state_is_empty_or_a_valid_minisign_key():
    """R6-F6 snapshot pin: at proof time ``pubkey`` is ``""`` (empty —
    the updater cannot verify signatures yet). This test accepts
    either the documented empty state or a real base64 minisign key,
    and fails on anything in between (a malformed non-empty value
    would silently break update verification at runtime)."""
    pubkey = _tauri_conf()["plugins"]["updater"]["pubkey"]
    if pubkey == "":
        return  # known gap R6-F6, documented in docs/ROUND6_INVARIANTS.md
    base64.b64decode(pubkey, validate=True)  # must at least be base64
    assert len(pubkey) >= 40


def test_cargo_declares_updater_and_single_instance_plugins():
    cargo = (REPO_ROOT / "src-tauri" / "Cargo.toml").read_text(
        encoding="utf-8"
    )
    assert "tauri-plugin-updater" in cargo
    assert "tauri-plugin-single-instance" in cargo
    assert "tauri-plugin-process" in cargo


def test_release_workflow_signs_via_ci_secrets_by_name_only():
    workflow = REPO_ROOT / ".github" / "workflows" / "release.yml"
    assert workflow.exists()
    text = workflow.read_text(encoding="utf-8", errors="ignore")
    # Signing identities are referenced as CI secrets, never values.
    assert "secrets.TAURI_SIGNING_PRIVATE_KEY" in text
    assert "signtool verify" in text
    assert "PRIVATE KEY-----" not in text
    assert not re.search(
        r"TAURI_SIGNING_PRIVATE_KEY\s*[:=]\s*[A-Za-z0-9+/=]{20,}", text
    ), "a Tauri signing key VALUE must never appear in the workflow"


def test_updater_pubkey_file_when_present_is_minisign_public_material():
    key_file = REPO_ROOT / "src-tauri" / "keys" / "updater-pubkey.txt"
    if not key_file.exists():
        pytest.skip("updater-pubkey.txt not present in this tree state")
    raw = base64.b64decode(key_file.read_text(encoding="utf-8").strip())
    assert b"untrusted comment" in raw  # minisign public-key container


def test_pyinstaller_spec_for_sidecar_encodes_onedir_contract():
    """Sidecar packaging config point (landed by the Round-6 packaging
    team while this proof pack was being written): the PyInstaller
    spec exists and encodes the documented contract — entry point is
    the real sidecar main, ``--onedir`` layout (EXE with
    ``exclude_binaries`` + ``COLLECT``), ``console=True`` for the
    listening-line handshake. Signing itself happens in CI (see the
    release-workflow test above); the spec is the config point this
    invariant can verify from the repo."""
    spec_path = REPO_ROOT / "packaging" / "yta_sidecar.spec"
    assert spec_path.exists(), (
        "packaging/yta_sidecar.spec missing — the sidecar packaging "
        "config point regressed (see R6-F6 in docs/ROUND6_INVARIANTS.md)"
    )
    spec = spec_path.read_text(encoding="utf-8", errors="ignore")
    assert "main.py" in spec  # entry: backend/app/main.py (app.main:run)
    assert "exclude_binaries=True" in spec  # --onedir, not --onefile
    assert "COLLECT(" in spec
    assert "console=True" in spec
