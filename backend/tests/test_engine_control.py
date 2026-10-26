"""Engine control endpoints (patch v22): STOP ALL / Pause / Resume.

The execution flags themselves are proven at the runner level by
test_stop_all_live.py; these tests pin the HTTP wire added in v22 —
the routes the UI's STOP ALL button actually calls:

* GET  /api/v1/engine/status  reports the persisted flags.
* POST /api/v1/engine/pause   sets pause_all only.
* POST /api/v1/engine/stop-all sets both flags AND cancels waiting
  (queued) jobs so nothing is left ready to start.
* POST /api/v1/engine/resume clears both flags (and reports that no
  service was attached to restart in this bare test app).

The flags live in the session-shared test DB (conftest pins
YTA_DATA_DIR), so every test clears them again on teardown — a leaked
stop_all would halt other suites' runners.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db as db_module
from app.errors import register_exception_handlers
from app.execution.flags import read_flags, set_flag
from app.queue import SQLiteJobQueue
from app.routers import api as api_module


def _build_app() -> FastAPI:
    app = FastAPI(title="YouTube Agent — engine control tests")
    register_exception_handlers(app)
    app.include_router(api_module.router)
    return app


def _factory():
    return lambda: db_module.get_connection()


@pytest.fixture(autouse=True)
def _clean_flags():
    set_flag(_factory(), "stop_all", False)
    set_flag(_factory(), "pause_all", False)
    yield
    set_flag(_factory(), "stop_all", False)
    set_flag(_factory(), "pause_all", False)


def test_status_defaults_clear():
    client = TestClient(_build_app())
    res = client.get("/api/v1/engine/status")
    assert res.status_code == 200
    body = res.json()
    assert body["pause_all"] is False
    assert body["stop_all"] is False
    assert body["service_attached"] is False


def test_pause_sets_only_pause_all():
    client = TestClient(_build_app())
    res = client.post("/api/v1/engine/pause")
    assert res.status_code == 202
    assert res.json()["pause_all"] is True
    assert res.json()["stop_all"] is False
    flags = read_flags(_factory())
    assert flags.pause_all is True
    assert flags.stop_all is False


def test_stop_all_sets_flags_and_cancels_queued_jobs(tmp_path):
    # The session test DB has no jobs table (the queue suites build
    # their own schema via make_db); point this app at such a DB —
    # the endpoint reads app.state.db_path for its factory.
    from tests.execution_fixtures import make_db

    db_path, factory = make_db(tmp_path)
    queue = SQLiteJobQueue(factory)
    job_id = queue.enqueue("stage.script", {"test": "engine-stop-all"})
    assert queue.get(job_id)["status"] == "queued"

    app = _build_app()
    app.state.db_path = str(db_path)
    client = TestClient(app)
    res = client.post("/api/v1/engine/stop-all")
    assert res.status_code == 202
    body = res.json()
    assert body["stop_all"] is True
    assert body["pause_all"] is True
    assert body["jobs_cancelled"] >= 1

    flags = read_flags(factory)
    assert flags.stop_all is True
    assert flags.pause_all is True
    assert queue.get(job_id)["status"] == "cancelled"


def test_resume_clears_flags():
    client = TestClient(_build_app())
    assert client.post("/api/v1/engine/stop-all").status_code == 202
    res = client.post("/api/v1/engine/resume")
    assert res.status_code == 202
    body = res.json()
    assert body["stop_all"] is False
    assert body["pause_all"] is False
    # The bare test app has no execution service attached.
    assert body["service_restarted"] is False
    flags = read_flags(_factory())
    assert flags.stop_all is False
    assert flags.pause_all is False
