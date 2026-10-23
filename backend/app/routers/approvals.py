"""Approval gate endpoints (roadmap section 31 — Gates A & B).

The approval queue is fed by the pipeline (Gate A script lock, Gate B
pre-publish).  Decisions are persisted in the ``approvals`` table
(migration 0002) so they survive a backend restart.

Persistence design (three paths, in priority order):

1. **Pending-row path** — a pipeline-created row already exists for
   the approval id: the decision UPDATEs it (decision, reason_code,
   note, decided_by, status='decided', decided_at epoch-ms).
2. **Insert-with-body-fields path** — no row exists, but the decision
   body carries both ``project_id`` and a valid ``gate`` ('a'/'b'):
   a fully-decided row is INSERTed.
3. **In-memory fallback (legacy skeleton path)** — a bare approval id
   with neither a pending row nor project/gate in the body cannot be
   stored (``approvals.project_id`` is NOT NULL with an FK to
   projects): the decision is kept in the module-level ``_DECISIONS``
   dict, exactly as the Round-1 skeleton behaved.  A restart forgets
   only these bare-id decisions.

The already-decided check consults the DB FIRST (409
``already_decided`` carries the STORED record), so a decision made
before a restart still blocks a second decision afterwards;
``_DECISIONS`` is only a fallback when no DB row exists.

The ``approvals`` table is ensured lazily (CREATE TABLE IF NOT EXISTS,
same DDL as migration 0002) inside the request helpers — never at
import time — so the router works whether or not alembic has run and
importing this module stays side-effect free for tests.

Decisions are idempotency-keyed; rejecting requires a ``reason_code``
(422 ``reason_code_required``).  After a persisted decision is stored,
the execution gates bridge (``app.execution.gates_bridge``, when
present) is asked to resume the project — a bridge failure is logged
and never fails the HTTP decision.  The resume call is
signature-aware: the bridge as landed expects a connection factory
as its first argument, so ``app.db.get_connection`` is supplied when
the first parameter is named ``conn_factory``.  Timestamps are epoch-ms INTEGERs
in the table; the HTTP decision record keeps the ISO ``decided_at``
string it has always returned.
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.errors import ApiError
from app.idempotency import require_idempotency_key
from app.idempotency import store as idem_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])

# Fallback decision log for the legacy bare-id path: approval_id ->
# decision record.  Authoritative decisions live in the DB table.
_DECISIONS: dict[str, dict[str, Any]] = {}

# Same DDL as migrations/versions/0002_approvals.py.
_APPROVALS_DDL = """
CREATE TABLE IF NOT EXISTS approvals (
    id                     TEXT PRIMARY KEY,
    project_id             TEXT NOT NULL
                           REFERENCES projects (id) ON DELETE RESTRICT,
    gate                   TEXT NOT NULL CHECK (gate IN ('a', 'b')),
    artefact_versions_json TEXT NULL,
    decision               TEXT NULL
                           CHECK (decision IN ('approved', 'rejected')),
    reason_code            TEXT NULL,
    note                   TEXT NULL,
    decided_by             TEXT NULL,
    status                 TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'decided')),
    created_at             INTEGER NOT NULL,
    decided_at             INTEGER NULL
)
"""
_APPROVALS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_approvals_project_gate "
    "ON approvals (project_id, gate)",
    "CREATE INDEX IF NOT EXISTS idx_approvals_status "
    "ON approvals (status)",
)

_VALID_GATES = ("a", "b")


class DecisionBody(BaseModel):
    reason_code: Optional[str] = None
    note: Optional[str] = None
    # Optional persistence context: when both project_id and gate are
    # supplied (and no pending row exists) a decided row is INSERTed.
    project_id: Optional[str] = None
    gate: Optional[str] = None
    decided_by: Optional[str] = None
    artefact_versions: Optional[Any] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ms_to_iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _conn():
    try:
        from app import db as _db

        return _db.get_connection()
    except Exception:
        return None


def _ensure_approvals_table(conn) -> None:
    """Create the approvals table + indexes if absent (lazy, not import)."""
    conn.execute(_APPROVALS_DDL)
    for statement in _APPROVALS_INDEXES:
        conn.execute(statement)
    try:
        conn.commit()
    except Exception:
        pass


def _fetch_row(conn, approval_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM approvals WHERE id = ?", (approval_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def _row_is_decided(row: dict[str, Any]) -> bool:
    return row.get("status") == "decided" or row.get("decision") is not None


def _row_to_record(row: dict[str, Any]) -> dict[str, Any]:
    """Stored row -> the HTTP decision-record shape (ISO decided_at)."""
    decided_at = row.get("decided_at")
    if isinstance(decided_at, (int, float)):
        decided_iso = _ms_to_iso(int(decided_at))
    elif decided_at:
        decided_iso = str(decided_at)
    else:
        decided_iso = _now_iso()
    return {
        "id": row.get("id"),
        "decision": row.get("decision"),
        "reason_code": row.get("reason_code"),
        "note": row.get("note"),
        "status": "decided",
        "decided_at": decided_iso,
    }


def _row_to_item(row: dict[str, Any]) -> dict[str, Any]:
    """Stored row -> list-endpoint item (artefact_versions parsed)."""
    raw_versions = row.get("artefact_versions_json")
    artefact_versions: Any = None
    if raw_versions:
        try:
            artefact_versions = json.loads(raw_versions)
        except (TypeError, ValueError):
            artefact_versions = None
    return {
        "id": row.get("id"),
        "project_id": row.get("project_id"),
        "gate": row.get("gate"),
        "artefact_versions": artefact_versions,
        "decision": row.get("decision"),
        "reason_code": row.get("reason_code"),
        "note": row.get("note"),
        "decided_by": row.get("decided_by"),
        "status": row.get("status") or "pending",
        "created_at": row.get("created_at"),
        "decided_at": row.get("decided_at"),
    }


def _db_decided_record(approval_id: str) -> Optional[dict[str, Any]]:
    """The stored decision record for ``approval_id``, or None.

    This is the restart-proof already-decided check: it reads the DB
    table, not process memory.
    """
    conn = _conn()
    if conn is None:
        return None
    try:
        _ensure_approvals_table(conn)
        row = _fetch_row(conn, approval_id)
        if row is not None and _row_is_decided(row):
            return _row_to_record(row)
        return None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _persist_decision(
    approval_id: str,
    decision: str,
    body: DecisionBody,
    now_ms: int,
) -> tuple[bool, Optional[str], Optional[str]]:
    """Persist a decision; return (persisted, project_id, gate).

    Pending row -> UPDATE.  No row + project_id/gate in the body ->
    INSERT a decided row.  Otherwise (bare legacy id) -> not
    persisted; the caller keeps the in-memory record only.
    """
    conn = _conn()
    if conn is None:
        return False, None, None
    try:
        _ensure_approvals_table(conn)
        row = _fetch_row(conn, approval_id)
        reason_code = body.reason_code if decision == "rejected" else None
        decided_by = body.decided_by or "user"
        if row is not None:
            assignments = (
                "decision = ?, reason_code = ?, note = ?, decided_by = ?, "
                "status = 'decided', decided_at = ?"
            )
            params: list[Any] = [
                decision,
                reason_code,
                body.note,
                decided_by,
                now_ms,
            ]
            if body.artefact_versions is not None:
                assignments += ", artefact_versions_json = ?"
                params.append(json.dumps(body.artefact_versions))
            params.append(approval_id)
            conn.execute(
                f"UPDATE approvals SET {assignments} WHERE id = ?", params
            )
            try:
                conn.commit()
            except Exception:
                pass
            return True, row.get("project_id"), row.get("gate")
        if body.project_id and body.gate in _VALID_GATES:
            conn.execute(
                "INSERT INTO approvals (id, project_id, gate, "
                "artefact_versions_json, decision, reason_code, note, "
                "decided_by, status, created_at, decided_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'decided', ?, ?)",
                (
                    approval_id,
                    body.project_id,
                    body.gate,
                    json.dumps(body.artefact_versions)
                    if body.artefact_versions is not None
                    else None,
                    decision,
                    reason_code,
                    body.note,
                    decided_by,
                    now_ms,
                    now_ms,
                ),
            )
            try:
                conn.commit()
            except Exception:
                pass
            return True, body.project_id, body.gate
        return False, None, None
    except Exception:
        logger.warning(
            "approvals: could not persist decision for %s", approval_id,
            exc_info=True,
        )
        return False, None, None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _trigger_resume(
    project_id: Optional[str],
    gate: Optional[str],
    decision: str,
    approval_id: str,
    reason_code: Optional[str],
) -> None:
    """Ask the execution gates bridge to resume; never fail the decision."""
    if not project_id or not gate:
        return
    try:
        from app.execution import gates_bridge

        # The bridge as landed takes a connection factory first
        # (apply_decision(conn_factory, project_id, gate, decision,
        # ...)); a spec-shaped bridge takes the project first.  Detect
        # which one is present and call it accordingly.
        params = [
            p
            for p in inspect.signature(
                gates_bridge.apply_decision
            ).parameters.values()
            if p.kind
            in (inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        if params and params[0].name == "conn_factory":
            from app import db as _db

            gates_bridge.apply_decision(
                _db.get_connection,
                project_id,
                gate,
                decision,
                approval_id=approval_id,
                reason_code=reason_code,
            )
        else:
            gates_bridge.apply_decision(
                project_id,
                gate,
                decision,
                approval_id=approval_id,
                reason_code=reason_code,
            )
    except Exception:
        logger.warning(
            "approvals: gates_bridge resume failed for approval %s "
            "(project %s, gate %s)",
            approval_id,
            project_id,
            gate,
            exc_info=True,
        )


def _queue_items() -> list[dict[str, Any]]:
    conn = _conn()
    if conn is None:
        return []
    try:
        _ensure_approvals_table(conn)
        rows = conn.execute(
            "SELECT * FROM approvals ORDER BY created_at ASC, id ASC"
        ).fetchall()
        return [_row_to_item(dict(r)) for r in rows]
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


@router.get("")
async def list_approvals() -> dict:
    return {"items": _queue_items(), "next_cursor": None}


async def _decide(
    approval_id: str, decision: str, body: DecisionBody, request: Request
):
    key = require_idempotency_key(request)
    replay = await idem_store.check(key, request)
    if replay is not None:
        return replay
    if decision == "rejected" and not (body.reason_code or "").strip():
        raise ApiError(
            422,
            "reason_code_required",
            "Rejecting an approval requires a reason_code.",
            details={"field": "reason_code"},
        )
    # DB first: a decision stored before a restart still counts.
    stored = _db_decided_record(approval_id)
    if stored is not None:
        raise ApiError(
            409,
            "already_decided",
            f"Approval '{approval_id}' has already been decided.",
            details={"decision": stored},
        )
    if approval_id in _DECISIONS:
        raise ApiError(
            409,
            "already_decided",
            f"Approval '{approval_id}' has already been decided.",
            details={"decision": _DECISIONS[approval_id]},
        )
    now_ms = _now_ms()
    record = {
        "id": approval_id,
        "decision": decision,
        "reason_code": body.reason_code if decision == "rejected" else None,
        "note": body.note,
        "status": "decided",
        "decided_at": _ms_to_iso(now_ms),
    }
    persisted, project_id, gate = _persist_decision(
        approval_id, decision, body, now_ms
    )
    _DECISIONS[approval_id] = record
    if persisted:
        _trigger_resume(
            project_id,
            gate,
            decision,
            approval_id,
            record["reason_code"],
        )
    response = JSONResponse(status_code=200, content=record)
    await idem_store.remember(key, request, 200, record)
    return response


@router.post("/{approval_id}/approve")
async def approve(approval_id: str, body: DecisionBody, request: Request):
    return await _decide(approval_id, "approved", body, request)


@router.post("/{approval_id}/reject")
async def reject(approval_id: str, body: DecisionBody, request: Request):
    return await _decide(approval_id, "rejected", body, request)
