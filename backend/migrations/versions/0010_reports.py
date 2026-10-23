"""Reporting tables (Round 5 closure — Agent 31 receipts + digests).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-23

Changes:
- New ``generated_reports`` table (W1 reporting engine,
  :mod:`app.reporting`): the engine's canonical stored copy of a
  generated report — ``scope`` 'video' (one project, ``project_id``
  set) or 'channel' (per-channel receipt, ``project_id`` NULL),
  the full report document inline in ``report_json`` plus the
  rendered ``markdown_text``; ``json_path`` / ``markdown_path``
  point at the optional file-store copies.
- New ``reports`` table (W2 HTTP store, :mod:`app.reports`): the
  adapted receipt artefacts the API serves (``kind='receipt'``)
  and the weekly channel digest W2 builds itself
  (``kind='weekly_digest'``), each row keeping both the structured
  ``data_json`` and the rendered ``markdown``.  ``channel_id`` is
  the owner scope (REFERENCES channels ON DELETE RESTRICT);
  ``project_id`` is set for per-video receipts and NULL for
  channel-scope reports.

Parity note: both tables were originally shipped runtime-only —
their owning modules (:mod:`app.reporting.schema` and
:mod:`app.reports.schema`) deliberately claimed no revision while
Round 5 teams were landing migrations concurrently, and documented
that the integration/closure pass would add a migration carrying
the same DDL once revision numbering was coordinated.  This is
that migration: the DDL constants below are kept identical with
the runtime modules (which keep their lazy ``ensure_*_schema``
functions — a database that has not run migrations still gets the
tables on first use, exactly like the sibling Round 5 features).

Timestamps are INTEGER Unix epoch milliseconds, matching the
section-15 global conventions in app.schema_ddl.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept identical with app.reporting.schema.GENERATED_REPORTS_DDL
# (the runtime module ensures the same shape whether or not alembic
# ran).  Note the runtime DDL deliberately carries no FK on
# channel_id — see that module's schema notes.
GENERATED_REPORTS_DDL = """
CREATE TABLE IF NOT EXISTS generated_reports (
    id              TEXT PRIMARY KEY,
    channel_id      TEXT NOT NULL,
    scope           TEXT NOT NULL
                    CHECK (scope IN ('video', 'channel')),
    project_id      TEXT NULL,
    period_start_ms INTEGER NULL,
    period_end_ms   INTEGER NULL,
    period_label    TEXT NULL,
    title           TEXT NULL,
    generated_at    INTEGER NOT NULL,
    json_path       TEXT NULL,
    markdown_path   TEXT NULL,
    report_json     TEXT NOT NULL,
    markdown_text   TEXT NULL
)
"""

# Kept identical with app.reports.schema.REPORTS_DDL.
REPORTS_DDL = """
CREATE TABLE IF NOT EXISTS reports (
    id              TEXT PRIMARY KEY,
    scope           TEXT NOT NULL
                    CHECK (scope IN ('video', 'channel')),
    kind            TEXT NOT NULL
                    CHECK (kind IN ('receipt', 'weekly_digest')),
    channel_id      TEXT NOT NULL
                    REFERENCES channels (id) ON DELETE RESTRICT,
    project_id      TEXT NULL
                    REFERENCES projects (id) ON DELETE RESTRICT,
    period_start_ms INTEGER NOT NULL,
    period_end_ms   INTEGER NOT NULL,
    title           TEXT NOT NULL,
    data_json       TEXT NOT NULL,
    markdown        TEXT NOT NULL,
    created_at      INTEGER NOT NULL
)
"""


def upgrade() -> None:
    op.execute(GENERATED_REPORTS_DDL)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_generated_reports_channel_time "
        "ON generated_reports (channel_id, generated_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_generated_reports_scope_time "
        "ON generated_reports (scope, generated_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_generated_reports_project "
        "ON generated_reports (project_id, generated_at DESC)"
    )
    op.execute(REPORTS_DDL)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_reports_channel_created "
        "ON reports (channel_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_reports_project "
        "ON reports (project_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_reports_project")
    op.execute("DROP INDEX IF EXISTS idx_reports_channel_created")
    op.execute("DROP TABLE IF EXISTS reports")
    op.execute("DROP INDEX IF EXISTS idx_generated_reports_project")
    op.execute("DROP INDEX IF EXISTS idx_generated_reports_scope_time")
    op.execute("DROP INDEX IF EXISTS idx_generated_reports_channel_time")
    op.execute("DROP TABLE IF EXISTS generated_reports")
