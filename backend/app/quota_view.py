"""Read model for YouTube quota exposure (Round 3).

Aggregates the append-only ``api_usage`` ledger into the three June 2026
granular buckets defined by :mod:`app.providers.youtube.quota`:

- ``videos.insert`` -> ``uploads`` bucket (limit 100/day)
- ``search.list``   -> ``search`` bucket (limit 100/day)
- everything else   -> ``shared`` bucket (limit 10,000 units/day)

Buckets reset at midnight Pacific Time.  Bucket classification and
per-method unit costs come from
:func:`app.providers.youtube.quota.classify_method`, so this view can
never drift from the quota-aware wrapper's own accounting.

Ledger contract (schema_ddl ``api_usage``, roadmap §8.6): one row per
external call with ``provider='youtube'``, ``operation`` = the API method
(``videos.insert``, ``search.list``, ``thumbnails.set``, ...),
``units`` = the quota units Google charged for the call, and
``created_at`` = Unix epoch **milliseconds**.  The stored ``units``
value is authoritative when present (the wrapper records what was
charged, including for failed calls); when a row carries ``units IS
NULL`` the view falls back to the per-method cost from
``classify_method`` so the row is still counted, never dropped.

When the ledger is empty (or the table/DB is unavailable) the view
returns honest zeros — never fabricated numbers.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, time, timedelta, timezone
from typing import Any, Optional

from app.providers.youtube.quota import (
    BUCKET_LIMITS,
    BUCKET_SEARCH,
    BUCKET_SHARED,
    BUCKET_UPLOAD,
    PACIFIC,
    classify_method,
    next_reset,
    pacific_day,
)

__all__ = [
    "get_quota_snapshot",
    "quota_snapshot_from_conn",
]

#: Internal bucket name (quota.py) -> wire bucket name (API contract).
_BUCKET_WIRE_NAMES: dict[str, str] = {
    BUCKET_UPLOAD: "uploads",
    BUCKET_SEARCH: "search",
    BUCKET_SHARED: "shared",
}


def _normalize_now(now: Optional[datetime]) -> datetime:
    """Return an aware datetime for ``now`` (naive input = Pacific time,
    mirroring :func:`app.providers.youtube.quota.pacific_day`)."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=PACIFIC)
    return now


def _empty_buckets() -> dict[str, dict[str, int]]:
    return {
        wire: {"used": 0, "limit": int(BUCKET_LIMITS[bucket])}
        for bucket, wire in _BUCKET_WIRE_NAMES.items()
    }


def _base_payload(now: datetime) -> dict[str, Any]:
    """The time scaffolding of the payload: Pacific date + reset math."""
    day = pacific_day(now)
    reset_dt = next_reset(now)
    seconds = int((reset_dt - now).total_seconds())
    if seconds < 0:  # clock skew guard; reset is by construction future
        seconds = 0
    resets_at = (
        reset_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    return {
        "buckets": _empty_buckets(),
        "date": day.isoformat(),
        "pacific_date": day.isoformat(),
        "resets_at": resets_at,
        "seconds_until_reset": seconds,
    }


def _pacific_day_window_ms(now: datetime) -> tuple[int, int]:
    """``[start_ms, end_ms)`` epoch-ms window of the current Pacific day."""
    day = pacific_day(now)
    start = datetime.combine(day, time.min, tzinfo=PACIFIC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=PACIFIC)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def quota_snapshot_from_conn(
    conn: sqlite3.Connection, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Aggregate ``api_usage`` for the current Pacific day.

    ``conn`` is caller-owned (never closed here).  Missing table/columns
    degrade to honest zeros, matching the other usage routers.
    """
    now = _normalize_now(now)
    payload = _base_payload(now)
    try:
        if not _table_exists(conn, "api_usage"):
            return payload
        cols = _columns(conn, "api_usage")
        if not {"provider", "operation", "created_at"} <= cols:
            return payload

        start_ms, end_ms = _pacific_day_window_ms(now)
        units_expr = "units" if "units" in cols else "NULL"
        rows = conn.execute(
            f"SELECT operation, {units_expr} AS units FROM api_usage "
            "WHERE LOWER(provider) = 'youtube' "
            "AND created_at >= ? AND created_at < ?",
            (start_ms, end_ms),
        ).fetchall()

        totals: dict[str, int] = {
            BUCKET_UPLOAD: 0,
            BUCKET_SEARCH: 0,
            BUCKET_SHARED: 0,
        }
        for row in rows:
            operation = row[0] if not isinstance(row, sqlite3.Row) else row["operation"]
            stored = row[1] if not isinstance(row, sqlite3.Row) else row["units"]
            bucket, fallback_cost = classify_method(str(operation or ""))
            if stored is None:
                units = fallback_cost
            else:
                try:
                    units = int(stored)
                except (TypeError, ValueError):
                    units = fallback_cost
            totals[bucket] = totals.get(bucket, 0) + units

        for bucket, wire in _BUCKET_WIRE_NAMES.items():
            payload["buckets"][wire]["used"] = int(totals.get(bucket, 0))
        return payload
    except Exception:
        # A broken ledger read must not fabricate usage: zeros are the
        # only honest fallback (same degradation as routers/usage.py).
        return _base_payload(now)


def get_quota_snapshot(now: Optional[datetime] = None) -> dict[str, Any]:
    """Quota snapshot from the app's configured SQLite database.

    Opens a short connection via ``app.db.get_connection()`` (looked up
    dynamically at call time, like the other routers) and closes it.
    Any failure degrades to honest zeros with correct reset math.
    """
    now = _normalize_now(now)
    conn = None
    try:
        from app import db as _db

        conn = _db.get_connection()
    except Exception:
        return _base_payload(now)
    try:
        return quota_snapshot_from_conn(conn, now=now)
    finally:
        try:
            conn.close()
        except Exception:
            pass
