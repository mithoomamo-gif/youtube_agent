"""Project endpoints (roadmap section 31).  One project = one video.

Creation is a work-creating POST: it REQUIRES an ``Idempotency-Key``
header (400 ``idempotency_key_required`` when missing) and honours the
24h idempotency store (same key + same request replays the original
201; same key + different request is a 409 conflict).

Persistence is best-effort against the ``projects`` table (FKs to
channels / niche_profiles are enforced by app.db, so a draft whose
channel does not exist yet is kept in the in-memory skeleton store and
still returned — the Supervisor/DAG generation is later work; the
``task_dag`` in the response is a summary stub).

Reads are read-through (R2-002): SQLite is the single source of truth,
so whenever a ``projects`` row exists its current values win over the
in-memory creation-time copy — the execution engine advances the DB
row (``status`` / ``current_phase`` / gates …) without ever touching
this module's dict.  The in-memory copy only fills response-only
fields the table has no column for (``topic_mode``, ``task_dag``) and
serves projects whose best-effort insert was dropped (unseeded FK
parents).
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.errors import ApiError
from app.idempotency import require_idempotency_key
from app.idempotency import store as idem_store

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

_PROJECTS: dict[str, dict[str, Any]] = {}
_PIPELINE_ACTIONS = {"start", "pause", "resume", "cancel"}
_PHASES = [
    "research",
    "scripting",
    "production",
    "editing",
    "packaging",
    "publishing",
    "growth",
    "safety_qa",
]


class ProjectCreate(BaseModel):
    channel_id: str
    niche_profile_id: str
    topic_mode: str = "auto"
    topic: Optional[str] = None
    target_minutes: int = 10
    publish_mode: str = "review_first"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _now_ms() -> int:
    return int(time.time() * 1000)


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


def _job_queue():
    try:
        from app import db as _db
        from app.queue import SQLiteJobQueue

        return SQLiteJobQueue(lambda: _db.get_connection())
    except Exception:
        return None


def _db_projects() -> list[dict[str, Any]]:
    conn = _conn()
    if conn is None:
        return []
    try:
        if "projects" not in _tables(conn):
            return []
        rows = conn.execute(
            "SELECT * FROM projects WHERE deleted_at IS NULL "
            "ORDER BY created_at DESC"
            if "deleted_at" in _cols(conn, "projects")
            else "SELECT * FROM projects ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _db_project(project_id: str) -> Optional[dict[str, Any]]:
    """Fetch one project's *current* DB row, or None.

    Mirrors :func:`_db_projects`' soft-delete filter.  Returns None when
    the DB / table is unavailable or no (live) row exists — callers
    then fall back to the in-memory creation-time copy.
    """
    conn = _conn()
    if conn is None:
        return None
    try:
        if "projects" not in _tables(conn):
            return None
        sql = "SELECT * FROM projects WHERE id = ?"
        if "deleted_at" in _cols(conn, "projects"):
            sql += " AND deleted_at IS NULL"
        row = conn.execute(sql, (project_id,)).fetchone()
        return dict(row) if row is not None else None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _read_through_project(project_id: str) -> Optional[dict[str, Any]]:
    """Current state of one project — DB first, memory as filler.

    The DB row (re-read on every call) is authoritative for every
    column it carries, notably ``status`` and ``current_phase``, which
    the execution engine advances after creation (R2-002: the detail
    endpoint used to serve the creation-time dict first, so it reported
    ``status='draft'`` / ``current_phase=None`` forever).  The
    in-memory copy only supplies fields the table has no column for
    (``topic_mode``, ``task_dag``); when no DB row exists (best-effort
    insert dropped on unseeded FK parents) it is served as-is, exactly
    as before.
    """
    cached = _PROJECTS.get(project_id)
    row = _db_project(project_id)
    if row is None:
        return dict(cached) if cached is not None else None
    if cached is None:
        return row
    # Keep the cache honest for any other in-process reader: it must
    # never again report a status/phase the DB has moved past.
    for field in ("status", "current_phase"):
        cached[field] = row.get(field)
    merged = dict(row)
    for key, value in cached.items():
        merged.setdefault(key, value)
    return merged


def _persist_project(project: dict[str, Any]) -> None:
    """Best-effort insert into the projects table (FK failures ignored:
    the draft may reference a channel/niche that only exists in the UI
    wizard so far).  When the insert lands, the DB row is authoritative
    for reads — see :func:`_read_through_project`."""
    conn = _conn()
    if conn is None:
        return
    try:
        if "projects" not in _tables(conn):
            return
        cols = _cols(conn, "projects")
        now = _now_ms()
        values: dict[str, Any] = {
            "id": project["id"],
            "channel_id": project["channel_id"],
            "niche_profile_id": project["niche_profile_id"],
            "topic": project.get("topic") or "",
            "topic_source": "user" if project["topic_mode"] == "manual" else "agent",
            "target_minutes": project["target_minutes"],
            "status": "draft",
            "publish_mode": project["publish_mode"]
            if project["publish_mode"] in ("auto", "review_first")
            else "review_first",
            "created_at": now,
            "updated_at": now,
        }
        use = {k: v for k, v in values.items() if k in cols}
        placeholders = ", ".join("?" for _ in use)
        conn.execute(
            f"INSERT OR IGNORE INTO projects ({', '.join(use)}) "
            f"VALUES ({placeholders})",
            tuple(use.values()),
        )
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


@router.get("")
async def list_projects(
    cursor: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    channel_id: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
) -> dict:
    items: list[dict[str, Any]] = []
    seen: set = set()
    for row in _db_projects():
        items.append(row)
        seen.add(row.get("id"))
    for pid, project in _PROJECTS.items():
        if pid not in seen:
            items.append(project)
    if channel_id:
        items = [p for p in items if p.get("channel_id") == channel_id]
    if status:
        items = [p for p in items if p.get("status") == status]
    if q:
        needle = q.lower()
        items = [
            p for p in items if needle in str(p.get("topic") or "").lower()
        ]
    return _paginate(items, cursor, limit)


@router.post("", status_code=201)
async def create_project(body: ProjectCreate, request: Request):
    key = require_idempotency_key(request)
    replay = await idem_store.check(key, request)
    if replay is not None:
        return replay

    if body.topic_mode not in ("auto", "manual"):
        raise ApiError(
            422,
            "validation_error",
            "topic_mode must be 'auto' or 'manual'.",
            details={"field": "topic_mode"},
        )
    if body.topic_mode == "manual" and not (body.topic and body.topic.strip()):
        raise ApiError(
            422,
            "topic_required",
            "topic_mode 'manual' requires a topic.",
            details={"field": "topic"},
        )

    project: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "channel_id": body.channel_id,
        "niche_profile_id": body.niche_profile_id,
        "topic_mode": body.topic_mode,
        "topic": body.topic,
        "target_minutes": body.target_minutes,
        "publish_mode": body.publish_mode,
        "status": "draft",
        "current_phase": None,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        # Summary stub — the real DAG is generated by the Supervisor
        # (orchestration team) when the pipeline starts.
        "task_dag": {
            "status": "not_started",
            "total_agents": 35,
            "phases": list(_PHASES),
        },
    }
    _PROJECTS[project["id"]] = project
    _persist_project(project)

    # Round 2: enqueue the real 10-job DAG for this project.  Lazy import
    # + try/except, like every other cross-team call in this module: if
    # the execution layer has not landed (or the queue is unavailable)
    # the summary stub above stays and creation still succeeds.  On
    # success the stub is replaced in-place — ``_PROJECTS`` holds the
    # same dict object, so the stored copy sees the enriched value too.
    # ``task_dag`` is not a projects-table column, so (as with the stub)
    # it remains response-only.
    try:
        from app.execution.dag import create_project_dag

        dag_queue = _job_queue()
        if dag_queue is not None:
            dag_result = create_project_dag(dag_queue, project["id"])
            if dag_result is not None and dag_result is not False:
                job_ids: list = []
                if isinstance(dag_result, (list, tuple)):
                    job_ids = list(dag_result)
                elif isinstance(dag_result, dict):
                    for _key in ("job_ids", "jobs", "ids"):
                        if isinstance(dag_result.get(_key), (list, tuple)):
                            job_ids = list(dag_result[_key])
                            break
                project["task_dag"] = {
                    "status": "queued",
                    "total_agents": 35,
                    "phases": list(_PHASES),
                    "job_ids": job_ids,
                }
    except Exception:
        pass  # keep the not_started stub — DAG creation is best-effort

    response = JSONResponse(status_code=201, content=project)
    await idem_store.remember(key, request, 201, project)
    return response


@router.get("/{project_id}")
async def get_project(project_id: str) -> dict:
    # Read-through (R2-002): the DB row is re-read on every request so
    # status/current_phase reflect the execution engine's progress,
    # not the creation-time in-memory snapshot.
    project = _read_through_project(project_id)
    if project is None:
        raise ApiError(
            404, "project_not_found", f"No project with id '{project_id}'."
        )
    return project


@router.post("/{project_id}/pipeline/{action}", status_code=202)
async def pipeline_action(project_id: str, action: str, request: Request):
    if action not in _PIPELINE_ACTIONS:
        raise ApiError(
            404,
            "unknown_pipeline_action",
            f"Unknown pipeline action '{action}'.",
            details={"allowed": sorted(_PIPELINE_ACTIONS)},
        )
    key = require_idempotency_key(request)
    replay = await idem_store.check(key, request)
    if replay is not None:
        return replay
    if project_id not in _PROJECTS and not any(
        row.get("id") == project_id for row in _db_projects()
    ):
        raise ApiError(
            404, "project_not_found", f"No project with id '{project_id}'."
        )

    job_id = str(uuid.uuid4())
    queue = _job_queue()
    if queue is not None:
        try:
            job_id = queue.enqueue(
                f"pipeline.{action}",
                {"project_id": project_id, "action": action},
                queue="default",
                idempotency_key=key,
                project_id=project_id
                if project_id in {r.get("id") for r in _db_projects()}
                else None,
            )
        except Exception:
            pass  # fall back to the stub job id above

    result = {
        "status": "accepted",
        "project_id": project_id,
        "action": action,
        "job_id": job_id,
    }
    response = JSONResponse(status_code=202, content=result)
    await idem_store.remember(key, request, 202, result)
    return response
