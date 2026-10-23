"""Ledger queries shared by the receipt builder and the weekly digest.

Every function here is a pure read over the ledgers of record —
``projects``, ``api_usage``, ``publish_records``,
``analytics_snapshots``, ``jobs``, ``agent_runs``, ``schedule_runs``
and ``schedules`` — scoped to ONE channel and one epoch-ms window.
Nothing is estimated and nothing is fabricated: a missing table or an
empty window yields empty structures, and the renderers state that
explicitly (e.g. the digest's "no analytics yet" line).

Publish honesty: the only publish path that exists today is the fake
provider (``app.execution.publish_finaliser``), which stores
``publish_records.youtube_video_id`` NAMESPACED as
``simulated:<provider_video_id>:<project_id>`` (the provider id
itself is e.g. ``fake-video-1``) and a NULL ``video_url``.  Any such
record is reported as ``simulated: True`` — a simulation is NEVER
presented as a live publication (roadmap: 200 from
``videos.insert`` ≠ live; a fake upload is not even that).
"""

from __future__ import annotations

import json
from typing import Any, Optional

WEEK_MS = 7 * 24 * 60 * 60 * 1000


def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


def _table_exists(conn, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def parse_json(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return default
    return default


# ---------------------------------------------------------------------------
# Channel + projects
# ---------------------------------------------------------------------------


def channel_row(conn, channel_id: str) -> Optional[dict]:
    rows = _rows(
        conn,
        "SELECT * FROM channels WHERE id = ? AND deleted_at IS NULL",
        (channel_id,),
    )
    return rows[0] if rows else None


def project_row(conn, project_id: str) -> Optional[dict]:
    rows = _rows(conn, "SELECT * FROM projects WHERE id = ?", (project_id,))
    return rows[0] if rows else None


def find_project_for_video(conn, channel_id: str, video_ref: str) -> Optional[dict]:
    """Resolve a ``video_id`` argument to a project of ``channel_id``.

    Accepts the project id itself or the project's YouTube video id.
    Returns None when no project of THIS channel matches (the caller
    turns that into a 404 — never a cross-channel leak).
    """
    rows = _rows(
        conn,
        "SELECT * FROM projects WHERE channel_id = ? AND id = ?",
        (channel_id, video_ref),
    )
    if rows:
        return rows[0]
    rows = _rows(
        conn,
        "SELECT * FROM projects WHERE channel_id = ? AND youtube_video_id = ?",
        (channel_id, video_ref),
    )
    return rows[0] if rows else None


def projects_created_in_window(
    conn, channel_id: str, start_ms: int, end_ms: int
) -> list[dict]:
    return _rows(
        conn,
        "SELECT * FROM projects WHERE channel_id = ?"
        " AND created_at >= ? AND created_at < ?"
        " ORDER BY created_at ASC",
        (channel_id, start_ms, end_ms),
    )


def channel_project_ids(conn, channel_id: str) -> list[str]:
    return [
        r["id"]
        for r in _rows(conn, "SELECT id FROM projects WHERE channel_id = ?", (channel_id,))
    ]


# ---------------------------------------------------------------------------
# Cost actuals + quota (api_usage ledger)
# ---------------------------------------------------------------------------


def usage_rollup(
    conn, project_ids: list[str], start_ms: int, end_ms: int
) -> dict[str, Any]:
    """Per-provider rollup of the ``api_usage`` ledger for ``project_ids``.

    Returns ``{providers: [...], total_cost_micros, total_calls}``.
    ``api_usage`` rows carry no channel column — attribution is via
    the project's channel, exactly as the Budget Guard accounts.
    """
    empty = {"providers": [], "total_cost_micros": 0, "total_calls": 0}
    if not project_ids or not _table_exists(conn, "api_usage"):
        return empty
    placeholders = ", ".join("?" for _ in project_ids)
    rows = _rows(
        conn,
        f"SELECT provider, COUNT(*) AS calls,"
        f" COALESCE(SUM(cost_micros), 0) AS cost_micros,"
        f" COALESCE(SUM(tokens_in), 0) AS tokens_in,"
        f" COALESCE(SUM(tokens_out), 0) AS tokens_out,"
        f" COALESCE(SUM(characters), 0) AS characters,"
        f" COALESCE(SUM(images), 0) AS images,"
        f" COALESCE(SUM(units), 0) AS units"
        f" FROM api_usage WHERE project_id IN ({placeholders})"
        f" AND created_at >= ? AND created_at < ?"
        f" GROUP BY provider ORDER BY provider",
        (*project_ids, start_ms, end_ms),
    )
    return {
        "providers": rows,
        "total_cost_micros": sum(int(r["cost_micros"]) for r in rows),
        "total_calls": sum(int(r["calls"]) for r in rows),
    }


def quota_used_in_window(conn, start_ms: int, end_ms: int) -> dict[str, Any]:
    """YouTube quota units consumed in the window, by June-2026 bucket.

    Quota is a per-Google-project budget (not per channel), so this is
    deliberately NOT channel-filtered — the digest labels it as the
    shared project-level figure.  Bucket classification reuses
    :func:`app.providers.youtube.quota.classify_method`, the same
    function the live quota view uses, so the two can never drift.
    When a ledger row carries ``units IS NULL`` the per-method cost
    from the classifier is used, mirroring :mod:`app.quota_view`.
    """
    buckets: dict[str, int] = {"uploads": 0, "search": 0, "shared": 0}
    result: dict[str, Any] = {"buckets": buckets, "total_units": 0, "scope": "google_project"}
    if not _table_exists(conn, "api_usage"):
        return result
    try:
        from app.providers.youtube.quota import BUCKET_LIMITS, classify_method
    except Exception:
        return result
    wire = {"upload": "uploads", "uploads": "uploads", "search": "search", "shared": "shared"}
    rows = _rows(
        conn,
        "SELECT operation, units FROM api_usage WHERE provider = 'youtube'"
        " AND created_at >= ? AND created_at < ?",
        (start_ms, end_ms),
    )
    limits: dict[str, int] = {}
    for row in rows:
        try:
            bucket, cost = classify_method(str(row.get("operation") or ""))
        except Exception:
            bucket, cost = "shared", 0
        key = wire.get(str(bucket), "shared")
        units = row.get("units")
        buckets[key] += int(units) if units is not None else int(cost or 0)
        try:
            limits[key] = int(BUCKET_LIMITS[bucket])  # type: ignore[index]
        except Exception:
            pass
    result["limits"] = limits
    result["total_units"] = sum(buckets.values())
    return result


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def is_simulated_publish(record: dict) -> bool:
    """True when a publish_records row came from the fake provider.

    The publish finaliser namespaces every simulated id as
    ``simulated:<provider_video_id>:<project_id>``
    (``app.execution.publish_finaliser``); the bare provider id
    (``fake-...``) is recognised too for rows written before the
    namespacing.  Missing the ``simulated:`` prefix here would
    present every real fake-provider publish as a live publication
    (caught by W7 reconciliation tests,
    ``tests/test_reporting_reconciliation_w7.py``).
    """
    video_id = str(record.get("youtube_video_id") or "")
    return video_id.startswith("simulated:") or video_id.startswith("fake")


def publishes_in_window(
    conn, channel_id: str, start_ms: int, end_ms: int
) -> list[dict]:
    if not _table_exists(conn, "publish_records"):
        return []
    rows = _rows(
        conn,
        "SELECT pr.*, p.topic AS project_topic FROM publish_records pr"
        " JOIN projects p ON p.id = pr.project_id"
        " WHERE p.channel_id = ? AND pr.created_at >= ? AND pr.created_at < ?"
        " ORDER BY pr.created_at ASC",
        (channel_id, start_ms, end_ms),
    )
    for row in rows:
        row["simulated"] = is_simulated_publish(row)
    return rows


def publish_record_for_project(conn, project_id: str) -> Optional[dict]:
    if not _table_exists(conn, "publish_records"):
        return None
    rows = _rows(
        conn, "SELECT * FROM publish_records WHERE project_id = ?", (project_id,)
    )
    if not rows:
        return None
    record = rows[0]
    record["simulated"] = is_simulated_publish(record)
    return record


# ---------------------------------------------------------------------------
# Analytics snapshots → performance deltas
# ---------------------------------------------------------------------------


def analytics_deltas(
    conn, channel_id: str, start_ms: int, end_ms: int
) -> dict[str, Any]:
    """Per-video first→last snapshot deltas within the window.

    Returns ``{present: bool, videos: [...], totals: {...}}``.
    ``present`` is False when the channel has no analytics snapshots
    in the window — the renderer must then say "no analytics yet"
    rather than printing zeros as if they were measurements.
    """
    empty = {"present": False, "videos": [], "totals": {}}
    if not _table_exists(conn, "analytics_snapshots"):
        return empty
    rows = _rows(
        conn,
        "SELECT * FROM analytics_snapshots WHERE channel_id = ?"
        " AND snapshot_at >= ? AND snapshot_at < ?"
        " ORDER BY snapshot_at ASC",
        (channel_id, start_ms, end_ms),
    )
    if not rows:
        return empty
    by_video: dict[str, list[dict]] = {}
    for row in rows:
        key = row.get("youtube_video_id") or row.get("project_id") or "unknown"
        by_video.setdefault(str(key), []).append(row)
    videos: list[dict] = []
    totals = {"views": 0, "watch_hours": 0.0, "likes": 0, "comments_count": 0, "subs_delta": 0}
    for key, snaps in sorted(by_video.items()):
        first, last = snaps[0], snaps[-1]
        delta = {
            "youtube_video_id": last.get("youtube_video_id"),
            "project_id": last.get("project_id"),
            "snapshots": len(snaps),
            "first_snapshot_at": first.get("snapshot_at"),
            "last_snapshot_at": last.get("snapshot_at"),
            "views_delta": int(last.get("views") or 0) - int(first.get("views") or 0),
            "views_latest": int(last.get("views") or 0),
            "watch_hours_delta": (
                int(last.get("watch_hours_x100") or 0) - int(first.get("watch_hours_x100") or 0)
            ) / 100.0,
            "likes_delta": int(last.get("likes") or 0) - int(first.get("likes") or 0),
            "comments_delta": int(last.get("comments_count") or 0)
            - int(first.get("comments_count") or 0),
            "subs_delta": sum(int(s.get("subs_delta") or 0) for s in snaps),
            "ctr_bps_latest": last.get("ctr_bps"),
            "avg_view_duration_sec_latest": last.get("avg_view_duration_sec"),
        }
        videos.append(delta)
        totals["views"] += delta["views_delta"]
        totals["watch_hours"] += delta["watch_hours_delta"]
        totals["likes"] += delta["likes_delta"]
        totals["comments_count"] += delta["comments_delta"]
        totals["subs_delta"] += delta["subs_delta"]
    totals["watch_hours"] = round(totals["watch_hours"], 2)
    return {"present": True, "videos": videos, "totals": totals}


# ---------------------------------------------------------------------------
# Failures, recoveries, gates
# ---------------------------------------------------------------------------


def failures_in_window(
    conn, channel_id: str, start_ms: int, end_ms: int
) -> dict[str, Any]:
    """Failed jobs / agent runs / schedule runs for the channel's work."""
    project_ids = set(channel_project_ids(conn, channel_id))
    failed_jobs: list[dict] = []
    if project_ids and _table_exists(conn, "jobs"):
        placeholders = ", ".join("?" for _ in project_ids)
        failed_jobs = _rows(
            conn,
            f"SELECT id, project_id, agent_key, kind, status, attempts,"
            f" error, error_code, updated_at FROM jobs"
            f" WHERE project_id IN ({placeholders})"
            f" AND status IN ('failed', 'dead')"
            f" AND updated_at >= ? AND updated_at < ?"
            f" ORDER BY updated_at ASC",
            (*project_ids, start_ms, end_ms),
        )
    failed_runs: list[dict] = []
    if project_ids and _table_exists(conn, "agent_runs"):
        placeholders = ", ".join("?" for _ in project_ids)
        failed_runs = _rows(
            conn,
            f"SELECT id, project_id, agent_key, status, error, finished_at"
            f" FROM agent_runs WHERE project_id IN ({placeholders})"
            f" AND status IN ('failed', 'blocked')"
            f" AND COALESCE(finished_at, created_at) >= ?"
            f" AND COALESCE(finished_at, created_at) < ?"
            f" ORDER BY created_at ASC",
            (*project_ids, start_ms, end_ms),
        )
    schedule_failures: list[dict] = []
    if _table_exists(conn, "schedule_runs") and _table_exists(conn, "schedules"):
        schedule_failures = _rows(
            conn,
            "SELECT sr.id, sr.schedule_id, sr.status, sr.reason, sr.created_at,"
            " s.name AS schedule_name FROM schedule_runs sr"
            " JOIN schedules s ON s.id = sr.schedule_id"
            " WHERE s.channel_id = ? AND sr.status != 'created'"
            " AND sr.created_at >= ? AND sr.created_at < ?"
            " ORDER BY sr.created_at ASC",
            (channel_id, start_ms, end_ms),
        )
    return {
        "failed_jobs": failed_jobs,
        "failed_agent_runs": failed_runs,
        "schedule_runs": schedule_failures,
    }


def recoveries_in_window(
    conn, channel_id: str, start_ms: int, end_ms: int, failed_jobs: list[dict]
) -> list[dict]:
    """Projects that hit a failed job in the window but are not failed now.

    A project counts as recovered when at least one of its jobs failed
    in the window and its current status is past the failure —
    anything other than failed/blocked/cancelled.  This is a ledger
    observation, not a claim about *why* it recovered.
    """
    failed_project_ids = {j["project_id"] for j in failed_jobs if j.get("project_id")}
    recovered: list[dict] = []
    for project_id in sorted(failed_project_ids):
        project = project_row(conn, project_id)
        if project and project.get("status") not in ("failed", "blocked", "cancelled"):
            recovered.append(
                {
                    "project_id": project_id,
                    "topic": project.get("topic"),
                    "current_status": project.get("status"),
                }
            )
    return recovered


def gate_summary(conn, projects: list[dict]) -> dict[str, Any]:
    """Gate decision counts from ``projects.gate_status_json``.

    The JSON is written by the gates bridge with ``gate_a`` / ``gate_b``
    keys whose values are decision strings or dicts carrying a
    decision/status/verdict field.  Parsed tolerantly; unknown shapes
    are counted as ``recorded`` rather than guessed at.
    """
    summary: dict[str, dict[str, int]] = {}
    for project in projects:
        gates = parse_json(project.get("gate_status_json"), {}) or {}
        if not isinstance(gates, dict):
            continue
        for gate_key, value in gates.items():
            # gate_status_json also carries non-gate provenance (e.g.
            # the publish finaliser's "publish" entry) — only real
            # gate keys belong in a gate-decisions summary.
            if not str(gate_key).startswith("gate"):
                continue
            decision = None
            if isinstance(value, str):
                decision = value.strip().lower()
            elif isinstance(value, dict):
                for field in ("decision", "status", "verdict", "outcome"):
                    if value.get(field):
                        decision = str(value[field]).strip().lower()
                        break
            bucket = summary.setdefault(str(gate_key), {})
            label = decision or "recorded"
            bucket[label] = bucket.get(label, 0) + 1
    return summary


# ---------------------------------------------------------------------------
# Planned work (schedules)
# ---------------------------------------------------------------------------


def planned_next_week(
    conn, channel_id: str, from_ms: int, horizon_ms: int
) -> list[dict]:
    """Enabled schedules for the channel + occurrences in the horizon.

    Occurrences are computed with the scheduler's own
    :func:`app.execution.scheduler.next_occurrence`, so the digest
    can never disagree with what the scheduler will actually fire.
    """
    if not _table_exists(conn, "schedules"):
        return []
    rows = _rows(
        conn,
        "SELECT * FROM schedules WHERE channel_id = ? AND deleted_at IS NULL"
        " ORDER BY name ASC",
        (channel_id,),
    )
    try:
        from app.execution.scheduler import next_occurrence, parse_cadence
    except Exception:
        next_occurrence = None  # type: ignore[assignment]
        parse_cadence = None  # type: ignore[assignment]
    planned: list[dict] = []
    for schedule in rows:
        cadence = parse_cadence(schedule.get("cadence_json")) if parse_cadence else None
        occurrences: list[int] = []
        if schedule.get("is_enabled") and next_occurrence is not None and cadence:
            cursor = from_ms - 1
            for _ in range(64):  # hard cap — a digest never loops forever
                nxt = next_occurrence(cadence, cursor)
                if nxt is None or nxt >= horizon_ms:
                    break
                if nxt >= from_ms:
                    occurrences.append(int(nxt))
                cursor = int(nxt)
        planned.append(
            {
                "schedule_id": schedule.get("id"),
                "name": schedule.get("name"),
                "enabled": bool(schedule.get("is_enabled")),
                "topic_mode": schedule.get("topic_mode"),
                "cadence_type": cadence.get("type") if isinstance(cadence, dict) else None,
                "next_run_at": schedule.get("next_run_at"),
                "occurrences_in_horizon": occurrences,
            }
        )
    return planned


def agent_runs_for_project(conn, project_id: str) -> list[dict]:
    if not _table_exists(conn, "agent_runs"):
        return []
    return _rows(
        conn,
        "SELECT agent_key, status, model, provider, tokens_in, tokens_out,"
        " cost_micros, error, started_at, finished_at FROM agent_runs"
        " WHERE project_id = ? ORDER BY created_at ASC",
        (project_id,),
    )
