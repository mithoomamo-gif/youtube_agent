"""Shared pytest fixtures for the backend test-suite.

Bootstrap (kept from the original conftest): inserts the backend
directory (the parent of this tests/ folder) into ``sys.path``,
regardless of the directory pytest is invoked from.

Also sets a deterministic launch environment BEFORE any ``app`` module
is imported (settings are cached process-wide), then provides:

* ``settings`` — :class:`app.config.Settings` built from the test env
* ``app`` — a FastAPI app from ``app.main.create_app`` (DB initialised
  via ``app.db.init_db`` when that module is available)
* ``client`` — a ``fastapi.testclient.TestClient`` whose default headers
  already carry ``X-Local-Token: <test token>``

FastAPI imports live inside the fixtures so pure-unit tests (e.g.
``test_config.py``) still collect and run on a machine where the web
dependencies have not been installed yet.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

TEST_TOKEN = "test-token-0123456789"
TEST_BOOT_ID = "test-boot"

# Session-wide test data dir (created before any app import reads env).
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="yta-test-data-")

os.environ["YTA_TOKEN"] = TEST_TOKEN
os.environ["YTA_BOOT_ID"] = TEST_BOOT_ID
os.environ["YTA_DATA_DIR"] = _TEST_DATA_DIR
os.environ.setdefault("YTA_LOG_LEVEL", "info")


@pytest.fixture(scope="session")
def test_data_dir() -> Path:
    return Path(_TEST_DATA_DIR)


@pytest.fixture()
def settings():
    from app.config import load_settings

    return load_settings()


@pytest.fixture()
def app(settings):
    from app.main import create_app

    application = create_app(settings)

    # Initialise the DB when worker 3's module is available; its absence
    # (parallel build) must not fail unrelated tests.
    try:
        from app import db as db_module

        init_db = getattr(db_module, "init_db", None)
        if callable(init_db):
            try:
                init_db(settings.db_path)
            except TypeError:
                init_db()
    except Exception as exc:  # pragma: no cover - parallel-build tolerance
        print(f"conftest: app.db.init_db unavailable/skipped: {exc!r}")

    return application


@pytest.fixture()
def client(app, settings):
    from fastapi.testclient import TestClient

    # base_url host 127.0.0.1 keeps the Host allowlist happy.
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        test_client.headers.update({"X-Local-Token": settings.token})
        yield test_client


def auth_headers(settings) -> dict[str, str]:
    """Default auth header helper for tests that build their own client."""
    return {"X-Local-Token": settings.token}
