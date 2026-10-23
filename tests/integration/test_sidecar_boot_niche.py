"""Round 2 acceptance tests A + B — real sidecar subprocess.

(A) Boot: the sidecar boots as a subprocess via the documented
    ``YTA_*`` env contract (backend/app/main.py, backend/app/config.py,
    docs/DEV_SETUP.md section 5.1), prints its ``listening`` JSON line,
    binds 127.0.0.1, and both health endpoints behave:
    ``GET /health/live`` is token-exempt and returns ``{"status": "ok"}``;
    ``GET /health/ready`` requires the ``X-Local-Token`` header and is
    green with it.

(B) Persistence: an "Other" niche created through
    ``POST /api/v1/niches`` (schema per backend/app/routers/niches.py:
    ``name`` 2-40 chars, ``idea`` 10-500 chars) returns ``source:
    "other"`` — and is still returned by ``GET /api/v1/niches`` after
    the sidecar is killed and restarted against the same data dir
    (with a fresh token + boot_id, as a real restart mints both), which
    proves the profile lives in the SQLite DB, not process memory.

The shared boot fixture lives in ``tests/integration/conftest.py``
(fixtures ``sidecar`` / ``sidecar_factory``); nothing here talks to the
network beyond 127.0.0.1 and no provider keys are involved.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

NICHE_NAME = "Cricket Analysis"
NICHE_IDEA = (
    "Urdu cricket match stories with player stats, turning points and "
    "scorecard analysis for long faceless videos."
)


# ---------------------------------------------------------------------------
# (A) Boot + health
# ---------------------------------------------------------------------------


def test_sidecar_boots_and_health_endpoints_are_green(sidecar):
    """Sidecar boots via the YTA_* contract; /health/live + /health/ready green."""
    # The subprocess itself is alive and announced itself exactly per
    # contract: one structured listening line carrying this boot's id
    # and the actual (ephemeral) port it bound.
    assert sidecar.is_running
    assert sidecar.listening is not None, "no listening line was parsed"
    assert sidecar.listening.get("event") == "listening"
    assert sidecar.listening.get("boot_id") == sidecar.boot_id
    assert sidecar.listening.get("port") == sidecar.port

    # Loopback only.
    assert sidecar.base_url.startswith("http://127.0.0.1:")

    # Liveness: the ONE token-exempt endpoint; body is exactly
    # {"status": "ok"} (it must not leak version/capability data).
    status, body = sidecar.get("/health/live", token=None)
    assert status == 200, f"GET /health/live -> {status}: {body!r}"
    assert body == {"status": "ok"}

    # Readiness without a token must be rejected (token-required path).
    status, body = sidecar.get("/health/ready", token=None)
    assert status == 401, f"GET /health/ready without token -> {status}: {body!r}"

    # Readiness with the launch token is green, DB included.
    status, body = sidecar.get("/health/ready")
    assert status == 200, f"GET /health/ready -> {status}: {body!r}"
    assert body.get("status") == "ready"
    assert body.get("db") == "ok"


# ---------------------------------------------------------------------------
# (B) Other niche creation + persistence across a restart
# ---------------------------------------------------------------------------


def test_other_niche_created_via_api_persists_across_restart(sidecar_factory, tmp_path):
    """POST an 'Other' niche, restart the sidecar, GET still returns it."""
    data_dir = tmp_path / "persist-data"

    # --- first boot: create the niche --------------------------------
    first = sidecar_factory(data_dir=data_dir)
    first.start().wait_ready()

    status, created = first.post(
        "/api/v1/niches",
        json_body={"name": NICHE_NAME, "idea": NICHE_IDEA},
    )
    assert 200 <= status < 300, f"POST /api/v1/niches -> {status}: {created!r}"
    assert created.get("name") == NICHE_NAME
    assert created.get("source") == "other", (
        f"created niche must have source 'other', got: {created!r}"
    )
    created_id = created.get("id")
    assert created_id, f"created niche has no id: {created!r}"

    # Visible in the list on the same boot, too.
    status, body = first.get("/api/v1/niches")
    assert status == 200, f"GET /api/v1/niches -> {status}: {body!r}"
    assert any(
        item.get("id") == created_id for item in body.get("items", [])
    ), f"created niche missing from list before restart: {body!r}"

    first_token, first_boot_id = first.token, first.boot_id
    first.stop()
    assert not first.is_running

    # --- second boot: same data dir, fresh token + boot_id ------------
    second = sidecar_factory(data_dir=data_dir)
    assert second.token != first_token, "a restart must mint a fresh token"
    assert second.boot_id != first_boot_id, "a restart must mint a fresh boot_id"
    second.start().wait_ready()

    status, body = second.get("/api/v1/niches")
    assert status == 200, f"GET /api/v1/niches after restart -> {status}: {body!r}"
    matches = [
        item
        for item in body.get("items", [])
        if item.get("id") == created_id or item.get("name") == NICHE_NAME
    ]
    assert matches, (
        f"niche {NICHE_NAME!r} (id={created_id}) did not survive the restart; "
        f"list after restart: {body!r}"
    )
    persisted = matches[0]
    assert persisted.get("id") == created_id
    assert persisted.get("name") == NICHE_NAME
    assert persisted.get("source") == "other"

    second.stop()
