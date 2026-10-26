"""Engine control endpoints — STOP ALL / Pause All / Resume.

The execution engine has always honoured two persisted kill-switch
flags (``app.execution.flags``: ``execution.stop_all`` cancels
in-flight jobs and exits the runner loops; ``execution.pause_all``
stops new claims while in-flight work finishes), but until now no HTTP
route could set them — ``tests/test_stop_all_live.py`` documents the
gap ("no HTTP router for execution flags exists in the backend"), and
the UI's STOP ALL button was a visual placeholder for the same reason.

These endpoints are the missing wire:

* ``GET  /engine/status``  — the current flag state (plus whether an
  execution service is attached), so the UI can render the real
  engine state and offer Resume when stopped/paused.
* ``POST /engine/stop-all`` — set ``stop_all`` (+ ``pause_all``) and
  cancel every queued/leased job immediately. Leased/running jobs are
  cancelled by the runner itself when it observes the flag (prompt
  task cancellation, proven by test_stop_all_live); the queued sweep
  here makes sure nothing is left waiting to start.
* ``POST /engine/pause`` — set ``pause_all`` only.
* ``POST /engine/resume`` — clear both flags and, when an execution
  service is attached, restart its loops: clearing the flag alone
  does NOT resurrect a runner that already exited on stop_all.

The toggles are naturally idempotent (writing the same flag twice is
a no-op), so — like ``POST /system/shutdown`` — they do not require
an idempotency key.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import db as _db
from app.execution.flags import read_flags, set_flag

router = APIRouter(prefix="/api/v1/engine", tags=["engine"])


def _conn_factory(request: Request):
    db_path = getattr(request.app.state, "db_path", None)
    return lambda: _db.get_connection(db_path)


def _status_payload(request: Request) -> dict[str, Any]:
    flags = read_flags(_conn_factory(request))
    service = getattr(request.app.state, "execution", None)
    return {
        "pause_all": bool(flags.pause_all),
        "stop_all": bool(flags.stop_all),
        "service_attached": service is not None,
    }


def _cancel_waiting_jobs(request: Request) -> int:
    """Cancel queued/leased jobs so a stop leaves nothing waiting.

    Mirrors the queue access pattern of ``app.routers.jobs``: build a
    ``SQLiteJobQueue`` over the app DB and cancel row by row. Returns
    how many jobs this call cancelled (0 when no queue is available).
    """
    try:
        from app.queue import SQLiteJobQueue

        queue = SQLiteJobQueue(_conn_factory(request))
    except Exception:
        return 0
    # The queue class has no list method; read the waiting ids straight
    # from the jobs table (same table app.routers.jobs reads).
    job_ids: list[str] = []
    try:
        conn = _conn_factory(request)()
        try:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status IN ('queued', 'leased')"
            ).fetchall()
            job_ids = [str(row[0]) for row in rows]
        finally:
            conn.close()
    except Exception:
        job_ids = []
    cancelled = 0
    for job_id in job_ids:
        try:
            if queue.cancel(job_id):
                cancelled += 1
        except Exception:
            continue
    return cancelled


@router.get("/status")
async def engine_status(request: Request) -> dict[str, Any]:
    return _status_payload(request)


@router.post("/stop-all", status_code=202)
async def engine_stop_all(request: Request) -> JSONResponse:
    factory = _conn_factory(request)
    set_flag(factory, "stop_all", True)
    set_flag(factory, "pause_all", True)
    cancelled = _cancel_waiting_jobs(request)
    payload = _status_payload(request)
    payload["jobs_cancelled"] = cancelled
    return JSONResponse(status_code=202, content=payload)


@router.post("/pause", status_code=202)
async def engine_pause(request: Request) -> JSONResponse:
    set_flag(_conn_factory(request), "pause_all", True)
    return JSONResponse(status_code=202, content=_status_payload(request))


@router.post("/resume", status_code=202)
async def engine_resume(request: Request) -> JSONResponse:
    factory = _conn_factory(request)
    # Clear the flags BEFORE restarting anything, so a revived runner
    # does not observe stop_all on its first poll and exit again.
    set_flag(factory, "stop_all", False)
    set_flag(factory, "pause_all", False)
    restarted = False
    service = getattr(request.app.state, "execution", None)
    if service is not None:
        try:
            await service.stop()
            await service.start()
            restarted = True
        except Exception:
            restarted = False
    payload = _status_payload(request)
    payload["service_restarted"] = restarted
    return JSONResponse(status_code=202, content=payload)
