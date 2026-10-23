"""Project DAG: the 10-job linear pipeline chain (Round 2, Worker 2).

The queue (:class:`app.queue.SQLiteJobQueue`) supports a *single*
``depends_on`` per job, so the project pipeline is a linear chain —
each job is enqueued with ``depends_on`` = the previous job's id, and
the queue only makes a job claimable once its dependency has finished
successfully (a failed/dead dependency dead-letters the rest of the
chain instead of letting it wait forever).

Chain order (shared contract — other workers rely on this)::

    stage.research -> stage.script -> gate.a -> stage.voice ->
    stage.visuals -> stage.render -> stage.thumbnail -> stage.qc ->
    stage.package -> gate.b

Phase mapping (``projects.current_phase``; roadmap phases are
``research, scripting, production, editing, packaging, publishing,
growth``)::

    research  -> research     script    -> scripting
    gate.a    -> scripting    (gate on the locked script)
    voice     -> production   visuals   -> production
    render    -> editing      thumbnail -> packaging
    qc        -> editing      package   -> packaging
    gate.b    -> packaging    (pre-publish gate on the finished pack)

Every job is enqueued with ``idempotency_key=f"dag:{project_id}:{kind}"``
so re-creating the DAG for a project returns the *existing* job ids
instead of duplicating the chain.

Every job is also enqueued with ``max_attempts=STAGE_MAX_ATTEMPTS`` (2:
one initial attempt + exactly one automatic retry) per the failure
policy in :mod:`app.execution.failure_policy`; a job that fails twice
dead-letters and the runner parks the project as ``failed``.
"""

from __future__ import annotations

from app.execution.failure_policy import STAGE_MAX_ATTEMPTS

__all__ = [
    "STAGES",
    "STAGE_KINDS",
    "STAGE_BY_KIND",
    "KIND_BY_STAGE",
    "PHASE_BY_KIND",
    "create_project_dag",
]

#: The pipeline, in order.  ``stage`` is the short name used in job
#: payloads and for the integration dispatch (``run_<stage>``);
#: ``phase`` is the ``projects.current_phase`` value while it runs.
STAGES: list[dict[str, str]] = [
    {"kind": "stage.research", "stage": "research", "phase": "research"},
    {"kind": "stage.script", "stage": "script", "phase": "scripting"},
    {"kind": "gate.a", "stage": "gate_a", "phase": "scripting"},
    {"kind": "stage.voice", "stage": "voice", "phase": "production"},
    {"kind": "stage.visuals", "stage": "visuals", "phase": "production"},
    {"kind": "stage.render", "stage": "render", "phase": "editing"},
    {"kind": "stage.thumbnail", "stage": "thumbnail", "phase": "packaging"},
    {"kind": "stage.qc", "stage": "qc", "phase": "editing"},
    {"kind": "stage.package", "stage": "package", "phase": "packaging"},
    {"kind": "gate.b", "stage": "gate_b", "phase": "packaging"},
]

#: Job kinds in chain order.
STAGE_KINDS: list[str] = [entry["kind"] for entry in STAGES]

#: Job kind -> short stage name (``"stage.research" -> "research"``).
STAGE_BY_KIND: dict[str, str] = {e["kind"]: e["stage"] for e in STAGES}

#: Short stage name -> job kind (``"gate_a" -> "gate.a"``).
KIND_BY_STAGE: dict[str, str] = {e["stage"]: e["kind"] for e in STAGES}

#: Job kind -> ``projects.current_phase`` value.
PHASE_BY_KIND: dict[str, str] = {e["kind"]: e["phase"] for e in STAGES}


def create_project_dag(queue, project_id, *, start_phase: int = 0) -> list[str]:
    """Enqueue the project pipeline chain and return the job ids in order.

    ``queue`` is a :class:`app.queue.SQLiteJobQueue`.  ``start_phase``
    is an index into :data:`STAGES` (0 = from research) so a project can
    be (re)started mid-chain; jobs before it are skipped, and the first
    enqueued job then has no dependency.

    Idempotent: the queue deduplicates on
    ``idempotency_key=f"dag:{project_id}:{kind}"``, so calling this
    twice for the same project returns the same ids and creates no
    duplicate jobs.
    """
    if not 0 <= start_phase < len(STAGES):
        raise ValueError(
            f"start_phase must be in [0, {len(STAGES)}), got {start_phase}"
        )
    job_ids: list[str] = []
    previous_id = None
    for entry in STAGES[start_phase:]:
        job_id = queue.enqueue(
            entry["kind"],
            {"project_id": project_id, "stage": entry["stage"]},
            depends_on=previous_id,
            idempotency_key=f"dag:{project_id}:{entry['kind']}",
            project_id=project_id,
            # Failure policy (app.execution.failure_policy): exactly one
            # automatic retry per stage — 1 initial attempt + 1 retry.
            max_attempts=STAGE_MAX_ATTEMPTS,
        )
        job_ids.append(job_id)
        previous_id = job_id
    return job_ids
