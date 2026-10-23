"""Generated-reports table — runtime DDL + lazy schema creation.

This module holds the DDL for the ``generated_reports`` table and
:func:`ensure_reports_schema`, which creates it lazily on a
connection — the pattern used by :mod:`app.style_lab.schema`
(``ensure_style_lab_schema``), :mod:`app.learning.schema`
(``ensure_learning_schema``) and
:mod:`app.execution.budget_reservations`
(``ensure_reservations_table``) — so the Reporting engine works on
databases that have not run migrations.

Parity (Round 5 closure): migration ``0010_reports`` now carries
this exact DDL (revision numbering was coordinated in the closure
pass, after the concurrent Round 5 migrations settled at 0009).
The lazy DDL below remains the runtime contract — it is kept
identical with the migration, so the table is created whether or
not Alembic has run.

Schema notes:

* TEXT primary keys are uuid4 hex; timestamps are INTEGER Unix epoch
  milliseconds, matching the section-15 global conventions in
  :mod:`app.schema_ddl`.
* ``channel_id`` is the report scope owner (REFERENCES channels ON
  DELETE RESTRICT where the FK can be enforced; the column is kept
  NOT NULL even on databases built without FK enforcement so a
  report is always attributable to exactly one channel).
* ``scope`` is ``'video'`` (one project / video receipt, with
  ``project_id`` set) or ``'channel'`` (a per-channel receipt over a
  period, ``project_id`` NULL).
* ``report_json`` is the full report document — the JSON model is
  the source of truth (see :mod:`app.reporting.models`) and is
  stored inline so a report can be fetched even if its rendered
  files are gone.  ``markdown_text`` stores the rendered Markdown
  exactly as generated.  ``json_path`` / ``markdown_path`` point at
  the optional file-store copies (NULL when no file store was
  available at generation time — the DB row is still complete).
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "GENERATED_REPORTS_TABLE",
    "GENERATED_REPORTS_DDL",
    "REPORTS_DDL",
    "INDEX_STATEMENTS",
    "REPORT_SCOPES",
    "ensure_reports_schema",
]

GENERATED_REPORTS_TABLE = "generated_reports"

#: generated_reports.scope values (mirror the schema CHECK).
REPORT_SCOPES = ("video", "channel")

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

#: All Reporting table DDL statements, in creation order.
REPORTS_DDL = (GENERATED_REPORTS_DDL,)

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_generated_reports_channel_time "
    "ON generated_reports (channel_id, generated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_generated_reports_scope_time "
    "ON generated_reports (scope, generated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_generated_reports_project "
    "ON generated_reports (project_id, generated_at DESC)",
)


def _commit_quietly(conn: Any) -> None:
    try:
        conn.commit()
    except Exception:
        pass


def ensure_reports_schema(conn: Any) -> None:
    """Create the generated-reports table + indexes if absent.

    Idempotent (every statement is IF NOT EXISTS) and safe to call on
    every store / service entry point, mirroring
    :func:`app.style_lab.schema.ensure_style_lab_schema`.
    """
    for statement in REPORTS_DDL:
        conn.execute(statement)
    for statement in INDEX_STATEMENTS:
        conn.execute(statement)
    _commit_quietly(conn)
