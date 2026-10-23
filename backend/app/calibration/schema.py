"""Calibration log table — runtime DDL + lazy schema creation.

Round 5 (Growth + Learning), Learning Loop team — calibration log.
This module owns the ``calibration_log`` table: one row per
(published video, snapshot point, metric) recording the expectation
band that was *issued at publish time* next to what *actually
happened* when the D+7 / D+28 analytics snapshot landed.  It is the
grade sheet for the system's own predictions (MASTER_ROADMAP §6.1
calibration loop: "at 7 and 28 days, Agent 29's actuals are compared
with the issued band; Agent 30 logs the error … Per-niche calibration
error is shown in reports — if the system has been wrong, the user
sees it").

Migration number choice (documented, deliberate)
------------------------------------------------
This table ships as Alembic migration **0009** (``down_revision =
"0008"``).  Numbers 0005, 0006 and 0007 were claimed by sibling
Round 5 teams while this module was being written — 0005
``learning_rules`` (Learning Store, ``app.learning``), 0006
``comments`` (comment intelligence) and 0007 ``analytics`` (taken
at the same moment this migration was first drafted as 0007, so
calibration moved to the next free number, 0008, chained on
0007; a sibling's ``0008_channel_quality_tier`` landed minutes
later still, also claiming 0008.  At Round 5 closure (R5-001) the
collision was resolved by renumbering this migration to 0009,
chained on the quality tier's 0008, noted in the migration file).
The migration file
``backend/migrations/versions/0009_calibration_log.py`` carries the
same DDL constants under the same names, per repo convention; the
runtime :func:`ensure_calibration_schema` below creates the table
lazily so the log works on databases that have not run migrations
(the :mod:`app.style_lab.schema` / :mod:`app.learning.schema`
pattern).

Schema notes (the calibration contract):

* TEXT primary keys are uuid4 hex; timestamps are INTEGER Unix epoch
  milliseconds, matching the section-15 global conventions in
  :mod:`app.schema_ddl`.
* Scope is BOTH ``channel_id`` (REFERENCES channels ON DELETE
  RESTRICT) and ``niche`` (TEXT, NOT NULL) on every row — a
  calibration row belongs to exactly one channel and one niche and is
  never pooled across either.  ``niche`` is the niche scope
  identifier recorded *at publish time* (niche profile id / key, as
  used interchangeably by the routers); it is deliberately not a
  FOREIGN KEY, mirroring :mod:`app.learning.schema`, so a row's scope
  survives niche profile edits and custom ("Other") niches.
* The video is identified by ``project_id`` (REFERENCES projects ON
  DELETE RESTRICT) and/or ``youtube_video_id`` — at least one is
  required (CHECK).  ``snapshot_ref`` is the reference of the
  analytics snapshot the actual came from (an ``analytics_snapshots``
  row id, or an external snapshot identifier string); it is a plain
  reference, not an FK, because snapshot rows may be recorded by the
  update engine before / independently of this log.
* The issued band is stored verbatim as issued:
  ``predicted_low`` / ``predicted_mid`` / ``predicted_high`` for
  ``metric`` (e.g. ``'views'``, ``'retention_pct'``,
  ``'watch_hours'``), with CHECKs ``0 <= predicted_low <=
  predicted_mid <= predicted_high``.  ``metric`` carries no CHECK:
  bands may be issued for any metric the analytics layer tracks, and
  inventing a closed list would silently drop future metrics.
* ``snapshot_point`` is ``'D+7'`` or ``'D+28'`` (CHECK) — the only
  two grading points (roadmap: learning rules are written only from
  D+7 / D+28 snapshots).
* Idempotency: a UNIQUE expression index over
  ``(COALESCE(project_id, ''), COALESCE(youtube_video_id, ''),
  snapshot_point, metric)`` makes one row per video + snapshot point
  + metric the most the table can hold; the store's recording seam
  additionally matches on either identifier alone, so re-recording
  the same snapshot is a no-op that returns the existing row (see
  :mod:`app.calibration.store`).  Rows are never updated in place —
  the first recorded actual for a snapshot point stands.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CALIBRATION_LOG_TABLE",
    "CALIBRATION_LOG_DDL",
    "CALIBRATION_DDL",
    "INDEX_STATEMENTS",
    "SNAPSHOT_POINTS",
    "ensure_calibration_schema",
]

CALIBRATION_LOG_TABLE = "calibration_log"

#: Snapshot points at which predictions are graded (mirror the CHECK).
SNAPSHOT_POINTS = ("D+7", "D+28")

# Kept in sync with migrations/versions/0009_calibration_log.py (the
# migration carries the same constant under the same name).
CALIBRATION_LOG_DDL = """
CREATE TABLE IF NOT EXISTS calibration_log (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NULL
                          REFERENCES projects (id) ON DELETE RESTRICT,
    youtube_video_id      TEXT NULL,
    channel_id            TEXT NOT NULL
                          REFERENCES channels (id) ON DELETE RESTRICT,
    niche                 TEXT NOT NULL,
    metric                TEXT NOT NULL,
    snapshot_point        TEXT NOT NULL
                          CHECK (snapshot_point IN ('D+7', 'D+28')),
    predicted_low         REAL NOT NULL
                          CHECK (predicted_low >= 0),
    predicted_mid         REAL NOT NULL,
    predicted_high        REAL NOT NULL,
    actual_value          REAL NOT NULL,
    snapshot_ref          TEXT NULL,
    prediction_issued_at  INTEGER NULL,
    published_at          INTEGER NULL,
    created_at            INTEGER NOT NULL,
    CHECK (predicted_low <= predicted_mid
           AND predicted_mid <= predicted_high),
    CHECK (project_id IS NOT NULL OR youtube_video_id IS NOT NULL)
)
"""

#: All calibration table DDL statements, in creation order.
CALIBRATION_DDL = (CALIBRATION_LOG_DDL,)

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_calibration_log_scope "
    "ON calibration_log (channel_id, niche, snapshot_point)",
    "CREATE INDEX IF NOT EXISTS idx_calibration_log_project "
    "ON calibration_log (project_id, snapshot_point)",
    "CREATE INDEX IF NOT EXISTS idx_calibration_log_video "
    "ON calibration_log (youtube_video_id, snapshot_point)",
    # One row per video + snapshot point + metric (idempotency
    # backstop; COALESCE mirrors the uq_pronunciation_lookup pattern
    # in app.schema_ddl because SQLite UNIQUE treats NULLs distinct).
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_calibration_log_entry "
    "ON calibration_log (COALESCE(project_id, ''), "
    "COALESCE(youtube_video_id, ''), snapshot_point, metric)",
)

# Alias following the budget_reservations naming pattern.
_INDEX_STATEMENTS = INDEX_STATEMENTS


def _commit_quietly(conn: Any) -> None:
    try:
        conn.commit()
    except Exception:
        pass


def ensure_calibration_schema(conn: Any) -> None:
    """Create the calibration log table + indexes on ``conn`` if absent.

    Idempotent (every statement is IF NOT EXISTS) and safe to call on
    every store / report entry point, mirroring
    :func:`app.style_lab.schema.ensure_style_lab_schema`.
    """
    for statement in CALIBRATION_DDL:
        conn.execute(statement)
    for statement in INDEX_STATEMENTS:
        conn.execute(statement)
    _commit_quietly(conn)
