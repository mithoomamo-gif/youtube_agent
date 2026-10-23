"""Read model for budget exposure (Round 4).

Mirrors :mod:`app.quota_view` in shape and clock discipline: the same
America/Los_Angeles ("Pacific") calendar defines *today* here as for
the YouTube quota buckets, so the UI never shows two different days.

For BOTH the current Pacific day and the current Pacific month this
view reports, at app level and per channel::

    {cap_usd, settled_usd, reserved_usd, spent_usd, remaining_usd}

(plus the same figures in integer micros, ``*_micros``, so no float
rounding can ever make the parts disagree with the totals).

Accounting model — identical to enforcement, by construction
-------------------------------------------------------------
This view is a pure read over the same stores the Budget Guard gate
(:mod:`app.execution.budget_gate`) enforces against; it duplicates
no enforcement logic and its ``spent`` figures equal the gate's
``bucket_committed_micros`` for the same bucket:

* ``settled`` — actual costs committed in the bucket:
  ``settled_micros`` of every reservation row in the bucket (a settled
  slice stays committed even after the row settles or is released —
  it is real spend), **plus** legacy actuals: ``projects
  .total_cost_micros`` of projects *created* inside the bucket window
  that have **no** reservation row at all (pre-Round-4 projects).  A
  project with a reservation is never in the legacy sum, so its spend
  is counted exactly once.
* ``reserved`` — the still-committed remainder of *active*
  reservations in the bucket: ``max(0, reserved - settled)`` per row.
  Released / settled rows commit no remainder.
* ``spent = settled + reserved`` — the gate's committed spend.
  ``remaining = max(0, cap - spent)``; caps come from the persisted
  budget policy (:mod:`app.providers.budget.policy`).

Reservations are read through the store's own read API
(:func:`app.execution.budget_reservations.list_reservations`, whose
rows carry the derived ``remaining_micros``); bucket membership uses
the ``pacific_day`` / ``pacific_month`` stamps the store writes at
reserve time, so a settle landing after Pacific midnight never moves
spend between days.  If the store module/table is unavailable the
view falls back to shape-adaptive SQL over any ``*reserv*`` table
(same model), and with no store at all ``reserved`` is an honest zero.

The ``api_usage`` provider ledger is deliberately **not** budget
spend: it is the quota/provider-call ledger (see
:mod:`app.execution.channel_context`, which likewise reports it
separately, never added).  Provider actuals reach the budget buckets
by settling against reservations as stage costs are recorded.

Channel attribution: reservation rows carry the project's
``channel_id`` (nullable in the store); legacy projects attribute via
their own ``channel_id``.  Anything unattributable lands under the
``"unassigned"`` channel key (:data:`UNASSIGNED_CHANNEL` — the
null/unassigned bucket, a string so the key is identical in Python
and JSON).  The app totals are built as the sum of the per-channel
micros, so per-channel figures sum **exactly** to the app totals.

When a ledger/store is missing or unreadable the view degrades to
honest zeros with correct caps and reset math, exactly like the
quota view — never fabricated numbers.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from app.providers.budget.money import micros_to_usd, usd_to_micros
from app.providers.budget.policy import (
    DEFAULT_BUDGET_POLICY,
    policy_from_settings,
)
from app.providers.youtube.quota import PACIFIC, next_reset, pacific_day

__all__ = [
    "UNASSIGNED_CHANNEL",
    "get_budget_snapshot",
    "budget_snapshot_from_conn",
    "pacific_month",
]

#: Channel key for spend that cannot be attributed to a channel (the
#: null/unassigned bucket).  A string, not ``None``, so the key is the
#: same in the Python payload and its JSON rendering.
UNASSIGNED_CHANNEL = "unassigned"

#: Reservation statuses that still commit a remainder.  The Round 4
#: store spells it ``active``; close synonyms are accepted by the
#: shape-adaptive fallback reader only.
_ACTIVE_STATUSES = {"active", "reserved", "pending", "held", "open"}

# Candidate column names for the shape-adaptive fallback reader, in
# priority order.  The first present in the discovered table wins.
_RESERVED_MICROS_COLS = (
    "reserved_micros",
    "reserved_cost_micros",
    "reserved_amount_micros",
    "amount_micros",
    "estimate_micros",
    "cost_micros",
)
_RESERVED_USD_COLS = (
    "reserved_usd",
    "reserved_amount_usd",
    "amount_usd",
    "estimate_usd",
    "cost_usd",
)
_SETTLED_MICROS_COLS = ("settled_micros", "settled_cost_micros", "actual_micros")
_SETTLED_USD_COLS = ("settled_usd", "settled_amount_usd", "actual_usd")
_STATUS_COLS = ("status", "state")
_DAY_BUCKET_COLS = ("pacific_day", "pacific_date", "day_bucket", "bucket_day", "day_pt", "day")
_MONTH_BUCKET_COLS = ("pacific_month", "month_bucket", "bucket_month", "month_pt", "month")
_TS_COLS = ("created_at", "reserved_at", "ts", "timestamp", "created_ms", "updated_at")


# ---------------------------------------------------------------------------
# Clock helpers (mirror quota_view.py / providers.youtube.quota)
# ---------------------------------------------------------------------------


def _normalize_now(now: Optional[datetime]) -> datetime:
    """Return an aware datetime for ``now`` (naive input = Pacific time,
    mirroring :func:`app.providers.youtube.quota.pacific_day`)."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=PACIFIC)
    return now


def pacific_month(now: Optional[datetime] = None) -> str:
    """The current calendar month in America/Los_Angeles, ``YYYY-MM``."""
    day = pacific_day(_normalize_now(now) if now is not None else None)
    return f"{day.year:04d}-{day.month:02d}"


def _pacific_day_window_ms(now: datetime) -> tuple[int, int]:
    """``[start_ms, end_ms)`` epoch-ms window of the current Pacific day."""
    day = pacific_day(now)
    start = datetime.combine(day, time.min, tzinfo=PACIFIC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=PACIFIC)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _month_bounds(day: date) -> tuple[date, date]:
    """``(first of day's month, first of next month)`` as Pacific dates."""
    first = day.replace(day=1)
    if first.month == 12:
        nxt = date(first.year + 1, 1, 1)
    else:
        nxt = date(first.year, first.month + 1, 1)
    return first, nxt


def _pacific_month_window_ms(now: datetime) -> tuple[int, int]:
    """``[start_ms, end_ms)`` epoch-ms window of the current Pacific month."""
    first, nxt = _month_bounds(pacific_day(now))
    start = datetime.combine(first, time.min, tzinfo=PACIFIC)
    end = datetime.combine(nxt, time.min, tzinfo=PACIFIC)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _next_month_reset(now: datetime) -> datetime:
    """Midnight Pacific on the first of next month."""
    _, nxt = _month_bounds(pacific_day(now))
    return datetime.combine(nxt, time.min, tzinfo=PACIFIC)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Payload scaffolding
# ---------------------------------------------------------------------------


def _bucket(
    cap_micros: Optional[int], settled_micros: int, reserved_micros: int
) -> dict[str, Any]:
    """One ``{cap, settled, reserved, spent, remaining}`` figure set."""
    spent = int(settled_micros) + int(reserved_micros)
    remaining: Optional[int] = (
        None if cap_micros is None else max(0, int(cap_micros) - spent)
    )

    def usd(micros: Optional[int]) -> Optional[float]:
        return None if micros is None else float(micros_to_usd(micros))

    return {
        "cap_usd": usd(cap_micros),
        "settled_usd": usd(int(settled_micros)),
        "reserved_usd": usd(int(reserved_micros)),
        "spent_usd": usd(spent),
        "remaining_usd": usd(remaining),
        "cap_micros": cap_micros,
        "settled_micros": int(settled_micros),
        "reserved_micros": int(reserved_micros),
        "spent_micros": spent,
        "remaining_micros": remaining,
    }


def _caps_from_policy(policy: dict[str, Any]) -> tuple[int, int]:
    return (
        usd_to_micros(str(policy["daily_cap_usd"])),
        usd_to_micros(str(policy["monthly_cap_usd"])),
    )


def _base_payload(
    now: datetime, policy: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """Time scaffolding + honest-zero buckets at the given policy caps."""
    policy = policy or DEFAULT_BUDGET_POLICY
    day_cap, month_cap = _caps_from_policy(policy)
    day = pacific_day(now)
    reset_dt = next_reset(now)
    month_reset_dt = _next_month_reset(now)
    seconds = max(0, int((reset_dt - now).total_seconds()))
    month_seconds = max(0, int((month_reset_dt - now).total_seconds()))
    return {
        "pacific_date": day.isoformat(),
        "date": day.isoformat(),
        "pacific_month": pacific_month(now),
        "resets_at": _iso_z(reset_dt),
        "seconds_until_reset": seconds,
        "month_resets_at": _iso_z(month_reset_dt),
        "seconds_until_month_reset": month_seconds,
        "day": _bucket(day_cap, 0, 0),
        "month": _bucket(month_cap, 0, 0),
        # Per-channel caps are not defined by the budget policy (only
        # app-level daily/monthly caps exist), so channel buckets carry
        # cap/remaining = None rather than a copied global figure.
        "channels": {},
        "policy": {
            "per_video_cap_usd": policy["per_video_cap_usd"],
            "daily_cap_usd": policy["daily_cap_usd"],
            "monthly_cap_usd": policy["monthly_cap_usd"],
            "warn_pct": policy["warn_pct"],
            "soft_pct": policy["soft_pct"],
        },
    }


# ---------------------------------------------------------------------------
# Small sqlite helpers (same style as quota_view.py)
# ---------------------------------------------------------------------------


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _pick(cols: set[str], candidates: tuple[str, ...]) -> Optional[str]:
    for name in candidates:
        if name in cols:
            return name
    return None


def _load_policy(conn: sqlite3.Connection) -> dict[str, Any]:
    """Effective budget policy from the settings table on ``conn``."""
    settings: dict[str, Any] = {}
    try:
        if _table_exists(conn, "settings"):
            import json

            for row in conn.execute("SELECT key, value_json FROM settings"):
                raw = row[1]
                try:
                    settings[row[0]] = json.loads(raw)
                except (TypeError, ValueError):
                    settings[row[0]] = raw
    except Exception:
        settings = {}
    return policy_from_settings(settings)


# ---------------------------------------------------------------------------
# Reservation rows — the store's own read API first, shape-adaptive SQL
# as a fallback.  Both paths normalise to:
#   {project_id, channel_id, status, settled_micros, remaining_micros,
#    day_key (str|None), month_key (str|None), created_ms (int|None)}
# ---------------------------------------------------------------------------


def _reservation_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """All reservation rows, normalised (empty when no store exists)."""
    try:
        from app.execution import budget_reservations as store

        if _table_exists(conn, store.RESERVATIONS_TABLE):
            rows = store.list_reservations(conn)
            return [
                {
                    "project_id": r.get("project_id"),
                    "channel_id": r.get("channel_id"),
                    "status": str(r.get("status") or ""),
                    "settled_micros": int(r.get("settled_micros") or 0),
                    "remaining_micros": int(r.get("remaining_micros") or 0),
                    "day_key": r.get("pacific_day"),
                    "month_key": r.get("pacific_month"),
                    "created_ms": r.get("created_at"),
                }
                for r in rows
            ]
    except Exception:
        pass
    return _reservation_rows_generic(conn)


def _amount_micros(row: Any, idx_map: dict[str, int], kind: str) -> int:
    micros_cols = (
        _RESERVED_MICROS_COLS if kind == "reserved" else _SETTLED_MICROS_COLS
    )
    usd_cols = _RESERVED_USD_COLS if kind == "reserved" else _SETTLED_USD_COLS
    for col in micros_cols:
        if col in idx_map:
            value = row[idx_map[col]]
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return 0
    for col in usd_cols:
        if col in idx_map:
            value = row[idx_map[col]]
            if value is not None:
                try:
                    return usd_to_micros(str(value))
                except Exception:
                    return 0
    return 0


def _ts_ms(value: Any) -> Optional[int]:
    """Normalise a stored timestamp to epoch ms (int ms / int s / ISO)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = int(value)
        # Heuristic: seconds-scale epochs are < 10^11 until year 5138.
        return v * 1000 if 0 < v < 100_000_000_000 else v
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=PACIFIC)
        return int(dt.timestamp() * 1000)
    return None


def _reservation_rows_generic(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Shape-adaptive fallback: read any ``*reserv*`` table by contract
    column names (USD- or micros-denominated), same accounting model."""
    try:
        tables = [
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND LOWER(name) LIKE '%reserv%'"
            )
        ]
    except Exception:
        return []
    tables.sort(key=lambda n: (0 if "budget" in n.lower() else 1, n))
    for table in tables:
        cols = _columns(conn, table)
        status_col = _pick(cols, _STATUS_COLS)
        if status_col is None:
            continue
        day_col = _pick(cols, _DAY_BUCKET_COLS)
        month_col = _pick(cols, _MONTH_BUCKET_COLS)
        ts_col = _pick(cols, _TS_COLS)
        wanted = {status_col, "channel_id", "project_id"}
        wanted.update(_RESERVED_MICROS_COLS, _RESERVED_USD_COLS)
        wanted.update(_SETTLED_MICROS_COLS, _SETTLED_USD_COLS)
        for extra in (day_col, month_col, ts_col):
            if extra:
                wanted.add(extra)
        select_cols = [c for c in sorted(wanted) if c in cols]
        if status_col not in select_cols:
            continue
        idx_map = {name: i for i, name in enumerate(select_cols)}
        out: list[dict[str, Any]] = []
        for row in conn.execute(f"SELECT {', '.join(select_cols)} FROM {table}"):
            status = str(row[idx_map[status_col]] or "")
            reserved = _amount_micros(row, idx_map, "reserved")
            settled = _amount_micros(row, idx_map, "settled")
            remaining = (
                max(0, reserved - settled)
                if status.strip().lower() in _ACTIVE_STATUSES
                else 0
            )
            out.append(
                {
                    "project_id": (
                        row[idx_map["project_id"]] if "project_id" in idx_map else None
                    ),
                    "channel_id": (
                        row[idx_map["channel_id"]] if "channel_id" in idx_map else None
                    ),
                    "status": status,
                    "settled_micros": settled,
                    "remaining_micros": remaining,
                    "day_key": row[idx_map[day_col]] if day_col else None,
                    "month_key": row[idx_map[month_col]] if month_col else None,
                    "created_ms": _ts_ms(row[idx_map[ts_col]]) if ts_col else None,
                }
            )
        return out
    return []


# ---------------------------------------------------------------------------
# Spend assembly
# ---------------------------------------------------------------------------


def _in_bucket(
    row: dict[str, Any],
    *,
    day_key: str,
    month_key: str,
    day_window: tuple[int, int],
    month_window: tuple[int, int],
) -> tuple[bool, bool]:
    """``(in_day, in_month)`` for a normalised reservation row.

    Stored Pacific bucket stamps win (the store's discipline);
    otherwise the creation timestamp is windowed like a cost row; a
    row with no time signal at all is a live record and counts in
    both buckets.
    """
    in_day: Optional[bool] = None
    in_month: Optional[bool] = None
    if row.get("day_key"):
        in_day = str(row["day_key"])[:10] == day_key
    if row.get("month_key"):
        in_month = str(row["month_key"])[:7] == month_key
    if (in_day is None or in_month is None) and row.get("created_ms") is not None:
        ts_ms = int(row["created_ms"])
        if in_day is None:
            in_day = day_window[0] <= ts_ms < day_window[1]
        if in_month is None:
            in_month = month_window[0] <= ts_ms < month_window[1]
    return (True if in_day is None else in_day, True if in_month is None else in_month)


def _spend_by_channel(
    conn: sqlite3.Connection,
    now: datetime,
    day_window: tuple[int, int],
    month_window: tuple[int, int],
) -> dict[Any, dict[str, int]]:
    """Per-channel settled/reserved micros for the day + month buckets.

    Returns ``{channel_key: {"day_settled", "day_reserved",
    "month_settled", "month_reserved"}}``.
    """
    out: dict[Any, dict[str, int]] = {}

    def entry(channel: Any) -> dict[str, int]:
        return out.setdefault(
            channel,
            {"day_settled": 0, "day_reserved": 0,
             "month_settled": 0, "month_reserved": 0},
        )

    day_key = pacific_day(now).isoformat()
    month_key = pacific_month(now)
    rows = _reservation_rows(conn)
    reserved_project_ids = {
        str(r["project_id"]) for r in rows if r.get("project_id") is not None
    }
    for r in rows:
        in_day, in_month = _in_bucket(
            r, day_key=day_key, month_key=month_key,
            day_window=day_window, month_window=month_window,
        )
        if not (in_day or in_month):
            continue
        channel = r.get("channel_id")
        if channel is None:
            channel = UNASSIGNED_CHANNEL
        e = entry(channel)
        if in_day:
            e["day_settled"] += int(r["settled_micros"])
            e["day_reserved"] += int(r["remaining_micros"])
        if in_month:
            e["month_settled"] += int(r["settled_micros"])
            e["month_reserved"] += int(r["remaining_micros"])

    # Legacy actuals: projects created inside the month window (the day
    # window nests inside it) with NO reservation row at all — the
    # gate's rule, so reservation-backed spend is counted exactly once.
    if _table_exists(conn, "projects"):
        cols = _columns(conn, "projects")
        if {"total_cost_micros", "created_at", "channel_id", "id"} <= cols:
            for row in conn.execute(
                "SELECT id, channel_id, total_cost_micros, created_at "
                "FROM projects WHERE created_at >= ? AND created_at < ?",
                (month_window[0], month_window[1]),
            ):
                if str(row[0]) in reserved_project_ids:
                    continue
                try:
                    cost = int(row[2] or 0)
                    ts_ms = int(row[3] or 0)
                except (TypeError, ValueError):
                    continue
                if cost == 0:
                    continue
                channel = row[1] if row[1] is not None else UNASSIGNED_CHANNEL
                e = entry(channel)
                e["month_settled"] += cost
                if day_window[0] <= ts_ms < day_window[1]:
                    e["day_settled"] += cost
    return out


# ---------------------------------------------------------------------------
# Snapshot assembly
# ---------------------------------------------------------------------------


def budget_snapshot_from_conn(
    conn: sqlite3.Connection, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Budget summary for the current Pacific day + month.

    ``conn`` is caller-owned (never closed here).  Missing tables /
    columns degrade to honest zeros, matching :mod:`app.quota_view`.
    """
    now = _normalize_now(now)
    policy = _load_policy(conn)
    payload = _base_payload(now, policy)
    try:
        day_window = _pacific_day_window_ms(now)
        month_window = _pacific_month_window_ms(now)
        spend = _spend_by_channel(conn, now, day_window, month_window)

        channels: dict[Any, dict[str, Any]] = {}
        totals = {"day_settled": 0, "day_reserved": 0,
                  "month_settled": 0, "month_reserved": 0}
        for key in sorted(spend, key=lambda k: str(k)):
            s = spend[key]
            channels[key] = {
                "day": _bucket(None, s["day_settled"], s["day_reserved"]),
                "month": _bucket(None, s["month_settled"], s["month_reserved"]),
            }
            for field in totals:
                totals[field] += s[field]

        day_cap, month_cap = _caps_from_policy(policy)
        payload["channels"] = channels
        payload["day"] = _bucket(
            day_cap, totals["day_settled"], totals["day_reserved"]
        )
        payload["month"] = _bucket(
            month_cap, totals["month_settled"], totals["month_reserved"]
        )
        return payload
    except Exception:
        # A broken ledger read must not fabricate spend: zeros (with
        # the real caps + reset math) are the only honest fallback.
        return _base_payload(now, policy)


def get_budget_snapshot(now: Optional[datetime] = None) -> dict[str, Any]:
    """Budget snapshot from the app's configured SQLite database.

    Opens a short connection via ``app.db.get_connection()`` (looked up
    dynamically at call time, like :mod:`app.quota_view`) and closes
    it.  Any failure degrades to honest zeros with correct reset math.
    """
    now = _normalize_now(now)
    conn = None
    try:
        from app import db as _db

        conn = _db.get_connection()
    except Exception:
        return _base_payload(now)
    try:
        return budget_snapshot_from_conn(conn, now=now)
    finally:
        try:
            conn.close()
        except Exception:
            pass
