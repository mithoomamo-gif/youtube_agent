"""Report builders — every figure read from a ledger, nothing else.

This is the read side of Agent 31 (MASTER_ROADMAP §3.15, Phase 7).
The builders take a caller-owned ``sqlite3.Connection`` and return a
validated :class:`~app.reporting.models.ReportDocument` dict (the
JSON source of truth).  They never write, never call a provider, and
never recompute a figure from stage state: each section names the
ledger / record table it reads.

Section → ledger map
----------------------
* production   ← ``projects``, ``project_artefacts``,
  ``project_assets`` + ``assets`` + ``asset_licences``,
  ``provenance_log`` (counts only)
* publish      ← ``publish_records`` + ``projects`` publish columns +
  ``projects.gate_status_json['publish']`` provenance.  Fake-provider
  evidence (``simulated:`` video ids, ``simulated=True``, state
  ``fake_provider_upload_simulated``) classifies as ``simulated`` —
  never ``published`` (the publish-finaliser's honesty rule).
* costs        ← ``projects.total_cost_micros`` (headline),
  ``api_usage.cost_micros`` (by provider / operation),
  ``agent_runs.cost_micros`` (by agent), ``budget_reservations``
  (settled / reserved, when the table exists).  Reported per ledger,
  never summed across ledgers (they overlap by design).
* quota        ← ``api_usage`` rows with ``provider='youtube'``;
  bucketing by :func:`app.providers.youtube.quota.classify_method`
  (stored ``units`` win; NULL falls back to the method cost — the
  same rule as :mod:`app.quota_view`).
* performance  ← ``analytics_snapshots``.  No rows → ``available``
  False with reason "no analytics yet"; totals are summed over the
  LATEST snapshot per video so re-pulled snapshots never double-count.
* failures / recoveries ← ``jobs`` (failed / dead, retried-then-done),
  ``agent_runs`` (failed / blocked, failed-then-succeeded per agent),
  ``schedule_runs`` (failed, channel scope / project's schedule).
* gates        ← ``projects.gate_status_json`` (the execution layer's
  record: ``gate_a`` / ``gate_b`` decision strings, nested
  ``GateStatus`` dumps, and the ``publish`` provenance block).
  Gate C is reported only from explicit ledger evidence (a
  ``gate_c`` key, or a publish record implying the publish gate let
  the upload through); otherwise ``not_recorded``.
* calibration  ← the learning-team seam in
  :func:`load_calibration_error`; absent → ``available`` False.
* next planned ← ``schedules`` (enabled, ``next_run_at``) +
  ``topic_queue`` (pending) + recent ``schedule_runs``.

Period semantics: an explicit ``period_start_ms`` / ``period_end_ms``
pair filters time-stamped ledgers to ``[start, end)``.  With no
bounds, a video report covers the project's lifetime and a channel
report covers all recorded time.  Project membership of a channel
report is by ``projects.created_at`` inside the period.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from app.providers.budget.money import micros_to_usd
from app.reporting.models import ReportDocument

__all__ = [
    "ReportNotFoundError",
    "build_video_report",
    "build_channel_report",
    "load_calibration_error",
    "classify_publish",
    "normalise_gate",
]

#: Ledger tables a report may read (also the ``sources`` provenance).
_LEDGER_TABLES = (
    "projects", "channels", "niche_profiles", "project_artefacts",
    "project_assets", "assets", "asset_licences", "publish_records",
    "api_usage", "agent_runs", "jobs", "analytics_snapshots",
    "schedules", "schedule_runs", "topic_queue", "provenance_log",
    "budget_reservations",
)

_SIMULATED_STATES = {"fake_provider_upload_simulated"}
_PASS_WORDS = {"approved", "auto_approved", "passed", "pass", "ok", "true", "1"}
_FAIL_WORDS = {"rejected", "failed", "fail", "blocked", "block", "denied"}
_PENDING_WORDS = {"pending", "awaiting_review", "queued", "running"}


class ReportNotFoundError(LookupError):
    """The project / channel a report was requested for does not exist."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso_z(ms: int) -> str:
    return (
        datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _usd(micros: Any) -> float:
    try:
        return float(micros_to_usd(int(micros or 0)))
    except Exception:
        return 0.0


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()


def _rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    except Exception:
        return []


def _row(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
    rows = _rows(conn, sql, params)
    return rows[0] if rows else None


def _parse_json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return default
    return default


def _period_clause(
    column: str, start: Optional[int], end: Optional[int]
) -> tuple[str, list[Any]]:
    """``column IN [start, end)`` SQL fragment + params (empty = unbounded)."""
    clauses: list[str] = []
    params: list[Any] = []
    if start is not None:
        clauses.append(f"{column} >= ?")
        params.append(int(start))
    if end is not None:
        clauses.append(f"{column} < ?")
        params.append(int(end))
    return (" AND ".join(clauses), params)


def _and(clause: str) -> str:
    return f" AND {clause}" if clause else ""


# ---------------------------------------------------------------------------
# Publish classification (the honesty core)
# ---------------------------------------------------------------------------


def classify_publish(
    project: Optional[Mapping[str, Any]],
    publish_record: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Classify one project's publish state from ledger evidence only.

    Returns a ``PublishSection``-shaped dict.  Simulated evidence
    ALWAYS wins over any "published"-looking field: a fake-provider
    run must never be reported as a real publication.
    """
    project = dict(project or {})
    record = dict(publish_record) if publish_record else None
    gate_status = _parse_json(project.get("gate_status_json"), default={})
    if not isinstance(gate_status, dict):
        gate_status = {}
    publish_info = gate_status.get("publish")
    if not isinstance(publish_info, dict):
        publish_info = {}

    record_video_id = str(record.get("youtube_video_id") or "") if record else ""
    project_video_id = str(project.get("youtube_video_id") or "")
    simulated = False
    provider: Optional[str] = None
    if publish_info.get("simulated") is True:
        simulated = True
    if str(publish_info.get("provider") or "").lower() == "fake":
        simulated = True
        provider = "fake"
    if str(publish_info.get("state") or "") in _SIMULATED_STATES:
        simulated = True
    if record_video_id.startswith("simulated:"):
        simulated = True
    if project_video_id.startswith("simulated:"):
        simulated = True

    base: dict[str, Any] = {
        "simulated": simulated,
        "provider": provider or publish_info.get("provider"),
        "publish_record_id": record.get("id") if record else None,
        "youtube_video_id": record_video_id or project_video_id or None,
        "video_url": record.get("video_url") if record else None,
        "privacy_status": record.get("privacy_status") if record else None,
        "published_at_ms": (
            record.get("published_at") if record and record.get("published_at")
            else project.get("published_at")
        ),
        "state": publish_info.get("state"),
        "detail": publish_info.get("detail") or publish_info.get("reason"),
    }

    if record is not None and simulated:
        base.update(
            classification="simulated",
            label="Simulated publish (fake provider) — not published to YouTube",
        )
        return base
    if record is None and simulated:
        base.update(
            classification="simulated",
            label="Simulated publish (fake provider) — not published to YouTube",
        )
        return base
    if record is not None:
        if str(project.get("status") or "") == "published":
            base.update(classification="published", label="Published")
        else:
            # A non-simulated record whose project never reached the
            # published state: report the record, do not upgrade it.
            base.update(
                classification="unknown",
                label="Publish record present — live state not confirmed in ledgers",
                detail=base.get("detail")
                or "publish_records row exists but projects.status is not "
                "'published'; reported as recorded, not as published.",
            )
        return base
    if publish_info.get("state") == "not_published":
        base.update(
            classification="not_published",
            label="Not published",
            detail=base.get("detail") or "Recorded as not published (gate outcome).",
        )
        return base
    base.update(classification="not_published", label="Not published")
    return base


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def normalise_gate(raw: Any) -> dict[str, Any]:
    """Normalise one raw gate value to ``{outcome, raw}``.

    Accepts decision strings (``approved`` / ``rejected`` / ...),
    booleans, and nested dicts (``{"decision": ...}`` /
    ``{"passed": bool}`` / ``{"verdict": ...}``).
    """
    if raw is None:
        return {"outcome": "not_recorded", "raw": None}
    if isinstance(raw, bool):
        return {"outcome": "passed" if raw else "failed", "raw": raw}
    if isinstance(raw, Mapping):
        for key in ("decision", "verdict", "outcome", "status", "state"):
            if raw.get(key) is not None:
                nested = normalise_gate(raw.get(key))
                nested["raw"] = dict(raw)
                return nested
        for key in ("passed", "gate_passed", "approved"):
            if isinstance(raw.get(key), bool):
                return {
                    "outcome": "passed" if raw[key] else "failed",
                    "raw": dict(raw),
                }
        return {"outcome": "not_recorded", "raw": dict(raw)}
    text = str(raw).strip().lower()
    if text in _PASS_WORDS:
        outcome = "passed"
    elif text in _FAIL_WORDS:
        outcome = "blocked" if text in ("blocked", "block") else "failed"
    elif text in _PENDING_WORDS:
        outcome = "pending"
    elif not text:
        outcome = "not_recorded"
    else:
        outcome = text
    return {"outcome": outcome, "raw": raw}


def _gates_for_project(
    project: Mapping[str, Any],
    publish_record: Optional[Mapping[str, Any]],
    publish: Mapping[str, Any],
) -> dict[str, Any]:
    gate_status = _parse_json(project.get("gate_status_json"), default={})
    if not isinstance(gate_status, dict):
        gate_status = {}

    def pick(*keys: str) -> Any:
        for key in keys:
            if gate_status.get(key) is not None:
                return gate_status.get(key)
        return None

    gate_a_raw = pick("gate_a", "gateA", "gate_a_decision")
    if gate_a_raw is None and gate_status.get("gate_a_script_locked") is not None:
        gate_a_raw = bool(gate_status.get("gate_a_script_locked"))
    gate_b_raw = pick("gate_b", "gateB", "gate_b_decision")
    if gate_b_raw is None and gate_status.get("gate_b_passed") is not None:
        gate_b_raw = bool(gate_status.get("gate_b_passed"))

    gate_c_raw = pick("gate_c", "gateC", "gate_c_decision")
    gate_c: dict[str, Any]
    if gate_c_raw is not None:
        gate_c = normalise_gate(gate_c_raw)
    elif publish_record is not None or publish.get("classification") in (
        "published", "simulated",
    ):
        # A publish (real or simulated) could only happen after the
        # publish gate let it through — ledger-implied, labelled as such.
        gate_c = {
            "outcome": "passed",
            "raw": None,
            "basis": "implied by publish record in ledgers",
        }
    else:
        gate_c = {"outcome": "not_recorded", "raw": None}

    tokens = gate_status.get("gate_b_tokens")
    gate_b = normalise_gate(gate_b_raw)
    if isinstance(tokens, list) and tokens:
        gate_b["tokens"] = tokens
    return {
        "gate_a": normalise_gate(gate_a_raw),
        "gate_b": gate_b,
        "gate_c": gate_c,
    }


# ---------------------------------------------------------------------------
# Ledger section readers (each takes an explicit project-id set / scope)
# ---------------------------------------------------------------------------


def _api_usage_rows(
    conn: sqlite3.Connection,
    project_ids: list[str],
    start: Optional[int],
    end: Optional[int],
) -> list[dict]:
    if not project_ids or not _table_exists(conn, "api_usage"):
        return []
    clause, params = _period_clause("created_at", start, end)
    placeholders = ", ".join("?" for _ in project_ids)
    return _rows(
        conn,
        f"SELECT * FROM api_usage WHERE project_id IN ({placeholders})"
        f"{_and(clause)} ORDER BY created_at ASC",
        (*project_ids, *params),
    )


def _costs_section(
    conn: sqlite3.Connection,
    projects: list[dict],
    usage_rows: list[dict],
    start: Optional[int],
    end: Optional[int],
) -> dict[str, Any]:
    project_ids = [str(p["id"]) for p in projects]
    project_total = sum(int(p.get("total_cost_micros") or 0) for p in projects)
    api_total = sum(int(r.get("cost_micros") or 0) for r in usage_rows)

    by_provider_map: dict[str, dict[str, Any]] = {}
    by_operation_map: dict[str, dict[str, Any]] = {}
    for r in usage_rows:
        for bucket_map, key in (
            (by_provider_map, str(r.get("provider") or "unknown")),
            (by_operation_map, str(r.get("operation") or "unknown")),
        ):
            entry = bucket_map.setdefault(
                key,
                {"calls": 0, "cost_micros": 0, "tokens_in": 0,
                 "tokens_out": 0, "characters": 0, "images": 0, "units": 0},
            )
            entry["calls"] += 1
            entry["cost_micros"] += int(r.get("cost_micros") or 0)
            for field in ("tokens_in", "tokens_out", "characters", "images", "units"):
                entry[field] += int(r.get(field) or 0)
    by_provider = [
        {"provider": k, **v, "cost_usd": _usd(v["cost_micros"])}
        for k, v in sorted(by_provider_map.items())
    ]
    by_operation = [
        {"operation": k, **v, "cost_usd": _usd(v["cost_micros"])}
        for k, v in sorted(by_operation_map.items())
    ]

    # agent_runs ledger (by agent) for the same projects.
    by_agent: list[dict[str, Any]] = []
    agent_total = 0
    if project_ids and _table_exists(conn, "agent_runs"):
        placeholders = ", ".join("?" for _ in project_ids)
        clause, params = _period_clause("created_at", start, end)
        grouped = _rows(
            conn,
            "SELECT agent_key, COUNT(*) AS runs,"
            " COALESCE(SUM(cost_micros), 0) AS cost_micros,"
            " COALESCE(SUM(tokens_in), 0) AS tokens_in,"
            " COALESCE(SUM(tokens_out), 0) AS tokens_out"
            f" FROM agent_runs WHERE project_id IN ({placeholders})"
            f"{_and(clause)} GROUP BY agent_key ORDER BY agent_key",
            (*project_ids, *params),
        )
        by_agent = [
            {**g, "cost_usd": _usd(g["cost_micros"])} for g in grouped
        ]
        agent_total = sum(int(g["cost_micros"]) for g in grouped)

    # Budget reservations for the same projects (when the store exists).
    reservations: list[dict[str, Any]] = []
    reserved_total = 0
    settled_total = 0
    if project_ids and _table_exists(conn, "budget_reservations"):
        placeholders = ", ".join("?" for _ in project_ids)
        for r in _rows(
            conn,
            f"SELECT * FROM budget_reservations"
            f" WHERE project_id IN ({placeholders}) ORDER BY created_at ASC",
            project_ids,
        ):
            reserved = int(r.get("reserved_micros") or 0)
            settled = int(r.get("settled_micros") or 0)
            remaining = (
                max(0, reserved - settled) if r.get("status") == "active" else 0
            )
            reservations.append({
                "id": r.get("id"), "project_id": r.get("project_id"),
                "channel_id": r.get("channel_id"), "status": r.get("status"),
                "reserved_micros": reserved, "settled_micros": settled,
                "remaining_micros": remaining,
                "committed_micros": settled + remaining,
                "pacific_day": r.get("pacific_day"),
                "pacific_month": r.get("pacific_month"),
            })
            reserved_total += remaining
            settled_total += settled

    by_project = [
        {
            "project_id": str(p["id"]),
            "topic": p.get("topic"),
            "cost_micros": int(p.get("total_cost_micros") or 0),
            "cost_usd": _usd(p.get("total_cost_micros")),
        }
        for p in projects
    ]
    return {
        "total_micros": project_total,
        "total_usd": _usd(project_total),
        "project_ledger_micros": project_total,
        "api_usage_micros": api_total,
        "agent_runs_micros": agent_total,
        "api_usage_calls": len(usage_rows),
        "by_provider": by_provider,
        "by_operation": by_operation,
        "by_agent": by_agent,
        "by_project": by_project,
        "reservations": reservations,
        "reserved_micros": reserved_total,
        "settled_micros": settled_total,
    }


def _quota_section(usage_rows: list[dict]) -> dict[str, Any]:
    try:
        from app.providers.youtube.quota import BUCKET_LIMITS, classify_method

        wire = {"upload": "uploads", "search": "search", "shared": "shared"}
        limits = {
            wire.get(k, k): int(v) for k, v in BUCKET_LIMITS.items()
        }
    except Exception:  # pragma: no cover - quota module is in-tree
        classify_method = None  # type: ignore[assignment]
        limits = {"uploads": 100, "search": 100, "shared": 10000}
        wire = {"upload": "uploads", "search": "search", "shared": "shared"}

    buckets: dict[str, dict[str, Any]] = {
        name: {"used": 0, "limit": limit} for name, limit in limits.items()
    }
    by_operation_map: dict[str, dict[str, Any]] = {}
    total_units = 0
    calls = 0
    for r in usage_rows:
        if str(r.get("provider") or "").lower() != "youtube":
            continue
        calls += 1
        operation = str(r.get("operation") or "")
        stored = r.get("units")
        if stored is None:
            units = classify_method(operation)[1] if classify_method else 1
        else:
            try:
                units = int(stored)
            except (TypeError, ValueError):
                units = classify_method(operation)[1] if classify_method else 1
        bucket_internal = classify_method(operation)[0] if classify_method else "shared"
        bucket = wire.get(bucket_internal, bucket_internal)
        buckets.setdefault(bucket, {"used": 0, "limit": limits.get(bucket)})
        buckets[bucket]["used"] += units
        total_units += units
        entry = by_operation_map.setdefault(
            operation, {"operation": operation, "calls": 0, "units": 0, "bucket": bucket}
        )
        entry["calls"] += 1
        entry["units"] += units
    return {
        "total_units": total_units,
        "calls": calls,
        "buckets": buckets,
        "by_operation": sorted(by_operation_map.values(), key=lambda d: d["operation"]),
    }


def _snapshot_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    watch_x100 = row.get("watch_hours_x100")
    return {
        "id": row.get("id"),
        "project_id": row.get("project_id"),
        "channel_id": row.get("channel_id"),
        "youtube_video_id": row.get("youtube_video_id"),
        "snapshot_at_ms": row.get("snapshot_at"),
        "views": int(row.get("views") or 0),
        "watch_hours": (int(watch_x100) / 100.0) if watch_x100 is not None else 0.0,
        "watch_hours_x100": watch_x100,
        "likes": int(row.get("likes") or 0),
        "comments_count": int(row.get("comments_count") or 0),
        "shares": int(row.get("shares") or 0),
        "ctr_bps": row.get("ctr_bps"),
        "avg_view_duration_sec": row.get("avg_view_duration_sec"),
        "subs_delta": row.get("subs_delta"),
        "revenue_micros": row.get("revenue_micros"),
        "source": row.get("source"),
    }


def _performance_section(snapshots: list[dict]) -> dict[str, Any]:
    if not snapshots:
        return {
            "available": False,
            "reason": "no analytics yet — no analytics_snapshots rows recorded",
            "snapshot_count": 0,
            "videos_with_analytics": 0,
            "latest": None,
            "totals_from_latest_per_video": {},
            "snapshots": [],
        }
    shaped = [_snapshot_dict(s) for s in snapshots]
    # Latest snapshot per video (keyed by project, falling back to the
    # YouTube video id, then the snapshot id for channel-level rows).
    latest_by_video: dict[str, dict[str, Any]] = {}
    for snap in shaped:
        key = str(snap.get("project_id") or snap.get("youtube_video_id") or snap["id"])
        current = latest_by_video.get(key)
        if current is None or int(snap["snapshot_at_ms"] or 0) >= int(
            current["snapshot_at_ms"] or 0
        ):
            latest_by_video[key] = snap
    latest_rows = list(latest_by_video.values())
    totals = {
        "views": sum(s["views"] for s in latest_rows),
        "likes": sum(s["likes"] for s in latest_rows),
        "comments_count": sum(s["comments_count"] for s in latest_rows),
        "shares": sum(s["shares"] for s in latest_rows),
        "watch_hours": round(sum(s["watch_hours"] for s in latest_rows), 2),
        "subs_delta": sum(int(s["subs_delta"] or 0) for s in latest_rows),
    }
    latest_overall = max(shaped, key=lambda s: int(s["snapshot_at_ms"] or 0))
    return {
        "available": True,
        "reason": None,
        "snapshot_count": len(shaped),
        "videos_with_analytics": len(latest_by_video),
        "latest": latest_overall,
        "totals_from_latest_per_video": totals,
        "snapshots": shaped,
    }


def _failures_section(
    conn: sqlite3.Connection,
    project_ids: list[str],
    schedule_ids: list[str],
    start: Optional[int],
    end: Optional[int],
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    recoveries: list[dict[str, Any]] = []
    failed_jobs = 0
    failed_runs = 0
    failed_schedule_runs = 0
    if not project_ids:
        return {
            "failure_count": 0, "recovery_count": 0, "failures": [],
            "recoveries": [], "failed_jobs": 0, "failed_agent_runs": 0,
            "failed_schedule_runs": 0,
        }
    placeholders = ", ".join("?" for _ in project_ids)

    if _table_exists(conn, "jobs"):
        clause, params = _period_clause("created_at", start, end)
        jobs = _rows(
            conn,
            f"SELECT * FROM jobs WHERE project_id IN ({placeholders})"
            f"{_and(clause)} ORDER BY created_at ASC",
            (*project_ids, *params),
        )
        for job in jobs:
            status = str(job.get("status") or "")
            attempts = int(job.get("attempts") or 0)
            if status in ("failed", "dead"):
                failed_jobs += 1
                failures.append({
                    "kind": "job", "id": job.get("id"),
                    "label": str(job.get("kind") or job.get("agent_key") or "job"),
                    "status": status, "attempts": attempts,
                    "error": job.get("error"), "error_code": job.get("error_code"),
                    "at_ms": job.get("updated_at"),
                })
            elif status == "done" and attempts > 1:
                recoveries.append({
                    "kind": "job", "id": job.get("id"),
                    "label": str(job.get("kind") or job.get("agent_key") or "job"),
                    "detail": f"succeeded after {attempts} attempts",
                    "attempts": attempts, "at_ms": job.get("updated_at"),
                })

    if _table_exists(conn, "agent_runs"):
        clause, params = _period_clause("created_at", start, end)
        runs = _rows(
            conn,
            f"SELECT * FROM agent_runs WHERE project_id IN ({placeholders})"
            f"{_and(clause)} ORDER BY created_at ASC, attempt ASC",
            (*project_ids, *params),
        )
        failed_by_agent: dict[tuple[str, str], dict] = {}
        succeeded_by_agent: dict[tuple[str, str], dict] = {}
        for run in runs:
            status = str(run.get("status") or "")
            key = (str(run.get("project_id")), str(run.get("agent_key")))
            if status in ("failed", "blocked"):
                failed_runs += 1
                failures.append({
                    "kind": "agent_run", "id": run.get("id"),
                    "label": str(run.get("agent_key") or "agent"),
                    "status": status, "attempts": run.get("attempt"),
                    "error": run.get("error"), "at_ms": run.get("finished_at") or run.get("created_at"),
                })
                failed_by_agent[key] = run
            elif status == "succeeded":
                succeeded_by_agent[key] = run
        for key, failed in failed_by_agent.items():
            succeeded = succeeded_by_agent.get(key)
            if succeeded is not None and int(succeeded.get("created_at") or 0) >= int(
                failed.get("created_at") or 0
            ):
                # A retry-then-success for the same agent + project is a
                # job-level recovery only when no job already recorded
                # it; agent-level evidence is listed either way but the
                # job recovery (above) is the canonical count source
                # when both exist for the same work.  To stay honest
                # and simple, agent recoveries are listed and counted
                # here; job recoveries above cover distinct records.
                recoveries.append({
                    "kind": "agent_run", "id": succeeded.get("id"),
                    "label": str(succeeded.get("agent_key") or "agent"),
                    "detail": "agent succeeded after an earlier failed/blocked run",
                    "attempts": succeeded.get("attempt"),
                    "at_ms": succeeded.get("finished_at") or succeeded.get("created_at"),
                })

    if schedule_ids and _table_exists(conn, "schedule_runs"):
        sched_placeholders = ", ".join("?" for _ in schedule_ids)
        clause, params = _period_clause("created_at", start, end)
        for run in _rows(
            conn,
            "SELECT * FROM schedule_runs"
            f" WHERE schedule_id IN ({sched_placeholders})"
            f" AND project_id IN ({placeholders}){_and(clause)}"
            " ORDER BY created_at ASC",
            (*schedule_ids, *project_ids, *params),
        ):
            if str(run.get("status") or "") == "failed":
                failed_schedule_runs += 1
                failures.append({
                    "kind": "schedule_run", "id": run.get("id"),
                    "label": "schedule run",
                    "status": "failed", "error": run.get("reason"),
                    "at_ms": run.get("created_at"),
                })

    return {
        "failure_count": len(failures),
        "recovery_count": len(recoveries),
        "failures": failures,
        "recoveries": recoveries,
        "failed_jobs": failed_jobs,
        "failed_agent_runs": failed_runs,
        "failed_schedule_runs": failed_schedule_runs,
    }


def _next_planned_section(
    conn: sqlite3.Connection, channel_id: str, schedule_ids: list[str]
) -> dict[str, Any]:
    schedules: list[dict[str, Any]] = []
    if _table_exists(conn, "schedules"):
        rows = _rows(
            conn,
            "SELECT * FROM schedules WHERE channel_id = ?"
            " AND deleted_at IS NULL ORDER BY next_run_at IS NULL,"
            " next_run_at ASC, created_at ASC",
            (channel_id,),
        )
        # A video report narrows to its own schedule first, but still
        # shows the channel's plan; ordering keeps its schedule first.
        rows.sort(key=lambda r: 0 if str(r.get("id")) in schedule_ids else 1)
        for r in rows:
            schedules.append({
                "id": r.get("id"), "name": r.get("name"),
                "channel_id": r.get("channel_id"),
                "niche_profile_id": r.get("niche_profile_id"),
                "publish_mode": r.get("publish_mode"),
                "is_enabled": bool(r.get("is_enabled")),
                "next_run_at_ms": r.get("next_run_at"),
                "last_run_at_ms": r.get("last_run_at"),
                "target_minutes": r.get("target_minutes"),
            })
    enabled = [s for s in schedules if s["is_enabled"]]
    next_runs = [s["next_run_at_ms"] for s in enabled if s["next_run_at_ms"] is not None]

    pending_topics: list[dict[str, Any]] = []
    pending_count = 0
    if _table_exists(conn, "topic_queue"):
        rows = _rows(
            conn,
            "SELECT * FROM topic_queue WHERE channel_id = ?"
            " AND status = 'pending' ORDER BY priority DESC, created_at ASC",
            (channel_id,),
        )
        pending_count = len(rows)
        pending_topics = [
            {"id": r.get("id"), "topic": r.get("topic"),
             "source": r.get("source"), "priority": r.get("priority"),
             "niche_profile_id": r.get("niche_profile_id")}
            for r in rows[:20]
        ]

    recent_runs: list[dict[str, Any]] = []
    sched_ids = [str(s["id"]) for s in schedules]
    if sched_ids and _table_exists(conn, "schedule_runs"):
        placeholders = ", ".join("?" for _ in sched_ids)
        recent_runs = [
            {"id": r.get("id"), "schedule_id": r.get("schedule_id"),
             "project_id": r.get("project_id"), "status": r.get("status"),
             "reason": r.get("reason"), "at_ms": r.get("created_at")}
            for r in _rows(
                conn,
                f"SELECT * FROM schedule_runs WHERE schedule_id IN ({placeholders})"
                " ORDER BY created_at DESC LIMIT 10",
                sched_ids,
            )
        ]
    return {
        "schedules": schedules,
        "enabled_schedule_count": len(enabled),
        "next_run_at_ms": min(next_runs) if next_runs else None,
        "pending_topics": pending_topics,
        "pending_topic_count": pending_count,
        "recent_schedule_runs": recent_runs,
    }


# ---------------------------------------------------------------------------
# Calibration error — the learning-team seam
# ---------------------------------------------------------------------------


def load_calibration_error(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    project_id: Optional[str] = None,
    niche_profile_id: Optional[str] = None,
) -> dict[str, Any]:
    """Calibration error for a scope, from the learning team if present.

    Seam, in priority order (first hit wins):

    0. THE landed Round 5 seam: :mod:`app.calibration` — the
       calibration log (``calibration_log`` table) + its read model
       :func:`app.calibration.report.build_calibration_report`.
       Its per-(channel, niche, metric) scopes are carried verbatim
       in ``detail['scopes']`` (D+7 / D+28 points kept separate, the
       team's own insufficient-evidence labels preserved).  ``value``
       stays None at this level on purpose: the calibration team's
       math pools NO errors across metrics, so a single pooled number
       would manufacture a meaningless figure.  For a video report,
       the video's own enriched rows are in ``detail['video_rows']``.
    1. A provider module — ``app.learning.calibration`` or
       ``app.analytics.calibration`` — exposing
       ``get_calibration_error(conn, channel_id=..., project_id=...,
       niche_profile_id=...)`` returning a mapping with ``value`` (and
       optionally ``unit`` / ``detail``), or ``None``.
    2. A ``calibration_errors`` ledger table with
       ``channel_id`` / ``value`` columns (``project_id`` /
       ``niche`` / ``unit`` used when present): the newest row in
       scope wins.

    When none of the seams has data for the scope, the honest
    default is returned: ``available`` False, no value, an explicit
    reason.  Nothing is ever estimated or fabricated here.
    """
    unavailable = {
        "available": False,
        "reason": (
            "not available — no calibration error has been provided "
            "for this scope yet"
        ),
        "source": None, "value": None, "unit": None, "detail": None,
    }
    # 0) The landed calibration seam (app.calibration / calibration_log).
    if _table_exists(conn, "calibration_log"):
        try:
            from app.calibration.report import build_calibration_report

            calib_report = build_calibration_report(conn, channel_id=channel_id)
        except Exception:
            calib_report = None
        if calib_report and calib_report.get("scopes"):
            scopes = [
                {
                    "channel_id": s.get("channel_id"),
                    "niche": s.get("niche"),
                    "metric": s.get("metric"),
                    "video_count": s.get("video_count"),
                    "entry_count": s.get("entry_count"),
                    "points": s.get("points"),
                }
                for s in calib_report["scopes"]
            ]
            detail: dict[str, Any] = {"scopes": scopes}
            if project_id is not None:
                video_rows = [
                    r for r in calib_report.get("rows") or []
                    if str(r.get("project_id") or "") == str(project_id)
                ]
                detail["video_rows"] = video_rows
                if not video_rows:
                    # The channel has calibration data, but none of it
                    # grades THIS video yet — say exactly that.
                    return {
                        "available": False,
                        "reason": (
                            "not available for this video — calibration "
                            "rows exist for the channel, but none grade "
                            "this project yet"
                        ),
                        "source": "app.calibration (calibration_log)",
                        "value": None, "unit": None, "detail": detail,
                    }
            return {
                "available": True, "reason": None,
                "source": "app.calibration (calibration_log)",
                "value": None, "unit": None, "detail": detail,
            }
    # 1) Provider-module seam.
    for module_name in ("app.learning.calibration", "app.analytics.calibration"):
        try:
            import importlib

            module = importlib.import_module(module_name)
        except Exception:
            continue
        provider = getattr(module, "get_calibration_error", None)
        if not callable(provider):
            continue
        try:
            result = provider(
                conn, channel_id=channel_id, project_id=project_id,
                niche_profile_id=niche_profile_id,
            )
        except Exception:
            continue
        if isinstance(result, Mapping) and result.get("value") is not None:
            return {
                "available": True, "reason": None, "source": module_name,
                "value": float(result["value"]),
                "unit": result.get("unit"),
                "detail": dict(result.get("detail") or {}) or None,
            }
    # 2) Ledger-table seam.
    if _table_exists(conn, "calibration_errors"):
        cols = _columns(conn, "calibration_errors")
        if {"channel_id", "value"} <= cols:
            clauses = ["channel_id = ?"]
            params: list[Any] = [channel_id]
            if project_id is not None and "project_id" in cols:
                clauses.append("(project_id = ? OR project_id IS NULL)")
                params.append(project_id)
            order = "created_at DESC" if "created_at" in cols else "rowid DESC"
            row = _row(
                conn,
                f"SELECT * FROM calibration_errors WHERE {' AND '.join(clauses)}"
                f" ORDER BY {order} LIMIT 1",
                params,
            )
            if row is not None and row.get("value") is not None:
                return {
                    "available": True, "reason": None,
                    "source": "calibration_errors ledger",
                    "value": float(row["value"]),
                    "unit": row.get("unit"),
                    "detail": {
                        k: row.get(k)
                        for k in ("niche", "project_id", "created_at")
                        if k in cols
                    } or None,
                }
    return unavailable


# ---------------------------------------------------------------------------
# Shared assembly helpers
# ---------------------------------------------------------------------------


def _channel_ref(conn: sqlite3.Connection, channel_id: str) -> Optional[dict]:
    if not _table_exists(conn, "channels"):
        return None
    row = _row(conn, "SELECT * FROM channels WHERE id = ?", (channel_id,))
    if row is None:
        return None
    return {
        "id": row.get("id"), "title": row.get("title"),
        "handle": row.get("handle"),
        "youtube_channel_id": row.get("youtube_channel_id"),
        "language": row.get("language"), "status": row.get("status"),
    }


def _publish_record_for(
    conn: sqlite3.Connection, project_id: str
) -> Optional[dict]:
    if not _table_exists(conn, "publish_records"):
        return None
    return _row(
        conn, "SELECT * FROM publish_records WHERE project_id = ?", (project_id,)
    )


def _finalise(
    payload: dict[str, Any], generated_at_ms: int, sources: list[str]
) -> dict[str, Any]:
    payload["generated_at_ms"] = int(generated_at_ms)
    payload["generated_at"] = _iso_z(generated_at_ms)
    payload["sources"] = sources
    document = ReportDocument.model_validate(payload)
    return document.model_dump()


# ---------------------------------------------------------------------------
# Video report
# ---------------------------------------------------------------------------


def build_video_report(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    report_id: Optional[str] = None,
    generated_at_ms: Optional[int] = None,
    period_start_ms: Optional[int] = None,
    period_end_ms: Optional[int] = None,
    period_label: Optional[str] = None,
) -> dict[str, Any]:
    """Build the per-video receipt for ``project_id`` (no persistence).

    Raises :class:`ReportNotFoundError` when the project does not
    exist.  A project with no ledger rows still builds: every section
    is present with honest empty values.
    """
    generated = int(generated_at_ms) if generated_at_ms is not None else _now_ms()
    project = (
        _row(conn, "SELECT * FROM projects WHERE id = ?", (project_id,))
        if _table_exists(conn, "projects") else None
    )
    if project is None:
        raise ReportNotFoundError(f"project {project_id} does not exist")
    channel_id = str(project.get("channel_id"))
    channel = _channel_ref(conn, channel_id)
    record = _publish_record_for(conn, str(project_id))
    publish = classify_publish(project, record)
    gates = _gates_for_project(project, record, publish)

    # Period: explicit bounds win; otherwise the project's lifetime.
    start = period_start_ms if period_start_ms is not None else project.get("created_at")
    end = period_end_ms if period_end_ms is not None else None
    label = period_label or ("custom" if period_start_ms is not None
                             or period_end_ms is not None else "lifetime")
    # Ledger time-filtering only applies to an explicitly bounded
    # period; a lifetime report reads all of the project's ledgers.
    filter_start = period_start_ms
    filter_end = period_end_ms

    usage_rows = _api_usage_rows(conn, [str(project_id)], filter_start, filter_end)
    costs = _costs_section(conn, [project], usage_rows, filter_start, filter_end)
    quota = _quota_section(usage_rows)

    artefacts: list[dict[str, Any]] = []
    if _table_exists(conn, "project_artefacts"):
        artefacts = [
            {"id": r.get("id"), "artefact_type": r.get("artefact_type"),
             "version": r.get("version"), "file_path": r.get("file_path"),
             "content_hash": r.get("content_hash"),
             "is_locked": bool(r.get("is_locked")),
             "created_at_ms": r.get("created_at")}
            for r in _rows(
                conn,
                "SELECT * FROM project_artefacts WHERE project_id = ?"
                " ORDER BY artefact_type, version",
                (project_id,),
            )
        ]
    assets_section: dict[str, Any] = {"asset_count": 0, "licence_count": 0, "by_type": []}
    if _table_exists(conn, "project_assets"):
        joined = _rows(
            conn,
            "SELECT a.asset_type, a.source_provider, COUNT(*) AS n"
            " FROM project_assets pa JOIN assets a ON a.id = pa.asset_id"
            " WHERE pa.project_id = ? GROUP BY a.asset_type, a.source_provider"
            " ORDER BY a.asset_type, a.source_provider",
            (project_id,),
        )
        licence_count = 0
        if _table_exists(conn, "asset_licences"):
            licence_count = len(_rows(
                conn,
                "SELECT l.asset_id FROM asset_licences l"
                " JOIN project_assets pa ON pa.asset_id = l.asset_id"
                " WHERE pa.project_id = ?",
                (project_id,),
            ))
        assets_section = {
            "asset_count": sum(int(r["n"]) for r in joined),
            "licence_count": licence_count,
            "by_type": [
                {"asset_type": r.get("asset_type"),
                 "source_provider": r.get("source_provider"),
                 "count": int(r["n"])}
                for r in joined
            ],
        }
    provenance_section: dict[str, Any] = {"event_count": 0, "by_event_type": {}}
    if _table_exists(conn, "provenance_log"):
        grouped = _rows(
            conn,
            "SELECT event_type, COUNT(*) AS n FROM provenance_log"
            " WHERE project_id = ? GROUP BY event_type ORDER BY event_type",
            (project_id,),
        )
        provenance_section = {
            "event_count": sum(int(r["n"]) for r in grouped),
            "by_event_type": {str(r["event_type"]): int(r["n"]) for r in grouped},
        }

    snapshots: list[dict] = []
    if _table_exists(conn, "analytics_snapshots"):
        clause, params = _period_clause("snapshot_at", filter_start, filter_end)
        snapshots = _rows(
            conn,
            f"SELECT * FROM analytics_snapshots WHERE project_id = ?"
            f"{_and(clause)} ORDER BY snapshot_at ASC",
            (project_id, *params),
        )
    performance = _performance_section(snapshots)

    schedule_ids = [str(project["schedule_id"])] if project.get("schedule_id") else []
    failures = _failures_section(
        conn, [str(project_id)], schedule_ids, filter_start, filter_end
    )
    calibration = load_calibration_error(
        conn, channel_id=channel_id, project_id=str(project_id),
        niche_profile_id=project.get("niche_profile_id"),
    )
    next_planned = _next_planned_section(conn, channel_id, schedule_ids)

    title = (
        (record or {}).get("title") or project.get("topic") or "Video report"
    )
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "report_id": report_id or uuid.uuid4().hex,
        "scope": "video",
        "channel_id": channel_id,
        "project_id": str(project_id),
        "channel": channel,
        "video": {
            "project_id": str(project_id),
            "topic": project.get("topic"),
            "title": (record or {}).get("title"),
            "status": project.get("status"),
            "current_phase": project.get("current_phase"),
            "niche_profile_id": project.get("niche_profile_id"),
            "topic_source": project.get("topic_source"),
            "target_minutes": project.get("target_minutes"),
            "target_words": project.get("target_words"),
            "publish_mode": project.get("publish_mode"),
            "schedule_id": project.get("schedule_id"),
            "youtube_video_id": project.get("youtube_video_id"),
            "created_at_ms": project.get("created_at"),
            "updated_at_ms": project.get("updated_at"),
        },
        "title": f"Video report — {title}",
        "period": {"start_ms": start, "end_ms": end, "label": label},
        "production": {
            "videos_produced": 1,
            "projects_by_status": {str(project.get("status")): 1},
            "artefacts": artefacts,
            "artefact_count": len(artefacts),
            "assets": assets_section,
            "provenance": provenance_section,
            "videos": [],
        },
        "publish": publish,
        "costs": costs,
        "quota": quota,
        "performance": performance,
        "gates": gates,
        "failures_recoveries": failures,
        "calibration_error": calibration,
        "next_planned": next_planned,
        "honesty_notes": [
            "All figures are read from ledger / record tables only; "
            "nothing is recomputed from stage state.",
            "Simulated (fake-provider) publishes are labelled "
            "simulated and never reported as published.",
        ],
    }
    sources = [t for t in _LEDGER_TABLES if _table_exists(conn, t)]
    return _finalise(payload, generated, sources)


# ---------------------------------------------------------------------------
# Channel report
# ---------------------------------------------------------------------------


def build_channel_report(
    conn: sqlite3.Connection,
    channel_id: str,
    *,
    report_id: Optional[str] = None,
    generated_at_ms: Optional[int] = None,
    period_start_ms: Optional[int] = None,
    period_end_ms: Optional[int] = None,
    period_label: Optional[str] = None,
) -> dict[str, Any]:
    """Build the per-channel receipt for ``channel_id`` (no persistence).

    Raises :class:`ReportNotFoundError` when the channel does not
    exist.  A channel with no projects / ledger rows still builds
    with honest empty sections.
    """
    generated = int(generated_at_ms) if generated_at_ms is not None else _now_ms()
    channel = _channel_ref(conn, channel_id)
    if channel is None:
        raise ReportNotFoundError(f"channel {channel_id} does not exist")
    label = period_label or (
        "custom" if period_start_ms is not None or period_end_ms is not None
        else "all_time"
    )

    projects: list[dict] = []
    if _table_exists(conn, "projects"):
        clause, params = _period_clause(
            "created_at", period_start_ms, period_end_ms
        )
        projects = _rows(
            conn,
            "SELECT * FROM projects WHERE channel_id = ?"
            " AND deleted_at IS NULL"
            f"{_and(clause)} ORDER BY created_at ASC",
            (channel_id, *params),
        )
    project_ids = [str(p["id"]) for p in projects]

    usage_rows = _api_usage_rows(
        conn, project_ids, period_start_ms, period_end_ms
    )
    costs = _costs_section(
        conn, projects, usage_rows, period_start_ms, period_end_ms
    )
    quota = _quota_section(usage_rows)

    # Per-video publish + gates, aggregated.
    publish_counts: dict[str, int] = {}
    gate_counts: dict[str, dict[str, int]] = {
        "gate_a": {}, "gate_b": {}, "gate_c": {}
    }
    videos: list[dict[str, Any]] = []
    schedule_ids: list[str] = []
    for project in projects:
        record = _publish_record_for(conn, str(project["id"]))
        publish = classify_publish(project, record)
        cls = str(publish["classification"])
        publish_counts[cls] = publish_counts.get(cls, 0) + 1
        gates = _gates_for_project(project, record, publish)
        for gate_key in ("gate_a", "gate_b", "gate_c"):
            outcome = str(gates[gate_key]["outcome"])
            gate_counts[gate_key][outcome] = (
                gate_counts[gate_key].get(outcome, 0) + 1
            )
        if project.get("schedule_id"):
            schedule_ids.append(str(project["schedule_id"]))
        videos.append({
            "project_id": str(project["id"]),
            "topic": project.get("topic"),
            "title": (record or {}).get("title"),
            "status": project.get("status"),
            "current_phase": project.get("current_phase"),
            "niche_profile_id": project.get("niche_profile_id"),
            "cost_micros": int(project.get("total_cost_micros") or 0),
            "cost_usd": _usd(project.get("total_cost_micros")),
            "publish_classification": cls,
            "publish_label": publish["label"],
            "simulated": bool(publish["simulated"]),
            "youtube_video_id": publish.get("youtube_video_id"),
            "gate_a": gates["gate_a"]["outcome"],
            "gate_b": gates["gate_b"]["outcome"],
            "gate_c": gates["gate_c"]["outcome"],
            "created_at_ms": project.get("created_at"),
        })

    by_status: dict[str, int] = {}
    for project in projects:
        status = str(project.get("status"))
        by_status[status] = by_status.get(status, 0) + 1

    # Performance: snapshots for the channel's videos in period, plus
    # channel-level snapshots (project_id NULL) inside the period.
    snapshots: list[dict] = []
    if _table_exists(conn, "analytics_snapshots"):
        clause, params = _period_clause(
            "snapshot_at", period_start_ms, period_end_ms
        )
        if project_ids:
            placeholders = ", ".join("?" for _ in project_ids)
            snapshots.extend(_rows(
                conn,
                f"SELECT * FROM analytics_snapshots"
                f" WHERE project_id IN ({placeholders}){_and(clause)}"
                " ORDER BY snapshot_at ASC",
                (*project_ids, *params),
            ))
        snapshots.extend(_rows(
            conn,
            "SELECT * FROM analytics_snapshots WHERE channel_id = ?"
            f" AND project_id IS NULL{_and(clause)} ORDER BY snapshot_at ASC",
            (channel_id, *params),
        ))
        snapshots.sort(key=lambda s: int(s.get("snapshot_at") or 0))
    performance = _performance_section(snapshots)

    failures = _failures_section(
        conn, project_ids, schedule_ids, period_start_ms, period_end_ms
    )
    # Channel scope also counts failed schedule runs for the channel's
    # schedules even when no project was created (skipped/failed runs).
    calibration = load_calibration_error(
        conn, channel_id=channel_id, project_id=None, niche_profile_id=None
    )
    next_planned = _next_planned_section(conn, channel_id, schedule_ids)

    simulated_count = publish_counts.get("simulated", 0)
    published_count = publish_counts.get("published", 0)
    if projects:
        publish_section: dict[str, Any] = {
            "classification": (
                "published" if published_count and not simulated_count
                else "simulated" if simulated_count and not published_count
                else "not_published" if not published_count and not simulated_count
                else "unknown"
            ),
            "label": (
                f"{published_count} published, {simulated_count} simulated "
                f"(fake provider — not published to YouTube), "
                f"{publish_counts.get('not_published', 0)} not published, "
                f"{publish_counts.get('unknown', 0)} unconfirmed"
            ),
            "simulated": bool(simulated_count),
            "provider": "fake" if simulated_count else None,
            "counts": publish_counts,
        }
    else:
        publish_section = {
            "classification": "not_published",
            "label": "Not published — no videos in this period",
            "simulated": False,
            "counts": {},
        }

    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "report_id": report_id or uuid.uuid4().hex,
        "scope": "channel",
        "channel_id": str(channel_id),
        "project_id": None,
        "channel": channel,
        "video": None,
        "title": f"Channel report — {channel.get('title') or channel_id}",
        "period": {
            "start_ms": period_start_ms, "end_ms": period_end_ms,
            "label": label,
        },
        "production": {
            "videos_produced": len(projects),
            "projects_by_status": by_status,
            "artefacts": [],
            "artefact_count": 0,
            "assets": {},
            "provenance": {},
            "videos": videos,
        },
        "publish": publish_section,
        "costs": costs,
        "quota": quota,
        "performance": performance,
        "gates": {
            "gate_a": {"outcome": "not_recorded", "raw": None},
            "gate_b": {"outcome": "not_recorded", "raw": None},
            "gate_c": {"outcome": "not_recorded", "raw": None},
            "counts": gate_counts,
        },
        "failures_recoveries": failures,
        "calibration_error": calibration,
        "next_planned": next_planned,
        "honesty_notes": [
            "All figures are read from ledger / record tables only; "
            "nothing is recomputed from stage state.",
            "Simulated (fake-provider) publishes are labelled "
            "simulated and never reported as published.",
            "Performance totals sum the latest snapshot per video so "
            "re-pulled snapshots never double-count.",
        ],
    }
    sources = [t for t in _LEDGER_TABLES if _table_exists(conn, t)]
    return _finalise(payload, generated, sources)
