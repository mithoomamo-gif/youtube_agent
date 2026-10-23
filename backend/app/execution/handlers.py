"""Job handlers for the execution engine (Round 2, Worker 2).

:func:`build_registry` returns ``{job_kind: async handler}`` for the
10 DAG kinds defined in :mod:`app.execution.dag`.  A handler receives
the queue's normalised job dict and returns a result dict (stored by
the runner as the job result).  Handlers are *job-level*: they know
nothing about LangGraph — orchestration lives elsewhere.

Stage handlers (``stage.*``)
----------------------------
1. Load the project row (``job.project_id``, falling back to
   ``payload.project_id``); a missing project raises ``ValueError``
   — it can never succeed for a missing project row; the runner
   applies the standard failure policy.
2. Try the real implementation via
   :func:`app.execution.integration_client.call_stage`.  Silent stub
   substitution on the execution / automation path is PROHIBITED
   (R4-003): a deterministic **stub** may be substituted *only* when
   stub / offline mode is explicitly selected for that job — see
   :func:`_stub_mode_selected` (``payload["stub_mode"]``,
   ``project["stub_mode"]``, or a truthy ``stub_mode`` / ``offline``
   in the project's ``config`` dict / parsed ``config_json``).
   The three outcomes are:

   * dict result -> proceed as a real result.  If the result or its
     artefact declares stub origin (``stub`` truthy, or a
     ``provenance`` of ``"deterministic-stub"``), the
     top-level job result carries ``stub: True`` so stub provenance
     is explicit in the job result and in the ``agent_runs``
     ``output_ref_json``.
   * exception -> the exception is always logged (project / stage /
     kind + traceback).  Without stub mode selected, a ``failed``
     ``agent_runs`` row is written and :class:`StageFailedError` is
     raised (chained from the original) so the runner's failure
     policy applies — retry exactly once, then park the project
     ``failed``.  No stub content, no artefact row, no project
     advance and no stage cost accrues for the failed attempt.
   * ``None`` (integration module missing, ``run_<stage>`` missing,
     the stage returned ``None``, or a non-dict result) -> without
     stub mode selected this is a failure exactly like the exception
     case: it is logged at error level (project / stage / kind + the
     no-result reason), a ``failed`` ``agent_runs`` row is written,
     and :class:`StageFailedError` is raised.

   With stub mode explicitly selected, the ``None`` / exception
   outcomes fall through to the previous deterministic-stub path
   unchanged: stub content, a result carrying ``stub: True``, and a
   ``succeeded`` agent run.
3. Record the run: one ``agent_runs`` row (status ``succeeded`` — the
   schema CHECK has no ``completed``) and, for stages with an artefact
   type, one ``project_artefacts`` row at the next version.
   NOTE: ``project_artefacts`` has no ``content_json`` column (checked
   in ``app/schema_ddl.py``), so the artefact content JSON is stored
   in the agent run's ``output_ref_json`` and hashed (SHA-256) into
   the artefact's ``content_hash``; ``file_path`` is the integration
   result's path when it provides one, else a ``stub://`` URI.
4. Advance the project: ``current_phase`` per
   :data:`app.execution.dag.PHASE_BY_KIND`, ``status='in_production'``.
5. Accrue cost: a small deterministic per-stage ``cost_micros`` (fixed
   table below; a real integration result may override it with its own
   ``cost_micros`` or ``cost_usd``) is added to
   ``projects.total_cost_micros`` and, when an events object is given,
   emitted as ``cost.ticker`` (guarded — event failures never fail a
   stage).

Gate handlers (``gate.a`` / ``gate.b``)
---------------------------------------
* ``publish_mode == 'auto'`` -> ``{gate, decision: 'auto_approved'}``.
* ``review_first`` with ``payload.approval.decision == 'approved'``
  -> ``{gate, decision: 'approved', approval_id}``;
  ``'rejected'`` -> raises :class:`GateRejected` (``error_code``
  ``gate_rejected``) so the runner fails the job permanently.
* ``review_first`` with no approval -> delegate to the gates bridge
  (``app.execution.gates_bridge.ensure_approval``, lazy-imported; it
  creates the approvals row + ``gate.pending`` event) when available,
  otherwise set the project ``status='awaiting_review'`` and emit
  ``gate.pending`` — then raise ``GatePending`` (imported lazily from
  :mod:`app.execution.runner`, Worker 1's module, so the runner can
  park — not fail — the job until a human decides).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import sqlite3
import time
import uuid
from typing import Any, Optional

from app.execution import integration_client
from app.execution.dag import PHASE_BY_KIND, STAGE_BY_KIND, STAGE_KINDS

logger = logging.getLogger(__name__)

__all__ = [
    "build_registry",
    "GateRejected",
    "GatePending",
    "StageFailedError",
    "STAGE_COST_MICROS",
    "ARTEFACT_TYPE_BY_STAGE",
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GateRejected(Exception):
    """A human rejected the project at a review gate.

    The runner fails the job permanently with ``error_code`` =
    ``gate_rejected`` — rejection is a decision, not a transient error,
    so the job must not be retried.
    """

    error_code = "gate_rejected"


class StageFailedError(RuntimeError):
    """A real integration stage failed on the execution path (R4-003).

    Raised by a stage handler when :func:`call_stage` raised, or
    returned ``None``, and stub / offline mode was *not* explicitly
    selected for the job — silent stub substitution is prohibited, so
    the failure must surface instead of being masked by stub content.

    Retry semantics (deliberate): this is a plain ``RuntimeError``
    subclass with **no** ``error_code`` attribute and no other
    permanent-failure marker.  :func:`app.execution.failure_policy.
    is_permanent_failure` only treats ``error_code ==
    "gate_rejected"`` as permanent, so a ``StageFailedError`` is
    retryable: :func:`app.execution.failure_policy.retryable_for`
    requeues a DAG job while its persisted ``attempts`` are below 2
    (exactly one retry) and, on the second failure, the runner
    dead-letters the job and parks the project ``failed``.  It is
    neither :class:`GatePending` nor :class:`GateRejected`, and no
    gate / budget / guardrail handler path raises or swallows it.
    """


class _GatePendingFallback(Exception):
    """Local stand-in for ``app.execution.runner.GatePending``.

    Used only when the runner module cannot be imported at gate time
    (e.g. this module is exercised before Worker 1's runner lands).
    The gate handler resolves the real class lazily on every call —
    see :func:`_gate_pending_class` — so once ``runner.py`` exists,
    the exception raised is the runner's own class.
    """

    error_code = "gate_pending"


def _gate_pending_class():
    """Resolve ``GatePending`` from the runner, falling back locally."""
    try:
        from app.execution.runner import GatePending

        return GatePending
    except Exception:
        return _GatePendingFallback


#: Public alias (the fallback class; the handler raises the runner's
#: class whenever ``app.execution.runner`` is importable).
GatePending = _GatePendingFallback


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

#: Deterministic simulated cost per short stage name, in micros
#: (USD x 1,000,000).  Gates are free — no provider work happens there.
STAGE_COST_MICROS: dict[str, int] = {
    "research": 12_000,
    "script": 25_000,
    "gate_a": 0,
    "voice": 8_000,
    "visuals": 15_000,
    "render": 5_000,
    "thumbnail": 6_000,
    "qc": 4_000,
    "package": 3_000,
    "gate_b": 0,
}

#: Short stage name -> ``project_artefacts.artefact_type`` (values are
#: constrained by the schema CHECK; gates produce no artefact).
ARTEFACT_TYPE_BY_STAGE: dict[str, str] = {
    "research": "research_brief",
    "script": "script",
    "voice": "audio",
    "visuals": "scene_list",
    "render": "master_video",
    "thumbnail": "thumbnail",
    "qc": "qc_report",
    "package": "metadata_pack",
}

GATE_KINDS = ("gate.a", "gate.b")


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _connect(conn_factory) -> sqlite3.Connection:
    conn = conn_factory()
    try:
        conn.row_factory = sqlite3.Row
    except Exception:
        pass
    return conn


def _load_project(conn: sqlite3.Connection, project_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def _update_project(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    phase: Optional[str] = None,
    status: Optional[str] = None,
    add_cost_micros: int = 0,
    gate_decision: Optional[tuple[str, str]] = None,
) -> Optional[int]:
    """Apply project updates; returns the new ``total_cost_micros``.

    ``gate_decision`` is a ``(gate_key, decision)`` pair merged into
    ``gate_status_json`` (e.g. ``("gate_a", "approved")``).
    """
    sets = ["updated_at = ?"]
    params: list[Any] = [_now_ms()]
    if phase is not None:
        sets.append("current_phase = ?")
        params.append(phase)
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if add_cost_micros:
        sets.append("total_cost_micros = total_cost_micros + ?")
        params.append(int(add_cost_micros))
    if gate_decision is not None:
        row = conn.execute(
            "SELECT gate_status_json FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        existing: dict = {}
        if row is not None and row[0]:
            try:
                parsed = json.loads(row[0])
                if isinstance(parsed, dict):
                    existing = parsed
            except (ValueError, TypeError):
                existing = {}
        existing[gate_decision[0]] = gate_decision[1]
        sets.append("gate_status_json = ?")
        params.append(json.dumps(existing))
    params.append(project_id)
    conn.execute(
        f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", params
    )
    row = conn.execute(
        "SELECT total_cost_micros FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    return int(row[0]) if row is not None else None


def _write_agent_run(
    conn: sqlite3.Connection,
    job: dict,
    project_id: str,
    kind: str,
    cost_micros: int,
    result: dict,
) -> Optional[str]:
    """Insert a succeeded ``agent_runs`` row; returns its id or None.

    Best-effort: an audit-row failure (e.g. a synthetic job id with no
    ``jobs`` row to FK against) must not fail the stage itself.
    """
    job_id = job.get("id")
    if not job_id:
        return None
    run_id = str(uuid.uuid4())
    now = _now_ms()
    try:
        attempt = int(job.get("attempts") or 1)
    except (TypeError, ValueError):
        attempt = 1
    try:
        conn.execute(
            "INSERT INTO agent_runs (id, job_id, project_id, agent_key,"
            " attempt, status, output_ref_json, cost_micros, started_at,"
            " finished_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'succeeded', ?, ?, ?, ?, ?)",
            (
                run_id,
                job_id,
                project_id,
                kind,
                attempt,
                json.dumps(result, default=str),
                int(cost_micros),
                now,
                now,
                now,
            ),
        )
        return run_id
    except Exception:
        return None


def _write_failed_agent_run(
    conn: sqlite3.Connection,
    job: dict,
    project_id: str,
    kind: str,
    stage: str,
    error: Any,
) -> Optional[str]:
    """Insert a ``failed`` ``agent_runs`` row; returns its id or None.

    Counterpart of :func:`_write_agent_run` for the R4-003 failure
    path: ``status='failed'``, the attempt is taken from the job
    exactly like the success writer, ``cost_micros=0`` (a failed
    attempt accrues no stage cost), and ``output_ref_json`` is
    ``{"error": "<ExcType>: <message>", "stage": stage, "stub":
    False}``.  For the no-result (``None``) case ``error`` is a
    :class:`StageFailedError` carrying the no-result reason.

    Best-effort like the success writer: an audit-write failure must
    not mask the original stage failure, so every error here is
    swallowed and the caller still raises the stage failure.
    """
    job_id = job.get("id")
    if not job_id:
        return None
    run_id = str(uuid.uuid4())
    now = _now_ms()
    try:
        attempt = int(job.get("attempts") or 1)
    except (TypeError, ValueError):
        attempt = 1
    if isinstance(error, BaseException):
        error_text = f"{type(error).__name__}: {error}"
    else:
        error_text = str(error)
    output_ref = {"error": error_text, "stage": stage, "stub": False}
    try:
        conn.execute(
            "INSERT INTO agent_runs (id, job_id, project_id, agent_key,"
            " attempt, status, output_ref_json, error, cost_micros,"
            " started_at, finished_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'failed', ?, ?, 0, ?, ?, ?)",
            (
                run_id,
                job_id,
                project_id,
                kind,
                attempt,
                json.dumps(output_ref, default=str),
                error_text,
                now,
                now,
                now,
            ),
        )
        return run_id
    except Exception:
        return None


def _write_artefact(
    conn: sqlite3.Connection,
    project_id: str,
    stage: str,
    content: dict,
    *,
    stub: bool,
    produced_by_run_id: Optional[str],
) -> Optional[dict]:
    """Insert the stage's ``project_artefacts`` row at the next version.

    The schema has no content column, so ``content`` is hashed into
    ``content_hash`` (its full JSON lives in the agent run's
    ``output_ref_json`` and in the handler result).  Best-effort like
    :func:`_write_agent_run`.
    """
    artefact_type = ARTEFACT_TYPE_BY_STAGE.get(stage)
    if artefact_type is None:
        return None
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM project_artefacts"
            " WHERE project_id = ? AND artefact_type = ?",
            (project_id, artefact_type),
        ).fetchone()
        version = int(row[0]) + 1
        content_hash = hashlib.sha256(
            json.dumps(content, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        file_path = content.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            file_path = f"stub://{project_id}/{stage}/v{version}"
        artefact_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO project_artefacts (id, project_id, artefact_type,"
            " version, file_path, content_hash, is_locked,"
            " produced_by_run_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                artefact_id,
                project_id,
                artefact_type,
                version,
                file_path,
                content_hash,
                produced_by_run_id,
                _now_ms(),
            ),
        )
        return {
            "artefact_id": artefact_id,
            "artefact_type": artefact_type,
            "version": version,
            "file_path": file_path,
            "content_hash": content_hash,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Job / result helpers
# ---------------------------------------------------------------------------


def _job_payload(job: dict) -> dict:
    """Extract the payload dict from a queue job dict (either shape)."""
    payload = job.get("payload")
    if isinstance(payload, dict):
        return payload
    raw = job.get("payload_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    return {}


def _project_id_for(job: dict, payload: dict) -> str:
    project_id = job.get("project_id") or payload.get("project_id")
    if not project_id:
        raise ValueError("job carries no project_id (job or payload)")
    return str(project_id)


def _resolve_cost(stage: str, result: dict) -> int:
    """Cost for a result: its own figure when present, else the table.

    D1 fix: an explicit ``0`` is *not* an authoritative recorded cost
    — it means the stage recorded no cost (e.g. the content bridge
    used to emit ``cost_micros=0`` for a $0 deterministic bundle).
    A zero therefore falls through to the next figure / the per-stage
    table instead of suppressing it; only a non-zero recorded figure
    overrides :data:`STAGE_COST_MICROS`.
    """
    value = result.get("cost_micros")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if int(value) != 0:
            return int(value)
    usd = result.get("cost_usd")
    if isinstance(usd, (int, float)) and not isinstance(usd, bool):
        if float(usd) != 0.0:
            return int(round(float(usd) * 1_000_000))
    return STAGE_COST_MICROS.get(stage, 0)


async def _emit(events, event_type: str, payload: dict, *, project_id, job_id):
    """Publish an event if an events object was provided; never raises."""
    if events is None:
        return
    publish = getattr(events, "publish", None)
    if not callable(publish):
        return
    try:
        outcome = publish(
            event_type, payload, project_id=project_id, job_id=job_id
        )
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:
        pass


def _stub_mode_selected(project: Any, payload: Any) -> bool:
    """True iff stub / offline mode is explicitly selected for a job.

    This is the ONLY way stub substitution may happen in a stage
    handler (R4-003).  Selected iff ANY of:

    * ``payload.get("stub_mode")`` is truthy;
    * ``project.get("stub_mode")`` is truthy;
    * ``project["config"]`` (if a dict) has a truthy ``"stub_mode"``
      or ``"offline"``;
    * the parsed ``project["config_json"]`` (if a JSON string — a
      dict value is also honoured) has a truthy ``"stub_mode"`` or
      ``"offline"``.

    Malformed / missing values simply do not select stub mode; this
    helper never raises.
    """
    if isinstance(payload, dict) and payload.get("stub_mode"):
        return True
    if not isinstance(project, dict):
        return False
    if project.get("stub_mode"):
        return True
    config = project.get("config")
    if isinstance(config, dict) and (
        config.get("stub_mode") or config.get("offline")
    ):
        return True
    config_json = project.get("config_json")
    parsed: Any = None
    if isinstance(config_json, dict):
        parsed = config_json
    elif isinstance(config_json, str) and config_json:
        try:
            decoded = json.loads(config_json)
        except (ValueError, TypeError):
            decoded = None
        if isinstance(decoded, dict):
            parsed = decoded
    if isinstance(parsed, dict) and (
        parsed.get("stub_mode") or parsed.get("offline")
    ):
        return True
    return False


def _provenance_is_stub(value: Any) -> bool:
    """True when a provenance value declares ``"deterministic-stub"``.

    The declaration is the bare string itself, or a provenance dict
    whose own ``"provenance"`` entry is that string.  Deliberately
    narrow: a structured provenance such as
    ``{"llm_path": "deterministic-stub"}`` on a *real* bridge result
    (the content layer's no-key deterministic bundle) is provenance
    detail recorded on that result, not a stub-origin declaration
    by the result / artefact itself.
    """
    if isinstance(value, str):
        return value == "deterministic-stub"
    if isinstance(value, dict):
        return value.get("provenance") == "deterministic-stub"
    return False


def _declares_stub_origin(result: dict, content: Any) -> bool:
    """True when a dict stage result / its artefact declares stub origin.

    Covers ``result.get("stub")`` / ``artefact["stub"]`` truthy, and
    a ``provenance`` of ``"deterministic-stub"`` on either the result
    or its artefact (see :func:`_provenance_is_stub`).
    """
    if not isinstance(result, dict):
        return False
    if result.get("stub"):
        return True
    if _provenance_is_stub(result.get("provenance")):
        return True
    if isinstance(content, dict):
        if content.get("stub"):
            return True
        if _provenance_is_stub(content.get("provenance")):
            return True
    return False


def _stub_content(stage: str, project: dict) -> dict:
    """Deterministic stub artefact content for one stage."""
    return {
        "stage": stage,
        "stub": True,
        "topic": project.get("topic"),
        "summary": f"Stub {stage} output for project {project.get('id')}",
        "items": [],
    }


# ---------------------------------------------------------------------------
# Stage handlers
# ---------------------------------------------------------------------------


async def _run_stage(conn_factory, events, kind: str, job: dict) -> dict:
    stage = STAGE_BY_KIND[kind]
    payload = _job_payload(job)
    project_id = _project_id_for(job, payload)

    conn = _connect(conn_factory)
    try:
        project = _load_project(conn, project_id)
        if project is None:
            raise ValueError(f"project not found: {project_id}")

        # Budget enforcement-lite (Round 3): the FIRST stage is also a
        # project start — a DAG enqueued at project creation reaches
        # the runner without pipeline.start.  Over the per-video cap,
        # budget_gate parks the project at Gate A (budget_pending
        # approval + budget.warning) and we raise GatePending so the
        # runner parks (not fails) this job.  See budget_gate module
        # for semantics, incl. the approved-override bypass.  Helper
        # errors fail open so a broken settings read never wedges a run.
        if stage == "research":
            # Guardrail (Round 4, D3) — FAIL-CLOSED, before budget: the
            # project topic is screened against the niche profile's
            # taboo list (prompt_matches_taboo via guardrail_gate).  A
            # match parks the project at Gate A (guardrail_taboo
            # approval + guardrail.blocked event, the budget_gate
            # pattern) and raises GatePending; no research artefact is
            # written.  Deliberately NOT wrapped in the budget check's
            # fail-open try/except: a taboo topic must never slip
            # through on a helper error.  The scheduler screens the
            # same list at materialisation; this is the fail-closed
            # backstop for projects created any other way.
            from app.execution import guardrail_gate

            guardrail_result = guardrail_gate.check_topic_for_project(
                conn_factory, project_id, events=events, project=project
            )
            if guardrail_result:
                raise _gate_pending_class()(
                    f"guardrail_taboo: blocked topic refused by "
                    f"guardrail — topic matches niche taboo blocked "
                    f"term {guardrail_result.get('matched_taboo_term')!r} "
                    f"for project {project_id} "
                    f"(approval {guardrail_result.get('approval_id')})"
                )
            try:
                from app.execution import budget_gate

                budget_result = budget_gate.check_budget_for_project(
                    conn_factory, project_id, events=events, project=project
                )
            except Exception:
                budget_result = None
            if budget_result:
                raise _gate_pending_class()(
                    f"budget_pending: estimated cost exceeds per-video cap "
                    f"for project {project_id} "
                    f"(approval {budget_result.get('approval_id')})"
                )

        # Channel context (Round 4): resolve the project's channel
        # profile / voice / budget parameters once, here at the stage
        # dispatch seam, and hand them to the integration stage on the
        # project + payload dicts (``channel_context`` key).  Helper
        # errors fail open — a context failure never wedges a stage.
        channel_context: Optional[dict] = None
        try:
            from app.execution.channel_context import (
                resolve_channel_context_from_conn,
            )

            ctx = resolve_channel_context_from_conn(conn, project_id)
            if ctx is not None:
                channel_context = ctx.to_dict()
        except Exception:
            channel_context = None
        stage_project = dict(project)
        stage_payload = dict(payload)
        if channel_context is not None:
            stage_project["channel_context"] = channel_context
            stage_payload["channel_context"] = channel_context

        # Real integration first (R4-003 closure of the D2 fix).
        # An integration exception is always LOGGED (project + stage
        # + traceback) — never swallowed silently.  But silent stub
        # substitution on the execution path is now PROHIBITED: the
        # deterministic-stub fallback survives ONLY when stub /
        # offline mode was explicitly selected for this job
        # (_stub_mode_selected).  Otherwise an exception, and equally
        # a None / non-dict result (the wrapper-None path: integration
        # module missing, run_<stage> missing, stage returned None,
        # or a non-dict result), writes a failed agent_runs row and
        # raises StageFailedError so the runner's failure policy
        # applies (retry exactly once, then park the project failed).
        # No stub content, artefact row, project advance or stage
        # cost happens for a failed attempt.  This block is separate
        # from the guardrail / budget / channel-context blocks above:
        # StageFailedError is not GatePending / GateRejected and is
        # not caught by any of their paths.
        stub_mode = _stub_mode_selected(project, payload)
        result: Optional[dict] = None
        try:
            candidate = integration_client.call_stage(
                stage, stage_project, stage_payload
            )
            if isinstance(candidate, dict):
                result = candidate
        except Exception as exc:
            logger.exception(
                "integration stage failed "
                "(project_id=%s stage=%s kind=%s stub_mode=%s)",
                project_id,
                stage,
                kind,
                stub_mode,
            )
            if not stub_mode:
                _write_failed_agent_run(
                    conn, job, project_id, kind, stage, exc
                )
                raise StageFailedError(
                    str(exc) or type(exc).__name__
                ) from exc
            result = None

        if result is None and not stub_mode:
            # Wrapper-None path — previously this fell through to the
            # stub with NO log line at all.  It is a real failure.
            reason = (
                "stage returned no result (integration unavailable "
                "or stage declined)"
            )
            logger.error(
                "integration stage returned no result "
                "(project_id=%s stage=%s kind=%s): %s",
                project_id,
                stage,
                kind,
                reason,
            )
            no_result_error = StageFailedError(reason)
            _write_failed_agent_run(
                conn, job, project_id, kind, stage, no_result_error
            )
            raise no_result_error

        stub = result is None
        declared_stub = False
        if stub:
            # Reachable only with stub mode explicitly selected.
            content = _stub_content(stage, stage_project)
            result = {"stage": stage, "stub": True, "artefact": content}
        else:
            result = dict(result)
            result.setdefault("stage", stage)
            content = result.get("artefact")
            if not isinstance(content, dict):
                content = {
                    key: value
                    for key, value in result.items()
                    if key not in ("cost_micros", "cost_usd")
                }
            # Stub provenance declared by a dict result (its own
            # ``stub`` flag, or a ``provenance`` of
            # "deterministic-stub" on the result or its artefact)
            # must be explicit at the top level of the job result —
            # and therefore in the agent_runs output_ref, which is
            # written from this result dict below.
            declared_stub = _declares_stub_origin(result, content)
            if declared_stub:
                result["stub"] = True

        # Guardrail (D3), resolved-topic screen: an 'auto:' project
        # carries no real topic at entry — research ideates one.  If
        # the topic this stage resolved matches the niche taboo list,
        # park exactly like the entry screen (Gate A guardrail_taboo
        # approval + GatePending) BEFORE any agent run / artefact /
        # cost is recorded for it.
        if stage == "research" and isinstance(content, dict):
            resolved_topic = content.get("resolved_topic") or content.get("topic")
            if (
                isinstance(resolved_topic, str)
                and resolved_topic.strip()
                and resolved_topic != project.get("topic")
            ):
                from app.execution import guardrail_gate

                resolved_result = guardrail_gate.check_topic_for_project(
                    conn_factory,
                    project_id,
                    events=events,
                    project=project,
                    topic_override=resolved_topic,
                )
                if resolved_result:
                    raise _gate_pending_class()(
                        f"guardrail_taboo: blocked topic refused by "
                        f"guardrail — resolved topic matches niche taboo "
                        f"blocked term "
                        f"{resolved_result.get('matched_taboo_term')!r} "
                        f"for project {project_id} "
                        f"(approval {resolved_result.get('approval_id')})"
                    )

        cost_micros = _resolve_cost(stage, result)
        result["kind"] = kind
        result["project_id"] = project_id
        result["cost_micros"] = cost_micros
        if channel_context is not None:
            result.setdefault("channel_context", channel_context)

        run_id = _write_agent_run(conn, job, project_id, kind, cost_micros, result)
        artefact_info = _write_artefact(
            conn, project_id, stage, content,
            stub=stub or declared_stub, produced_by_run_id=run_id,
        )
        if artefact_info is not None:
            merged = dict(content) if isinstance(content, dict) else {}
            merged.update(artefact_info)
            result["artefact"] = merged

        total = _update_project(
            conn,
            project_id,
            phase=PHASE_BY_KIND[kind],
            status="in_production",
            add_cost_micros=cost_micros,
        )
        if total is not None:
            result["total_cost_micros"] = total
    finally:
        conn.close()

    # Budget Guard (Round 4): settle this stage's recorded cost
    # against the project's reservation (reserved at DAG start by
    # budget_gate).  The settled slice replaces an equal slice of the
    # reservation's remainder, so bucket spend never double-counts.
    # The final spend stage (package) settles final, freeing any
    # unspent remainder.  Best-effort: a store failure must never
    # fail a stage whose cost is already recorded on the project.
    try:
        from app.execution import budget_reservations

        budget_reservations.settle_for_project(
            conn_factory,
            project_id,
            cost_micros,
            final=(stage == "package"),
        )
    except Exception:
        pass

    await _emit(
        events,
        "cost.ticker",
        {
            "project_id": project_id,
            "stage": stage,
            "cost_micros": result["cost_micros"],
            "total_cost_micros": result.get("total_cost_micros"),
        },
        project_id=project_id,
        job_id=job.get("id"),
    )
    return result


# ---------------------------------------------------------------------------
# Gate handlers
# ---------------------------------------------------------------------------


async def _run_gate(conn_factory, events, kind: str, job: dict) -> dict:
    gate = "a" if kind == "gate.a" else "b"
    gate_key = f"gate_{gate}"
    payload = _job_payload(job)
    project_id = _project_id_for(job, payload)

    conn = _connect(conn_factory)
    try:
        project = _load_project(conn, project_id)
        if project is None:
            raise ValueError(f"project not found: {project_id}")

        base = {
            "gate": gate,
            "kind": kind,
            "project_id": project_id,
            "cost_micros": 0,
        }
        publish_mode = project.get("publish_mode") or "review_first"

        if publish_mode == "auto":
            _update_project(
                conn, project_id,
                status="in_production",
                gate_decision=(gate_key, "auto_approved"),
            )
            return {**base, "decision": "auto_approved"}

        approval = payload.get("approval")
        if isinstance(approval, dict):
            decision = str(approval.get("decision") or "").strip().lower()
            if decision == "approved":
                _update_project(
                    conn, project_id,
                    status="in_production",
                    gate_decision=(gate_key, "approved"),
                )
                return {
                    **base,
                    "decision": "approved",
                    "approval_id": approval.get("approval_id")
                    or approval.get("id"),
                }
            if decision == "rejected":
                _update_project(
                    conn, project_id, gate_decision=(gate_key, "rejected")
                )
                raise GateRejected(
                    f"gate {gate} rejected for project {project_id}"
                    f" (approval {approval.get('approval_id') or approval.get('id')})"
                )

        # No decision yet: hand off to the gates bridge when present
        # (it creates the approvals row + gate.pending event), else park
        # the project ourselves — then raise GatePending either way so
        # the runner parks the job instead of failing/retrying it.
        bridge_used = False
        try:
            from app.execution import gates_bridge

            ensure = getattr(gates_bridge, "ensure_approval", None)
            if callable(ensure):
                bridge_used = True
                outcome = ensure(conn_factory, project, gate, job)
                if inspect.isawaitable(outcome):
                    await outcome
        except Exception:
            bridge_used = False

        if not bridge_used:
            _update_project(
                conn, project_id,
                status="awaiting_review",
                gate_decision=(gate_key, "pending"),
            )
    finally:
        conn.close()

    if not bridge_used:
        await _emit(
            events,
            "gate.pending",
            {"project_id": project_id, "gate": gate, "kind": kind},
            project_id=project_id,
            job_id=job.get("id"),
        )
    raise _gate_pending_class()(
        f"gate {gate} awaiting approval for project {project_id}"
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def build_registry(conn_factory, events=None) -> dict:
    """Build ``{job_kind: async handler}`` for all 10 DAG kinds.

    ``conn_factory()`` must return a fresh ``sqlite3.Connection``
    (e.g. ``functools.partial(app.db.connect, path)``); each handler
    call opens and closes its own connection.  ``events`` is optional;
    when given it must expose ``publish(type, payload, project_id=,
    job_id=)`` (the :class:`app.ws_hub.WSHub` signature) and is used
    for ``cost.ticker`` / ``gate.pending`` — failures are swallowed.
    """
    registry: dict[str, Any] = {}
    for kind in STAGE_KINDS:
        if kind in GATE_KINDS:
            async def handler(job: dict, _kind: str = kind) -> dict:
                return await _run_gate(conn_factory, events, _kind, job)
        else:
            async def handler(job: dict, _kind: str = kind) -> dict:
                return await _run_stage(conn_factory, events, _kind, job)
        handler.__name__ = "handle_" + kind.replace(".", "_")
        registry[kind] = handler
    return registry
