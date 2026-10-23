"""Initial schema — all section-15 tables, indexes and triggers.

Revision ID: 0001
Revises:
Create Date: 2026-10-23

upgrade() executes the DDL statements from app.schema_ddl verbatim
(single source of truth — app.db.init_db applies the same statements).
downgrade() drops the tables children-first.
"""

from typing import Sequence, Union

from alembic import op

from app.schema_ddl import DDL_STATEMENTS, DROP_ORDER, SCHEMA_VERSION

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for statement in DDL_STATEMENTS:
        op.execute(statement)
    # Record the schema version exactly as app.db.init_db does.
    op.execute(
        "INSERT INTO schema_meta (id, version, updated_at) "
        "VALUES (1, %d, 0) "
        "ON CONFLICT(id) DO UPDATE SET version = excluded.version"
        % SCHEMA_VERSION
    )


def downgrade() -> None:
    for table in DROP_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {table}")
