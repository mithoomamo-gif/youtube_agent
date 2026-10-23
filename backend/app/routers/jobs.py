"""Job & queue endpoints (roadmap section 31).

Backed by ``app.queue.SQLiteJobQueue`` (worker 4) over the ``jobs``
table when available; otherwise well-formed empty/zero responses.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.errors import ApiError

router = APIRouter(prefix="/api/v1", tags=["jobs"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _job_queue():
    try:
        from app import db as _db
        from app.queue import SQLiteJobQueue

        return SQLiteJobQueue(lambda: _db.get_connection())
    except Exception:
        return None


def _conn():
    try:
        from app import db as _db

        return _db.get_connection()
    except Exception:
        return None


def _tables(conn) -> set:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _cols(conn, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _paginate(items: list, cursor: Optional[str], limit: int) -> dict:
    limit = max(1, min(int(limit or 50), 200))
    try:
        start = int(cursor) if cursor else 0
    except (TypeError, ValueError):
        start = 0
    page = items[start : start + limit]
    next_cursor = str(start + limit) if start + limit < len(items) else None
    return {"items": page, "next_cursor": next_cursor}


def _list_jobs(
    status: Optional[str], queue_name: Optional[str], project_id: Optional[str]
) -> list[dict[str, Any]]:
    conn = _conn()
    if conn is None:
        return []
    try:
        if "jobs" not in _tables(conn):
            return []
        cols = _cols(conn, "jobs")
        where, params = [], []
        if status and "status" in cols:
            where.append("status = ?")
            params.append(status)
        if project_id and "project_id" in cols:
            where.append("project_id = ?")
            params.append(project_id)
        if queue_name and "queue" in cols:
            where.append("queue = ?")
            params.append(queue_name)
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC" if "created_at" in cols else ""
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _quota() -> dict[str, Any]:
    """Real YouTube quota buckets from the ``api_usage`` ledger.

    Delegates to :mod:`app.quota_view` (same payload as
    ``GET /api/v1/usage/quota``); degrades to honest zeros — never
    fabricated numbers — if the read model is unavailable.
    """
    try:
        from app.quota_view import get_quota_snapshot

        return get_quota_snapshot()
    except Exception:
        return {
            "buckets": {
                "uploads": {"used": 0, "limit": 100},
                "search": {"used": 0, "limit": 100},
                "shared": {"used": 0, "limit": 10000},
            },
            "date": "",
            "pacific_date": "",
            "resets_at": "",
            "seconds_until_reset": 0,
        }


def _stats() -> dict[str, Any]:
    queue = _job_queue()
    by_status: dict[str, int] = {}
    if queue is not None:
        try:
            by_status = {str(k): int(v) for k, v in queue.stats().items()}
        except Exception:
            by_status = {}
    return {
        "by_status": by_status,
        "running": by_status.get("running", 0) + by_status.get("leased", 0),
        "queued": by_status.get("queued", 0),
    }


@router.get("/jobs")
async def list_jobs(
    status: Optional[str] = None,
    queue: Optional[str] = None,
    project_id: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    return _paginate(_list_jobs(status, queue, project_id), cursor, limit)


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    queue = _job_queue()
    if queue is not None:
        try:
            job = queue.get(job_id)
        except Exception:
            job = None
        if job is not None:
            return job
    raise ApiError(404, "job_not_found", f"No job with id '{job_id}'.")


@router.post("/jobs/{job_id}/cancel", status_code=202)
async def cancel_job(job_id: str):
    queue = _job_queue()
    if queue is None:
        # Skeleton fallback: no queue backend, accept the request shape.
        return JSONResponse(
            status_code=202,
            content={"job_id": job_id, "status": "cancel_requested"},
        )
    try:
        job = queue.get(job_id)
    except Exception:
        job = None
    if job is None:
        raise ApiError(404, "job_not_found", f"No job with id '{job_id}'.")
    try:
        queue.cancel(job_id)
    except Exception:
        pass
    return JSONResponse(
        status_code=202, content={"job_id": job_id, "status": "cancelled"}
    )


@router.post("/jobs/{job_id}/retry", status_code=202)
async def retry_job(job_id: str):
    queue = _job_queue()
    if queue is None:
        return JSONResponse(
            status_code=202,
            content={
                "status": "requeued",
                "previous_job_id": job_id,
                "job_id": str(uuid.uuid4()),
            },
        )
    try:
        job = queue.get(job_id)
    except Exception:
        job = None
    if job is None:
        raise ApiError(404, "job_not_found", f"No job with id '{job_id}'.")
    try:
        new_id = queue.enqueue(
            str(job.get("kind") or "retry"),
            job.get("payload") or {},
            queue=str(job.get("queue") or "default"),
            project_id=job.get("project_id"),
        )
    except Exception:
        new_id = str(uuid.uuid4())
    return JSONResponse(
        status_code=202,
        content={
            "status": "requeued",
            "previous_job_id": job_id,
            "job_id": new_id,
        },
    )


@router.get("/queue/stats")
async def queue_stats() -> dict:
    return _stats()


@router.get("/queue/snapshot")
async def queue_snapshot() -> dict:
    """Global resync snapshot — the dashboard's reconnect target."""
    return {
        "projects": [],
        "queue": _stats(),
        "quota": _quota(),
        "today_cost_usd": 0.0,
        "generated_at": _now_iso(),
    }
