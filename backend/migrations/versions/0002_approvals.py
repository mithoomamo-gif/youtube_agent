"""Approvals persistence + provider_credentials.last4.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23

Changes:
- New ``approvals`` table: durable Gate A / Gate B decision records
  (one row per gate request for a project).  Decisions that used to
  live only in the approvals router's in-memory dict now survive a
  backend restart.  Timestamps are INTEGER Unix epoch milliseconds,
  matching the section-15 global conventions in app.schema_ddl.
- ``provider_credentials.last4``: the last-4 hint for an API key used
  to be cached only in-process by the providers router; it is now a
  real (nullable) column so the hint survives restarts too.  It is
  metadata only — never the key value itself.
"""

from typing import Sequence, Union

from sqlalchemy import text

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept in sync with the lazy CREATE TABLE in app.routers.approvals
# (the router ensures the table exists whether or not alembic ran).
APPROVALS_DDL = """
CREATE TABLE IF NOT EXISTS approvals (
    id                     TEXT PRIMARY KEY,
    project_id             TEXT NOT NULL
                           REFERENCES projects (id) ON DELETE RESTRICT,
    gate                   TEXT NOT NULL CHECK (gate IN ('a', 'b')),
    artefact_versions_json TEXT NULL,
    decision               TEXT NULL
                           CHECK (decision IN ('approved', 'rejected')),
    reason_code            TEXT NULL,
    note                   TEXT NULL,
    decided_by             TEXT NULL,
    status                 TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'decided')),
    created_at             INTEGER NOT NULL,
    decided_at             INTEGER NULL
)
"""


def _column_names(table: str):
    """Column names for ``table`` via PRAGMA, or None if unknown.

    SQLite has no ``ADD COLUMN IF NOT EXISTS``, so the last4 addition
    must be guarded by introspection.  ``op.get_bind()`` is used so
    this works against the live connection in online mode; any
    failure (e.g. offline mode, where there is no real connection to
    introspect) returns None and the caller falls back to attempting
    the ALTER defensively.
    """
    try:
        bind = op.get_bind()
        rows = bind.execute(text(f"PRAGMA table_info({table})")).fetchall()
        names = {row[1] for row in rows}
        if not all(isinstance(name, str) for name in names):
            return None
        return names
    except Exception:
        return None


def upgrade() -> None:
    op.execute(APPROVALS_DDL)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_approvals_project_gate "
        "ON approvals (project_id, gate)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_approvals_status "
        "ON approvals (status)"
    )

    columns = _column_names("provider_credentials")
    if columns is None:
        # No introspection available (offline): emit the ALTER; on a
        # real run against a database that already has the column the
        # caller can re-check — online runs take the guarded path.
        try:
            op.execute(
                "ALTER TABLE provider_credentials ADD COLUMN last4 TEXT NULL"
            )
        except Exception:
            pass
    elif "last4" not in columns:
        op.execute(
            "ALTER TABLE provider_credentials ADD COLUMN last4 TEXT NULL"
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS approvals")

    columns = _column_names("provider_credentials")
    if columns is None or "last4" in columns:
        # SQLite >= 3.35 supports DROP COLUMN; wrap defensively so a
        # downgrade on an older SQLite / already-dropped column does
        # not abort the whole migration.
        try:
            op.execute(
                "ALTER TABLE provider_credentials DROP COLUMN last4"
            )
        except Exception:
            pass
