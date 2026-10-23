"""Calibration log persistence (Round 5 — Growth + Learning).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-23

Number choice: 0005 (``learning_rules``, Learning Store), 0006
(``comments``, comment intelligence) and 0007 (``analytics``) were
claimed by sibling Round 5 teams — 0007 was taken by the analytics
team at the same moment this migration was first drafted as 0007,
so calibration moved to the next free number, 0008, chained on
0007.  The choice is documented in ``app.calibration.schema``.
COLLISION RESOLVED (Round 5 closure, R5-001): minutes after this
file first landed as ``0008_calibration_log``, a sibling team also
created ``0008_channel_quality_tier`` (revision "0008",
down_revision "0007") — two files claiming revision "0008" gave
Alembic two heads and blocked ``upgrade head`` outright.  The
channel quality tier migration keeps "0008"; this calibration
migration was renumbered to "0009" with ``down_revision = "0008"``,
restoring a single linear chain.  This module's table is
unaffected — the runtime ``ensure_calibration_schema`` creates it
independently of the alembic chain.

Changes (Learning Loop — calibration log):
- New ``calibration_log`` table: one row per (published video,
  snapshot point, metric) grading the expectation band issued at
  publish time against the actual measured when the D+7 / D+28
  analytics snapshot landed.  The issued band is stored verbatim
  (``predicted_low`` / ``predicted_mid`` / ``predicted_high`` for
  ``metric``), alongside ``actual_value``, the ``snapshot_ref`` the
  actual came from, and ``prediction_issued_at`` / ``published_at``
  stamps.  Scope is ``channel_id`` (REFERENCES channels ON DELETE
  RESTRICT) + ``niche`` (TEXT, deliberately not an FK — mirroring
  migration 0005 — so scope survives niche profile edits and custom
  "Other" niches); error aggregates are never pooled across scopes.
  Idempotency is enforced by the UNIQUE expression index
  ``uq_calibration_log_entry`` over (COALESCE(project_id, ''),
  COALESCE(youtube_video_id, ''), snapshot_point, metric) — one row
  per video + snapshot point + metric; rows are append-only in
  practice (the store never updates a recorded entry).
  Timestamps are INTEGER Unix epoch milliseconds, matching the
  section-15 global conventions in app.schema_ddl.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept in sync with app.calibration.schema (the runtime module ensures
# the table exists whether or not alembic ran).
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


def upgrade() -> None:
    op.execute(CALIBRATION_LOG_DDL)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_calibration_log_scope "
        "ON calibration_log (channel_id, niche, snapshot_point)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_calibration_log_project "
        "ON calibration_log (project_id, snapshot_point)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_calibration_log_video "
        "ON calibration_log (youtube_video_id, snapshot_point)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_calibration_log_entry "
        "ON calibration_log (COALESCE(project_id, ''), "
        "COALESCE(youtube_video_id, ''), snapshot_point, metric)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_calibration_log_entry")
    op.execute("DROP INDEX IF EXISTS idx_calibration_log_video")
    op.execute("DROP INDEX IF EXISTS idx_calibration_log_project")
    op.execute("DROP INDEX IF EXISTS idx_calibration_log_scope")
    op.execute("DROP TABLE IF EXISTS calibration_log")
