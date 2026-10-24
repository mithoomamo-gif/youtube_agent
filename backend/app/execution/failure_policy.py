"""Failure & retry policy for scheduled / DAG production (Round 4).

This module is the single place that defines what "failure" means for
the 10-job project DAG (:mod:`app.execution.dag`).  It is layered over
the queue — :class:`app.queue.SQLiteJobQueue` stays the source of truth
for claim / fail / sweep mechanics — and is consumed by
:mod:`app.execution.runner` (per-job failure handling + startup
recovery) and :mod:`app.execution.dag` (the per-job attempt budget).

Policy (the contract the rest of the system can rely on)
--------------------------------------------------------
1. **Retry exactly once.**  A stage/gate job whose handler raises a
   real error is re-attempted exactly once: the job row is enqueued
   with ``max_attempts = 2`` (one initial attempt + one automatic
   retry) and the runner additionally refuses to mark a DAG job
   retryable once its persisted ``attempts`` reach 2, so even a legacy
   row created with the queue default of 3 cannot be retried twice.
   Attempt counts live in ``jobs.attempts`` (incremented atomically by
   ``queue.claim``), never in memory — a process restart cannot reset
   the budget into an infinite loop (see point 4).

2. **Parking is not failure.**  ``GatePending`` (human gates *and* the
   budget-pending park, which handlers raise as the same exception —
   see :mod:`app.execution.budget_gate`) is handled by the runner's
   dedicated requeue path, which refunds the claim's attempt.  It never
   reaches this module and never consumes the retry.  A human gate
   *rejection* (``GateRejected``, ``error_code='gate_rejected'``) is a
   decision, not a transient error: it is terminal with **no** retry,
   and the project is *not* re-parked as failed — the gates bridge has
   already parked it as ``blocked`` (see :func:`is_permanent_failure`).

3. **Second failure parks the project terminally.**  When the queue
   dead-letters a DAG job (``queue.fail`` returns ``'dead'``),
   :func:`park_project_failed`:

   * sets ``projects.status = 'failed'`` and persists
     ``projects.error_summary`` (stage + kind + attempt count + error);
   * dead-letters every still-queued DAG job of the project
     (``error_code='dependency_failed'``, the queue's own convention),
     so no downstream job is left dangling / claimable;
   * emits ``project.failed`` **both** persisted (an ``event_log``
     journal row) **and** live (via the caller's events object / WS
     hub).  When the events object is backed by a real ``WSHub`` —
     whose ``publish`` already journals to ``event_log`` — the direct
     journal insert is skipped so the event is persisted exactly once.

   Why ``'failed'`` and not ``'blocked'``: in the existing code
   ``'blocked'`` is the *human-actionable* state — a gate rejection
   parks there (:mod:`app.execution.gates_bridge` ``apply_decision``)
   and QC/gate blocks use the same vocabulary — i.e. "waiting on a
   person, resumable by a decision".  A stage that exhausted its retry
   is a *system* failure: nothing a human approves will resume it, the
   chain is dead, and ``projects.error_summary`` exists precisely to
   carry its summary.  ``schedule_runs`` likewise records terminal
   slot failures as ``'failed'``.  So terminal production failure is
   ``'failed'``; ``'blocked'`` stays reserved for gate decisions.

4. **Crash / restart safety.**  Leases are recovered by the queue's
   existing sweep (``sweep_expired_leases``: requeue while
   ``attempts < max_attempts``, else dead-letter with
   ``error_code='lease_expired'``).  :func:`recover_and_park` runs that
   sweep at runner startup and then parks any project whose DAG job
   died in the sweep (or was left dead by a previous crash before it
   could be parked), so a crash between "job dead" and "project
   parked" cannot leave a silently stalled project either.  The scan
   is idempotent: parked projects are ``'failed'`` and are skipped on
   later passes.

5. **Scheduler level.**  The scheduler already records a failed slot
   in ``schedule_runs`` and advances ``next_run_at`` exactly as on
   success (:mod:`app.execution.scheduler` ``_record_failure``), so a
   failed slot never re-materialises.  This module deliberately does
   not touch that logic; scheduler regression tests pin it.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

__all__ = [
    "STAGE_MAX_ATTEMPTS",
    "FAILURE_EVENT_TYPE",
    "TERMINAL_PROJECT_STATUS",
    "DOWNSTREAM_ERROR_CODE",
    "is_dag_kind",
    "is_permanent_failure",
    "retryable_for",
    "stage_label_for",
    "park_project_failed",
    "recover_and_park",
]

#: Total attempts for a DAG stage/gate job: 1 initial + exactly 1 retry.
STAGE_MAX_ATTEMPTS = 2

#: WS / journal event type emitted when a project is parked as failed.
FAILURE_EVENT_TYPE = "project.failed"

#: Terminal ``projects.status`` for an exhausted production chain.
#: (``'blocked'`` is reserved for human gate decisions — see docstring.)
TERMINAL_PROJECT_STATUS = "failed"

#: ``jobs.error_code`` for downstream jobs dead-lettered because their
#: dependency died — the queue's own convention, reused here so the UI
#: and queue propagation agree on the vocabulary.
DOWNSTREAM_ERROR_CODE = "dependency_failed"

#: Project statuses a terminal failure may overwrite.  Human / terminal
#: states (``blocked`` gate rejection, ``cancelled``, ``published``,
#: already ``failed``) are never clobbered.
_PARKABLE_STATUSES = (None, "draft", "in_production", "awaiting_review", "qa", "ready", "scheduled")

#: Boot id used for direct ``event_log`` inserts when the live events
#: object carries no hub boot id of its own.
_FALLBACK_BOOT_ID = "execution-failure-policy"

_ERROR_SUMMARY_MAX = 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Classification helpers (pure — used by the runner on every failure)
# ---------------------------------------------------------------------------

def is_dag_kind(kind: Any) -> bool:
    """True for the DAG's stage/gate job kinds (``stage.*`` / ``gate.*``)."""
    return isinstance(kind, str) and (
        kind.startswith("stage.") or kind.startswith("gate.")
    )


def is_permanent_failure(exc: BaseException) -> bool:
    """True when ``exc`` must never be retried (a human gate rejection).

    Duck-typed on the ``error_code`` attribute so the runner does not
    need to import :mod:`app.execution.handlers` (which imports the
    runner lazily — a top-level import here would risk a cycle).
    """
    return getattr(exc, "error_code", None) == "gate_rejected"


def retryable_for(job: dict, exc: BaseException) -> bool:
    """Whether the queue should requeue ``job`` after ``exc``.

    * permanent failures (gate rejection) -> never;
    * DAG jobs -> only while the persisted attempt count is below
      :data:`STAGE_MAX_ATTEMPTS` (the claim that produced this failure
      already incremented ``jobs.attempts``, so ``attempts == 1`` means
      the one retry is still available, ``attempts >= 2`` means it is
      spent — regardless of the row's ``max_attempts``);
    * any other kind -> defer to the queue's own ``max_attempts``.
    """
    if is_permanent_failure(exc):
        return False
    if is_dag_kind(job.get("kind")):
        try:
            attempts = int(job.get("attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
        return attempts < STAGE_MAX_ATTEMPTS
    return True


def stage_label_for(job: dict) -> str:
    """Human stage label for summaries: payload ``stage`` or the kind."""
    payload = job.get("payload")
    if not isinstance(payload, dict):
        raw = job.get("payload_json")
        if isinstance(raw, str) and raw:
            try:
                parsed = json.loads(raw)
                payload = parsed if isinstance(parsed, dict) else {}
            except (ValueError, TypeError):
                payload = {}
        else:
            payload = raw if isinstance(raw, dict) else {}
    stage = payload.get("stage") if isinstance(payload, dict) else None
    return str(stage or job.get("kind") or "unknown")


# ---------------------------------------------------------------------------
# Event persistence / emission
# ---------------------------------------------------------------------------

def _discover_hub(events: Any, depth: int = 0) -> Any:
    """Find a journal-persisting hub behind an events facade, if any.

    Walks the known wrapper shapes (``_EventsFacade._hub`` / ``.hub`` /
    ``._emitter``, ``EventEmitter.hub``) looking for an object that
    journals on publish (a ``WSHub``: it has ``_persist`` + ``boot_id``).
    Returns that hub or ``None``.
    """
    if events is None or depth > 4:
        return None
    if hasattr(events, "_persist") and hasattr(events, "boot_id"):
        return events
    for attr in ("_hub", "hub", "_emitter", "emitter"):
        child = getattr(events, attr, None)
        if child is not None and child is not events:
            found = _discover_hub(child, depth + 1)
            if found is not None:
                return found
    return None


def _discover_boot_id(events: Any) -> str:
    hub = _discover_hub(events)
    if hub is not None and getattr(hub, "boot_id", None):
        return str(hub.boot_id)
    boot_id = getattr(events, "boot_id", None)
    return str(boot_id) if boot_id else _FALLBACK_BOOT_ID


def _persist_event_log(conn_factory, events: Any, payload: dict, *, project_id, job_id) -> bool:
    """Insert the failure event into ``event_log``; never raises.

    Skipped (returns False) when the live events object is backed by a
    real hub whose ``publish`` already journals the frame — the event
    is then persisted exactly once, by the hub.
    """
    if _discover_hub(events) is not None:
        return False
    conn = None
    try:
        conn = conn_factory()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(event_log)")}
        if not cols:
            return False
        boot_id = _discover_boot_id(events)
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM event_log WHERE boot_id = ?",
            (boot_id,),
        ).fetchone()
        seq = int(row[0] or 0) + 1 if row is not None else 1
        envelope = {
            "v": 1,
            "type": FAILURE_EVENT_TYPE,
            "event_id": uuid.uuid4().hex,
            "seq": seq,
            "boot_id": boot_id,
            "ts": _now_ms(),
            "payload": dict(payload),
            "project_id": project_id,
            "job_id": job_id,
        }
        values = {
            "id": envelope["event_id"],
            "seq": seq,
            "boot_id": boot_id,
            "type": FAILURE_EVENT_TYPE,
            "payload_json": json.dumps(envelope),
            "created_at_ms": _now_ms(),
        }
        use = [c for c in values if c in cols]
        conn.execute(
            f"INSERT INTO event_log ({', '.join(use)}) "
            f"VALUES ({', '.join('?' for _ in use)})",
            [values[c] for c in use],
        )
        try:
            conn.commit()
        except Exception:
            pass
        return True
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _emit_live(events: Any, payload: dict, *, project_id, job_id) -> None:
    """Publish the failure event through the events object; never raises."""
    if events is None:
        return
    try:
        result = events.publish(
            FAILURE_EVENT_TYPE, dict(payload), project_id=project_id, job_id=job_id
        )
    except TypeError:
        try:
            result = events.publish(FAILURE_EVENT_TYPE, dict(payload))
        except Exception:
            return
    except Exception:
        return
    # A hub/facade publish is synchronous in this codebase; nothing to await.


# ---------------------------------------------------------------------------
# Terminal parking
# ---------------------------------------------------------------------------

def park_project_failed(
    conn_factory,
    *,
    project_id: Any,
    job: Optional[dict] = None,
    stage: Optional[str] = None,
    kind: Optional[str] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    attempts: Optional[int] = None,
    events: Any = None,
) -> dict:
    """Park a project terminally after its DAG job died.  Never raises.

    Updates the project row (status + ``error_summary``), dead-letters
    the project's still-queued DAG jobs, and emits ``project.failed``
    persisted + live.  Idempotent in effect: an already-parked project
    keeps its first ``error_summary`` (the originating failure) while
    downstream cleanup / event emission stay safe to repeat — callers
    invoke this exactly once per dead job, and the startup recovery
    scan (:func:`recover_and_park`) skips ``failed`` projects.

    Returns an info dict ``{parked, project_id, stage, attempts,
    error_summary, downstream_dead, event_persisted}``.
    """
    job = job or {}
    project_id_str = str(project_id) if project_id else None
    kind_str = str(kind or job.get("kind") or "")
    stage_str = str(stage or stage_label_for(job) or kind_str or "unknown")
    code = str(error_code or job.get("error_code") or "error")
    message = str(error_message or job.get("error_message") or code)
    if attempts is None:
        try:
            attempts = int(job.get("attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
    attempts = int(attempts)

    error_summary = (
        f"stage '{stage_str}' ({kind_str or stage_str}) failed after "
        f"{attempts} attempt(s): {code}: {message}"
    )[:_ERROR_SUMMARY_MAX]

    info: dict[str, Any] = {
        "parked": False,
        "project_id": project_id_str,
        "stage": stage_str,
        "kind": kind_str,
        "attempts": attempts,
        "error_summary": error_summary,
        "downstream_dead": 0,
        "event_persisted": False,
    }
    if not project_id_str:
        return info

    conn = None
    try:
        conn = conn_factory()
        try:
            conn.row_factory = __import__("sqlite3").Row
        except Exception:
            pass
        now = _now_ms()
        row = conn.execute(
            "SELECT status, error_summary FROM projects WHERE id = ?",
            (project_id_str,),
        ).fetchone()
        if row is not None:
            current_status = row[0] if not hasattr(row, "keys") else row["status"]
            existing_summary = (
                row[1] if not hasattr(row, "keys") else row["error_summary"]
            )
            if current_status in _PARKABLE_STATUSES:
                conn.execute(
                    "UPDATE projects SET status = ?, error_summary = ?, "
                    "updated_at = ? WHERE id = ?",
                    (
                        TERMINAL_PROJECT_STATUS,
                        existing_summary or error_summary,
                        now,
                        project_id_str,
                    ),
                )
                info["parked"] = True
                if existing_summary:
                    info["error_summary"] = existing_summary
            # Downstream cleanup runs even when the status was already
            # terminal (e.g. blocked): no queued DAG job may survive.
            job_cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
            if job_cols:
                failed_job_id = job.get("id")
                sets = ["status = 'dead'", "lease_owner = NULL"]
                params: list[Any] = []
                if "lease_expires_at" in job_cols:
                    sets.append("lease_expires_at = NULL")
                if "lease_expires_at_ms" in job_cols:
                    sets.append("lease_expires_at_ms = NULL")
                err_parts = []
                if "error_code" in job_cols:
                    err_parts.append("error_code = ?")
                    params.append(DOWNSTREAM_ERROR_CODE)
                if "error" in job_cols:
                    err_parts.append("error = ?")
                    params.append(
                        f"{DOWNSTREAM_ERROR_CODE}: dependency job failed or died"
                    )
                sets.extend(err_parts)
                updated_col = (
                    "updated_at" if "updated_at" in job_cols
                    else "updated_at_ms" if "updated_at_ms" in job_cols else None
                )
                if updated_col:
                    sets.append(f"{updated_col} = ?")
                    params.append(now)
                sql = (
                    f"UPDATE jobs SET {', '.join(sets)} "
                    "WHERE project_id = ? AND status = 'queued' "
                    "AND (kind LIKE 'stage.%' OR kind LIKE 'gate.%')"
                )
                params.append(project_id_str)
                if failed_job_id:
                    sql += " AND id != ?"
                    params.append(failed_job_id)
                cur = conn.execute(sql, params)
                info["downstream_dead"] = cur.rowcount or 0
            try:
                conn.commit()
            except Exception:
                pass
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    payload = {
        "project_id": project_id_str,
        "stage": stage_str,
        "kind": kind_str,
        "attempts": attempts,
        "max_attempts": STAGE_MAX_ATTEMPTS,
        "error_code": code,
        "error_summary": info["error_summary"],
        "status": TERMINAL_PROJECT_STATUS,
        "ts": _now_ms(),
    }
    info["event_persisted"] = _persist_event_log(
        conn_factory, events, payload,
        project_id=project_id_str, job_id=job.get("id"),
    )
    _emit_live(events, payload, project_id=project_id_str, job_id=job.get("id"))
    if _discover_hub(events) is not None:
        # The hub's publish journals to event_log itself.
        info["event_persisted"] = True
    info["payload"] = payload
    return info


# ---------------------------------------------------------------------------
# Crash / restart recovery
# ---------------------------------------------------------------------------

def _effective_error_code(job: dict) -> Optional[str]:
    """Effective error code for a raw ``jobs`` row.

    The physical schema carries an ``error_code`` column but no
    ``error_message`` column, so the queue's split-field path never
    fires (``queue._Schema.has_error_split`` requires *both* columns)
    and ``error_code`` stays NULL on queue-written rows: the code lives
    only in the single ``error`` column as a ``"code: message"``
    string.  Normalise the code from that prefix; a populated
    ``error_code`` (e.g. rows written by :func:`park_project_failed`'s
    downstream cleanup, which sets both fields) wins when present.
    """
    code = job.get("error_code")
    if code:
        return str(code)
    raw = job.get("error")
    if isinstance(raw, str) and raw and ": " in raw:
        return raw.split(": ", 1)[0] or None
    return None


def recover_and_park(conn_factory, queue=None, events: Any = None) -> dict:
    """Startup recovery: sweep expired leases, then park orphan failures.

    1. ``queue.sweep_expired_leases()`` (the queue's existing semantics:
       requeue while attempts remain, dead-letter when exhausted).
    2. Scan for DAG jobs in ``dead`` whose project is still in a live
       status — a job can be dead-without-park when the process died
       between the queue transition and the park, or when the sweep
       itself dead-lettered it.  Each such project is parked via
       :func:`park_project_failed`, keyed on the *originating* dead job
       (the earliest dead DAG job whose error is not itself
       ``dependency_failed``).

    Idempotent and never raises.  Returns
    ``{swept: int, parked: [project_id, ...]}``.
    """
    result: dict[str, Any] = {"swept": 0, "parked": []}
    if queue is not None:
        try:
            result["swept"] = int(queue.sweep_expired_leases() or 0)
        except Exception:
            result["swept"] = 0
    conn = None
    try:
        conn = conn_factory()
        try:
            conn.row_factory = __import__("sqlite3").Row
        except Exception:
            pass
        live = [s for s in _PARKABLE_STATUSES if s]
        rows = conn.execute(
            "SELECT j.* FROM jobs j JOIN projects p ON p.id = j.project_id "
            "WHERE j.status = 'dead' AND j.project_id IS NOT NULL "
            f"AND p.status IN ({', '.join('?' for _ in live)}) "
            "AND (j.kind LIKE 'stage.%' OR j.kind LIKE 'gate.%') "
            "ORDER BY j.updated_at ASC, j.created_at ASC",
            live,
        ).fetchall()
        by_project: dict[str, dict] = {}
        for row in rows:
            job = dict(row)
            pid = str(job.get("project_id"))
            existing = by_project.get(pid)
            if existing is None:
                by_project[pid] = job
            elif (_effective_error_code(existing) == DOWNSTREAM_ERROR_CODE
                  and _effective_error_code(job) != DOWNSTREAM_ERROR_CODE):
                # Prefer the originating failure over a downstream
                # ``dependency_failed`` marker regardless of row order:
                # jobs dead-lettered by one sweep share the same
                # ``updated_at`` (and often ``created_at``), so the
                # ORDER BY above cannot be trusted to surface the
                # originator first.  The comparison must use the
                # *effective* code — raw rows carry the code only in
                # the ``error`` column (see _effective_error_code), so
                # comparing ``error_code`` directly never fires.
                by_project[pid] = job
    except Exception:
        by_project = {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    for pid, job in by_project.items():
        # Normalise the queue row's error fields (physical schema uses a
        # single `error` column in the section-15 layout).
        code = job.get("error_code")
        message = job.get("error_message")
        if not code and isinstance(job.get("error"), str) and job["error"]:
            raw = job["error"]
            if ": " in raw:
                code, message = raw.split(": ", 1)
            else:
                code, message = "lease_expired", raw
        info = park_project_failed(
            conn_factory,
            project_id=pid,
            job=job,
            error_code=code or "dead",
            error_message=message or "job dead-lettered",
            attempts=job.get("attempts"),
            events=events,
        )
        if info.get("parked"):
            result["parked"].append(pid)
    return result
