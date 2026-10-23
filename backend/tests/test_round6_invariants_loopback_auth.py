"""ROUND 6 — Invariant 2 proof pack: no unauthenticated request reaches
any sidecar endpoint, including loopback.

The sidecar binds 127.0.0.1 only, and ``LaunchTokenMiddleware``
(``app.middleware``) requires the per-launch ``X-Local-Token`` on every
HTTP path. This module *enumerates the live OpenAPI surface from the
app itself* (so routes added later are swept automatically) and
asserts, for EVERY operation:

* no token header        -> 401, error code ``missing_token``
* a wrong token          -> 401, error code ``invalid_token``

The single legitimate exemption is ``GET /health/live`` (the shell
polls liveness before the UI — and the launch token — exists; it
returns exactly ``{"status": "ok"}`` and nothing else). The exemption
set is pinned by test: any future addition to
``app.middleware.EXEMPT_PATHS`` turns this suite red until it is
justified in ``docs/ROUND6_INVARIANTS.md``. Undocumented exceptions =
FAIL, by design.

Also covered: FastAPI's framework endpoints (``/openapi.json``,
``/docs``, ``/redoc``) are behind the same token gate, unknown paths
fail closed (401 before routing), and the WebSocket endpoint rejects
hello frames without a valid single-use ticket (close code 4401) —
WS scope bypasses the HTTP middleware, so its ticket handshake is the
auth boundary and is proven separately.

Rate-limit note: the middleware turns >10 failed token checks in 60 s
into 429s. The sweep therefore probes in chunks of 5 operations
(5 missing + 5 invalid = 10 failures) per freshly built app, so a 401
can never be masked by the limiter.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

CHUNK_OPS = 5  # 5 ops x (missing + invalid) = 10 failures < 429 threshold
SAMPLE_ID = "r6probe"
INVALID_TOKEN = "r6-invalid-token-0000000000"


def _operations(app) -> list[tuple[str, str]]:
    """Sorted ``(METHOD, path)`` list from the app's own OpenAPI spec."""
    spec = app.openapi()
    ops: list[tuple[str, str]] = []
    for path, methods in spec["paths"].items():
        for method in methods:
            if method.lower() in ("get", "post", "put", "patch", "delete"):
                ops.append((method.upper(), path))
    return sorted(ops)


def _instantiate(path: str) -> str:
    return re.sub(r"\{[^}]+\}", SAMPLE_ID, path)


def _fresh_client(settings) -> TestClient:
    from app.main import create_app

    app = create_app(settings)
    return TestClient(app, base_url="http://127.0.0.1")


def _error_code(resp) -> str | None:
    try:
        return resp.json().get("error", {}).get("code")
    except Exception:
        return None


def test_every_openapi_operation_rejects_missing_and_invalid_token(settings):
    """The full sweep: every operation, both failure modes, 401."""
    from app.middleware import EXEMPT_PATHS

    from app.main import create_app

    ops = _operations(create_app(settings))
    assert len(ops) >= 100, (
        f"expected the full sidecar surface (>=100 operations), got "
        f"{len(ops)} — route registration may have regressed"
    )

    failures: list[str] = []
    checked = 0
    for start in range(0, len(ops), CHUNK_OPS):
        chunk = ops[start : start + CHUNK_OPS]
        with _fresh_client(settings) as client:
            for method, path in chunk:
                url = _instantiate(path)
                kwargs = (
                    {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
                )
                if path in EXEMPT_PATHS:
                    # The sole exemption must still answer (200) and
                    # must never require — or leak — anything else.
                    resp = client.request(method, url, **kwargs)
                    if resp.status_code != 200:
                        failures.append(
                            f"{method} {path}: exempt path returned "
                            f"{resp.status_code}, expected 200"
                        )
                    checked += 1
                    continue
                missing = client.request(method, url, **kwargs)
                checked += 1
                if not (
                    missing.status_code == 401
                    and _error_code(missing) == "missing_token"
                ):
                    failures.append(
                        f"{method} {path}: no-token -> "
                        f"{missing.status_code} {_error_code(missing)!r}, "
                        "expected 401 missing_token"
                    )
                    continue
                invalid = client.request(
                    method,
                    url,
                    headers={"X-Local-Token": INVALID_TOKEN},
                    **kwargs,
                )
                if not (
                    invalid.status_code == 401
                    and _error_code(invalid) == "invalid_token"
                ):
                    failures.append(
                        f"{method} {path}: bad-token -> "
                        f"{invalid.status_code} {_error_code(invalid)!r}, "
                        "expected 401 invalid_token"
                    )
    assert checked == len(ops)
    assert failures == [], (
        "operations that did not reject unauthenticated requests:\n"
        + "\n".join(failures)
    )


def test_health_live_is_the_sole_documented_exemption(settings):
    """Pin the exemption set: exactly {/health/live}, returning only ok."""
    from app.middleware import EXEMPT_PATHS

    assert EXEMPT_PATHS == frozenset({"/health/live"}), (
        f"EXEMPT_PATHS changed to {sorted(EXEMPT_PATHS)} — every "
        "exemption must be individually justified in "
        "docs/ROUND6_INVARIANTS.md before this pin is updated"
    )
    with _fresh_client(settings) as client:
        resp = client.get("/health/live")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_framework_docs_endpoints_also_require_token(settings):
    """/openapi.json, /docs, /redoc are NOT side doors: same 401 gate."""
    with _fresh_client(settings) as client:
        for path in ("/openapi.json", "/docs", "/redoc",
                     "/docs/oauth2-redirect"):
            resp = client.get(path)
            assert resp.status_code == 401, (path, resp.status_code)
            assert resp.json()["error"]["code"] == "missing_token"


def test_unknown_paths_fail_closed(settings):
    """Even a path that does not exist is rejected before routing."""
    with _fresh_client(settings) as client:
        resp = client.get("/api/v1/definitely-not-a-route")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "missing_token"


def test_valid_token_is_accepted(settings):
    """Control: the gate is a gate, not a wall — the real token works."""
    with _fresh_client(settings) as client:
        client.headers.update({"X-Local-Token": settings.token})
        ready = client.get("/health/ready")
        assert ready.status_code != 401
        info = client.get("/api/v1/system/info")
        assert info.status_code == 200


# ---------------------------------------------------------------------------
# WebSocket boundary: single-use ticket in the client.hello frame
# ---------------------------------------------------------------------------


def _hello(ticket_marker):
    frame = {
        "type": "client.hello",
        "ws_schema_versions": [1],
        "subscriptions": ["all"],
    }
    if ticket_marker is not None:
        frame["ticket"] = ticket_marker
    return frame


def test_websocket_rejects_bogus_ticket_with_4401(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        with client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json(_hello("bogus-ticket-that-was-never-minted"))
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == 4401


def test_websocket_rejects_missing_ticket_with_4401(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        with client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json(_hello(None))
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == 4401


def test_websocket_ticket_minting_itself_requires_launch_token(app):
    """POST /api/v1/ws-ticket without the launch token mints nothing."""
    with TestClient(app, base_url="http://127.0.0.1") as client:
        resp = client.post("/api/v1/ws-ticket")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "missing_token"
