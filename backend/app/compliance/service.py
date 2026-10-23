"""Compliance Gate service — stored state + deliberate transitions.

The compliance state lives in the ``compliance_items`` /
``compliance_transitions`` tables (see :mod:`app.compliance.schema`).
There is exactly ONE write path: :func:`transition_item`, a deliberate
logged action which

* requires an ``actor`` (who) — anonymous state changes do not exist;
* requires ``evidence`` when the target status is ``passed`` (the
  stored evidence may satisfy this if it was recorded earlier, but a
  pass with NO evidence anywhere is refused);
* appends one ``compliance_transitions`` row per change (from/to,
  actor, evidence, note, timestamp) — the audit/history record.

The overall state is always derived from the items, never stored,
so it can never drift from the checklist.  Reads return the full
honest picture the Settings UI renders: per-item status + who/when +
evidence/note, the derived overall, the recent history, and the
practical effect (public publishing allowed or blocked/forced
private).
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from app.compliance.gate import REASON_CODE, REASON_MESSAGE
from app.compliance.schema import (
    ITEM_DEFINITIONS,
    ITEM_KEYS,
    ITEM_STATUSES,
    derive_overall,
    ensure_compliance_schema,
)

__all__ = [
    "ComplianceTransitionError",
    "default_state",
    "get_state",
    "get_state_from_default_db",
    "overall_from_conn",
    "transition_item",
    "transition_on_default_db",
]

_LABELS = {key: label for key, label, _ in ITEM_DEFINITIONS}
_DESCRIPTIONS = {key: desc for key, _, desc in ITEM_DEFINITIONS}


class ComplianceTransitionError(Exception):
    """Typed refusal of a compliance transition.

    ``code`` is the machine reason (the router maps it onto the API
    error envelope); ``status_code`` is the HTTP class.
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _now_ms() -> int:
    return int(time.time() * 1000)


def _commit_quietly(conn: Any) -> None:
    try:
        conn.commit()
    except Exception:
        pass


def _item_dict(key: str, row: Optional[dict]) -> dict:
    if row is None:
        return {
            "key": key,
            "label": _LABELS[key],
            "description": _DESCRIPTIONS[key],
            "status": "pending",
            "evidence": None,
            "note": None,
            "updated_by": None,
            "updated_at": None,
            "created_at": None,
        }
    return {
        "key": key,
        "label": _LABELS[key],
        "description": _DESCRIPTIONS[key],
        "status": row.get("status") or "pending",
        "evidence": row.get("evidence"),
        "note": row.get("note"),
        "updated_by": row.get("updated_by"),
        "updated_at": row.get("updated_at"),
        "created_at": row.get("created_at"),
    }


def default_state() -> dict:
    """The honest fresh-install state: nothing started, public blocked."""
    items = [_item_dict(key, None) for key in ITEM_KEYS]
    return {
        "overall": "not_started",
        "passed": False,
        "public_publishing_allowed": False,
        "forced_private": True,
        "reason_code": REASON_CODE,
        "effect": REASON_MESSAGE,
        "items": items,
        "history": [],
    }


def get_state(conn: Any, *, history_limit: int = 50) -> dict:
    """Full compliance state from ``conn`` (schema ensured + seeded)."""
    ensure_compliance_schema(conn)
    rows = {}
    for row in conn.execute("SELECT * FROM compliance_items"):
        item = dict(row)
        rows[str(item["key"])] = item
    items = [_item_dict(key, rows.get(key)) for key in ITEM_KEYS]
    overall = derive_overall([item["status"] for item in items])
    passed = overall == "passed"
    history: list[dict] = []
    for row in conn.execute(
        "SELECT * FROM compliance_transitions ORDER BY created_at DESC, rowid DESC "
        "LIMIT ?",
        (int(history_limit),),
    ):
        entry = dict(row)
        history.append(
            {
                "id": entry.get("id"),
                "item_key": entry.get("item_key"),
                "from_status": entry.get("from_status"),
                "to_status": entry.get("to_status"),
                "actor": entry.get("actor"),
                "evidence": entry.get("evidence"),
                "note": entry.get("note"),
                "created_at": entry.get("created_at"),
            }
        )
    return {
        "overall": overall,
        "passed": passed,
        "public_publishing_allowed": passed,
        "forced_private": not passed,
        "reason_code": None if passed else REASON_CODE,
        "effect": (
            "Compliance audit passed — public publishing is allowed "
            "and requested visibility is honoured."
            if passed
            else REASON_MESSAGE
        ),
        "items": items,
        "history": history,
    }


def overall_from_conn(conn: Any) -> str:
    """Derived overall state for ``conn``; ``not_started`` on any failure."""
    try:
        return str(get_state(conn)["overall"])
    except Exception:
        return "not_started"


def transition_item(
    conn: Any,
    *,
    item_key: str,
    to_status: str,
    actor: Optional[str],
    evidence: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """Apply ONE deliberate, logged checklist transition.

    Returns the full post-transition state.  Raises
    :class:`ComplianceTransitionError` (typed) for an unknown item,
    an invalid status, a missing actor, a no-op change, or a
    ``passed`` transition without evidence.
    """
    ensure_compliance_schema(conn)
    key = (item_key or "").strip()
    if key not in ITEM_KEYS:
        raise ComplianceTransitionError(
            404,
            "compliance_item_not_found",
            f"compliance item {item_key!r} is not on the checklist",
        )
    target = (to_status or "").strip().lower()
    if target not in ITEM_STATUSES:
        raise ComplianceTransitionError(
            422,
            "compliance_status_invalid",
            f"status must be one of {list(ITEM_STATUSES)}; got {to_status!r}",
        )
    who = (actor or "").strip()
    if not who:
        raise ComplianceTransitionError(
            422,
            "compliance_actor_required",
            "a compliance transition must record who made it (actor)",
        )
    row = conn.execute(
        "SELECT * FROM compliance_items WHERE key = ?", (key,)
    ).fetchone()
    current = dict(row) if row is not None else None
    from_status = (current or {}).get("status") or "pending"
    if target == from_status:
        raise ComplianceTransitionError(
            422,
            "compliance_transition_noop",
            f"item {key} is already {target}; no transition was recorded",
        )
    new_evidence = evidence.strip() if isinstance(evidence, str) and evidence.strip() else None
    effective_evidence = new_evidence or (current or {}).get("evidence")
    if target == "passed" and not effective_evidence:
        raise ComplianceTransitionError(
            422,
            "compliance_evidence_required",
            f"item {key} cannot be marked passed without evidence "
            "(e.g. the audit approval reference, the live policy URL, "
            "or the verified project id)",
        )
    new_note = note.strip() if isinstance(note, str) and note.strip() else None
    effective_note = new_note if new_note is not None else (current or {}).get("note")
    now = _now_ms()
    conn.execute(
        "UPDATE compliance_items SET status = ?, evidence = ?, note = ?, "
        "updated_by = ?, updated_at = ? WHERE key = ?",
        (target, effective_evidence, effective_note, who, now, key),
    )
    conn.execute(
        "INSERT INTO compliance_transitions "
        "(id, item_key, from_status, to_status, actor, evidence, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uuid.uuid4().hex,
            key,
            from_status,
            target,
            who,
            effective_evidence,
            effective_note,
            now,
        ),
    )
    _commit_quietly(conn)
    return get_state(conn)


# ---------------------------------------------------------------------------
# Default-DB convenience (the app's configured SQLite file)
# ---------------------------------------------------------------------------


def _default_conn():
    from app import db as db_module

    return db_module.get_connection()


def get_state_from_default_db() -> dict:
    """State from the app's default DB; honest default on any failure."""
    try:
        conn = _default_conn()
    except Exception:
        return default_state()
    try:
        return get_state(conn)
    except Exception:
        return default_state()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def transition_on_default_db(**kwargs: Any) -> dict:
    """Apply a transition against the app's default DB."""
    conn = _default_conn()
    try:
        return transition_item(conn, **kwargs)
    finally:
        try:
            conn.close()
        except Exception:
            pass
