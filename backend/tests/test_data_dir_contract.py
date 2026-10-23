"""Round 6 data-dir contract — consumer + boot-level proof.

The canonical resolver (``app.data_dir.resolve_data_dir``) and its
precedence are unit-tested in ``test_data_dir.py``.  This file proves
the CONTRACT at the consumers that historically escaped it, plus a
genuine subprocess boot:

* stock-footage cache default (was: inside the repo checkout),
* faster-whisper model download root (was: ``<repo>/models``),
* standalone produce CLI default base (was: ``<repo>/outputs``),

all root under ``YTA_DATA_DIR`` when it is set, and keep their exact
historical dev locations when it is unset; and

* a real ``python -m app.main`` boot with ``YTA_DATA_DIR`` pointed at
  a fresh directory creates the DB and the log file THERE — and
  writes nothing new into the repo tree / process CWD.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent


# ---------------------------------------------------------------------------
# explicit_data_dir — the "override was actually set" distinction
# ---------------------------------------------------------------------------


def test_explicit_data_dir_set_blank_unset() -> None:
    from app.data_dir import explicit_data_dir

    assert explicit_data_dir({"YTA_DATA_DIR": "/data/x"}) == Path("/data/x")
    assert explicit_data_dir({"YTA_DATA_DIR": "  "}) is None
    assert explicit_data_dir({"YTA_DATA_DIR": ""}) is None
    assert explicit_data_dir({}) is None


def test_layout_cache_and_model_paths() -> None:
    from app.data_dir import data_dir_layout

    layout = data_dir_layout(Path("/data/root"))
    assert layout.cache_dir == Path("/data/root/cache")
    assert layout.stock_cache_dir == Path("/data/root/cache/stock")
    assert layout.models_dir == Path("/data/root/models")
    assert layout.whisper_model_dir == Path("/data/root/models/whisper")


# ---------------------------------------------------------------------------
# Stock cache default
# ---------------------------------------------------------------------------


def test_stock_cache_default_under_data_dir_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.integration.stock_fetch import default_cache_dir

    monkeypatch.setenv("YTA_DATA_DIR", str(tmp_path))
    assert default_cache_dir() == tmp_path / "cache" / "stock"


def test_stock_cache_default_legacy_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.integration.stock_fetch import DEFAULT_CACHE_DIR, default_cache_dir

    monkeypatch.delenv("YTA_DATA_DIR", raising=False)
    assert default_cache_dir() == DEFAULT_CACHE_DIR
    assert default_cache_dir() == BACKEND_DIR / "cache" / "stock"


def test_stock_fetcher_construction_uses_contract_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.integration.stock_fetch import StockFetcher

    monkeypatch.setenv("YTA_DATA_DIR", str(tmp_path))
    fetcher = StockFetcher(adapter=object())
    assert fetcher.cache_dir == tmp_path / "cache" / "stock"
    assert fetcher.search_cache_dir == tmp_path / "cache" / "stock" / "search"


# ---------------------------------------------------------------------------
# Whisper model dir
# ---------------------------------------------------------------------------


def test_whisper_model_dir_under_data_dir_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.providers.transcription.whisper_faster import default_model_dir

    monkeypatch.delenv("YTA_WHISPER_MODEL_DIR", raising=False)
    monkeypatch.setenv("YTA_DATA_DIR", str(tmp_path))
    assert default_model_dir() == tmp_path / "models" / "whisper"


def test_whisper_model_dir_specific_override_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.providers.transcription.whisper_faster import default_model_dir

    monkeypatch.setenv("YTA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("YTA_WHISPER_MODEL_DIR", str(tmp_path / "custom"))
    assert default_model_dir() == tmp_path / "custom"


def test_whisper_model_dir_legacy_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.providers.transcription import whisper_faster

    monkeypatch.delenv("YTA_WHISPER_MODEL_DIR", raising=False)
    monkeypatch.delenv("YTA_DATA_DIR", raising=False)
    repo_root = Path(whisper_faster.__file__).resolve().parents[4]
    assert whisper_faster.default_model_dir() == repo_root / "models" / "whisper"


# ---------------------------------------------------------------------------
# Standalone produce CLI default base
# ---------------------------------------------------------------------------


def test_cli_default_base_under_data_dir_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.integration.run import _default_base_dir

    monkeypatch.setenv("YTA_DATA_DIR", str(tmp_path))
    assert _default_base_dir("Facts") == tmp_path / "projects" / "cli_facts"


def test_cli_default_base_legacy_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.integration.run import _default_base_dir

    monkeypatch.delenv("YTA_DATA_DIR", raising=False)
    assert _default_base_dir("Facts") == REPO_ROOT / "outputs" / "round2" / "cli_facts"


# ---------------------------------------------------------------------------
# Boot-level proof: real subprocess, env-driven resolution
# ---------------------------------------------------------------------------


def _git_status_filtered() -> str:
    """Repo changes in the places a mis-resolving backend would write."""
    result = subprocess.run(
        [
            "git", "-C", str(REPO_ROOT), "status", "--porcelain", "--",
            "backend/cache", "outputs", "backend/app.db", "backend/.dev-data",
            ".dev-data",
        ],
        capture_output=True, text=True, timeout=60,
    )
    return result.stdout


def _http_get(port: int, path: str, token: str | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if token:
        request.add_header("X-Local-Token", token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


@pytest.mark.skipif(not (BACKEND_DIR / ".venv" / "bin" / "python").exists(),
                    reason="backend venv python not present")
def test_boot_with_yta_data_dir_writes_only_under_it(tmp_path: Path) -> None:
    data_dir = tmp_path / "yta-data"
    systmp = tmp_path / "systmp"
    systmp.mkdir()
    token = "boot-contract-token-123456"

    repo_status_before = _git_status_filtered()
    backend_listing_before = set(os.listdir(BACKEND_DIR))

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "TMPDIR": str(systmp),
        "PYTHONUNBUFFERED": "1",
        "YTA_DATA_DIR": str(data_dir),
        "YTA_TOKEN": token,
        "YTA_PORT": "0",
        "YTA_BOOT_ID": "boot-contract",
        "YTA_EXECUTION": "0",
        "YTA_LOG_LEVEL": "info",
    }
    proc = subprocess.Popen(
        [str(BACKEND_DIR / ".venv" / "bin" / "python"), "-m", "app.main"],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines: queue.Queue[str] = queue.Queue()

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)

    threading.Thread(target=_reader, daemon=True).start()
    try:
        port: int | None = None
        seen: list[str] = []
        for _ in range(600):  # up to ~60 s for the listening line
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            seen.append(line)
            stripped = line.strip()
            if stripped.startswith("{") and '"listening"' in stripped:
                port = int(json.loads(stripped)["port"])
                break
        assert port, f"no listening line from sidecar boot; output: {seen!r}"

        status, payload = _http_get(port, "/health/live")
        assert status == 200 and payload.get("status") == "ok"
        status, payload = _http_get(port, "/health/ready", token=token)
        assert status == 200 and payload.get("db") == "ok"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    # Everything the boot initialised lives under YTA_DATA_DIR …
    assert (data_dir / "app.db").is_file()
    assert (data_dir / "logs" / "backend.log").is_file()

    # … and nothing new appeared in the repo tree / CWD.
    assert _git_status_filtered() == repo_status_before
    assert set(os.listdir(BACKEND_DIR)) == backend_listing_before
    assert not (BACKEND_DIR / "app.db").exists()
    assert not (BACKEND_DIR / "logs").exists()
