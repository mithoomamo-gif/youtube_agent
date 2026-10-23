"""Compliance Gate tables (Round 6 — roadmap's #1 launch blocker).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-23

Changes:
- New ``compliance_items`` table: the per-app compliance checklist
  as DATA — one row per canonical item (``api_audit_status``,
  ``terms_branding_review``, ``privacy_policy_live``,
  ``quota_project_verified``), each with ``status``
  (pending / in_progress / passed / failed), ``evidence`` /
  ``note`` fields, and ``updated_by`` / ``updated_at`` who/when
  stamps.  The overall state is DERIVED from these rows
  (passed iff all passed) and is deliberately not stored.
- New ``compliance_transitions`` table: the append-only audit /
  history record — one row per deliberate transition (item,
  from/to status, actor, evidence, note, timestamp).  State can
  only change through the logged transition action in
  :mod:`app.compliance.service`; there is no free-form edit path.

Parity note: the DDL constants below are kept identical with
:mod:`app.compliance.schema`, whose ``ensure_compliance_schema``
also SEEDS the four checklist rows (INSERT OR IGNORE) at runtime —
a database that has not run migrations gets the same tables and
the same seeded checklist on first use, exactly like the sibling
Round 5 features.  Seeding lives in the runtime ensure (not here)
so migrated and unmigrated databases seed identically.

Timestamps are INTEGER Unix epoch milliseconds, matching the
section-15 global conventions in app.schema_ddl.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept identical with app.compliance.schema.COMPLIANCE_ITEMS_DDL.
COMPLIANCE_ITEMS_DDL = """
CREATE TABLE IF NOT EXISTS compliance_items (
    key        TEXT PRIMARY KEY,
    status     TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'in_progress',
                                 'passed', 'failed')),
    evidence   TEXT NULL,
    note       TEXT NULL,
    updated_by TEXT NULL,
    updated_at INTEGER NULL,
    created_at INTEGER NOT NULL
)
"""

# Kept identical with app.compliance.schema.COMPLIANCE_TRANSITIONS_DDL.
COMPLIANCE_TRANSITIONS_DDL = """
CREATE TABLE IF NOT EXISTS compliance_transitions (
    id          TEXT PRIMARY KEY,
    item_key    TEXT NOT NULL
                REFERENCES compliance_items (key) ON DELETE RESTRICT,
    from_status TEXT NOT NULL
                CHECK (from_status IN ('pending', 'in_progress',
                                        'passed', 'failed')),
    to_status   TEXT NOT NULL
                CHECK (to_status IN ('pending', 'in_progress',
                                      'passed', 'failed')),
    actor       TEXT NOT NULL,
    evidence    TEXT NULL,
    note        TEXT NULL,
    created_at  INTEGER NOT NULL
)
"""


def upgrade() -> None:
    op.execute(COMPLIANCE_ITEMS_DDL)
    op.execute(COMPLIANCE_TRANSITIONS_DDL)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_compliance_transitions_item_time "
        "ON compliance_transitions (item_key, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_compliance_transitions_time "
        "ON compliance_transitions (created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_compliance_transitions_time")
    op.execute("DROP INDEX IF EXISTS idx_compliance_transitions_item_time")
    op.execute("DROP TABLE IF EXISTS compliance_transitions")
    op.execute("DROP TABLE IF EXISTS compliance_items")
