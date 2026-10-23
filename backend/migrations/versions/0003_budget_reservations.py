"""Budget Guard reservations persistence (Round 4).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-23

Changes:
- New ``budget_reservations`` table: durable per-project reservations
  against the global daily / monthly Budget Guard caps (see
  ``app.execution.budget_reservations`` for the accounting model).
  One row per DAG-start reservation: the incremental estimate in
  integer micros (``reserved_micros``), the actuals settled against
  it as stages record costs (``settled_micros``), and a lifecycle
  ``status`` of ``active`` -> ``settled`` / ``released``.  The
  unsettled remainder of an active row is derived
  (``reserved - settled``), never stored, so settle/release can never
  disagree with the row.  ``pacific_day`` / ``pacific_month`` stamp
  the America/Los_Angeles bucket the reservation belongs to (the
  Pacific date / month at creation), matching the quota ledger's
  Pacific-midnight discipline.  ``channel_id`` is nullable: NULL is
  the unattributed bucket, so per-channel sums agree exactly with the
  app total.  Timestamps are INTEGER Unix epoch milliseconds,
  matching the section-15 global conventions in app.schema_ddl.

  ``project_id`` deliberately carries no FOREIGN KEY (mirroring the
  defensive DDL in ``app.execution.gates_bridge`` for ``approvals``):
  reservations are an accounting ledger that must stay writable even
  for projects whose rows were created outside the section-15 flow.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept in sync with app.execution.budget_reservations.RESERVATIONS_DDL
# (the store ensures the table exists whether or not alembic ran).
BUDGET_RESERVATIONS_DDL = """
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


def upgrade() -> None:
    op.execute(BUDGET_RESERVATIONS_DDL)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_budget_res_project "
        "ON budget_reservations (project_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_budget_res_day "
        "ON budget_reservations (pacific_day, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_budget_res_month "
        "ON budget_reservations (pacific_month, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_budget_res_channel_day "
        "ON budget_reservations (channel_id, pacific_day)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS budget_reservations")
