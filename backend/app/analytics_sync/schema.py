"""Analytics sync bookkeeping — runtime DDL + lazy schema creation.

Canonical-shape decision (R5-ANALYTICS-001, settled): the STORAGE
team's shape is the single canonical shape for the analytics data
tables — ``app.analytics.schema`` (migration ``0007_analytics``) owns
``analytics_snapshots`` and ``channel_metric_snapshots``, and the
sync writes through :class:`app.analytics.store.AnalyticsStore`
exclusively. This module therefore defines NO parallel version of
those tables (an earlier sync-side definition with
``snapshot_at``/``snapshot_day``/``subscriber_count`` columns raced
the storage DDL and broke its index creation — removed). The store
ensures the canonical tables itself on every call, so this module
only owns the sync bookkeeping:

``analytics_sync_runs``
    One row per sync attempt (manual or scheduled), whatever the
    outcome: ``status`` is ``running`` / ``success`` / ``partial`` /
    ``not_connected`` / ``quota_exhausted`` / ``failed`` / ``skipped``;
    ``reason`` carries the machine-readable failure reason and
    ``detail_json`` the counters + per-call errors. This is the
    durable "every failure is recorded" record — a sync is never a
    silent no-op.

``analytics_sync_state``
    One row per channel (PRIMARY KEY ``channel_id``): the freshness
    surface the read-API team consumes — ``last_synced_at`` (last
    *successful* sync; NULL while a channel has never synced),
    ``last_attempt_at``, ``last_status``, ``last_reason``,
    ``last_run_id`` and the counters of the last run.

Created lazily with ``IF NOT EXISTS`` (the pattern used by
:mod:`app.style_lab.schema` and :mod:`app.learning.schema`), so the
sync works on databases that have not run migrations. Timestamps are
INTEGER Unix epoch milliseconds and ids are uuid4 hex, matching the
section-15 global conventions.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ANALYTICS_SYNC_RUNS_TABLE",
    "ANALYTICS_SYNC_STATE_TABLE",
    "ANALYTICS_SYNC_RUNS_DDL",
    "ANALYTICS_SYNC_STATE_DDL",
    "ANALYTICS_SYNC_DDL",
    "INDEX_STATEMENTS",
    "RUN_STATUSES",
    "ensure_analytics_sync_schema",
]

ANALYTICS_SYNC_RUNS_TABLE = "analytics_sync_runs"
ANALYTICS_SYNC_STATE_TABLE = "analytics_sync_state"

#: analytics_sync_runs.status / analytics_sync_state.last_status values.
RUN_STATUSES = (
    "running",
    "success",
    "partial",
    "not_connected",
    "quota_exhausted",
    "failed",
    "skipped",
)

ANALYTICS_SYNC_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS analytics_sync_runs (
    id                        TEXT PRIMARY KEY,
    channel_id                TEXT NOT NULL
                              REFERENCES channels (id) ON DELETE RESTRICT,
    trigger                   TEXT NOT NULL DEFAULT 'scheduled'
                              CHECK (trigger IN ('manual', 'scheduled')),
    status                    TEXT NOT NULL DEFAULT 'running'
                              CHECK (status IN ('running', 'success',
                                     'partial', 'not_connected',
                                     'quota_exhausted', 'failed',
                                     'skipped')),
    reason                    TEXT NULL,
    videos_seen               INTEGER NOT NULL DEFAULT 0,
    snapshots_written         INTEGER NOT NULL DEFAULT 0,
    channel_snapshots_written INTEGER NOT NULL DEFAULT 0,
    retention_written         INTEGER NOT NULL DEFAULT 0,
    quota_units_charged       INTEGER NOT NULL DEFAULT 0,
    detail_json               TEXT NULL,
    started_at                INTEGER NOT NULL,
    finished_at               INTEGER NULL,
    created_at                INTEGER NOT NULL
)
"""

ANALYTICS_SYNC_STATE_DDL = """
CREATE TABLE IF NOT EXISTS analytics_sync_state (
    channel_id                TEXT PRIMARY KEY
                              REFERENCES channels (id) ON DELETE RESTRICT,
    last_synced_at            INTEGER NULL,
    last_attempt_at           INTEGER NULL,
    last_status               TEXT NULL,
    last_reason               TEXT NULL,
    last_run_id               TEXT NULL,
    last_snapshots_written    INTEGER NOT NULL DEFAULT 0,
    updated_at                INTEGER NOT NULL
)
"""

ANALYTICS_SYNC_DDL = (
    ANALYTICS_SYNC_RUNS_DDL,
    ANALYTICS_SYNC_STATE_DDL,
)

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_analytics_sync_runs_channel "
    "ON analytics_sync_runs (channel_id, started_at DESC)",
)


def _commit_quietly(conn: Any) -> None:
    try:
        conn.commit()
    except Exception:
        pass


def ensure_analytics_sync_schema(conn: Any) -> None:
    """Create the sync bookkeeping tables on ``conn`` if absent, and
    ensure the canonical analytics tables BY DELEGATION.

    Idempotent and safe to call on every service entry point. The
    analytics data tables are NOT defined here — they belong to
    ``app.analytics.schema`` — but the sync ensures them (via the
    storage layer's own ``ensure_analytics_schema``, the same call
    ``AnalyticsStore`` makes) so that after any sync attempt —
    including a ``not_connected`` / quota-blocked one that writes
    nothing — the canonical tables exist (empty) for raw-SQL readers
    instead of raising "no such table".

    The delegation is STRICT (R5-002 closure): a failure inside
    ``ensure_analytics_schema`` propagates to the caller instead of
    being swallowed. The pre-closure version caught and discarded
    every exception, which is precisely how the competing-schema
    defect stayed silent — the canonical tables could be missing or
    mis-shaped while the sync reported success and per-video reads
    answered ``no_data``. Fail loud here; the sync service records
    the failure on its run row like any other sync failure.
    """
    for statement in ANALYTICS_SYNC_DDL:
        conn.execute(statement)
    for statement in INDEX_STATEMENTS:
        conn.execute(statement)
    from app.analytics.schema import ensure_analytics_schema

    ensure_analytics_schema(conn)
    _commit_quietly(conn)
