"""Backend HTTP API contract tests (L1/L2).

Contract under test (MASTER_ROADMAP §12.7 / section 31 resolutions):

* ``GET /health/live`` is token-exempt and answers *only* the minimal
  shape ``{"status": "ok"}`` — no version, no diagnostics. It is exempt
  *because* it leaks nothing.
* Everything else, including ``GET /health/ready``, requires the launch
  token (missing token -> HTTP 401 from LaunchTokenMiddleware).
* The launch token reaches the sidecar **via environment variable only**
  (``YTA_TOKEN``, set for the spawn call; never argv/file/URL). The app
  factory therefore legitimately refuses to build without it — these
  tests set a test-only dummy token through the environment, exactly
  like the shell's spawn does, and then call ``/health/live`` *without*
  presenting the token.

The backend landed as ``app.main.create_app()`` (backend/ on sys.path;
also importable as ``backend.app.main``). Until fastapi/httpx are
installed in the test environment, every test here skips — the suite
must stay green in a pytest-only env (see tests/README.md).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.contract

# Test-only dummy launch token. Never a real credential; set via env
# purely so create_app() can build Settings the way production spawn
# does. (Value is a placeholder on purpose.)
_TEST_TOKEN = "test-only-launch-token"
_BASE_URL = "http://127.0.0.1:8000"  # Host allowlist: loopback only

# Candidate module paths for the FastAPI app, in landing-order likelihood.
# Each entry: (module, attribute holding the app or a zero-arg factory).
_APP_CANDIDATES = (
    ("app.main", "create_app"),
    ("app.main", "app"),
    ("backend.app.main", "create_app"),
    ("backend.app.main", "app"),
    ("backend.main", "create_app"),
    ("backend.main", "app"),
    ("backend.app", "create_app"),
    ("backend.app", "app"),
    ("backend", "create_app"),
    ("backend", "app"),
)
# Modules whose factory raising a real error is a contract break (fail),
# not a "not landed yet" signal (skip).
_PRIMARY_MODULES = {"app.main", "backend.app.main", "backend.main"}


def _load_app(monkeypatch):
    """Return the FastAPI app instance, or skip/fail with a precise reason.

    * No candidate importable at all            -> skip (not landed).
    * Primary factory importable but its call
      raises a non-TypeError                    -> fail (real breakage).
    * Otherwise                                 -> the built app.
    """
    import importlib
    import os

    # Production spawn delivers the launch token via env only (§31);
    # mirror that so create_app() can build its Settings.
    if not os.environ.get("YTA_TOKEN"):
        monkeypatch.setenv("YTA_TOKEN", _TEST_TOKEN)

    tried: list[str] = []
    primary_errors: list[str] = []
    for modname, attr in _APP_CANDIDATES:
        tried.append(f"{modname}:{attr}")
        try:
            module = importlib.import_module(modname)
        except ImportError:
            continue
        candidate = getattr(module, attr, None)
        if candidate is None:
            continue
        if callable(candidate) and not hasattr(candidate, "routes"):
            try:
                candidate = candidate()
            except TypeError:
                continue
            except Exception as exc:  # factory landed but broke
                if modname in _PRIMARY_MODULES:
                    primary_errors.append(f"{modname}:{attr} raised {exc!r}")
                continue
        if hasattr(candidate, "routes"):
            return candidate

    if primary_errors:
        pytest.fail(
            "app factory landed but failed to build the app: "
            + "; ".join(primary_errors)
        )
    pytest.skip("FastAPI app not landed yet (tried: " + ", ".join(tried) + ")")


def _client(monkeypatch):
    """A TestClient over the real app, on a loopback Host (allowlisted)."""
    pytest.importorskip("fastapi", reason="fastapi not installed yet")
    pytest.importorskip(
        "httpx", reason="httpx (TestClient transport) not installed yet"
    )
    app = _load_app(monkeypatch)
    from fastapi.testclient import TestClient

    return TestClient(app, base_url=_BASE_URL)


def test_health_live_contract(monkeypatch):
    """GET /health/live -> 200 {"status": "ok"} and nothing extra."""
    with _client(monkeypatch) as client:
        response = client.get("/health/live")

    assert response.status_code == 200, response.text
    body = response.json()
    # Exact shape: the liveness probe is token-exempt *because* it leaks
    # nothing — any extra field is a contract break, not a nit.
    assert body == {"status": "ok"}, f"/health/live body drifted: {body!r}"


def test_health_live_needs_no_token(monkeypatch):
    """The probe must answer without any auth header (it is token-exempt)."""
    with _client(monkeypatch) as client:
        # Deliberately no X-Local-Token header.
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json().get("status") == "ok"


def test_health_ready_requires_token(monkeypatch):
    """Non-exempt endpoints reject a tokenless request with 401 (§31)."""
    pytest.importorskip(
        "app.middleware", reason="LaunchTokenMiddleware not landed yet"
    )
    with _client(monkeypatch) as client:
        response = client.get("/health/ready")  # no token header
    assert response.status_code == 401, (
        f"/health/ready without a launch token returned "
        f"{response.status_code}, expected 401 (missing_token)"
    )
