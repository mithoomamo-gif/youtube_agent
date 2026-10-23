"""Production lessons — aggregated gate/approval reject reasons.

A SEPARATE view from performance (audience) learning rules:

* Performance rules (``learning_rules``, consumed via
  :mod:`app.learning.consumption`) say what the *audience* responded
  to, per (channel, niche), and are written by the learning loop.
* Production lessons (this module) say what *production* kept getting
  rejected for, aggregated from the structured reject reasons already
  stored on approval/gate decision records (``approvals`` rows with
  ``decision='rejected'`` and a ``reason_code`` — e.g. repeated
  ``weak_hook`` rejects at Gate A).  They are computed on read from
  those records; nothing here reads or writes ``learning_rules``,
  and the two views share no structure or endpoint.

Aggregation contract:

* Scope is exactly one ``(channel_id, niche)``: decisions are reached
  through their project (``approvals.project_id -> projects``), and a
  project matches the scope when its ``channel_id`` equals the scope
  channel AND its niche matches — either by ``niche_profile_id``
  (when the caller passes the profile id) or by the joined
  ``niche_profiles.key`` (when the caller passes the niche key, the
  Learning Store's scope spelling).  Decisions from any other
  channel or niche are invisible.
* Each distinct ``reason_code`` becomes one entry carrying:
  ``evidence_count`` (the number of rejected decisions with that
  reason — every count is shown, including 1), ``example_project_ids``
  (project ids ONLY, distinct, first-seen order, capped at
  :data:`MAX_EXAMPLE_PROJECTS` — no titles, notes or other project
  content), ``gates`` (the gates the rejects happened at), and
  ``first_seen_at`` / ``last_seen_at`` (epoch-ms of the earliest /
  latest decision, from ``decided_at`` falling back to
  ``created_at``).
* Threshold: an entry with ``evidence_count >= LESSON_THRESHOLD``
  (3) is a **lesson**; below that it is an **observation**.  A reason
  seen once is listed with count 1 as an observation — it is never
  promoted to a lesson headline on a single occurrence.  The view
  returns ``lessons`` and ``observations`` as separate lists, each
  sorted by evidence count (desc) then reason code.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "LESSON_THRESHOLD",
    "MAX_EXAMPLE_PROJECTS",
    "aggregate_production_lessons",
    "production_lessons_for_scope",
]

#: Minimum rejected-decision count for a reason to count as a lesson.
LESSON_THRESHOLD = 3

#: Cap on example project ids kept per entry (ids only, ever).
MAX_EXAMPLE_PROJECTS = 5


def _table_exists(conn: Any, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def aggregate_production_lessons(
    conn: Any, *, channel_id: str, niche: str
) -> dict:
    """Aggregate rejected-decision reasons for one (channel, niche).

    ``conn`` is a ``sqlite3`` connection (any row factory).  A missing
    ``approvals`` table yields an honest empty view, not an error —
    the approvals table is created lazily by its owning router.
    """
    channel = str(channel_id or "").strip()
    niche_s = str(niche or "").strip()
    if not channel:
        raise ValueError("channel_id is required (lessons are channel-scoped)")
    if not niche_s:
        raise ValueError("niche is required (lessons are never pooled across niches)")

    view: dict[str, Any] = {
        "channel_id": channel,
        "niche": niche_s,
        "source": "approvals.rejected_decisions",
        "threshold": LESSON_THRESHOLD,
        "total_rejected_decisions": 0,
        "lessons": [],
        "observations": [],
    }
    if not _table_exists(conn, "approvals") or not _table_exists(conn, "projects"):
        return view

    rows = conn.execute(
        "SELECT a.reason_code AS reason_code, a.gate AS gate,"
        " a.project_id AS project_id,"
        " COALESCE(a.decided_at, a.created_at) AS seen_at"
        " FROM approvals a"
        " JOIN projects p ON p.id = a.project_id"
        " LEFT JOIN niche_profiles np ON np.id = p.niche_profile_id"
        " WHERE a.decision = 'rejected'"
        " AND a.reason_code IS NOT NULL AND TRIM(a.reason_code) != ''"
        " AND p.channel_id = ?"
        " AND (p.niche_profile_id = ? OR np.key = ?)"
        " ORDER BY seen_at ASC, a.id ASC",
        (channel, niche_s, niche_s),
    ).fetchall()

    groups: dict[str, dict[str, Any]] = {}
    total = 0
    for row in rows:
        data = dict(row)
        reason = str(data.get("reason_code") or "").strip()
        if not reason:
            continue
        total += 1
        seen_at = data.get("seen_at")
        seen_at = int(seen_at) if seen_at is not None else None
        group = groups.get(reason)
        if group is None:
            group = {
                "reason_code": reason,
                "evidence_count": 0,
                "count": 0,
                "example_project_ids": [],
                "gates": [],
                "first_seen_at": seen_at,
                "last_seen_at": seen_at,
            }
            groups[reason] = group
        group["evidence_count"] += 1
        group["count"] = group["evidence_count"]
        project_id = str(data.get("project_id") or "")
        if (
            project_id
            and project_id not in group["example_project_ids"]
            and len(group["example_project_ids"]) < MAX_EXAMPLE_PROJECTS
        ):
            group["example_project_ids"].append(project_id)
        gate = str(data.get("gate") or "").strip()
        if gate and gate not in group["gates"]:
            group["gates"].append(gate)
        if seen_at is not None:
            if group["first_seen_at"] is None or seen_at < group["first_seen_at"]:
                group["first_seen_at"] = seen_at
            if group["last_seen_at"] is None or seen_at > group["last_seen_at"]:
                group["last_seen_at"] = seen_at

    entries = sorted(
        groups.values(),
        key=lambda g: (-g["evidence_count"], g["reason_code"]),
    )
    for entry in entries:
        is_lesson = entry["evidence_count"] >= LESSON_THRESHOLD
        entry["status"] = "lesson" if is_lesson else "observation"
        entry["is_lesson"] = is_lesson
        entry["gates"] = sorted(entry["gates"])
    view["total_rejected_decisions"] = total
    view["lessons"] = [e for e in entries if e["is_lesson"]]
    view["observations"] = [e for e in entries if not e["is_lesson"]]
    view["entries"] = entries
    return view


def production_lessons_for_scope(
    source: Any, *, channel_id: str, niche: str
) -> dict:
    """Lessons view from a connection, a conn_factory, or a path.

    ``source`` may be a ``sqlite3`` connection (used as-is, never
    closed), a callable returning one (closed after the read), or a
    filesystem path to the DB file.
    """
    if hasattr(source, "execute"):
        return aggregate_production_lessons(
            source, channel_id=channel_id, niche=niche
        )
    factory = source
    if not callable(factory):
        from pathlib import Path

        from app import db as _db

        path = Path(str(source))
        factory = lambda: _db.connect(path)  # noqa: E731
    conn = factory()
    try:
        return aggregate_production_lessons(
            conn, channel_id=channel_id, niche=niche
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass
