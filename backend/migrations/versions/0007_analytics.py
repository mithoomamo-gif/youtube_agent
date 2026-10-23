"""Analytics ingestion snapshots (Round 5 — Growth + Learning).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-23

Revision-slot note: this migration was specced as "0005" in the
Round 5 storage task, but by the time it landed the parallel
Round 5 teams had already claimed 0005 (``0005_learning_rules``)
and 0006 (``0006_comments``) in this directory, so analytics takes
the next free slot, 0007, chained on 0006.  The head-revision test
expectations were bumped to '0007' accordingly.

Changes:
- ``analytics_snapshots`` EVOLVED in place (the table dates from
  the section-15 initial schema, migration 0001 — it is NOT
  recreated, and none of its legacy columns are dropped or
  renamed, because live readers such as the learning loop's
  ``SqliteAnalyticsSource`` select ``snapshot_at`` / ``views`` /
  ``watch_hours_x100`` / ``ctr_bps`` / ``avg_view_duration_sec``
  directly).  Added columns:
  ``snapshot_kind`` ('d3' / 'd7' / 'd28' / 'd90' / 'manual' /
  'scheduled', CHECK-enforced, legacy rows default 'scheduled'),
  ``capture_day`` (TEXT 'YYYY-MM-DD', the UTC day of the capture
  instant, derived at write time and stored — it is the day
  component of the idempotency uniqueness below),
  ``metrics_json`` (the canonical Round 5 metrics object; the
  store mirrors its well-known keys into the legacy discrete
  columns on every write), ``captured_at`` (the Round 5 name for
  the capture instant, written identical to the legacy
  ``snapshot_at``) and ``updated_at``.  Legacy rows are backfilled
  (``captured_at = snapshot_at``, ``updated_at = created_at``,
  ``capture_day`` = UTC date of ``snapshot_at``) and de-duplicated
  on the new key (newest row kept) before the unique index lands:
  ``uq_analytics_snapshots_kind_day`` UNIQUE on (channel_id,
  youtube_video_id, snapshot_kind, capture_day) — a re-sync of
  the same video / kind / UTC day updates in place, never
  duplicates.  NULL-video rows are exempt (SQLite unique indexes
  treat NULLs as distinct).
- New ``channel_metric_snapshots`` table: one row per (channel,
  kind, UTC day) of channel-level figures for YPP progress
  tracking — ``subscribers``, ``total_views``, ``watch_hours``
  (ordinary watch time) and ``qualified_watch_hours`` (the
  Analytics qualification figure, nullable — never estimated from
  ordinary hours).  The discriminator is ``snapshot_kind``
  ('scheduled' / 'manual'); ``metrics_json`` carries extra raw
  metrics.  Uniqueness: ``uq_channel_metric_snapshots_kind_day``
  on (channel_id, snapshot_kind, capture_day).

Downgrade drops ``channel_metric_snapshots``, the new indexes and
the five added columns (guarded DROP COLUMN, mirroring migration
0002's ``last4`` handling), restoring the section-15 table shape.

DDL constants are kept in sync with ``app.analytics.schema`` (the
runtime module ensures the same shape whether or not alembic ran).
Timestamps are INTEGER Unix epoch milliseconds, matching the
section-15 global conventions in app.schema_ddl.
"""

from typing import Sequence, Union

from sqlalchemy import text

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept in sync with app.analytics.schema (the same constants under
# the same names).
ANALYTICS_SNAPSHOTS_DDL = """
CREATE TABLE IF NOT EXISTS analytics_snapshots (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NULL
                          REFERENCES projects (id) ON DELETE RESTRICT,
    channel_id            TEXT NOT NULL
                          REFERENCES channels (id) ON DELETE RESTRICT,
    youtube_video_id      TEXT NULL,
    snapshot_at           INTEGER NOT NULL,
    views                 INTEGER NOT NULL DEFAULT 0,
    watch_hours_x100      INTEGER NOT NULL DEFAULT 0,
    likes                 INTEGER NOT NULL DEFAULT 0,
    comments_count        INTEGER NOT NULL DEFAULT 0,
    shares                INTEGER NOT NULL DEFAULT 0,
    ctr_bps               INTEGER NULL,
    avg_view_duration_sec INTEGER NULL,
    retention_curve_json  TEXT NULL,
    subs_delta            INTEGER NULL,
    revenue_micros        INTEGER NULL,
    source                TEXT NOT NULL DEFAULT 'youtube_analytics_api',
    created_at            INTEGER NOT NULL,
    snapshot_kind         TEXT NOT NULL DEFAULT 'scheduled'
                          CHECK (snapshot_kind IN ('d3', 'd7', 'd28',
                                 'd90', 'manual', 'scheduled')),
    capture_day           TEXT NULL,
    metrics_json          TEXT NULL,
    captured_at           INTEGER NULL,
    updated_at            INTEGER NULL
)
"""

CHANNEL_METRIC_SNAPSHOTS_DDL = """
CREATE TABLE IF NOT EXISTS channel_metric_snapshots (
    id                    TEXT PRIMARY KEY,
    channel_id            TEXT NOT NULL
                          REFERENCES channels (id) ON DELETE RESTRICT,
    snapshot_kind         TEXT NOT NULL DEFAULT 'scheduled'
                          CHECK (snapshot_kind IN ('scheduled',
                                 'manual')),
    captured_at           INTEGER NOT NULL,
    capture_day           TEXT NOT NULL,
    subscribers           INTEGER NULL,
    total_views           INTEGER NULL,
    watch_hours           REAL NULL,
    qualified_watch_hours REAL NULL,
    metrics_json          TEXT NULL,
    created_at            INTEGER NOT NULL,
    updated_at            INTEGER NOT NULL
)
"""

# (column name, ADD COLUMN fragment) pairs, in append order — kept
# in sync with app.analytics.schema.COLUMN_ADDITIONS.
COLUMN_ADDITIONS = (
    (
        "snapshot_kind",
        "snapshot_kind TEXT NOT NULL DEFAULT 'scheduled' "
        "CHECK (snapshot_kind IN ('d3', 'd7', 'd28', 'd90', "
        "'manual', 'scheduled'))",
    ),
    ("capture_day", "capture_day TEXT NULL"),
    ("metrics_json", "metrics_json TEXT NULL"),
    ("captured_at", "captured_at INTEGER NULL"),
    ("updated_at", "updated_at INTEGER NULL"),
)


def _column_names(table: str):
    """Column names for ``table`` via PRAGMA, or None if unknown
    (offline mode), mirroring migration 0002's helper."""
    try:
        bind = op.get_bind()
        rows = bind.execute(text(f"PRAGMA table_info({table})")).fetchall()
        names = {row[1] for row in rows}
        if not all(isinstance(name, str) for name in names):
            return None
        return names
    except Exception:
        return None


def _table_exists(table: str):
    """True/False when introspection is available, else None."""
    try:
        bind = op.get_bind()
        row = bind.execute(
            text(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = :name"
            ),
            {"name": table},
        ).fetchone()
        return row is not None
    except Exception:
        return None


def upgrade() -> None:
    # analytics_snapshots normally already exists (migration 0001);
    # create it in final shape only if it somehow does not.
    if _table_exists("analytics_snapshots") is not True:
        op.execute(ANALYTICS_SNAPSHOTS_DDL)

    columns = _column_names("analytics_snapshots")
    for name, fragment in COLUMN_ADDITIONS:
        if columns is None:
            try:
                op.execute(
                    f"ALTER TABLE analytics_snapshots ADD COLUMN {fragment}"
                )
            except Exception:
                pass
        elif name not in columns:
            op.execute(
                f"ALTER TABLE analytics_snapshots ADD COLUMN {fragment}"
            )

    # Backfill legacy rows, then dedupe on the new uniqueness key
    # (newest row kept) BEFORE the unique index is created.
    op.execute(
        "UPDATE analytics_snapshots SET captured_at = snapshot_at "
        "WHERE captured_at IS NULL"
    )
    op.execute(
        "UPDATE analytics_snapshots SET updated_at = created_at "
        "WHERE updated_at IS NULL"
    )
    op.execute(
        "UPDATE analytics_snapshots SET capture_day = "
        "date(snapshot_at / 1000, 'unixepoch') WHERE capture_day IS NULL"
    )
    op.execute(
        "DELETE FROM analytics_snapshots "
        "WHERE youtube_video_id IS NOT NULL AND rowid NOT IN ("
        "SELECT MAX(rowid) FROM analytics_snapshots "
        "WHERE youtube_video_id IS NOT NULL "
        "GROUP BY channel_id, youtube_video_id, snapshot_kind, capture_day)"
    )

    op.execute(CHANNEL_METRIC_SNAPSHOTS_DDL)

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_video "
        "ON analytics_snapshots (youtube_video_id, snapshot_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_channel "
        "ON analytics_snapshots (channel_id, snapshot_at)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_analytics_snapshots_kind_day "
        "ON analytics_snapshots (channel_id, youtube_video_id, "
        "snapshot_kind, capture_day)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_analytics_snapshots_project "
        "ON analytics_snapshots (project_id, snapshot_kind)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_channel_metric_snapshots_kind_day "
        "ON channel_metric_snapshots (channel_id, snapshot_kind, capture_day)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS "
        "idx_channel_metric_snapshots_channel_time "
        "ON channel_metric_snapshots (channel_id, captured_at)"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS idx_channel_metric_snapshots_channel_time"
    )
    op.execute("DROP INDEX IF EXISTS uq_channel_metric_snapshots_kind_day")
    op.execute("DROP TABLE IF EXISTS channel_metric_snapshots")
    op.execute("DROP INDEX IF EXISTS idx_analytics_snapshots_project")
    op.execute("DROP INDEX IF EXISTS uq_analytics_snapshots_kind_day")

    columns = _column_names("analytics_snapshots")
    for name, _fragment in reversed(COLUMN_ADDITIONS):
        if columns is None or name in columns:
            # SQLite >= 3.35 supports DROP COLUMN; wrap defensively
            # (same pattern as migration 0002's last4 downgrade).
            try:
                op.execute(
                    f"ALTER TABLE analytics_snapshots DROP COLUMN {name}"
                )
            except Exception:
                pass
