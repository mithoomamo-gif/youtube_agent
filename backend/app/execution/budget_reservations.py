"""Persistent Budget Guard reservations (Round 4, full enforcement).

This module is the *store* half of the Round 4 Budget Guard: durable
per-project reservations against the global daily / monthly caps in
:mod:`app.providers.budget.policy`.  The *gate* half (parking a project
at Gate A when a cap would be breached) lives in
:mod:`app.execution.budget_gate`, which calls into this store at DAG
start.  Stage handlers settle against the store as real costs are
recorded; ``pipeline.cancel`` releases the unsettled remainder.

Accounting model (the shared Round 4 contract)
----------------------------------------------
Money is integer **micros** everywhere (USD x 1,000,000), per the
project convention in :mod:`app.providers.budget.money`.  API dicts
additionally carry ``*_usd`` float mirrors for display / contract
readers.

One row per reservation::

    budget_reservations(
        id, project_id, channel_id NULL,
        reserved_micros, settled_micros,
        status IN ('active', 'settled', 'released'),
        pacific_day 'YYYY-MM-DD', pacific_month 'YYYY-MM',
        created_at, updated_at, settled_at NULL, released_at NULL,
        release_reason NULL)

* ``reserved_micros`` — the DAG estimate reserved at start (the
  *incremental* future cost: full estimate minus actuals already
  accrued on the project, so past spend is never reserved twice).
* ``settled_micros`` — actual costs recorded against the reservation
  as stages complete.  May exceed ``reserved_micros`` (a truthful
  overrun); the excess still counts as spend.
* ``remaining_micros`` — derived, never stored:
  ``max(0, reserved - settled)`` while ``status='active'``, else 0.
* ``committed_micros`` — derived: ``settled_micros + remaining_micros``.
  This is what a reservation contributes to a bucket.  While active it
  equals ``max(reserved, settled)`` (settling shrinks the *remaining*
  slice, never double-counts); once settled/released only the actual
  settled amount remains committed, so the unspent remainder is freed.

Buckets use **America/Los_Angeles** boundaries, exactly like
:mod:`app.quota_view` / :mod:`app.providers.youtube.quota`: a
reservation is bucketed by the Pacific day / month in which it was
*created* (stored on the row as ``pacific_day`` / ``pacific_month``),
so a settle that lands after Pacific midnight does not move spend
between days.

Per-channel attribution: every reservation carries the project's
``channel_id`` (nullable).  ``NULL`` is the unattributed bucket;
:func:`spend_by_channel` returns a mapping keyed by channel id with a
``None`` key for unattributed spend, and the values sum *exactly* to
the global bucket total.

Clock seam (for tests)
----------------------
All "now" handling goes through the module-level function
:func:`now_pacific` — **tests monkeypatch
``app.execution.budget_reservations.now_pacific``** to control the
clock.  Every public function also accepts an explicit
``now: datetime | None`` override which, when given, wins over the
provider.  :mod:`app.execution.budget_gate` obtains its clock from
this same provider, so patching it steers the gate too.

Persistence: the table is created by Alembic migration ``0003`` and,
following the ``gates_bridge`` pattern, also lazily by
:func:`ensure_reservations_table` on every store entry point, so the
store works on databases that have not run migrations (tests build
their schema from ``app.schema_ddl`` via ``app.db.init_db``).
"""
from __future__ import annotations

import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Optional

from app.providers.budget.money import micros_to_usd
from app.providers.youtube.quota import PACIFIC, pacific_day

__all__ = [
    "RESERVATIONS_TABLE",
    "RESERVATIONS_DDL",
    "STATUS_ACTIVE",
    "STATUS_SETTLED",
    "STATUS_RELEASED",
    "ALL_CHANNELS",
    "now_pacific",
    "ensure_reservations_table",
    "reserve",
    "ensure_reservation",
    "get_reservation",
    "get_active_reservation",
    "list_reservations",
    "settle_reservation",
    "settle_for_project",
    "release_reservation",
    "release_for_project",
    "committed_micros",
    "remaining_micros",
    "bucket_spend_micros",
    "daily_spend_micros",
    "monthly_spend_micros",
    "spend_by_channel",
    "bucket_totals",
    "pacific_month_of",
    "pacific_day_window_ms",
    "pacific_month_window_ms",
]

RESERVATIONS_TABLE = "budget_reservations"

STATUS_ACTIVE = "active"
STATUS_SETTLED = "settled"
STATUS_RELEASED = "released"

#: Sentinel for "no channel filter" (global total across all channels,
#: including the unattributed NULL bucket).  Distinct from ``None``,
#: which as a ``channel_id`` argument means the NULL bucket only.
ALL_CHANNELS: Any = object()

RESERVATIONS_DDL = """
CREATE TABLE IF NOT EXISTS budget_reservations (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    channel_id      TEXT NULL,
    reserved_micros INTEGER NOT NULL DEFAULT 0,
    settled_micros  INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'settled', 'released')),
    pacific_day     TEXT NOT NULL,
    pacific_month   TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    settled_at      INTEGER NULL,
    released_at     INTEGER NULL,
    release_reason  TEXT NULL
)
"""

_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_budget_res_project "
    "ON budget_reservations (project_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_budget_res_day "
    "ON budget_reservations (pacific_day, status)",
    "CREATE INDEX IF NOT EXISTS idx_budget_res_month "
    "ON budget_reservations (pacific_month, status)",
    "CREATE INDEX IF NOT EXISTS idx_budget_res_channel_day "
    "ON budget_reservations (channel_id, pacific_day)",
)


# ---------------------------------------------------------------------------
# Clock seam
# ---------------------------------------------------------------------------


def now_pacific() -> datetime:
    """Current instant (aware, America/Los_Angeles).

    THE clock seam: tests monkeypatch this symbol
    (``app.execution.budget_reservations.now_pacific``) to steer every
    bucket boundary in this module and in ``budget_gate``.
    """
    return datetime.now(PACIFIC)


def _resolve_now(now: Optional[datetime]) -> datetime:
    if now is None:
        return now_pacific()
    if now.tzinfo is None:
        # Naive input is Pacific wall-time, mirroring quota_view.
        return now.replace(tzinfo=PACIFIC)
    return now.astimezone(PACIFIC)


def _now_ms(now: Optional[datetime] = None) -> int:
    if now is not None:
        return int(_resolve_now(now).timestamp() * 1000)
    return int(time.time() * 1000)


def pacific_month_of(moment: datetime) -> str:
    """``'YYYY-MM'`` of ``moment`` in America/Los_Angeles."""
    local = _resolve_now(moment)
    return f"{local.year:04d}-{local.month:02d}"


def pacific_day_window_ms(moment: datetime) -> tuple[int, int]:
    """``[start_ms, end_ms)`` epoch-ms window of ``moment``'s Pacific day."""
    from datetime import time as _time

    day = pacific_day(_resolve_now(moment))
    start = datetime.combine(day, _time.min, tzinfo=PACIFIC)
    end = datetime.combine(day + timedelta(days=1), _time.min, tzinfo=PACIFIC)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def pacific_month_window_ms(moment: datetime) -> tuple[int, int]:
    """``[start_ms, end_ms)`` epoch-ms window of ``moment``'s Pacific month."""
    from datetime import time as _time

    local = _resolve_now(moment)
    first = date(local.year, local.month, 1)
    if local.month == 12:
        nxt = date(local.year + 1, 1, 1)
    else:
        nxt = date(local.year, local.month + 1, 1)
    start = datetime.combine(first, _time.min, tzinfo=PACIFIC)
    end = datetime.combine(nxt, _time.min, tzinfo=PACIFIC)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Connection handling (same convention as gates_bridge / budget policy)
# ---------------------------------------------------------------------------


def _open_conn(conn_factory: Any):
    """Return ``(conn, should_close)`` for a factory or bare connection."""
    if conn_factory is None:
        from app import db as _db

        return _db.get_connection(), True
    if callable(conn_factory):
        try:
            conn = conn_factory()
        except TypeError:
            conn = conn_factory(None)  # type: ignore[call-arg]
        try:
            probe = conn_factory()
        except Exception:
            return conn, True
        if probe is conn:
            return conn, False
        try:
            probe.close()
        except Exception:
            pass
        return conn, True
    if hasattr(conn_factory, "execute"):
        return conn_factory, False
    raise TypeError("conn_factory must be callable or a DB connection")


def _commit_quietly(conn: Any) -> None:
    try:
        conn.commit()
    except Exception:
        pass


def _close_quietly(conn: Any, should_close: bool) -> None:
    if should_close:
        try:
            conn.close()
        except Exception:
            pass


def ensure_reservations_table(conn: Any) -> None:
    """Create the table + indexes on ``conn`` if they do not exist."""
    conn.execute(RESERVATIONS_DDL)
    for stmt in _INDEX_STATEMENTS:
        conn.execute(stmt)
    _commit_quietly(conn)


def _table_exists(conn: Any) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (RESERVATIONS_TABLE,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Row shaping / derived amounts
# ---------------------------------------------------------------------------


def remaining_micros(row: Any) -> int:
    """Unsettled remainder still held by a reservation row/dict."""
    if isinstance(row, dict):
        status, reserved, settled = row.get("status"), row.get("reserved_micros"), row.get("settled_micros")
    else:
        status, reserved, settled = row["status"], row["reserved_micros"], row["settled_micros"]
    if status != STATUS_ACTIVE:
        return 0
    return max(0, int(reserved or 0) - int(settled or 0))


def committed_micros(row: Any) -> int:
    """What a reservation contributes to its bucket (settled + remaining)."""
    settled = int(row["settled_micros"] or 0) if not isinstance(row, dict) else int(row.get("settled_micros") or 0)
    return settled + remaining_micros(row)


def _usd(micros: int) -> float:
    return float(micros_to_usd(int(micros)))


def _row_to_dict(row: Any) -> Optional[dict]:
    if row is None:
        return None
    if isinstance(row, dict):
        data = dict(row)
    else:
        try:
            data = {k: row[k] for k in row.keys()}
        except Exception:
            try:
                data = dict(row)
            except Exception:
                return None
    reserved = int(data.get("reserved_micros") or 0)
    settled = int(data.get("settled_micros") or 0)
    remaining = remaining_micros(data)
    committed = settled + remaining
    data["reserved_micros"] = reserved
    data["settled_micros"] = settled
    data["remaining_micros"] = remaining
    data["committed_micros"] = committed
    # Contract-name mirrors (USD floats) alongside the micros truth.
    data["reserved_usd"] = _usd(reserved)
    data["settled_usd"] = _usd(settled)
    data["remaining_usd"] = _usd(remaining)
    data["committed_usd"] = _usd(committed)
    return data


def _fetch_one(conn: Any, where: str, params: tuple) -> Optional[dict]:
    row = conn.execute(
        f"SELECT * FROM {RESERVATIONS_TABLE} WHERE {where}", params
    ).fetchone()
    return _row_to_dict(row)


def _fetch_all(conn: Any, where: str = "1 = 1", params: tuple = ()) -> list[dict]:
    rows = conn.execute(
        f"SELECT * FROM {RESERVATIONS_TABLE} WHERE {where} ORDER BY created_at ASC",
        params,
    ).fetchall()
    out = []
    for row in rows:
        shaped = _row_to_dict(row)
        if shaped is not None:
            out.append(shaped)
    return out


# ---------------------------------------------------------------------------
# Reserve
# ---------------------------------------------------------------------------


def reserve(
    conn_factory: Any,
    project_id: Any,
    channel_id: Optional[str] = None,
    reserved_micros: int = 0,
    *,
    now: Optional[datetime] = None,
) -> dict:
    """Create a new ``active`` reservation row; return it as a dict.

    Always creates a fresh row — callers that must not double-reserve
    a project use :func:`ensure_reservation` (the gate does).
    ``reserved_micros`` is the incremental DAG estimate in micros.
    """
    moment = _resolve_now(now)
    ts = _now_ms(moment)
    row_id = uuid.uuid4().hex
    conn, should_close = _open_conn(conn_factory)
    try:
        ensure_reservations_table(conn)
        conn.execute(
            f"INSERT INTO {RESERVATIONS_TABLE} (id, project_id, channel_id,"
            " reserved_micros, settled_micros, status, pacific_day,"
            " pacific_month, created_at, updated_at, settled_at,"
            " released_at, release_reason)"
            " VALUES (?, ?, ?, ?, 0, 'active', ?, ?, ?, ?, NULL, NULL, NULL)",
            (
                row_id,
                str(project_id),
                str(channel_id) if channel_id is not None else None,
                int(reserved_micros),
                pacific_day(moment).isoformat(),
                pacific_month_of(moment),
                ts,
                ts,
            ),
        )
        _commit_quietly(conn)
        result = _fetch_one(conn, "id = ?", (row_id,))
        assert result is not None
        return result
    finally:
        _close_quietly(conn, should_close)


def get_active_reservation(conn_factory: Any, project_id: Any) -> Optional[dict]:
    """The project's live (``active``) reservation, or ``None``."""
    conn, should_close = _open_conn(conn_factory)
    try:
        if not _table_exists(conn):
            return None
        return _fetch_active(conn, project_id)
    finally:
        _close_quietly(conn, should_close)


def _fetch_active(conn: Any, project_id: Any) -> Optional[dict]:
    row = conn.execute(
        f"SELECT * FROM {RESERVATIONS_TABLE} WHERE project_id = ?"
        " AND status = 'active' ORDER BY created_at DESC LIMIT 1",
        (str(project_id),),
    ).fetchone()
    return _row_to_dict(row)


def ensure_reservation(
    conn_factory: Any,
    project_id: Any,
    channel_id: Optional[str] = None,
    reserved_micros: int = 0,
    *,
    now: Optional[datetime] = None,
) -> dict:
    """Return the project's active reservation, creating it if absent.

    Idempotent per project while a reservation is active — this is
    what makes the gate's double seam (``pipeline.start`` +
    ``stage.research``) reserve exactly once.
    """
    existing = get_active_reservation(conn_factory, project_id)
    if existing is not None:
        return existing
    return reserve(
        conn_factory, project_id, channel_id, reserved_micros, now=now
    )


def get_reservation(conn_factory: Any, reservation_id: Any) -> Optional[dict]:
    conn, should_close = _open_conn(conn_factory)
    try:
        if not _table_exists(conn):
            return None
        return _fetch_one(conn, "id = ?", (str(reservation_id),))
    finally:
        _close_quietly(conn, should_close)


def list_reservations(
    conn_factory: Any,
    *,
    project_id: Any = None,
    channel_id: Any = ALL_CHANNELS,
    status: Optional[str] = None,
) -> list[dict]:
    """Reservations filtered by project / channel / status.

    ``channel_id=None`` selects only the unattributed (NULL) bucket;
    the default :data:`ALL_CHANNELS` applies no channel filter.
    """
    conn, should_close = _open_conn(conn_factory)
    try:
        if not _table_exists(conn):
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(str(project_id))
        if channel_id is not ALL_CHANNELS:
            if channel_id is None:
                clauses.append("channel_id IS NULL")
            else:
                clauses.append("channel_id = ?")
                params.append(str(channel_id))
        if status is not None:
            clauses.append("status = ?")
            params.append(str(status))
        where = " AND ".join(clauses) if clauses else "1 = 1"
        return _fetch_all(conn, where, tuple(params))
    finally:
        _close_quietly(conn, should_close)


# ---------------------------------------------------------------------------
# Settle / release
# ---------------------------------------------------------------------------


def _apply_settle(
    conn: Any,
    row: dict,
    actual_micros: int,
    *,
    final: bool,
    moment: datetime,
) -> dict:
    settled = int(row["settled_micros"] or 0) + int(actual_micros)
    reserved = int(row["reserved_micros"] or 0)
    ts = _now_ms(moment)
    if final or settled >= reserved:
        status = STATUS_SETTLED
        settled_at: Optional[int] = ts
    else:
        status = STATUS_ACTIVE
        settled_at = row.get("settled_at")
    conn.execute(
        f"UPDATE {RESERVATIONS_TABLE} SET settled_micros = ?, status = ?,"
        " settled_at = ?, updated_at = ? WHERE id = ?",
        (settled, status, settled_at, ts, row["id"]),
    )
    _commit_quietly(conn)
    result = _fetch_one(conn, "id = ?", (row["id"],))
    assert result is not None
    return result


def settle_reservation(
    conn_factory: Any,
    reservation_id: Any,
    actual_micros: int,
    *,
    final: bool = False,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """Add ``actual_micros`` of real spend to a reservation.

    The reservation stays ``active`` (with a shrunken remainder) until
    the settled total reaches the reserved estimate or ``final=True``,
    when it becomes ``settled`` and any unspent remainder is freed.
    Settling a ``settled``/``released`` reservation is a no-op that
    returns the row unchanged (stage-cost hooks are best-effort and
    may fire after a release).  Returns ``None`` for an unknown id.
    """
    moment = _resolve_now(now)
    conn, should_close = _open_conn(conn_factory)
    try:
        if not _table_exists(conn):
            return None
        row = _fetch_one(conn, "id = ?", (str(reservation_id),))
        if row is None or row["status"] != STATUS_ACTIVE:
            return row
        return _apply_settle(conn, row, int(actual_micros), final=final, moment=moment)
    finally:
        _close_quietly(conn, should_close)


def settle_for_project(
    conn_factory: Any,
    project_id: Any,
    actual_micros: int,
    *,
    final: bool = False,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """Settle ``actual_micros`` against the project's active reservation.

    This is the hook stage handlers call when a stage cost is recorded
    (``projects.total_cost_micros`` grows by the same amount in the
    handler, so bucket accounting never double-counts: the settled
    slice replaces an equal slice of the reservation's remainder).
    No active reservation -> returns ``None`` (a pre-Round-4 project).
    """
    moment = _resolve_now(now)
    conn, should_close = _open_conn(conn_factory)
    try:
        if not _table_exists(conn):
            return None
        row = _fetch_active(conn, project_id)
        if row is None:
            return None
        return _apply_settle(conn, row, int(actual_micros), final=final, moment=moment)
    finally:
        _close_quietly(conn, should_close)


def _apply_release(conn: Any, row: dict, reason: str, moment: datetime) -> dict:
    ts = _now_ms(moment)
    conn.execute(
        f"UPDATE {RESERVATIONS_TABLE} SET status = 'released',"
        " released_at = ?, release_reason = ?, updated_at = ? WHERE id = ?",
        (ts, reason, ts, row["id"]),
    )
    _commit_quietly(conn)
    result = _fetch_one(conn, "id = ?", (row["id"],))
    assert result is not None
    return result


def release_reservation(
    conn_factory: Any,
    reservation_id: Any,
    *,
    reason: str = "run_ended",
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """Release a reservation: the unsettled remainder is freed.

    Any already-settled amount stays committed (it is real spend and
    keeps counting in the bucket via ``settled_micros``).  Releasing a
    non-active reservation is a no-op returning the row; an unknown id
    returns ``None``.
    """
    moment = _resolve_now(now)
    conn, should_close = _open_conn(conn_factory)
    try:
        if not _table_exists(conn):
            return None
        row = _fetch_one(conn, "id = ?", (str(reservation_id),))
        if row is None or row["status"] != STATUS_ACTIVE:
            return row
        return _apply_release(conn, row, reason, moment)
    finally:
        _close_quietly(conn, should_close)


def release_for_project(
    conn_factory: Any,
    project_id: Any,
    *,
    reason: str = "run_ended",
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """Release the project's active reservation (failure/cancel/stop)."""
    moment = _resolve_now(now)
    conn, should_close = _open_conn(conn_factory)
    try:
        if not _table_exists(conn):
            return None
        row = _fetch_active(conn, project_id)
        if row is None:
            return None
        return _apply_release(conn, row, reason, moment)
    finally:
        _close_quietly(conn, should_close)


# ---------------------------------------------------------------------------
# Bucket accounting
# ---------------------------------------------------------------------------


def _bucket_rows(
    conn: Any, period: str, moment: datetime, channel_id: Any
) -> list[dict]:
    if period == "daily":
        column, key = "pacific_day", pacific_day(moment).isoformat()
    elif period == "monthly":
        column, key = "pacific_month", pacific_month_of(moment)
    else:
        raise ValueError(f"unknown bucket period {period!r} (daily|monthly)")
    clauses = [f"{column} = ?"]
    params: list[Any] = [key]
    if channel_id is not ALL_CHANNELS:
        if channel_id is None:
            clauses.append("channel_id IS NULL")
        else:
            clauses.append("channel_id = ?")
            params.append(str(channel_id))
    return _fetch_all(conn, " AND ".join(clauses), tuple(params))


def bucket_spend_micros(
    conn_factory: Any,
    period: str = "daily",
    *,
    now: Optional[datetime] = None,
    channel_id: Any = ALL_CHANNELS,
) -> int:
    """Committed spend (settled + active remainders) in a bucket.

    ``period`` is ``'daily'`` or ``'monthly'`` (Pacific boundaries).
    ``channel_id`` defaults to :data:`ALL_CHANNELS` (the global total);
    pass a channel id for that channel's bucket, or ``None`` for the
    unattributed bucket only.
    """
    moment = _resolve_now(now)
    conn, should_close = _open_conn(conn_factory)
    try:
        if not _table_exists(conn):
            return 0
        rows = _bucket_rows(conn, period, moment, channel_id)
        return sum(int(r["committed_micros"]) for r in rows)
    finally:
        _close_quietly(conn, should_close)


def daily_spend_micros(
    conn_factory: Any, *, now: Optional[datetime] = None, channel_id: Any = ALL_CHANNELS
) -> int:
    return bucket_spend_micros(conn_factory, "daily", now=now, channel_id=channel_id)


def monthly_spend_micros(
    conn_factory: Any, *, now: Optional[datetime] = None, channel_id: Any = ALL_CHANNELS
) -> int:
    return bucket_spend_micros(conn_factory, "monthly", now=now, channel_id=channel_id)


def spend_by_channel(
    conn_factory: Any,
    period: str = "daily",
    *,
    now: Optional[datetime] = None,
) -> dict[Optional[str], int]:
    """Committed spend per channel for a bucket.

    Keys are channel ids; unattributed spend sits under the ``None``
    key (present only when such spend exists).  ``sum(values())``
    equals :func:`bucket_spend_micros` for the same period — exactly,
    in integer micros.
    """
    moment = _resolve_now(now)
    conn, should_close = _open_conn(conn_factory)
    try:
        if not _table_exists(conn):
            return {}
        rows = _bucket_rows(conn, period, moment, ALL_CHANNELS)
        totals: dict[Optional[str], int] = {}
        for row in rows:
            key = row.get("channel_id")
            totals[key] = totals.get(key, 0) + int(row["committed_micros"])
        return totals
    finally:
        _close_quietly(conn, should_close)


def bucket_totals(conn_factory: Any, *, now: Optional[datetime] = None) -> dict:
    """Both buckets' totals + per-channel split (the view worker's read).

    Shape::

        {"daily":   {"total_micros": int, "total_usd": float,
                     "by_channel": {channel_id|None: micros},
                     "pacific_day": "YYYY-MM-DD"},
         "monthly": {..., "pacific_month": "YYYY-MM"}}
    """
    moment = _resolve_now(now)
    out: dict[str, Any] = {}
    for period in ("daily", "monthly"):
        total = bucket_spend_micros(conn_factory, period, now=moment)
        entry: dict[str, Any] = {
            "total_micros": total,
            "total_usd": _usd(total),
            "by_channel": spend_by_channel(conn_factory, period, now=moment),
        }
        if period == "daily":
            entry["pacific_day"] = pacific_day(moment).isoformat()
        else:
            entry["pacific_month"] = pacific_month_of(moment)
        out[period] = entry
    return out
