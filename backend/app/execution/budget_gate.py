"""Budget Guard enforcement at project start (Round 3 lite, Round 4 full).

Semantics — read before changing
---------------------------------
Round 3 shipped *enforcement-lite* (per-video cap only).  Round 4
extends the same seams to the full persisted policy — per-video,
**daily** and **monthly** caps — backed by durable reservations in
:mod:`app.execution.budget_reservations`:

* **When:** at project start — the ``pipeline.start`` control handler
  in :mod:`app.execution.service` and the first DAG stage
  (``stage.research``) in :mod:`app.execution.handlers`.  Both call
  :func:`check_budget_for_project`; the check is idempotent: while a
  project is parked the second call does not re-warn, and once a
  reservation is active for the project the second call is a no-op
  (the DAG estimate was already reserved by the first call).
* **Estimate:** the estimate is the project's accumulated actuals
  plus the DAG's reserved estimate::

      estimate_micros = projects.total_cost_micros
                        + sum(handlers.STAGE_COST_MICROS.values())

  ``STAGE_COST_MICROS`` is the deterministic per-stage cost table the
  handlers accrue against, i.e. exactly what a stub/early run reserves.
  When real provider estimates land, they replace the second term —
  :func:`estimate_project_cost_micros` is the single seam (tests
  monkeypatch it to place the estimate on either side of the cap).
  The *incremental* amount a new run would add — and the amount
  reserved against the daily/monthly buckets — is
  ``max(0, estimate - total_cost_micros)``: past spend is never
  reserved (or counted) twice.
* **Bucket spend** (daily / monthly, America/Los_Angeles boundaries —
  the same Pacific discipline as :mod:`app.quota_view`) is *committed*
  spend: settled actuals + the unsettled remainders of active
  reservations, from the reservation store, **plus** legacy settled
  actuals for projects with no reservation row at all (pre-Round-4
  projects): per project, ``max(projects.total_cost_micros,
  api_usage ledger sum)`` inside the bucket window — the rollup and
  the ledger describe the same money, so they are never added
  together.  A project with a reservation is never in the legacy sum,
  so its spend is counted exactly once.
* **Over any cap** (``estimate > per-video cap``, or
  ``bucket spend + incremental > daily/monthly cap``): the project
  does **not** proceed silently and does **not** hard-fail.  It is
  parked at Gate A — :func:`app.execution.gates_bridge.ensure_approval`
  creates the single pending Gate A approval and sets
  ``projects.status='awaiting_review'`` — the approval's
  ``reason_code`` is set to ``budget_pending`` and its ``note`` carries
  the figures, and a guaranteed ``budget.warning`` WS event is emitted
  (exactly once per park) with ``spent`` / ``remaining`` / ``needed``
  (micros + USD) for the *breached* cap, plus explicit per-cap blocks
  (``per_video_*``, ``daily_*``, ``monthly_*`` and ``cap_scope``
  naming the breached cap: ``per_video`` | ``daily`` | ``monthly``):

  - ``spent``     = committed spend against the breached cap
    (per-video: the project's ``total_cost_micros``),
  - ``remaining`` = ``max(0, cap - spent)`` — headroom before this run,
  - ``needed``    = extra beyond the cap that finishing this video
    would require.

  No DAG is created and **no reservation is made** for a parked
  project — nothing is committed until the run is allowed to start.
* **Under / at every cap:** an ``active`` reservation for the
  incremental estimate is persisted (idempotently) and ``None`` is
  returned; execution proceeds normally.  Stage handlers settle real
  stage costs against the reservation as they are recorded; a
  cancelled run releases the unsettled remainder (see
  :mod:`app.execution.budget_reservations`).
* **Resume:** approving the budget approval is the user's explicit
  override — unchanged from Round 3.  Once a *decided/approved*
  approval with ``reason_code='budget_pending'`` exists for Gate A,
  the check is bypassed for that project (otherwise start/resume would
  re-park forever); a tracking reservation is still recorded so the
  spend stays attributed.  Raising a cap in Settings has the same
  effect for new starts, because the estimate is then no longer over
  the cap.  Rejecting the approval leaves the project blocked via the
  normal gate flow.
* **Clock seam:** bucket boundaries come from
  :func:`app.execution.budget_reservations.now_pacific` (re-exported
  here as :func:`now_pacific`); tests monkeypatch that symbol to
  steer day/month boundaries.
* **Fail-open on helper errors:** a broken settings read, reservation
  store or events hub must never wedge the pipeline — callers wrap the
  check and proceed on unexpected exceptions (logged).  An
  *established* over-cap result is not an exception and is never
  swallowed.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from app.providers.budget.money import micros_to_usd, usd_to_micros
from app.providers.budget.policy import load_budget_policy

__all__ = [
    "BUDGET_PENDING_REASON",
    "DAG_RESERVED_ESTIMATE_MICROS",
    "estimate_project_cost_micros",
    "check_budget_for_project",
    "now_pacific",
    "bucket_committed_micros",
    "ensure_budget_reservation",
    "settle_budget_for_project",
    "release_budget_for_project",
]

#: approvals.reason_code / result reason for a budget park.
BUDGET_PENDING_REASON = "budget_pending"


def _dag_reserved_estimate_micros() -> int:
    try:
        from app.execution.handlers import STAGE_COST_MICROS

        return int(sum(int(v) for v in STAGE_COST_MICROS.values()))
    except Exception:
        return 0


#: Reserved estimate for one full DAG run, from the handlers' cost table.
DAG_RESERVED_ESTIMATE_MICROS = _dag_reserved_estimate_micros()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _usd(micros: int) -> float:
    return float(micros_to_usd(int(micros)))


# ---------------------------------------------------------------------------
# Round 4: reservation store plumbing + Pacific bucket accounting
# ---------------------------------------------------------------------------


def now_pacific():
    """Current instant in America/Los_Angeles (the gate's clock seam).

    Delegates dynamically to
    :func:`app.execution.budget_reservations.now_pacific`, so tests may
    monkeypatch either ``budget_gate.now_pacific`` or
    ``budget_reservations.now_pacific`` to steer bucket boundaries.
    """
    try:
        from app.execution import budget_reservations

        return budget_reservations.now_pacific()
    except Exception:
        from datetime import datetime

        from app.providers.youtube.quota import PACIFIC

        return datetime.now(PACIFIC)


def _legacy_settled_micros(conn_factory: Any, period: str, now: Any) -> int:
    """Settled actuals in a bucket from pre-reservation projects.

    Two existing records describe settled spend, and in the real
    pipeline they describe the *same* money (``projects.total_cost_micros``
    is the per-project rollup of the stage costs whose provider calls
    also land in the ``api_usage`` ledger), so the legacy figure is
    computed **per project** as ``max(rollup, ledger)`` — never their
    sum — over projects that have **no** reservation row at all (a
    project with a reservation is counted by the store, exactly once):

    * the rollup term counts a project created inside the Pacific
      bucket window at its ``total_cost_micros``;
    * the ledger term counts ``api_usage.cost_micros`` rows created
      inside the window, grouped by project; rows with no project
      (``project_id IS NULL``) count directly as unattributed spend.

    Fails soft to 0 — the store's figures remain authoritative for
    every project that has a reservation.
    """
    try:
        from app.execution import budget_reservations

        if period == "daily":
            start_ms, end_ms = budget_reservations.pacific_day_window_ms(now)
        else:
            start_ms, end_ms = budget_reservations.pacific_month_window_ms(now)
        conn = conn_factory()
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "projects" not in tables and "api_usage" not in tables:
                return 0
            reserved_projects: set = set()
            if "budget_reservations" in tables:
                reserved_projects = {
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT project_id FROM budget_reservations"
                    )
                }
            per_project: dict[str, list[int]] = {}  # id -> [rollup, ledger]

            def _slot(project_id: str) -> list[int]:
                return per_project.setdefault(project_id, [0, 0])

            if "projects" in tables:
                for row in conn.execute(
                    "SELECT id, total_cost_micros FROM projects"
                    " WHERE created_at >= ? AND created_at < ?",
                    (start_ms, end_ms),
                ):
                    pid = str(row[0])
                    if pid not in reserved_projects:
                        _slot(pid)[0] = int(row[1] or 0)
            unattributed = 0
            if "api_usage" in tables:
                for row in conn.execute(
                    "SELECT project_id, COALESCE(SUM(cost_micros), 0)"
                    " FROM api_usage"
                    " WHERE created_at >= ? AND created_at < ?"
                    " GROUP BY project_id",
                    (start_ms, end_ms),
                ):
                    if row[0] is None:
                        unattributed += int(row[1] or 0)
                    else:
                        pid = str(row[0])
                        if pid not in reserved_projects:
                            _slot(pid)[1] = int(row[1] or 0)
            return unattributed + sum(
                max(rollup, ledger) for rollup, ledger in per_project.values()
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return 0


def bucket_committed_micros(conn_factory: Any, period: str, now: Any = None) -> int:
    """Committed spend for a ``'daily'``/``'monthly'`` bucket, in micros.

    Reservation-store committed spend (settled actuals + active
    remainders) plus legacy settled actuals (see
    :func:`_legacy_settled_micros`).  Helper errors fail soft: a broken
    store read contributes 0 rather than wedging the pipeline.
    """
    moment = now if now is not None else now_pacific()
    total = 0
    try:
        from app.execution import budget_reservations

        total += int(
            budget_reservations.bucket_spend_micros(
                conn_factory, period, now=moment
            )
        )
    except Exception:
        pass
    total += _legacy_settled_micros(conn_factory, period, moment)
    return total


def ensure_budget_reservation(
    conn_factory: Any,
    project_id: str,
    channel_id: Optional[str],
    reserved_micros: int,
    now: Any = None,
) -> Optional[dict]:
    """Persist (idempotently) the project's active reservation.

    Best-effort by design (the gate's fail-open doctrine): returns the
    reservation dict, or ``None`` when there is nothing to reserve
    (``reserved_micros <= 0``) or the store failed.
    """
    if int(reserved_micros) <= 0:
        return None
    try:
        from app.execution import budget_reservations

        return budget_reservations.ensure_reservation(
            conn_factory,
            project_id,
            channel_id,
            int(reserved_micros),
            now=now if now is not None else now_pacific(),
        )
    except Exception:
        return None


def _active_reservation(conn_factory: Any, project_id: str) -> Optional[dict]:
    try:
        from app.execution import budget_reservations

        return budget_reservations.get_active_reservation(
            conn_factory, project_id
        )
    except Exception:
        return None


def settle_budget_for_project(
    conn_factory: Any, project_id: Any, actual_micros: int, *, final: bool = False
) -> Optional[dict]:
    """Settle real stage costs against the project's reservation.

    Thin delegate to
    :func:`app.execution.budget_reservations.settle_for_project` —
    stage handlers call it (via the store directly) as each stage cost
    is recorded on the project.
    """
    from app.execution import budget_reservations

    return budget_reservations.settle_for_project(
        conn_factory, project_id, int(actual_micros), final=final
    )


def release_budget_for_project(
    conn_factory: Any, project_id: Any, *, reason: str = "run_ended"
) -> Optional[dict]:
    """Release the project's unsettled reservation remainder.

    Thin delegate to
    :func:`app.execution.budget_reservations.release_for_project` —
    called when a run ends without settling (failed / cancelled /
    stopped) so the budget frees up for the next attempt.
    """
    from app.execution import budget_reservations

    return budget_reservations.release_for_project(
        conn_factory, project_id, reason=reason
    )


def estimate_project_cost_micros(conn_factory: Any, project_id: str, project: Optional[dict] = None) -> int:
    """Estimated total cost of ``project_id`` in micros.

    ``total_cost_micros`` (actuals already accrued) + the DAG reserved
    estimate.  This is the monkeypatch seam for tests / future real
    Budget Guard estimates.
    """
    spent = 0
    if isinstance(project, dict) and project.get("total_cost_micros") is not None:
        try:
            spent = int(project["total_cost_micros"])
        except (TypeError, ValueError):
            spent = 0
    else:
        conn = conn_factory()
        try:
            row = conn.execute(
                "SELECT total_cost_micros FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if row is not None:
                spent = int(row[0] or 0)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return spent + DAG_RESERVED_ESTIMATE_MICROS


def _load_project(conn_factory: Any, project_id: str) -> Optional[dict]:
    conn = conn_factory()
    try:
        try:
            conn.row_factory = __import__("sqlite3").Row
        except Exception:
            pass
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _has_approved_budget_override(conn_factory: Any, project_id: str) -> bool:
    """True once the user approved a budget_pending Gate A approval."""
    conn = conn_factory()
    try:
        try:
            conn.execute("SELECT 1 FROM approvals LIMIT 1")
        except Exception:
            return False
        row = conn.execute(
            "SELECT id FROM approvals WHERE project_id = ? AND gate = 'a' "
            "AND reason_code = ? AND decision = 'approved' AND status = 'decided' LIMIT 1",
            (project_id, BUDGET_PENDING_REASON),
        ).fetchone()
        return row is not None
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _stamp_budget_reason(conn_factory: Any, approval_id: str, note: str, figures: dict) -> None:
    """Set reason_code/note (and budget figures) on the approval row."""
    conn = conn_factory()
    try:
        try:
            conn.execute(
                "UPDATE approvals SET reason_code = ?, note = ?, "
                "artefact_versions_json = ? WHERE id = ?",
                (BUDGET_PENDING_REASON, note, json.dumps({"budget": figures}), approval_id),
            )
        except Exception:
            # artefact_versions_json may already carry data in exotic
            # schemas; the reason_code is the contract, keep it alone.
            conn.execute(
                "UPDATE approvals SET reason_code = ?, note = ? WHERE id = ?",
                (BUDGET_PENDING_REASON, note, approval_id),
            )
        try:
            conn.commit()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _emit_budget_warning(events: Any, payload: dict, project_id: str) -> None:
    """Emit ``budget.warning`` via EventEmitter/hub/publish; never raises."""
    if events is None:
        return
    try:
        budget_warning = getattr(events, "budget_warning", None)
        if callable(budget_warning):
            budget_warning(project_id, **{k: v for k, v in payload.items() if k != "project_id"})
            return
    except Exception:
        pass
    try:
        publish = getattr(events, "publish", None)
        if callable(publish):
            publish("budget.warning", payload, project_id=project_id, job_id=None)
            return
    except Exception:
        pass
    try:
        if callable(events):
            events("budget.warning", payload)
    except Exception:
        pass


def check_budget_for_project(
    conn_factory: Any,
    project_id: Any,
    events: Any = None,
    *,
    project: Optional[dict] = None,
    estimate_micros: Optional[int] = None,
) -> Optional[dict]:
    """Park ``project_id`` at Gate A if starting it breaches a cap.

    All three persisted caps are enforced: per-video (the project's
    own estimate), daily and monthly (committed bucket spend + this
    run's incremental estimate).  Returns ``None`` when the project may
    proceed — in which case the incremental estimate has been reserved
    against the daily/monthly buckets — otherwise a dict
    ``{paused: True, reason: 'budget_pending', approval_id, gate: 'a',
    cap_scope, ...figures in micros + USD...}`` where the headline
    ``spent`` / ``cap`` / ``remaining`` / ``needed`` figures describe
    the *breached* cap.
    """
    project_id_str = str(project_id)
    project_row = project if isinstance(project, dict) else _load_project(conn_factory, project_id_str)
    if project_row is None:
        return None
    # Normalise id from the row when caller passed a dict-like project.
    project_id_str = str(project_row.get("id") or project_id_str)
    channel_id = project_row.get("channel_id")
    channel_id = str(channel_id) if channel_id is not None else None
    moment = now_pacific()

    policy = load_budget_policy(conn_factory)
    per_video_cap_micros = usd_to_micros(str(policy["per_video_cap_usd"]))
    daily_cap_micros = usd_to_micros(str(policy["daily_cap_usd"]))
    monthly_cap_micros = usd_to_micros(str(policy["monthly_cap_usd"]))
    spent_micros = 0
    try:
        spent_micros = int(project_row.get("total_cost_micros") or 0)
    except (TypeError, ValueError):
        spent_micros = 0
    estimate = int(estimate_micros) if estimate_micros is not None else int(
        estimate_project_cost_micros(conn_factory, project_id_str, project_row)
    )
    # The new money this run would spend; past actuals are already
    # inside the buckets' settled spend and must not be counted twice.
    incremental_micros = max(0, estimate - spent_micros)

    if _has_approved_budget_override(conn_factory, project_id_str):
        # Explicit user override: bypass every cap for this project,
        # but still record the tracking reservation so the spend
        # stays attributed to its channel/day/month buckets.
        ensure_budget_reservation(
            conn_factory, project_id_str, channel_id, incremental_micros, moment
        )
        return None

    if _active_reservation(conn_factory, project_id_str) is not None:
        # This DAG start already reserved its estimate (the gate has
        # two seams — pipeline.start and stage.research — and the
        # first one wins).  Never reserve or re-check twice.
        return None

    daily_spent_micros = bucket_committed_micros(conn_factory, "daily", moment)
    monthly_spent_micros = bucket_committed_micros(
        conn_factory, "monthly", moment
    )

    per_video_breach = estimate > per_video_cap_micros
    daily_breach = daily_spent_micros + incremental_micros > daily_cap_micros
    monthly_breach = (
        monthly_spent_micros + incremental_micros > monthly_cap_micros
    )

    if not (per_video_breach or daily_breach or monthly_breach):
        ensure_budget_reservation(
            conn_factory, project_id_str, channel_id, incremental_micros, moment
        )
        return None

    # Breach priority: per-video first (Round 3 semantics preserved
    # when several caps trip at once), then daily, then monthly.
    if per_video_breach:
        cap_scope = "per_video"
        cap_micros = per_video_cap_micros
        headline_spent = spent_micros
        headline_needed = max(0, estimate - cap_micros)
    elif daily_breach:
        cap_scope = "daily"
        cap_micros = daily_cap_micros
        headline_spent = daily_spent_micros
        headline_needed = max(
            0, daily_spent_micros + incremental_micros - cap_micros
        )
    else:
        cap_scope = "monthly"
        cap_micros = monthly_cap_micros
        headline_spent = monthly_spent_micros
        headline_needed = max(
            0, monthly_spent_micros + incremental_micros - cap_micros
        )
    remaining_micros = max(0, cap_micros - headline_spent)
    needed_micros = headline_needed
    daily_remaining_micros = max(0, daily_cap_micros - daily_spent_micros)
    daily_needed_micros = max(
        0, daily_spent_micros + incremental_micros - daily_cap_micros
    )
    monthly_remaining_micros = max(
        0, monthly_cap_micros - monthly_spent_micros
    )
    monthly_needed_micros = max(
        0, monthly_spent_micros + incremental_micros - monthly_cap_micros
    )
    figures = {
        "spent_micros": headline_spent,
        "spent_usd": _usd(headline_spent),
        "estimated_micros": estimate,
        "estimated_usd": _usd(estimate),
        "cap_micros": cap_micros,
        "cap_usd": _usd(cap_micros),
        "remaining_micros": remaining_micros,
        "remaining_usd": _usd(remaining_micros),
        "needed_micros": needed_micros,
        "needed_usd": _usd(needed_micros),
        "cap_scope": cap_scope,
        "breached_cap": cap_scope,
        "project_spent_micros": spent_micros,
        "project_spent_usd": _usd(spent_micros),
        "incremental_micros": incremental_micros,
        "incremental_usd": _usd(incremental_micros),
        "per_video_cap_micros": per_video_cap_micros,
        "per_video_cap_usd": policy["per_video_cap_usd"],
        "daily_cap_micros": daily_cap_micros,
        "daily_cap_usd": policy["daily_cap_usd"],
        "daily_spent_micros": daily_spent_micros,
        "daily_spent_usd": _usd(daily_spent_micros),
        "daily_remaining_micros": daily_remaining_micros,
        "daily_remaining_usd": _usd(daily_remaining_micros),
        "daily_needed_micros": daily_needed_micros,
        "daily_needed_usd": _usd(daily_needed_micros),
        "monthly_cap_micros": monthly_cap_micros,
        "monthly_cap_usd": policy["monthly_cap_usd"],
        "monthly_spent_micros": monthly_spent_micros,
        "monthly_spent_usd": _usd(monthly_spent_micros),
        "monthly_remaining_micros": monthly_remaining_micros,
        "monthly_remaining_usd": _usd(monthly_remaining_micros),
        "monthly_needed_micros": monthly_needed_micros,
        "monthly_needed_usd": _usd(monthly_needed_micros),
        "channel_id": channel_id,
        "warn_pct": policy["warn_pct"],
        "soft_pct": policy["soft_pct"],
    }

    # Park at Gate A via the shared bridge (idempotent: one pending
    # approval per (project, gate); project -> awaiting_review).
    from app.execution import gates_bridge

    # A job parked on this gate polls repeatedly; remember whether a
    # budget park was already in place so budget.warning fires once
    # per park, not once per poll.
    already_parked = False
    try:
        existing = gates_bridge.find_pending_approval(conn_factory, project_id_str, "a")
        already_parked = bool(
            existing and existing.get("reason_code") == BUDGET_PENDING_REASON
        )
    except Exception:
        already_parked = False

    job_stub = {"project_id": project_id_str, "payload": {"project_id": project_id_str, "stage": "gate_a"}}
    approval_id = gates_bridge.ensure_approval(
        conn_factory, project_row, "a", job_stub, events=None
    )
    if cap_scope == "per_video":
        note = (
            f"Estimated cost ${_usd(estimate):.2f} exceeds per-video cap "
            f"${_usd(cap_micros):.2f} (spent ${_usd(spent_micros):.2f}, "
            f"remaining ${_usd(remaining_micros):.2f}, "
            f"needed ${_usd(needed_micros):.2f} beyond cap)."
        )
    else:
        note = (
            f"Estimated additional cost ${_usd(incremental_micros):.2f} "
            f"would exceed {cap_scope} cap ${_usd(cap_micros):.2f} "
            f"(spent ${_usd(headline_spent):.2f}, "
            f"remaining ${_usd(remaining_micros):.2f}, "
            f"needed ${_usd(needed_micros):.2f} beyond cap)."
        )
    _stamp_budget_reason(conn_factory, str(approval_id), note, figures)

    result = {
        "paused": True,
        "reason": BUDGET_PENDING_REASON,
        "reason_code": BUDGET_PENDING_REASON,
        "gate": "a",
        "approval_id": str(approval_id),
        "project_id": project_id_str,
        **figures,
    }
    # budget.warning is a guaranteed event (events.py); emit after the
    # approval exists so listeners can act on approval_id immediately.
    # Suppressed on repeat polls of an already-parked project.
    if not already_parked:
        _emit_budget_warning(events, dict(result), project_id_str)
    return result
