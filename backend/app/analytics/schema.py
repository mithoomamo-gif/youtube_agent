"""Analytics snapshot tables — runtime DDL + lazy schema creation.

This module holds the DDL for the two analytics-ingestion tables and
:func:`ensure_analytics_schema`, which creates / evolves them lazily
on a connection — the pattern used by
:mod:`app.style_lab.schema` (``ensure_style_lab_schema``) and
:mod:`app.execution.budget_reservations`
(``ensure_reservations_table``) — so analytics storage works on
databases that have not run migrations.

The DDL constants below are kept in sync with Alembic migration
``0007_analytics`` (``backend/migrations/versions/0007_analytics.py``),
which carries the same constants under the same names.

Why ``analytics_snapshots`` is *evolved*, not created fresh
-----------------------------------------------------------
``analytics_snapshots`` already exists in the section-15 initial
schema (:mod:`app.schema_ddl`, migration ``0001``) with discrete
metric columns (``views``, ``watch_hours_x100``, ``likes``,
``comments_count``, ``shares``, ``ctr_bps``,
``avg_view_duration_sec``, ``subs_delta``, ``revenue_micros``), the
capture instant in ``snapshot_at``, and indexes
``idx_snapshots_video`` / ``idx_snapshots_channel``.  A live consumer
already reads those columns directly
(:class:`app.learning_loop.adapters.SqliteAnalyticsSource` selects
``snapshot_at, views, watch_hours_x100, ctr_bps,
avg_view_duration_sec``), so the Round 5 shape is reached by
*adding* columns, never by dropping or renaming the legacy ones:

* ``snapshot_kind`` — 'd3' / 'd7' / 'd28' / 'd90' / 'manual' /
  'scheduled' (CHECK-enforced; legacy rows default to 'scheduled');
* ``capture_day`` — TEXT 'YYYY-MM-DD', the **UTC** calendar day of
  the capture instant, derived at write time and stored on the row
  (SQLite has no functional unique index over a derived date that
  would survive the app's upsert path, so the day is materialised —
  the same bucket-column discipline as ``budget_reservations``'
  ``pacific_day``).  It is the day component of the idempotency
  uniqueness below;
* ``metrics_json`` — the canonical Round 5 metrics object (views,
  watch minutes, avg view duration / percentage, likes, comments,
  shares, subs gained, CTR nullable, …).  The store mirrors the
  well-known keys into the legacy discrete columns on every write,
  so old readers keep working;
* ``captured_at`` — the Round 5 name for the capture instant; the
  store writes it identical to the legacy ``snapshot_at`` on every
  row (``snapshot_at`` stays the indexed legacy column);
* ``updated_at`` — epoch ms of the last upsert touching the row
  (the legacy table had only ``created_at``).

Idempotency uniqueness: ``uq_analytics_snapshots_kind_day`` is a
UNIQUE index on ``(channel_id, youtube_video_id, snapshot_kind,
capture_day)`` — a re-sync of the same video, kind and UTC day
updates the one row in place.  Rows with a NULL ``youtube_video_id``
are exempt (SQLite treats NULLs as distinct in unique indexes);
they are channel-level misc snapshots and always insert.

``channel_metric_snapshots`` (new in Round 5) is one row per
(channel, kind, UTC day) of channel-level figures for YPP progress
tracking: ``subscribers``, ``total_views``, ``watch_hours``
(ordinary, all watch time) and ``qualified_watch_hours`` (the
YouTube Analytics *qualification* figure, nullable — NULL means
"qualification data unavailable", it is never estimated from
ordinary watch time).  Its discriminator field is also named
``snapshot_kind`` (values 'scheduled' / 'manual') for symmetry with
the video table, and ``metrics_json`` carries any extra raw channel
metrics the sync records.  Uniqueness:
``uq_channel_metric_snapshots_kind_day`` on ``(channel_id,
snapshot_kind, capture_day)``.

Conventions: TEXT uuid4-hex primary keys; timestamps are INTEGER
Unix epoch milliseconds; ``channel_id`` REFERENCES channels ON
DELETE RESTRICT and ``project_id`` REFERENCES projects ON DELETE
RESTRICT (the dominant section-15 convention — the project link is
nullable because a synced video may have no project row).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

__all__ = [
    "ANALYTICS_SNAPSHOTS_TABLE",
    "CHANNEL_METRIC_SNAPSHOTS_TABLE",
    "SNAPSHOT_KINDS",
    "CHANNEL_SNAPSHOT_KINDS",
    "ANALYTICS_SNAPSHOTS_DDL",
    "CHANNEL_METRIC_SNAPSHOTS_DDL",
    "ANALYTICS_DDL",
    "COLUMN_ADDITIONS",
    "BACKFILL_STATEMENTS",
    "DEDUPE_STATEMENT",
    "INDEX_STATEMENTS",
    "capture_day_for",
    "ensure_analytics_schema",
]

ANALYTICS_SNAPSHOTS_TABLE = "analytics_snapshots"
CHANNEL_METRIC_SNAPSHOTS_TABLE = "channel_metric_snapshots"

#: analytics_snapshots.snapshot_kind values (mirror the schema CHECK).
SNAPSHOT_KINDS = ("d3", "d7", "d28", "d90", "manual", "scheduled")
#: channel_metric_snapshots.snapshot_kind values (mirror the CHECK).
CHANNEL_SNAPSHOT_KINDS = ("scheduled", "manual")

# Kept in sync with migrations/versions/0007_analytics.py (the
# migration carries the same constants under the same names).  The
# first 17 columns reproduce the section-15 definition verbatim (see
# app.schema_ddl); the last five are the Round 5 additions, in the
# same order the migration's ALTER TABLE statements append them, so
# a migrated table and a freshly created one have identical
# PRAGMA table_info.
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

#: (column name, ADD COLUMN fragment) pairs applied to a legacy
#: (section-15) ``analytics_snapshots`` table, in append order.
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

#: Backfills for legacy rows after the columns land.  ``date(x/1000,
#: 'unixepoch')`` is the UTC calendar day of an epoch-ms instant
#: (``/`` on integers is integer division in SQLite), matching
#: :func:`capture_day_for`.
BACKFILL_STATEMENTS = (
    "UPDATE analytics_snapshots SET captured_at = snapshot_at "
    "WHERE captured_at IS NULL",
    "UPDATE analytics_snapshots SET updated_at = created_at "
    "WHERE updated_at IS NULL",
    "UPDATE analytics_snapshots SET capture_day = "
    "date(snapshot_at / 1000, 'unixepoch') WHERE capture_day IS NULL",
)

#: Pre-unique-index dedupe: legacy rows that collide on the new
#: uniqueness key keep only the most recently inserted row (highest
#: rowid).  NULL-video rows are exempt — the unique index ignores
#: them too.
DEDUPE_STATEMENT = (
    "DELETE FROM analytics_snapshots "
    "WHERE youtube_video_id IS NOT NULL AND rowid NOT IN ("
    "SELECT MAX(rowid) FROM analytics_snapshots "
    "WHERE youtube_video_id IS NOT NULL "
    "GROUP BY channel_id, youtube_video_id, snapshot_kind, capture_day)"
)

#: All analytics table DDL statements, in creation order.
ANALYTICS_DDL = (ANALYTICS_SNAPSHOTS_DDL, CHANNEL_METRIC_SNAPSHOTS_DDL)

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_snapshots_video "
    "ON analytics_snapshots (youtube_video_id, snapshot_at)",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_channel "
    "ON analytics_snapshots (channel_id, snapshot_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_analytics_snapshots_kind_day "
    "ON analytics_snapshots (channel_id, youtube_video_id, "
    "snapshot_kind, capture_day)",
    "CREATE INDEX IF NOT EXISTS idx_analytics_snapshots_project "
    "ON analytics_snapshots (project_id, snapshot_kind)",
    "CREATE UNIQUE INDEX IF NOT EXISTS "
    "uq_channel_metric_snapshots_kind_day "
    "ON channel_metric_snapshots (channel_id, snapshot_kind, capture_day)",
    "CREATE INDEX IF NOT EXISTS "
    "idx_channel_metric_snapshots_channel_time "
    "ON channel_metric_snapshots (channel_id, captured_at)",
)

# Alias following the budget_reservations naming pattern.
_INDEX_STATEMENTS = INDEX_STATEMENTS


def capture_day_for(captured_at_ms: int) -> str:
    """UTC calendar day (``'YYYY-MM-DD'``) of an epoch-ms instant.

    This is the stored ``capture_day`` derivation used at write
    time; day boundaries are UTC midnights.
    """
    moment = datetime.fromtimestamp(int(captured_at_ms) / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%d")


def _commit_quietly(conn: Any) -> None:
    try:
        conn.commit()
    except Exception:
        pass


def _table_exists(conn: Any, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _column_names(conn: Any, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_analytics_schema(conn: Any) -> None:
    """Create / evolve the analytics tables on ``conn`` if needed.

    Idempotent and safe to call on every store entry point,
    mirroring :func:`app.style_lab.schema.ensure_style_lab_schema`:

    * a missing ``analytics_snapshots`` is created in its final
      (Round 5) shape; an existing legacy table gains the Round 5
      columns via guarded ``ADD COLUMN`` (in
      :data:`COLUMN_ADDITIONS` order);
    * legacy rows are backfilled (``captured_at`` / ``updated_at`` /
      ``capture_day``) and de-duplicated against the new uniqueness
      key *before* the unique index is created, so the index build
      cannot fail on pre-Round-5 data;
    * ``channel_metric_snapshots`` and all indexes are created
      ``IF NOT EXISTS``.
    """
    if _table_exists(conn, ANALYTICS_SNAPSHOTS_TABLE):
        existing = _column_names(conn, ANALYTICS_SNAPSHOTS_TABLE)
        for name, fragment in COLUMN_ADDITIONS:
            if name not in existing:
                conn.execute(
                    f"ALTER TABLE {ANALYTICS_SNAPSHOTS_TABLE} "
                    f"ADD COLUMN {fragment}"
                )
    else:
        conn.execute(ANALYTICS_SNAPSHOTS_DDL)
    for statement in BACKFILL_STATEMENTS:
        conn.execute(statement)
    conn.execute(DEDUPE_STATEMENT)
    conn.execute(CHANNEL_METRIC_SNAPSHOTS_DDL)
    for statement in INDEX_STATEMENTS:
        conn.execute(statement)
    _commit_quietly(conn)
