"""Shared sidecar-boot fixtures for integration tests.

Other integration workers: import nothing — just request the fixtures:

* ``sidecar`` — one sidecar, already booted and ready, on an ephemeral
  port with a fresh temporary data dir. Yields a :class:`SidecarProcess`.
* ``sidecar_factory`` — a factory to boot additional / replacement
  sidecars (e.g. restart-against-the-same-data-dir tests)::

      first = sidecar_factory(data_dir=some_dir)
      first.start(); first.wait_ready()
      ...
      first.stop()
      second = sidecar_factory(data_dir=some_dir)  # new token + boot_id
      second.start(); second.wait_ready()

  Every sidecar created through the factory is stopped at teardown.

Boot contract under test (backend/app/main.py, backend/app/config.py,
docs/DEV_SETUP.md section 5.1):

* Process entry point is ``app.main:run`` — launched here as
  ``<sys.executable> -m app.main`` with ``cwd`` / ``PYTHONPATH`` set to
  ``backend/``. (The ``uvicorn app.main:app`` form printed in DEV_SETUP
  does not work against the landed code: ``app.main`` defines no
  module-level ``app`` object, only ``create_app()`` and ``run()``.)
* Configuration arrives ONLY via the ``YTA_*`` environment:
  ``YTA_PORT`` (0 = pick a free ephemeral port), ``YTA_TOKEN``
  (required), ``YTA_BOOT_ID``, ``YTA_DATA_DIR``, plus
  ``YTA_DETACHED=1`` for standalone runs.
* On startup the process prints exactly one structured stdout line:
  ``{"event": "listening", "port": <port>, "boot_id": "<boot_id>"}``.
* It binds 127.0.0.1 only; clients send the token as the
  ``X-Local-Token`` header on every path except ``GET /health/live``.

This module uses only the standard library + pytest so it imports
cleanly in any test environment; HTTP is done with ``urllib``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS_DIR.parent
BACKEND_DIR = REPO_ROOT / "backend"

# Bounded but generous: DEV_SETUP budgets 30 s for cold start.
BOOT_TIMEOUT_SECONDS = 30.0
REQUEST_TIMEOUT_SECONDS = 10.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0


def pick_free_port() -> int:
    """Return a currently-free loopback TCP port (helper for other workers)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class SidecarProcess:
    """A booted (or bootable) backend sidecar subprocess.

    Attributes after :meth:`wait_ready`:
      * ``port`` — the actual bound port (parsed from the listening line)
      * ``base_url`` — ``http://127.0.0.1:<port>``
      * ``listening`` — the parsed listening JSON line (dict)
      * ``token`` / ``boot_id`` / ``data_dir`` — the launch env used
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        token: str | None = None,
        boot_id: str | None = None,
        port: int = 0,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.token = token or f"test-token-{uuid.uuid4().hex}"
        self.boot_id = boot_id or str(uuid.uuid4())
        self.requested_port = port
        self.port: int | None = None
        self.listening: dict | None = None
        self._proc: subprocess.Popen | None = None
        self._stdout_offset = 0  # bytes already in the log before this boot
        self._stdout_path = self.data_dir / "sidecar.stdout.log"
        self._stderr_path = self.data_dir / "sidecar.stderr.log"
        self._stdout_fh = None
        self._stderr_fh = None

    # -- lifecycle ------------------------------------------------------

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise RuntimeError("sidecar port not known yet — call wait_ready() first")
        return f"http://127.0.0.1:{self.port}"

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> "SidecarProcess":
        if self.is_running:
            raise RuntimeError("sidecar already running")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        env.update(
            {
                "YTA_PORT": str(self.requested_port),
                "YTA_TOKEN": self.token,
                "YTA_BOOT_ID": self.boot_id,
                "YTA_DATA_DIR": str(self.data_dir),
                "YTA_DETACHED": "1",
                "YTA_LOG_LEVEL": "info",
                "PYTHONUNBUFFERED": "1",
            }
        )
        backend = str(BACKEND_DIR)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = backend + (os.pathsep + existing if existing else "")

        # On a restart against the same data dir the log already holds the
        # previous boot's listening line — only parse past this offset, and
        # only accept a line carrying THIS boot's boot_id.
        try:
            self._stdout_offset = self._stdout_path.stat().st_size
        except OSError:
            self._stdout_offset = 0
        self._stdout_fh = open(self._stdout_path, "ab")
        self._stderr_fh = open(self._stderr_path, "ab")
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "app.main"],
            cwd=backend,
            env=env,
            stdout=self._stdout_fh,
            stderr=self._stderr_fh,
        )
        return self

    def _read_stdout_text(self) -> str:
        try:
            raw = self._stdout_path.read_bytes()
        except OSError:
            return ""
        return raw[self._stdout_offset :].decode("utf-8", errors="replace")

    def _diagnostics(self) -> str:
        parts = [f"data_dir={self.data_dir}", f"requested_port={self.requested_port}"]
        if self._proc is not None:
            parts.append(f"returncode={self._proc.poll()}")
        for label, path in (("stdout", self._stdout_path), ("stderr", self._stderr_path)):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[-4000:]
            except OSError:
                text = "<unreadable>"
            parts.append(f"--- sidecar {label} ({path}) ---\n{text}")
        return "\n".join(parts)

    def wait_ready(self, timeout: float = BOOT_TIMEOUT_SECONDS) -> "SidecarProcess":
        """Wait for the listening line, then for GET /health/live == ok."""
        if self._proc is None:
            raise RuntimeError("sidecar not started — call start() first")
        deadline = time.monotonic() + timeout

        # 1) The structured listening line on stdout.
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise AssertionError(
                    "sidecar exited before printing its listening line\n"
                    + self._diagnostics()
                )
            for line in self._read_stdout_text().splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("event") == "listening" and (
                    payload.get("boot_id") in (None, self.boot_id)
                ):
                    self.listening = payload
                    self.port = int(payload["port"])
                    break
            if self.listening is not None:
                break
            time.sleep(0.1)
        if self.listening is None:
            raise AssertionError(
                f"sidecar printed no listening line within {timeout:.0f}s\n"
                + self._diagnostics()
            )

        # 2) Liveness polling (the one token-exempt endpoint).
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise AssertionError(
                    "sidecar exited after listening but before becoming live\n"
                    + self._diagnostics()
                )
            try:
                status, body = self.request("GET", "/health/live", token=None)
            except OSError:
                status, body = None, None
            if status == 200 and isinstance(body, dict) and body.get("status") == "ok":
                return self
            time.sleep(0.1)
        raise AssertionError(
            f"sidecar /health/live not ok within {timeout:.0f}s\n" + self._diagnostics()
        )

    def stop(self) -> None:
        """Terminate the sidecar (SIGTERM, escalating to SIGKILL). Idempotent."""
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
        for fh in (self._stdout_fh, self._stderr_fh):
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
        self._stdout_fh = self._stderr_fh = None

    # ``kill`` is the word the acceptance task uses; same hard stop as stop()
    # from the test's point of view (process is gone afterwards).
    kill = stop

    # -- HTTP -------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = ...,
        json_body: dict | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ):
        """One HTTP request; returns ``(status, parsed_json_or_text)``.

        ``token=...`` (the default) sends this sidecar's launch token as
        ``X-Local-Token``; ``token=None`` sends no header; a string sends
        that token instead. HTTP error statuses are returned, not raised.
        """
        url = self.base_url + path
        data = None
        headers = {"Accept": "application/json"}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        effective_token = self.token if token is ... else token
        if effective_token:
            headers["X-Local-Token"] = effective_token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return resp.status, _parse_body(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            return exc.code, _parse_body(raw)

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self.request("POST", path, **kwargs)


def _parse_body(raw: str):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def start_sidecar(data_dir: Path, **kwargs) -> SidecarProcess:
    """Boot a sidecar and wait for readiness (helper for other workers)."""
    return SidecarProcess(data_dir, **kwargs).start().wait_ready()


@pytest.fixture
def sidecar_factory(tmp_path):
    """Factory for sidecars; everything it creates is stopped at teardown."""
    created: list[SidecarProcess] = []
    counter = {"n": 0}

    def _make(data_dir: Path | None = None, **kwargs) -> SidecarProcess:
        counter["n"] += 1
        if data_dir is None:
            data_dir = tmp_path / f"sidecar-data-{counter['n']}"
        sidecar = SidecarProcess(Path(data_dir), **kwargs)
        created.append(sidecar)
        return sidecar

    yield _make

    for sidecar in reversed(created):
        sidecar.stop()


@pytest.fixture
def sidecar(sidecar_factory):
    """One sidecar, booted and ready, with a fresh temporary data dir."""
    instance = sidecar_factory()
    instance.start().wait_ready()
    yield instance
    instance.stop()
