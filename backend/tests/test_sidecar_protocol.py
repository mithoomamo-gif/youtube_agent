"""Shell <-> sidecar protocol contract tests (Round 6).

Covers the exact contract the Tauri shell relies on, per
MASTER_ROADMAP §4.5/§4.6 (§31) and the conflict resolutions in
Appendix/§14-vs-§31 (#6 health auth, #7 token delivery):

* Startup line: exactly one stdout line
  ``{"event": "listening", "port": <int>, "boot_id": "<str>"}``,
  printed once the app has started (i.e. when bound), no extra keys.
* Launch token: taken from the ``YTA_TOKEN`` env var, sent by clients
  as the ``X-Local-Token`` header, enforced on every route EXCEPT
  ``GET /health/live``. No token -> 401, wrong token -> 401,
  correct token -> accepted.
* Health semantics: ``/health/live`` = process up only, answers the
  minimal ``{"status": "ok"}`` with no token and no detail, and does
  not depend on the DB. ``/health/ready`` = token-required, 200 only
  when the DB is usable, else the 503 ``not_ready`` envelope.
* Exit codes (§4.6): 0 clean shutdown · 2 fatal bind-scope/config
  (do not auto-restart) · 3 port bind failure (shell retries a fresh
  port) · 4 DB schema newer than this build (downgrade protection)
  · 5 missing/invalid launch environment (do not restart).
  Exit-code paths are proven at subprocess level (the real process
  entry ``python -m app.main``), plus in-process unit tests for the
  individual guards in ``app.main.run()``.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import (
    EXIT_BAD_ENV,
    EXIT_BIND_FAILURE,
    EXIT_BIND_SCOPE,
    EXIT_SCHEMA_TOO_NEW,
    create_app,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent
TOKEN = "protocol-test-token-0123456789"
BOOT_ID = "proto-boot-1"
PORT = 8123
BASE_URL = "http://127.0.0.1:8123"
AUTH = {"X-Local-Token": TOKEN}
BAD_AUTH = {"X-Local-Token": "definitely-the-wrong-token"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path, port: int = PORT) -> Settings:
    return Settings(
        port=port,
        token=TOKEN,
        boot_id=BOOT_ID,
        data_dir=tmp_path,
        log_level="warning",
    )


def _make_app(tmp_path: Path, port: int = PORT):
    return create_app(_settings(tmp_path, port=port))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_db_with_schema(data_dir: Path, version: int) -> Path:
    """Create an app.db whose recorded schema version is ``version``.

    Writes BOTH version stores the backend reads: ``alembic_version``
    (preferred by app.backup.database_schema_version) and
    ``schema_meta`` (app.db.schema_version's source).
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "app.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
        )
        conn.execute(
            "INSERT INTO alembic_version (version_num) VALUES (?)",
            (f"{version:04d}",),
        )
        conn.execute(
            "CREATE TABLE schema_meta ("
            " id INTEGER PRIMARY KEY CHECK (id = 1),"
            " version INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_meta (id, version, updated_at) VALUES (1, ?, 0)",
            (version,),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _clean_yta_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("YTA_"):
            monkeypatch.delenv(name, raising=False)


def _set_launch_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **overrides: str
) -> None:
    env = {
        "YTA_TOKEN": TOKEN,
        "YTA_BOOT_ID": BOOT_ID,
        "YTA_DATA_DIR": str(tmp_path),
        "YTA_LOG_LEVEL": "warning",
        "YTA_EXECUTION": "0",
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


# ---------------------------------------------------------------------------
# 1. Startup ("listening") line
# ---------------------------------------------------------------------------


def test_listening_line_shape_and_exactly_once(tmp_path, capsys):
    app = _make_app(tmp_path)
    with TestClient(app, base_url=BASE_URL):
        pass
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if '"listening"' in line]
    assert len(lines) == 1, f"expected exactly one listening line, got: {out!r}"
    payload = json.loads(lines[0])
    # Roadmap §4.6: {"event":"listening","port":…,"boot_id":"…"} — no
    # extra fields (in particular no host and no token, ever).
    assert payload == {"event": "listening", "port": PORT, "boot_id": BOOT_ID}
    assert isinstance(payload["port"], int)
    assert TOKEN not in lines[0]

    # A second lifespan startup on the same app must NOT re-print it.
    with TestClient(app, base_url=BASE_URL):
        pass
    out2 = capsys.readouterr().out
    assert '"listening"' not in out2


# ---------------------------------------------------------------------------
# 2. Token enforcement + health semantics (in-process)
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app, base_url=BASE_URL) as test_client:
        yield test_client


def test_live_is_token_exempt_and_minimal(client):
    resp = client.get("/health/live")
    assert resp.status_code == 200
    # Master resolution (§14 vs §31): minimal body, no version/data.
    assert resp.json() == {"status": "ok"}
    # Even a WRONG token does not change live (it is the pre-token poll).
    resp = client.get("/health/live", headers=BAD_AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ready_requires_token(client):
    resp = client.get("/health/ready")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_token"

    resp = client.get("/health/ready", headers=BAD_AUTH)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_token"


def test_api_routes_require_token(client):
    resp = client.get("/api/v1/system/info")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_token"

    resp = client.get("/api/v1/system/info", headers=BAD_AUTH)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_token"

    resp = client.get("/api/v1/system/info", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["boot_id"] == BOOT_ID


def test_ready_ok_when_db_usable(client, tmp_path, monkeypatch):
    import app.db as db_module

    monkeypatch.setattr(
        db_module,
        "get_connection",
        lambda *a, **k: db_module.connect(tmp_path / "app.db"),
    )
    resp = client.get("/health/ready", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["db"] == "ok"
    assert body["migrations"] == "ok"


def test_ready_503_when_db_down_but_live_stays_ok(client, monkeypatch):
    import app.db as db_module

    def _boom(*args, **kwargs):
        raise RuntimeError("db down (test)")

    monkeypatch.setattr(db_module, "get_connection", _boom)
    resp = client.get("/health/ready", headers=AUTH)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "not_ready"
    assert resp.json()["error"]["retryable"] is True
    # Liveness is process-up ONLY: the DB being down must not affect it.
    resp = client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# 3. Exit-code guards (in-process unit tests of app.main.run)
# ---------------------------------------------------------------------------


def test_run_exit_5_missing_token(monkeypatch, tmp_path):
    from app import main

    _clean_yta_env(monkeypatch)
    _set_launch_env(monkeypatch, tmp_path)
    monkeypatch.delenv("YTA_TOKEN")
    with pytest.raises(SystemExit) as excinfo:
        main.run()
    assert excinfo.value.code == EXIT_BAD_ENV == 5


@pytest.mark.parametrize("bad_port", ["not-a-port", "70000", "-1"])
def test_run_exit_5_invalid_launch_env(monkeypatch, tmp_path, bad_port):
    from app import main

    _clean_yta_env(monkeypatch)
    _set_launch_env(monkeypatch, tmp_path, YTA_PORT=bad_port)
    with pytest.raises(SystemExit) as excinfo:
        main.run()
    assert excinfo.value.code == EXIT_BAD_ENV == 5


def test_run_exit_2_non_loopback_bind_scope(monkeypatch, tmp_path):
    from app import main

    _clean_yta_env(monkeypatch)
    _set_launch_env(monkeypatch, tmp_path)
    # HOST is a compile-time constant in production; patching it here
    # proves the guard itself maps a non-loopback target to exit 2.
    monkeypatch.setattr(main, "HOST", "0.0.0.0")
    with pytest.raises(SystemExit) as excinfo:
        main.run()
    assert excinfo.value.code == EXIT_BIND_SCOPE == 2


def test_run_exit_3_port_already_bound(monkeypatch, tmp_path, capsys):
    from app import main

    _clean_yta_env(monkeypatch)
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        port = int(blocker.getsockname()[1])
        _set_launch_env(monkeypatch, tmp_path, YTA_PORT=str(port))
        with pytest.raises(SystemExit) as excinfo:
            main.run()
        assert excinfo.value.code == EXIT_BIND_FAILURE == 3
    finally:
        blocker.close()
    # The failure happens BEFORE any startup: no listening line may be
    # printed for a port that could not be bound.
    assert '"listening"' not in capsys.readouterr().out


def test_run_exit_4_schema_newer_than_build(monkeypatch, tmp_path):
    from app import main

    _clean_yta_env(monkeypatch)
    _write_db_with_schema(tmp_path, 9999)
    _set_launch_env(monkeypatch, tmp_path, YTA_PORT=str(_free_port()))
    with pytest.raises(SystemExit) as excinfo:
        main.run()
    assert excinfo.value.code == EXIT_SCHEMA_TOO_NEW == 4


def test_schema_guard_accepts_current_and_absent_db(tmp_path):
    from app.main import _refuse_if_schema_too_new, _supported_schema_version

    supported = _supported_schema_version()
    assert supported is not None and supported >= 10  # Alembic head 0010

    # No DB file yet (first run): nothing to refuse.
    _refuse_if_schema_too_new(_settings(tmp_path / "fresh"))
    # DB exactly at the supported head: fine.
    _write_db_with_schema(tmp_path, supported)
    _refuse_if_schema_too_new(_settings(tmp_path))
    # DB one past the head: refused with exit 4.
    newer = tmp_path / "newer"
    _write_db_with_schema(newer, supported + 1)
    with pytest.raises(SystemExit) as excinfo:
        _refuse_if_schema_too_new(_settings(newer))
    assert excinfo.value.code == EXIT_SCHEMA_TOO_NEW == 4


def _write_db_with_revision(
    data_dir: Path, revision: str, meta_version: int
) -> Path:
    """Create an app.db with a RAW alembic revision string.

    Unlike :func:`_write_db_with_schema` (numeric versions written to
    both stores), this writes an arbitrary ``version_num`` string so
    tests can model a future build whose revision scheme the numeric
    readers cannot rank, with ``schema_meta`` pinned independently.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "app.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
        )
        conn.execute(
            "INSERT INTO alembic_version (version_num) VALUES (?)",
            (revision,),
        )
        conn.execute(
            "CREATE TABLE schema_meta ("
            " id INTEGER PRIMARY KEY CHECK (id = 1),"
            " version INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_meta (id, version, updated_at) VALUES (1, ?, 0)",
            (meta_version,),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_run_exit_4_logs_database_newer_than_binary(
    monkeypatch, tmp_path, capsys
):
    from app import main

    _clean_yta_env(monkeypatch)
    _write_db_with_schema(tmp_path, 9999)
    _set_launch_env(monkeypatch, tmp_path, YTA_PORT=str(_free_port()))
    with pytest.raises(SystemExit) as excinfo:
        main.run()
    assert excinfo.value.code == EXIT_SCHEMA_TOO_NEW == 4
    captured = capsys.readouterr()
    # The literal detail string the shell attaches to exit code 4
    # must appear in the backend's own stderr log line...
    assert "database_newer_than_binary" in captured.err
    # ...and the refusal happens before any listening announcement.
    assert '"listening"' not in captured.out


def test_run_exit_4_unknown_future_revision(monkeypatch, tmp_path, capsys):
    """G3: a DB stamped by a future build with a revision string this
    build does not know (and cannot rank numerically — schema_meta is
    pinned LOW, so the numeric comparison alone would wave it
    through) is still refused with exit 4 via the known-revision-set
    check."""
    from app import main

    _clean_yta_env(monkeypatch)
    _write_db_with_revision(tmp_path, "future_unknown_rev", meta_version=1)
    _set_launch_env(monkeypatch, tmp_path, YTA_PORT=str(_free_port()))
    with pytest.raises(SystemExit) as excinfo:
        main.run()
    assert excinfo.value.code == EXIT_SCHEMA_TOO_NEW == 4
    captured = capsys.readouterr()
    assert "database_newer_than_binary" in captured.err
    assert '"listening"' not in captured.out


def test_schema_guard_accepts_known_revision_string(tmp_path):
    from app.main import _known_schema_revisions, _refuse_if_schema_too_new

    known = _known_schema_revisions()
    assert known is not None and "0001" in known
    head = max(known)
    # A DB at the known head revision (string form) is accepted.
    _write_db_with_revision(tmp_path, head, meta_version=int(head))
    _refuse_if_schema_too_new(_settings(tmp_path))


def test_run_exit_0_when_server_returns_cleanly(monkeypatch, tmp_path):
    import uvicorn

    from app import main

    _clean_yta_env(monkeypatch)
    _set_launch_env(monkeypatch, tmp_path, YTA_PORT=str(_free_port()))
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    # A clean uvicorn return = clean shutdown = no SystemExit at all,
    # so the process exit status is 0.
    assert main.run() is None


# ---------------------------------------------------------------------------
# 4. Exit codes + wire protocol at subprocess level (the real entry point)
# ---------------------------------------------------------------------------


def _subprocess_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("YTA_")}
    env["PYTHONPATH"] = str(BACKEND_DIR)
    env.update(
        {
            "YTA_TOKEN": TOKEN,
            "YTA_BOOT_ID": BOOT_ID,
            "YTA_DATA_DIR": str(tmp_path),
            "YTA_LOG_LEVEL": "warning",
            "YTA_EXECUTION": "0",
        }
    )
    env.update(overrides)
    return env


def _run_sidecar(env: dict[str, str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "app.main"],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_subprocess_exit_5_missing_token(tmp_path):
    env = _subprocess_env(tmp_path)
    del env["YTA_TOKEN"]
    proc = _run_sidecar(env)
    assert proc.returncode == 5
    assert "YTA_TOKEN" in proc.stderr


def test_subprocess_exit_5_invalid_env(tmp_path):
    proc = _run_sidecar(_subprocess_env(tmp_path, YTA_PORT="bogus"))
    assert proc.returncode == 5


def test_subprocess_exit_4_schema_too_new(tmp_path):
    _write_db_with_schema(tmp_path, 9999)
    proc = _run_sidecar(_subprocess_env(tmp_path, YTA_PORT=str(_free_port())))
    assert proc.returncode == 4
    assert "newer" in proc.stderr
    assert "database_newer_than_binary" in proc.stderr
    assert '"listening"' not in proc.stdout


def test_subprocess_exit_4_unknown_future_revision(tmp_path):
    _write_db_with_revision(tmp_path, "future_unknown_rev", meta_version=1)
    proc = _run_sidecar(_subprocess_env(tmp_path, YTA_PORT=str(_free_port())))
    assert proc.returncode == 4
    assert "database_newer_than_binary" in proc.stderr
    assert '"listening"' not in proc.stdout


def test_subprocess_exit_3_bind_failure(tmp_path):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        port = int(blocker.getsockname()[1])
        proc = _run_sidecar(_subprocess_env(tmp_path, YTA_PORT=str(port)))
    finally:
        blocker.close()
    assert proc.returncode == 3
    # No listening line for a port that was never bound.
    assert '"listening"' not in proc.stdout


def _http_get(port: int, path: str, token: str | None = None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if token is not None:
        req.add_header("X-Local-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_subprocess_end_to_end_protocol_and_clean_exit(tmp_path):
    port = _free_port()
    env = _subprocess_env(tmp_path, YTA_PORT=str(port))
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.main"],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    listening = None
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                break
            if '"listening"' in line:
                listening = json.loads(line.strip())
                break
        assert listening is not None, "sidecar never printed its listening line"
        assert listening == {"event": "listening", "port": port, "boot_id": BOOT_ID}

        # /health/live: token-exempt, minimal body.
        status, body = _http_get(port, "/health/live")
        assert status == 200
        assert json.loads(body) == {"status": "ok"}
        # /health/ready: token enforced.
        assert _http_get(port, "/health/ready")[0] == 401
        assert _http_get(port, "/health/ready", token="wrong-token")[0] == 401
        status, body = _http_get(port, "/health/ready", token=TOKEN)
        assert status == 200
        assert json.loads(body)["db"] == "ok"
        # API routes: token enforced, correct token accepted.
        assert _http_get(port, "/api/v1/system/info")[0] == 401
        status, body = _http_get(port, "/api/v1/system/info", token=TOKEN)
        assert status == 200
        assert json.loads(body)["boot_id"] == BOOT_ID
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        try:
            returncode = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            returncode = proc.wait()
    # Clean shutdown (SIGTERM -> graceful uvicorn stop) exits 0.
    assert returncode == 0
