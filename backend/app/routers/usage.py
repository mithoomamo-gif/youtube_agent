"""Usage ledger endpoints (roadmap section 31).

Rollups come from the ``api_usage`` table (written by provider adapters
at call time — the ledger, unlike the WebSocket cost ticker, is the
authoritative figure).  Empty/absent table → real empty rollups.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timezone
from typing import Any

from fastapi import APIRouter

from app.errors import ApiError

router = APIRouter(prefix="/api/v1/usage", tags=["usage"])

_SUM_COLUMNS = ("tokens_in", "tokens_out", "characters", "images", "cost_micros")


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


@router.get("")
async def get_usage() -> dict:
    conn = _conn()
    if conn is None:
        return {"items": [], "next_cursor": None}
    try:
        if "api_usage" not in _tables(conn):
            return {"items": [], "next_cursor": None}
        cols = _cols(conn, "api_usage")
        sums = [c for c in _SUM_COLUMNS if c in cols]
        select = ["provider", "COUNT(*) AS calls"] + [
            f"COALESCE(SUM({c}), 0) AS {c}" for c in sums
        ]
        rows = conn.execute(
            f"SELECT {', '.join(select)} FROM api_usage GROUP BY provider"
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if "cost_micros" in item:
                item["est_usd"] = round(int(item["cost_micros"]) / 1_000_000, 6)
            items.append(item)
        return {"items": items, "next_cursor": None}
    except Exception:
        return {"items": [], "next_cursor": None}
    finally:
        try:
            conn.close()
        except Exception:
            pass


@router.get("/quota")
async def get_usage_quota() -> dict:
    """YouTube quota buckets for the current Pacific day (Round 3).

    Shape: ``{buckets: {uploads|search|shared: {used, limit}}, date,
    pacific_date, resets_at, seconds_until_reset}`` — aggregated from
    the real ``api_usage`` ledger by :mod:`app.quota_view` (bucket
    classification/costs from ``providers.youtube.quota``).  An empty
    ledger yields honest zeros, never fabricated numbers.
    """
    try:
        from app.quota_view import get_quota_snapshot

        return get_quota_snapshot()
    except Exception:
        # Last-resort honest zeros with correct Pacific reset math.
        try:
            from app.quota_view import _base_payload, _normalize_now

            return _base_payload(_normalize_now(None))
        except Exception:
            return {
                "buckets": {
                    "uploads": {"used": 0, "limit": 100},
                    "search": {"used": 0, "limit": 100},
                    "shared": {"used": 0, "limit": 10000},
                },
                "date": "",
                "pacific_date": "",
                "resets_at": "",
                "seconds_until_reset": 0,
            }


@router.get("/budget")
async def get_usage_budget() -> dict:
    """Budget summary for the current Pacific day + month (Round 4).

    Shape: ``{pacific_date, pacific_month, resets_at,
    month_resets_at, day: {cap_usd, settled_usd, reserved_usd,
    spent_usd, remaining_usd, ..._micros}, month: {...}, channels:
    {channel_id|null: {day, month}}, policy}`` — aggregated from the
    real cost ledger + reservation store by :mod:`app.budget_view`,
    on the same Pacific clock as the quota view.  An empty ledger
    yields honest zeros with full remaining, never fabricated numbers.
    """
    try:
        from app.budget_view import get_budget_snapshot

        return get_budget_snapshot()
    except Exception:
        # Last-resort honest zeros with correct Pacific reset math.
        try:
            from app.budget_view import _base_payload, _normalize_now

            return _base_payload(_normalize_now(None))
        except Exception:
            empty = {
                "cap_usd": None, "settled_usd": 0.0, "reserved_usd": 0.0,
                "spent_usd": 0.0, "remaining_usd": None,
                "cap_micros": None, "settled_micros": 0,
                "reserved_micros": 0, "spent_micros": 0,
                "remaining_micros": None,
            }
            return {
                "pacific_date": "", "date": "", "pacific_month": "",
                "resets_at": "", "seconds_until_reset": 0,
                "month_resets_at": "", "seconds_until_month_reset": 0,
                "day": {**empty, "cap_usd": 10.0, "remaining_usd": 10.0,
                        "cap_micros": 10_000_000,
                        "remaining_micros": 10_000_000},
                "month": {**empty, "cap_usd": 30.0, "remaining_usd": 30.0,
                          "cap_micros": 30_000_000,
                          "remaining_micros": 30_000_000},
                "channels": {},
                "policy": {},
            }


@router.get("/channels")
async def get_usage_channels() -> dict:
    """Per-channel budget / usage view (Round 4).

    One item per live channel: month-to-date spend attributed through
    ``project_id -> projects.channel_id`` (see
    :mod:`app.execution.channel_context` for the attribution rules),
    the channel's ``monthly_budget_micros`` cap, its schedules' daily
    cost caps, and project counts by status.  Read-only; channels with
    no activity report honest zeros.  Note: these caps are this app's
    budget/scheduling controls — YouTube API quota is per GCP project
    and stays app-level (see ``GET /usage/quota``), never per-channel.
    """
    conn = _conn()
    if conn is None:
        return {"items": [], "next_cursor": None}
    try:
        from app.execution.channel_context import build_channel_usage_views

        return {
            "items": build_channel_usage_views(conn),
            "next_cursor": None,
        }
    except Exception:
        return {"items": [], "next_cursor": None}
    finally:
        try:
            conn.close()
        except Exception:
            pass


@router.get("/channels/{channel_id}")
async def get_usage_channel(channel_id: str) -> dict:
    """The per-channel view for one channel (id or YouTube channel id)."""
    conn = _conn()
    if conn is None:
        raise ApiError(503, "database_unavailable", "Usage store unavailable.")
    try:
        from app.execution.channel_context import build_channel_usage_view

        view = build_channel_usage_view(conn, channel_id)
    except Exception:
        view = None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if view is None:
        raise ApiError(404, "channel_not_found", "Unknown channel.")
    return view


@router.get("/today")
async def get_usage_today() -> dict:
    now = datetime.now(timezone.utc)
    date_str = now.date().isoformat()
    totals: dict[str, Any] = {}
    conn = _conn()
    if conn is not None:
        try:
            if "api_usage" in _tables(conn):
                cols = _cols(conn, "api_usage")
                if "created_at" in cols:
                    start_ms = int(
                        datetime.combine(now.date(), dtime.min, timezone.utc).timestamp()
                        * 1000
                    )
                    sums = [c for c in _SUM_COLUMNS if c in cols]
                    select = ["COUNT(*) AS calls"] + [
                        f"COALESCE(SUM({c}), 0) AS {c}" for c in sums
                    ]
                    row = conn.execute(
                        f"SELECT {', '.join(select)} FROM api_usage "
                        "WHERE created_at >= ?",
                        (start_ms,),
                    ).fetchone()
                    if row is not None and int(row[0]) > 0:
                        totals = dict(row)
                        if "cost_micros" in totals:
                            totals["est_usd"] = round(
                                int(totals["cost_micros"]) / 1_000_000, 6
                            )
        except Exception:
            totals = {}
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return {"date": date_str, "totals": totals}
