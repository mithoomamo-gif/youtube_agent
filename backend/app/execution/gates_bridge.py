"""Gate / approval bridge between the job runner, the DB and humans.

Resume paths — read this first
-------------------------------
There are two ways a paused gate can resume, and they are **not**
interchangeable:

* **PRIMARY — job-level payload flip** (:func:`apply_decision`).
  Production projects run as rows in the ``jobs`` table: a gate job
  (``kind = "gate.a"`` / ``"gate.b"``, payload ``{project_id, stage}``)
  raises ``GatePending`` and the runner requeues it (``run_after``
  +15 s) while it waits.  :func:`ensure_approval` creates the single
  pending ``approvals`` row for the gate and parks the project at
  ``status='awaiting_review'``.  When a human decides, the approvals
  router calls :func:`apply_decision`, which merges
  ``payload['approval'] = {decision, approval_id, reason_code}`` into
  that gate job and — for ``approved`` — zeroes ``run_after`` so the
  runner picks it up immediately.  On its next poll the job sees
  ``approval.decision == 'approved'`` and completes; ``'rejected'``
  fails it.  No graph state is involved on this path.

* **SECONDARY — LangGraph resume** (:func:`resume_graph`).
  Only for projects executed *directly* through the supervisor graph
  (``app.orchestration.graph.build_supervisor_graph``), whose gate
  nodes pause with ``langgraph.types.interrupt()``.  Resuming means
  ``graph.invoke(Command(resume={'decision': ...}), thread config)``.
  The approvals router does **not** use this path; it exists so
  graph-driven runs / tests can be resumed with the same decision
  vocabulary.

Idempotency: the runner polls a gate job repeatedly, so
:func:`ensure_approval` is idempotent — at most one *pending* approval
per ``(project_id, gate)``; repeat calls return the existing id and do
not re-emit ``gate.pending``.

The ``approvals`` table is owned by another Round-2 worker; because
test / startup order is not guaranteed, every entry point here creates
it defensively with ``CREATE TABLE IF NOT EXISTS`` (identical DDL)
before use.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

__all__ = [
    "APPROVALS_DDL",
    "ensure_approvals_table",
    "ensure_approval",
    "apply_decision",
    "find_pending_approval",
    "resume_graph",
]

#: Defensive DDL for the approvals table (Worker 3 owns the canonical
#: definition; the columns / value domains are the shared contract).
APPROVALS_DDL = """
CREATE TABLE IF NOT EXISTS approvals (
    id                     TEXT PRIMARY KEY,
    project_id             TEXT NOT NULL,
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


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _normalize_gate(gate: Any) -> str:
    """Normalise a gate label to ``'a'`` / ``'b'``.

    Accepts ``'a'``, ``'A'``, ``'gate_a'``, ``'gate.a'``, ``'gateA'`` …
    Raises :class:`ValueError` for anything else.
    """
    raw = str(gate or "").strip().lower()
    raw = raw.replace("-", "_")
    for prefix in ("gate_", "gate.", "gate"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    raw = raw.strip("_. ")
    if raw in ("a", "b"):
        return raw
    raise ValueError(f"unknown gate: {gate!r} (expected 'a' or 'b')")


def _gate_key(gate: Any) -> str:
    """``projects.gate_status_json`` key for a gate: ``gate_a``/``gate_b``."""
    return f"gate_{_normalize_gate(gate)}"


def _normalize_decision(decision: Any) -> str:
    raw = str(decision or "").strip().lower()
    if raw in ("approved", "approve", "approval", "pass", "passed", "yes", "ok", "accepted"):
        return "approved"
    if raw in ("rejected", "reject", "rejection", "fail", "failed", "no", "denied"):
        return "rejected"
    raise ValueError(
        f"unknown decision: {decision!r} (expected 'approved' or 'rejected')"
    )


def _parse_json(value: Any, default: Any = None) -> Any:
    if value is None:
        return {} if default is None else default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {} if default is None else default
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return {} if default is None else default
    return {} if default is None else default


def _row_to_dict(row: Any) -> Optional[dict]:
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    keys = getattr(row, "keys", None)
    if callable(keys):
        try:
            return {k: row[k] for k in keys()}
        except Exception:
            pass
    try:
        return dict(row)
    except Exception:
        return None


def _open_conn(conn_factory: Any):
    """Return ``(conn, should_close)`` for a factory or a bare connection.

    ``conn_factory`` is normally a zero-arg callable returning a fresh
    ``sqlite3.Connection`` (the ``app.db`` / queue pattern — callers
    close it).  A bare connection object is also accepted and is *not*
    closed, so tests may pass ``lambda: conn`` or the conn itself.
    """
    if conn_factory is None:
        raise TypeError("conn_factory is required")
    if callable(conn_factory):
        try:
            conn = conn_factory()
        except TypeError:
            # A factory that wants a path argument: pass None (default).
            conn = conn_factory(None)  # type: ignore[call-arg]
        # Lifecycle probe: gate calls are infrequent, so one extra
        # factory call is cheap insurance.  If a second call returns a
        # *different* connection this is a fresh-connection factory
        # (the app.db / queue pattern) and we own + close ours; if it
        # returns the *same* object (e.g. ``lambda: conn`` in tests)
        # the caller owns its lifecycle and we must not close it.
        try:
            probe = conn_factory()
        except Exception:
            return conn, True
        if probe is conn:
            return conn, False
        _close_quietly(probe)
        return conn, True
    if hasattr(conn_factory, "execute"):
        return conn_factory, False
    raise TypeError("conn_factory must be callable or a DB connection")


def _close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _commit_quietly(conn: Any) -> None:
    try:
        conn.commit()
    except Exception:
        pass


def ensure_approvals_table(conn: Any) -> None:
    """Create the ``approvals`` table on ``conn`` if it does not exist."""
    conn.execute(APPROVALS_DDL)
    _commit_quietly(conn)


def _project_id_of(project: Any) -> Optional[str]:
    if project is None:
        return None
    if isinstance(project, str):
        return project
    if isinstance(project, dict):
        value = project.get("id") or project.get("project_id")
        return str(value) if value is not None else None
    keys = getattr(project, "keys", None)
    if callable(keys):
        try:
            for name in ("id", "project_id"):
                if name in set(keys()) and project[name] is not None:
                    return str(project[name])
        except Exception:
            pass
    value = getattr(project, "id", None) or getattr(project, "project_id", None)
    return str(value) if value is not None else None


def _project_dict(project: Any) -> dict:
    if isinstance(project, dict):
        return dict(project)
    row = _row_to_dict(project)
    if row is not None:
        return row
    pid = _project_id_of(project)
    return {"id": pid} if pid else {}


def _load_project_row(conn: Any, project_id: str) -> Optional[dict]:
    try:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    except Exception:
        return None
    return _row_to_dict(row)


def _merge_gate_status(conn: Any, project_id: str, gate: str, value: str, *, project_status: Optional[str] = None, fallback_gate_status: Any = None) -> None:
    """Merge ``{gate_a|gate_b: value}`` into ``projects.gate_status_json``.

    The current JSON is read from the DB row when present (falling back
    to ``fallback_gate_status``), unrelated keys are preserved, and —
    when ``project_status`` is given — ``projects.status`` is updated
    in the same statement.  ``updated_at`` is bumped when the column
    exists in practice (it always does in the real schema).
    """
    current: dict = {}
    row = _load_project_row(conn, project_id)
    raw = None
    if row is not None:
        raw = row.get("gate_status_json")
    if raw is None:
        raw = fallback_gate_status
    parsed = _parse_json(raw, default={})
    if isinstance(parsed, dict):
        current = dict(parsed)
    current[_gate_key(gate)] = value
    now = _now_ms()
    if project_status is not None:
        conn.execute(
            "UPDATE projects SET status = ?, gate_status_json = ?,"
            " updated_at = ? WHERE id = ?",
            (project_status, json.dumps(current), now, project_id),
        )
    else:
        conn.execute(
            "UPDATE projects SET gate_status_json = ?, updated_at = ?"
            " WHERE id = ?",
            (json.dumps(current), now, project_id),
        )


def _artefact_versions_json(project: dict, job: Any) -> str:
    """Serialise whatever artefact / gate info the caller already has.

    Sources, in order: an explicit ``artefact_versions`` /
    ``artefact_versions_json`` / ``artefacts`` on the project (or the
    job / its payload), then the project's ``gate_status_json`` /
    ``gate_status`` recorded under ``gate_status``.  ``'{}'`` when
    nothing is available.
    """
    info: dict[str, Any] = {}

    job_dict = _project_dict(job) if job is not None else {}
    payload = _parse_json(job_dict.get("payload_json"), default={})
    if not isinstance(payload, dict):
        payload = _parse_json(job_dict.get("payload"), default={})
    if not isinstance(payload, dict):
        payload = {}

    for source in (project, job_dict, payload):
        if not isinstance(source, dict):
            continue
        for key in ("artefact_versions", "artefacts"):
            if source.get(key) is not None and key not in info:
                info[key] = _parse_json(source.get(key), default=source.get(key))
        raw_versions = source.get("artefact_versions_json")
        if raw_versions is not None and "artefact_versions" not in info:
            parsed = _parse_json(raw_versions, default=None)
            if isinstance(parsed, dict) and parsed:
                info.update(parsed) if not info else info.setdefault(
                    "artefact_versions", parsed
                )

    gate_status_raw = project.get("gate_status_json", project.get("gate_status"))
    if gate_status_raw is not None and "gate_status" not in info:
        parsed_gs = _parse_json(gate_status_raw, default=None)
        if isinstance(parsed_gs, dict) and parsed_gs:
            info["gate_status"] = parsed_gs
        elif isinstance(gate_status_raw, str) and gate_status_raw.strip():
            info["gate_status_json"] = gate_status_raw

    return json.dumps(info)


def _emit_gate_pending(events: Any, project_id: str, gate: str, approval_id: str) -> None:
    """Emit ``gate.pending`` through an EventEmitter / hub / callable."""
    if events is None:
        return
    try:
        gate_pending = getattr(events, "gate_pending", None)
        if callable(gate_pending):
            gate_pending(project_id, gate, approval_id)
            return
        publish = getattr(events, "publish", None)
        if callable(publish):
            publish(
                "gate.pending",
                {
                    "project_id": project_id,
                    "gate": gate,
                    "approval_id": approval_id,
                    "ts": _now_ms(),
                },
                project_id=project_id,
                job_id=None,
            )
            return
        if callable(events):
            events(project_id, gate, approval_id)
    except Exception:
        pass  # events never break the gate flow


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_pending_approval(
    conn_factory: Any, project_id: Any, gate: Any
) -> Optional[dict]:
    """The pending approval row for ``(project_id, gate)``, or ``None``."""
    gate_norm = _normalize_gate(gate)
    conn, should_close = _open_conn(conn_factory)
    try:
        ensure_approvals_table(conn)
        row = conn.execute(
            "SELECT * FROM approvals WHERE project_id = ? AND gate = ?"
            " AND status = 'pending' ORDER BY created_at ASC LIMIT 1",
            (str(project_id), gate_norm),
        ).fetchone()
        return _row_to_dict(row)
    finally:
        if should_close:
            _close_quietly(conn)


def ensure_approval(
    conn_factory: Any,
    project: dict,
    gate: str,
    job: dict,
    events: Any = None,
) -> str:
    """Ensure exactly one pending approval exists for a gate; return its id.

    Idempotent (the runner polls gate jobs): if a pending approval for
    ``(project_id, gate)`` already exists its id is returned and
    ``gate.pending`` is **not** emitted again.  Otherwise a new row
    (``id=uuid4``, ``status='pending'``, ``created_at`` in ms,
    ``artefact_versions_json`` from whatever the project/job already
    carry) is created, the project is parked at
    ``status='awaiting_review'`` with ``gate_status_json`` merged to
    ``{gate_a|gate_b: 'pending'}``, and ``gate.pending`` is emitted
    through ``events`` (an :class:`~app.execution.events.EventEmitter`)
    when provided.
    """
    gate_norm = _normalize_gate(gate)
    project_dict = _project_dict(project)
    project_id = _project_id_of(project)
    if not project_id:
        raise ValueError("ensure_approval: project has no id")
    # Prefer the caller's payload project_id if the dict disagrees —
    # they are the same in every real call; the DB row is the truth
    # for gate_status merging below either way.

    conn, should_close = _open_conn(conn_factory)
    try:
        ensure_approvals_table(conn)

        existing = conn.execute(
            "SELECT * FROM approvals WHERE project_id = ? AND gate = ?"
            " AND status = 'pending' ORDER BY created_at ASC LIMIT 1",
            (project_id, gate_norm),
        ).fetchone()
        existing_dict = _row_to_dict(existing)
        if existing_dict is not None:
            return str(existing_dict["id"])

        approval_id = str(uuid.uuid4())
        now = _now_ms()
        conn.execute(
            "INSERT INTO approvals (id, project_id, gate,"
            " artefact_versions_json, decision, reason_code, note,"
            " decided_by, status, created_at, decided_at)"
            " VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, 'pending', ?, NULL)",
            (
                approval_id,
                project_id,
                gate_norm,
                _artefact_versions_json(project_dict, job),
                now,
            ),
        )
        # Park the project for human review (best-effort if the row is
        # somehow absent — the approval row above is the deliverable).
        try:
            _merge_gate_status(
                conn,
                project_id,
                gate_norm,
                "pending",
                project_status="awaiting_review",
                fallback_gate_status=project_dict.get("gate_status_json"),
            )
        except Exception:
            pass
        _commit_quietly(conn)
    finally:
        if should_close:
            _close_quietly(conn)

    _emit_gate_pending(events, project_id, gate_norm, approval_id)
    return approval_id


def apply_decision(
    conn_factory: Any,
    project_id: Any,
    gate: Any,
    decision: Any,
    *,
    approval_id: Optional[str] = None,
    reason_code: Optional[str] = None,
    note: Optional[str] = None,
    decided_by: Optional[str] = None,
    events: Any = None,
) -> bool:
    """Apply a human decision — the PRIMARY (job-level) resume path.

    * Finds the latest live gate job for the project
      (``kind = 'gate.' + gate``, ``status IN ('queued','leased',
      'running')``) and merges into its ``payload_json``:
      ``payload['approval'] = {decision, approval_id, reason_code}``.
      For ``approved`` the job's ``run_after`` is zeroed so the runner
      picks it up immediately (rejected jobs are failed by the runner
      when it next polls them and sees the decision).
    * Marks the matching approval row ``decided`` (when one exists).
    * Updates the project: ``gate_status_json`` merged to
      ``{gate_a|gate_b: decision}`` and ``status`` —
      ``approved -> 'in_production'``, ``rejected -> 'blocked'``.

    Returns ``True`` iff a gate job was updated.  The project/approval
    updates happen even when no live job exists (e.g. it was already
    reaped), so the UI state never lies about the decision.
    ``events`` is accepted for call-site symmetry with
    :func:`ensure_approval`; no event is emitted here (the runner's
    next poll publishes the resulting status change).
    """
    _ = events  # reserved — see docstring
    gate_norm = _normalize_gate(gate)
    decision_norm = _normalize_decision(decision)
    project_id_str = str(project_id)
    kind = f"gate.{gate_norm}"
    now = _now_ms()

    conn, should_close = _open_conn(conn_factory)
    try:
        ensure_approvals_table(conn)

        # Resolve the approval id when the caller did not supply one:
        # prefer the pending row, else the latest row for this gate.
        resolved_approval_id = approval_id
        if not resolved_approval_id:
            row = conn.execute(
                "SELECT id FROM approvals WHERE project_id = ? AND gate = ?"
                " ORDER BY (status = 'pending') DESC, created_at DESC"
                " LIMIT 1",
                (project_id_str, gate_norm),
            ).fetchone()
            row_dict = _row_to_dict(row)
            if row_dict is not None:
                resolved_approval_id = row_dict.get("id")

        # --- approval row -> decided ----------------------------------
        try:
            if resolved_approval_id:
                conn.execute(
                    "UPDATE approvals SET decision = ?, reason_code = ?,"
                    " note = COALESCE(?, note),"
                    " decided_by = COALESCE(?, decided_by),"
                    " status = 'decided', decided_at = ? WHERE id = ?",
                    (
                        decision_norm,
                        reason_code,
                        note,
                        decided_by,
                        now,
                        resolved_approval_id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE approvals SET decision = ?, reason_code = ?,"
                    " status = 'decided', decided_at = ?"
                    " WHERE project_id = ? AND gate = ? AND status = 'pending'",
                    (decision_norm, reason_code, now, project_id_str, gate_norm),
                )
        except Exception:
            pass  # approvals bookkeeping must not block the job flip

        # --- latest live gate job -> payload flip ----------------------
        job_updated = False
        try:
            job_row = conn.execute(
                "SELECT * FROM jobs WHERE kind = ? AND project_id = ?"
                " AND status IN ('queued', 'leased', 'running')"
                " ORDER BY created_at DESC, updated_at DESC LIMIT 1",
                (kind, project_id_str),
            ).fetchone()
        except Exception:
            job_row = None
        job_dict = _row_to_dict(job_row)
        if job_dict is not None:
            payload = _parse_json(job_dict.get("payload_json"), default={})
            if not isinstance(payload, dict):
                payload = {"_raw_payload": payload}
            payload["approval"] = {
                "decision": decision_norm,
                "approval_id": resolved_approval_id,
                "reason_code": reason_code,
            }
            if decision_norm == "approved":
                conn.execute(
                    "UPDATE jobs SET payload_json = ?, run_after = 0,"
                    " updated_at = ? WHERE id = ?",
                    (json.dumps(payload), now, job_dict["id"]),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET payload_json = ?, updated_at = ?"
                    " WHERE id = ?",
                    (json.dumps(payload), now, job_dict["id"]),
                )
            job_updated = True

        # --- project state ---------------------------------------------
        try:
            _merge_gate_status(
                conn,
                project_id_str,
                gate_norm,
                decision_norm,
                project_status=(
                    "in_production" if decision_norm == "approved" else "blocked"
                ),
            )
        except Exception:
            pass
        _commit_quietly(conn)
        return job_updated
    finally:
        if should_close:
            _close_quietly(conn)


def resume_graph(db_path: Any, thread_id: Any, decision: Any) -> dict:
    """Resume a supervisor-graph run paused at a gate — SECONDARY path.

    Only for projects executed directly through
    ``app.orchestration.graph.build_supervisor_graph`` (see the module
    docstring: production resume is the job-level flip in
    :func:`apply_decision`).  Builds the graph for ``db_path`` and
    invokes ``Command(resume={'decision': decision})`` on
    ``thread_id``.

    Returns the resulting state as a plain ``dict`` summary.

    Raises whatever LangGraph raises when the thread has no pending
    interrupt to resume (e.g. ``langgraph.errors.InvalidUpdateError``
    / ``GraphInterrupt``-family errors, depending on version) — that
    is the documented, predictable behaviour for a fresh thread, and
    callers/tests should catch ``Exception`` around it.  ``langgraph``
    imports are lazy so importing this module never requires them.
    """
    from langgraph.types import Command

    from app.orchestration.graph import build_supervisor_graph

    graph = build_supervisor_graph(
        db_path=str(db_path) if db_path is not None else None
    )
    resume_value = decision if isinstance(decision, dict) else {"decision": decision}
    result = graph.invoke(
        Command(resume=resume_value),
        config={"configurable": {"thread_id": str(thread_id)}},
    )
    if isinstance(result, dict):
        return dict(result)
    return {"result": result}
