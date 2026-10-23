"""Schedule endpoints (roadmap section 31).

The backend owns schedule logic (Windows Task Scheduler only wakes the
app).  Creation is idempotency-keyed.

Round 4 rewrite — what changed and why
--------------------------------------
The Round 1-3 version of this router had three structural defects:

1. **Two divergent record shapes.**  Schedules created in-process were
   returned in a camel-ish ad-hoc shape (``rule``, ``enabled``,
   ``daily_budget_cap_usd``, ISO timestamps) while schedules read back
   from SQLite came out as raw table rows (``cadence_json``,
   ``is_enabled``, ``daily_cost_cap_micros``, epoch-ms).  Every response
   from this router now goes through :func:`_public`, one normalised
   shape (see its docstring).
2. **Cadence the scheduler cannot read.**  Create persisted
   ``cadence_json = {"rule": <free text>}``, but the real scheduler
   (:mod:`app.execution.scheduler`) only understands structured
   cadences — ``{"type": "daily", "time": "HH:MM"}``,
   ``{"type": "weekly", "weekday": 0..6, "time": "HH:MM"}`` and
   ``{"type": "interval", "interval_hours": N}`` — so API-created
   schedules never advanced.  Create/edit now accept that structured
   ``cadence`` object (validated, with ``next_run_at`` computed via the
   scheduler's own :func:`next_occurrence`), plus ``topics`` for the
   ``fixed_list`` topic mode.  The legacy free-text ``rule``/``cron``
   fields are still accepted for compatibility and stored as
   ``{"type": "rule", "rule": <text>}`` with ``next_run_at = null`` —
   honestly unschedulable until a structured cadence replaces it.
3. **Silent non-persistence.**  Create swallowed every DB error
   (``INSERT OR IGNORE`` inside ``except: pass``), so a schedule whose
   channel/niche failed the FK checks lived only in process memory,
   invisible to the scheduler and gone on restart.  Create now
   validates channel + niche against the DB (422 when either does not
   exist), upserts a minimal ``niche_profiles`` row for built-in /
   session-created profiles (built-ins are not DB rows, and the
   ``schedules`` FK requires one), and fails loudly (503) when the
   database is unavailable instead of pretending success.

Run-now is real: it marks the schedule due and runs one
:class:`~app.execution.scheduler.SchedulerTick`, returning the tick
summary and the resulting ``schedule_runs`` row when one was written.
A disabled schedule returns 409 ``schedule_disabled`` — resume it
first; run-now never silently flips the enabled flag.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app.errors import ApiError
from app.idempotency import require_idempotency_key
from app.idempotency import store as idem_store

router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"])

#: In-memory mirror for records created while the DB was reachable in
#: older builds / for tests that exercise the router without a DB.
#: New creates always persist (or fail); this only feeds list/get
#: merges for ids the DB does not know.
_SCHEDULES: dict[str, dict[str, Any]] = {}

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_TOPIC_MODES = ("auto", "queue", "fixed_list")
_PUBLISH_MODES = ("auto", "review_first")


class ScheduleCreate(BaseModel):
    channel_id: str
    niche_profile_id: str
    name: Optional[str] = None
    #: Structured cadence (see module docstring). Wins over rule/cron.
    cadence: Optional[dict] = None
    rule: Optional[str] = None
    cron: Optional[str] = None
    topic_mode: str = "auto"
    target_minutes: Optional[int] = None
    publish_mode: str = "review_first"
    daily_budget_cap_usd: Optional[float] = None
    daily_cost_cap_micros: Optional[int] = None
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    """Partial edit — every field optional; only sent fields change."""

    name: Optional[str] = None
    channel_id: Optional[str] = None
    niche_profile_id: Optional[str] = None
    cadence: Optional[dict] = None
    rule: Optional[str] = None
    topic_mode: Optional[str] = None
    target_minutes: Optional[int] = None
    publish_mode: Optional[str] = None
    daily_budget_cap_usd: Optional[float] = None
    daily_cost_cap_micros: Optional[int] = None
    enabled: Optional[bool] = None


# ---------------------------------------------------------------------------
# Small DB helpers (same pattern as the sibling routers)
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _conn():
    try:
        from app import db as _db

        return _db.get_connection()
    except Exception:
        return None


def _tables(conn) -> set:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _cols(conn, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _validation(field: str, message: str) -> ApiError:
    return ApiError(
        422, "validation_error", message, details={"field": field}
    )


# ---------------------------------------------------------------------------
# Cadence handling
# ---------------------------------------------------------------------------


def _next_occurrence(cadence: Optional[dict], after_ms: int) -> Optional[int]:
    """Next run for a cadence, via the scheduler's own arithmetic."""
    if not cadence:
        return None
    try:
        from app.execution.scheduler import next_occurrence

        return next_occurrence(cadence, after_ms)
    except Exception:
        return None


def _validate_cadence(raw: Any) -> dict:
    """Validate a structured cadence; returns the normalised dict.

    Extra keys (e.g. ``topics`` for fixed_list mode) are preserved.
    Rule-type cadences (legacy free text) are accepted but carry no
    schedulable occurrence.
    """
    if not isinstance(raw, dict):
        raise _validation("cadence", "Cadence must be an object.")
    cadence = dict(raw)
    ctype = cadence.get("type")
    if ctype == "rule":
        if not str(cadence.get("rule") or "").strip():
            raise _validation("cadence.rule", "A rule cadence needs rule text.")
        return cadence
    if ctype not in ("daily", "weekly", "interval"):
        raise _validation(
            "cadence.type",
            "Cadence type must be 'daily', 'weekly' or 'interval'.",
        )
    if ctype == "interval":
        hours = cadence.get("interval_hours")
        if isinstance(hours, bool) or not isinstance(hours, (int, float)):
            raise _validation(
                "cadence.interval_hours", "interval_hours must be a number."
            )
        if not (0 < float(hours) <= 24 * 30):
            raise _validation(
                "cadence.interval_hours",
                "interval_hours must be between 0 and 720.",
            )
    if ctype in ("daily", "weekly"):
        time_value = cadence.get("time")
        if not isinstance(time_value, str) or _next_occurrence(
            {"type": "daily", "time": time_value}, 0
        ) is None:
            raise _validation(
                "cadence.time", "Cadence time must be 'HH:MM' (24h, UTC)."
            )
    if ctype == "weekly":
        weekday = cadence.get("weekday")
        if (
            isinstance(weekday, bool)
            or not isinstance(weekday, int)
            or not (0 <= weekday <= 6)
        ):
            raise _validation(
                "cadence.weekday", "Weekday must be an integer 0 (Mon) .. 6 (Sun)."
            )
    topics = cadence.get("topics")
    if topics is not None:
        if not isinstance(topics, list) or not all(
            isinstance(t, str) and t.strip() for t in topics
        ):
            raise _validation(
                "cadence.topics", "Cadence topics must be a list of non-empty strings."
            )
    return cadence


def _cadence_summary(cadence: Optional[dict]) -> str:
    if not cadence:
        return "No cadence set"
    ctype = cadence.get("type")
    if ctype == "daily":
        return f"Daily at {cadence.get('time', '?')} UTC"
    if ctype == "weekly":
        weekday = cadence.get("weekday")
        day = (
            _WEEKDAYS[weekday]
            if isinstance(weekday, int) and 0 <= weekday <= 6
            else "?"
        )
        return f"Weekly on {day} at {cadence.get('time', '?')} UTC"
    if ctype == "interval":
        hours = cadence.get("interval_hours")
        label = (
            f"{hours:g}" if isinstance(hours, (int, float)) else str(hours)
        )
        return f"Every {label}h"
    if ctype == "rule":
        return str(cadence.get("rule") or "Custom rule")
    return "Custom cadence"


def _parse_cadence(raw: Any) -> Optional[dict]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


# ---------------------------------------------------------------------------
# Normalised public shape
# ---------------------------------------------------------------------------


def _public(
    row: dict,
    *,
    channel_title: Optional[str] = None,
    niche_name: Optional[str] = None,
) -> dict:
    """One shape for every schedule response, DB row or memory record.

    Timestamps are epoch-ms integers (the DB convention).  ``enabled``
    (bool) and ``is_enabled`` (0/1) are both carried, as are
    ``daily_cost_cap_micros`` and its USD mirror, so existing consumers
    of either spelling keep working.
    """
    cadence = _parse_cadence(row.get("cadence_json") or row.get("cadence"))
    if cadence is None and row.get("rule"):
        cadence = {"type": "rule", "rule": row["rule"]}
    cap_micros = row.get("daily_cost_cap_micros")
    if cap_micros is None and row.get("daily_budget_cap_usd") is not None:
        cap_micros = int(round(float(row["daily_budget_cap_usd"]) * 1_000_000))
    enabled_raw = row.get("is_enabled", row.get("enabled", 1))
    enabled = bool(enabled_raw)
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "channel_id": row.get("channel_id"),
        "channel_title": channel_title,
        "niche_profile_id": row.get("niche_profile_id"),
        "niche_name": niche_name,
        "cadence": cadence,
        "cadence_summary": _cadence_summary(cadence),
        "rule": _cadence_summary(cadence),
        "topic_mode": row.get("topic_mode") or "auto",
        "target_minutes": row.get("target_minutes"),
        "publish_mode": row.get("publish_mode") or "review_first",
        "daily_cost_cap_micros": cap_micros,
        "daily_budget_cap_usd": (
            round(cap_micros / 1_000_000, 6) if cap_micros is not None else None
        ),
        "enabled": enabled,
        "is_enabled": 1 if enabled else 0,
        "next_run_at": row.get("next_run_at"),
        "last_run_at": row.get("last_run_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _names_for(conn, rows: list[dict]) -> dict[str, dict[str, Optional[str]]]:
    """Resolve channel titles + niche names for a batch of rows."""
    names: dict[str, dict[str, Optional[str]]] = {}
    if conn is None:
        return names
    tables = _tables(conn)
    for row in rows:
        entry: dict[str, Optional[str]] = {
            "channel_title": None,
            "niche_name": None,
        }
        channel_id = row.get("channel_id")
        if channel_id and "channels" in tables:
            try:
                found = conn.execute(
                    "SELECT title FROM channels WHERE id = ?", (channel_id,)
                ).fetchone()
                if found is not None:
                    entry["channel_title"] = found[0]
            except Exception:
                pass
        niche_id = row.get("niche_profile_id")
        if niche_id:
            entry["niche_name"] = _niche_display_name(conn, niche_id, tables)
        names[row.get("id")] = entry
    return names


def _niche_display_name(conn, niche_id: str, tables: Optional[set] = None) -> Optional[str]:
    if conn is not None:
        tables = tables if tables is not None else _tables(conn)
        if "niche_profiles" in tables:
            try:
                found = conn.execute(
                    "SELECT label FROM niche_profiles WHERE id = ? OR key = ?",
                    (niche_id, niche_id),
                ).fetchone()
                if found is not None:
                    return found[0]
            except Exception:
                pass
    try:
        from app.routers.niches import BUILTIN_NICHES

        for niche in BUILTIN_NICHES:
            if niche["id"] == niche_id:
                return niche["name"]
    except Exception:
        pass
    try:
        from app.niches.builtins import BUILTIN_PROFILES

        profile = BUILTIN_PROFILES.get(niche_id)
        if profile is not None:
            return profile.name
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# DB access
# ---------------------------------------------------------------------------


def _db_schedules(conn) -> list[dict]:
    if conn is None or "schedules" not in _tables(conn):
        return []
    cols = _cols(conn, "schedules")
    where = "WHERE deleted_at IS NULL" if "deleted_at" in cols else ""
    rows = conn.execute(
        f"SELECT * FROM schedules {where} ORDER BY created_at ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def _db_schedule(conn, schedule_id: str) -> Optional[dict]:
    if conn is None or "schedules" not in _tables(conn):
        return None
    cols = _cols(conn, "schedules")
    where = "AND deleted_at IS NULL" if "deleted_at" in cols else ""
    row = conn.execute(
        f"SELECT * FROM schedules WHERE id = ? {where}", (schedule_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def _list_public() -> list[dict]:
    conn = _conn()
    items: list[dict] = []
    seen: set = set()
    try:
        rows = _db_schedules(conn)
        names = _names_for(conn, rows)
        for row in rows:
            entry = names.get(row.get("id"), {})
            items.append(_public(row, **entry))
            seen.add(row.get("id"))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    for sid, record in _SCHEDULES.items():
        if sid not in seen:
            items.append(_public(record))
    return items


def _find_public(schedule_id: str) -> Optional[dict]:
    for item in _list_public():
        if item["id"] == schedule_id:
            return item
    return None


def _resolve_niche_for_persist(conn, niche_profile_id: str) -> str:
    """Return the niche_profiles row id to reference, upserting a
    minimal row for built-in / session-created profiles (the schedules
    FK needs a real row).  Raises 422 for an unknown niche."""
    tables = _tables(conn)
    if "niche_profiles" in tables:
        found = conn.execute(
            "SELECT id FROM niche_profiles WHERE id = ? OR key = ?",
            (niche_profile_id, niche_profile_id),
        ).fetchone()
        if found is not None:
            return str(found[0])

    label: Optional[str] = None
    source = "other"
    try:
        from app.niches.builtins import BUILTIN_PROFILES

        profile = BUILTIN_PROFILES.get(niche_profile_id)
        if profile is not None:
            label, source = profile.name, "builtin"
    except Exception:
        pass
    if label is None:
        try:
            from app.routers import niches as niches_router

            for niche in niches_router.BUILTIN_NICHES:
                if niche["id"] == niche_profile_id:
                    label, source = niche["name"], "builtin"
                    break
            if label is None and niche_profile_id in niches_router._CREATED:
                created = niches_router._CREATED[niche_profile_id]
                label, source = created.get("name"), "other"
        except Exception:
            pass
    if label is None:
        raise _validation(
            "niche_profile_id",
            f"No niche profile with id '{niche_profile_id}'.",
        )
    if "niche_profiles" in tables:
        now = _now_ms()
        cols = _cols(conn, "niche_profiles")
        values: dict[str, Any] = {
            "id": niche_profile_id,
            "key": niche_profile_id,
            "label": label,
            "source": source,
            "created_at": now,
            "updated_at": now,
        }
        use = {k: v for k, v in values.items() if k in cols}
        conn.execute(
            f"INSERT OR IGNORE INTO niche_profiles ({', '.join(use)}) "
            f"VALUES ({', '.join('?' for _ in use)})",
            tuple(use.values()),
        )
    return niche_profile_id


def _validate_fields(
    *,
    topic_mode: Optional[str] = None,
    publish_mode: Optional[str] = None,
    target_minutes: Optional[int] = None,
    daily_budget_cap_usd: Optional[float] = None,
    daily_cost_cap_micros: Optional[int] = None,
) -> None:
    if topic_mode is not None and topic_mode not in _TOPIC_MODES:
        raise _validation(
            "topic_mode", "topic_mode must be 'auto', 'queue' or 'fixed_list'."
        )
    if publish_mode is not None and publish_mode not in _PUBLISH_MODES:
        raise _validation(
            "publish_mode", "publish_mode must be 'auto' or 'review_first'."
        )
    if target_minutes is not None and (
        isinstance(target_minutes, bool)
        or not isinstance(target_minutes, int)
        or not (1 <= target_minutes <= 600)
    ):
        raise _validation(
            "target_minutes", "target_minutes must be an integer between 1 and 600."
        )
    if daily_budget_cap_usd is not None and daily_budget_cap_usd < 0:
        raise _validation(
            "daily_budget_cap_usd", "daily_budget_cap_usd cannot be negative."
        )
    if daily_cost_cap_micros is not None and daily_cost_cap_micros < 0:
        raise _validation(
            "daily_cost_cap_micros", "daily_cost_cap_micros cannot be negative."
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_schedules() -> dict:
    return {"items": _list_public(), "next_cursor": None}


@router.post("", status_code=201)
async def create_schedule(body: ScheduleCreate, request: Request):
    key = require_idempotency_key(request)
    replay = await idem_store.check(key, request)
    if replay is not None:
        return replay

    _validate_fields(
        topic_mode=body.topic_mode,
        publish_mode=body.publish_mode,
        target_minutes=body.target_minutes,
        daily_budget_cap_usd=body.daily_budget_cap_usd,
        daily_cost_cap_micros=body.daily_cost_cap_micros,
    )
    if body.cadence is not None:
        cadence = _validate_cadence(body.cadence)
    elif (body.rule or body.cron or "").strip():
        cadence = {"type": "rule", "rule": (body.rule or body.cron or "").strip()}
    else:
        raise _validation(
            "cadence",
            "A cadence is required: {type: 'daily'|'weekly'|'interval', ...}.",
        )
    if body.topic_mode == "fixed_list" and not (
        isinstance(cadence.get("topics"), list) and cadence["topics"]
    ):
        raise _validation(
            "cadence.topics",
            "topic_mode 'fixed_list' needs a non-empty cadence.topics list.",
        )

    cap_micros = body.daily_cost_cap_micros
    if cap_micros is None and body.daily_budget_cap_usd is not None:
        cap_micros = int(round(body.daily_budget_cap_usd * 1_000_000))

    conn = _conn()
    if conn is None or "schedules" not in _tables(conn):
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        raise ApiError(
            503,
            "database_unavailable",
            "Cannot create a schedule: the schedules store is unavailable.",
            retryable=True,
        )
    try:
        channel = None
        if "channels" in _tables(conn):
            channel = conn.execute(
                "SELECT id, title FROM channels WHERE id = ? "
                "AND deleted_at IS NULL",
                (body.channel_id,),
            ).fetchone()
        if channel is None:
            raise _validation(
                "channel_id", f"No channel with id '{body.channel_id}'."
            )
        niche_row_id = _resolve_niche_for_persist(conn, body.niche_profile_id)

        now = _now_ms()
        schedule_id = str(uuid.uuid4())
        next_run_at = (
            _next_occurrence(cadence, now) if body.enabled else None
        )
        values: dict[str, Any] = {
            "id": schedule_id,
            "name": body.name or f"Schedule {schedule_id[:8]}",
            "channel_id": body.channel_id,
            "niche_profile_id": niche_row_id,
            "cadence_json": json.dumps(cadence),
            "topic_mode": body.topic_mode,
            "target_minutes": body.target_minutes,
            "publish_mode": body.publish_mode,
            "daily_cost_cap_micros": cap_micros,
            "is_enabled": 1 if body.enabled else 0,
            "next_run_at": next_run_at,
            "last_run_at": None,
            "created_at": now,
            "updated_at": now,
        }
        cols = _cols(conn, "schedules")
        use = {k: v for k, v in values.items() if k in cols}
        conn.execute(
            f"INSERT INTO schedules ({', '.join(use)}) "
            f"VALUES ({', '.join('?' for _ in use)})",
            tuple(use.values()),
        )
        row = _db_schedule(conn, schedule_id) or values
        record = _public(
            row,
            channel_title=channel[1],
            niche_name=_niche_display_name(conn, niche_row_id),
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass

    _SCHEDULES[schedule_id] = dict(record)
    response = JSONResponse(status_code=201, content=record)
    await idem_store.remember(key, request, 201, record)
    return response


@router.get("/{schedule_id}")
async def get_schedule(schedule_id: str) -> dict:
    record = _find_public(schedule_id)
    if record is None:
        raise ApiError(
            404,
            "schedule_not_found",
            f"No schedule with id '{schedule_id}'.",
        )
    return record


@router.patch("/{schedule_id}")
async def update_schedule(schedule_id: str, body: ScheduleUpdate) -> dict:
    """Edit a schedule in place (channel, niche, cadence, topic mode,
    target minutes, publish mode, daily cap, enabled flag).

    Changing the cadence or re-enabling recomputes ``next_run_at``
    from now; disabling clears it (the scheduler ignores disabled
    schedules either way — a null next run is the honest display).
    """
    _validate_fields(
        topic_mode=body.topic_mode,
        publish_mode=body.publish_mode,
        target_minutes=body.target_minutes,
        daily_budget_cap_usd=body.daily_budget_cap_usd,
        daily_cost_cap_micros=body.daily_cost_cap_micros,
    )
    conn = _conn()
    if conn is None:
        raise ApiError(
            503, "database_unavailable", "Schedule store unavailable.",
            retryable=True,
        )
    try:
        row = _db_schedule(conn, schedule_id)
        if row is None:
            raise ApiError(
                404,
                "schedule_not_found",
                f"No schedule with id '{schedule_id}'.",
            )
        updates: dict[str, Any] = {}
        cadence_changed = False
        if body.name is not None:
            updates["name"] = body.name
        if body.channel_id is not None:
            channel = conn.execute(
                "SELECT id FROM channels WHERE id = ? AND deleted_at IS NULL",
                (body.channel_id,),
            ).fetchone()
            if channel is None:
                raise _validation(
                    "channel_id", f"No channel with id '{body.channel_id}'."
                )
            updates["channel_id"] = body.channel_id
        if body.niche_profile_id is not None:
            updates["niche_profile_id"] = _resolve_niche_for_persist(
                conn, body.niche_profile_id
            )
        if body.cadence is not None:
            updates["cadence_json"] = json.dumps(_validate_cadence(body.cadence))
            cadence_changed = True
        elif body.rule is not None:
            updates["cadence_json"] = json.dumps(
                {"type": "rule", "rule": body.rule}
            )
            cadence_changed = True
        if body.topic_mode is not None:
            updates["topic_mode"] = body.topic_mode
        if body.target_minutes is not None:
            updates["target_minutes"] = body.target_minutes
        if body.publish_mode is not None:
            updates["publish_mode"] = body.publish_mode
        if body.daily_cost_cap_micros is not None:
            updates["daily_cost_cap_micros"] = body.daily_cost_cap_micros
        elif body.daily_budget_cap_usd is not None:
            updates["daily_cost_cap_micros"] = int(
                round(body.daily_budget_cap_usd * 1_000_000)
            )
        if body.enabled is not None:
            updates["is_enabled"] = 1 if body.enabled else 0

        merged = dict(row)
        merged.update(updates)
        if body.topic_mode == "fixed_list" or (
            body.topic_mode is None and merged.get("topic_mode") == "fixed_list"
            and cadence_changed
        ):
            cadence = _parse_cadence(merged.get("cadence_json"))
            if not (cadence and isinstance(cadence.get("topics"), list)
                    and cadence["topics"]):
                raise _validation(
                    "cadence.topics",
                    "topic_mode 'fixed_list' needs a non-empty cadence.topics list.",
                )

        enabled = bool(merged.get("is_enabled", 1))
        if not enabled:
            updates["next_run_at"] = None
        elif cadence_changed or body.enabled is True or not merged.get("next_run_at"):
            cadence = _parse_cadence(merged.get("cadence_json"))
            updates["next_run_at"] = _next_occurrence(cadence, _now_ms())

        updates["updated_at"] = _now_ms()
        cols = _cols(conn, "schedules")
        use = {k: v for k, v in updates.items() if k in cols}
        if use:
            conn.execute(
                f"UPDATE schedules SET "
                f"{', '.join(f'{k} = ?' for k in use)} WHERE id = ?",
                (*use.values(), schedule_id),
            )
        fresh = _db_schedule(conn, schedule_id) or merged
        names = _names_for(conn, [fresh]).get(schedule_id, {})
        record = _public(fresh, **names)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    _SCHEDULES[schedule_id] = dict(record)
    return record


@router.post("/{schedule_id}/pause")
async def pause_schedule(schedule_id: str) -> dict:
    """Disable a schedule (is_enabled = 0, next_run_at cleared)."""
    return await update_schedule(
        schedule_id, ScheduleUpdate(enabled=False)
    )


@router.post("/{schedule_id}/resume")
async def resume_schedule(schedule_id: str) -> dict:
    """Re-enable a schedule and recompute its next run from now."""
    return await update_schedule(
        schedule_id, ScheduleUpdate(enabled=True)
    )


@router.delete("/{schedule_id}")
async def delete_schedule(schedule_id: str):
    if _find_public(schedule_id) is None:
        raise ApiError(
            404,
            "schedule_not_found",
            f"No schedule with id '{schedule_id}'.",
        )
    _SCHEDULES.pop(schedule_id, None)
    conn = _conn()
    if conn is not None:
        try:
            if "schedules" in _tables(conn):
                cols = _cols(conn, "schedules")
                if "deleted_at" in cols:
                    conn.execute(
                        "UPDATE schedules SET deleted_at = ? WHERE id = ?",
                        (_now_ms(), schedule_id),
                    )
                else:
                    conn.execute(
                        "DELETE FROM schedules WHERE id = ?", (schedule_id,)
                    )
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return Response(status_code=204)


@router.post(
    "/{schedule_id}/run-now", status_code=202, operation_id="run_schedule_now"
)
@router.post(
    "/{schedule_id}/run_now",
    status_code=202,
    operation_id="run_schedule_now_underscore_alias",
)
async def run_now(schedule_id: str, request: Request):
    """Trigger one real scheduler tick for this schedule, now.

    Marks the schedule due (``next_run_at = now``) and runs
    :class:`SchedulerTick` synchronously; the tick may also materialise
    any *other* already-due schedules (they were due anyway — the
    periodic tick would have taken them within the minute).  Returns
    the tick summary plus this schedule's newest ``schedule_runs`` row
    when the tick wrote one (``run.status`` is ``created`` with a
    ``project_id``, or ``failed`` with a ``reason``).
    """
    key = require_idempotency_key(request)
    replay = await idem_store.check(key, request)
    if replay is not None:
        return replay
    record = _find_public(schedule_id)
    if record is None:
        raise ApiError(
            404,
            "schedule_not_found",
            f"No schedule with id '{schedule_id}'.",
        )
    if not record["enabled"]:
        raise ApiError(
            409,
            "schedule_disabled",
            "This schedule is paused. Resume it before running it now.",
        )

    now = _now_ms()
    conn = _conn()
    if conn is None or _db_schedule(conn, schedule_id) is None:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        raise ApiError(
            409,
            "schedule_not_persisted",
            "This schedule has no stored row, so the scheduler cannot "
            "run it. Re-create it so it is saved first.",
        )
    try:
        conn.execute(
            "UPDATE schedules SET next_run_at = ?, updated_at = ? WHERE id = ?",
            (now, now, schedule_id),
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass

    try:
        from app import db as _db
        from app.execution.scheduler import SchedulerTick

        summary = SchedulerTick(lambda: _db.get_connection()).tick(now)
    except Exception as exc:
        raise ApiError(
            503,
            "scheduler_unavailable",
            f"The scheduler could not run: {type(exc).__name__}.",
            retryable=True,
        )

    run: Optional[dict] = None
    conn = _conn()
    if conn is not None:
        try:
            if "schedule_runs" in _tables(conn):
                row = conn.execute(
                    "SELECT * FROM schedule_runs WHERE schedule_id = ? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (schedule_id,),
                ).fetchone()
                if row is not None and int(row["created_at"]) >= now - 1000:
                    run = _public_run(dict(row))
        except Exception:
            run = None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    result = {
        "status": "accepted",
        "schedule_id": schedule_id,
        "tick": summary,
        "run": run,
        "project_id": run.get("project_id") if run else None,
    }
    response = JSONResponse(status_code=202, content=result)
    await idem_store.remember(key, request, 202, result)
    return response


def _public_run(row: dict) -> dict:
    """A schedule_runs row with detail_json decoded when possible."""
    out = dict(row)
    detail_raw = out.get("detail_json")
    if isinstance(detail_raw, str) and detail_raw.strip():
        try:
            parsed = json.loads(detail_raw)
            if isinstance(parsed, dict):
                out["detail"] = parsed
        except Exception:
            pass
    return out


@router.get("/{schedule_id}/runs")
async def schedule_runs(schedule_id: str) -> dict:
    if _find_public(schedule_id) is None:
        raise ApiError(
            404,
            "schedule_not_found",
            f"No schedule with id '{schedule_id}'.",
        )
    conn = _conn()
    items: list[dict] = []
    if conn is not None:
        try:
            if "schedule_runs" in _tables(conn):
                rows = conn.execute(
                    "SELECT * FROM schedule_runs WHERE schedule_id = ? "
                    "ORDER BY created_at DESC",
                    (schedule_id,),
                ).fetchall()
                items = [_public_run(dict(r)) for r in rows]
        except Exception:
            items = []
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return {"items": items, "next_cursor": None}
