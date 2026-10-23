"""Per-channel production quality tier (Round 5 — master encode).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-23

Changes:
- ``channels.quality_tier``: the channel's output quality setting,
  stored with the channel like its other first-class settings
  (``daily_upload_cap``, ``monthly_budget_micros``).  Values are
  exactly the render path's ``quality_tier`` vocabulary:
  ``'draft'`` / ``'master_1080p'`` / ``'master_1440p_upscale'``
  (TEXT + CHECK, section-15 convention).  NOT NULL DEFAULT
  ``'draft'``: every existing channel keeps the historical 720p
  draft behaviour until a user flips the setting, and every new
  channel starts there too.

  Numbering note: this piece was briefed as "migration 0005", but
  revisions 0005 (learning rules), 0006 (comments) and 0007
  (calibration log) were claimed by concurrent Round 5 workers in
  the same tree, so this migration is 0008 revising 0007 — a
  duplicate revision id would have broken the Alembic chain.

  The column addition is guarded by PRAGMA introspection (SQLite
  has no ``ADD COLUMN IF NOT EXISTS``), mirroring migration 0002's
  ``provider_credentials.last4`` addition.  The same definition is
  written literally into the ``channels`` DDL in app.schema_ddl
  (fresh databases) and is ensured lazily at runtime by
  ``app.channel_quality.ensure_quality_tier_column`` (databases
  that have not run migrations) — all three stay in sync.
"""

from typing import Sequence, Union

from sqlalchemy import text

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept in sync with app.channel_quality.QUALITY_TIER_COLUMN_DDL and
# the channels DDL in app.schema_ddl.
QUALITY_TIER_ADD_COLUMN = (
    "ALTER TABLE channels ADD COLUMN quality_tier "
    "TEXT NOT NULL DEFAULT 'draft' "
    "CHECK (quality_tier IN ('draft', 'master_1080p', "
    "'master_1440p_upscale'))"
)


def _column_names(table: str):
    """Column names for ``table`` via PRAGMA, or None if unknown.

    Mirrors migration 0002's helper: ``op.get_bind()`` introspection
    for online runs; None (offline / failure) makes the caller fall
    back to attempting the ALTER defensively.
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
    columns = _column_names("channels")
    if columns is None:
        # No introspection available (offline): emit the ALTER; on a
        # real run against a database that already has the column the
        # caller can re-check — online runs take the guarded path.
        try:
            op.execute(QUALITY_TIER_ADD_COLUMN)
        except Exception:
            pass
    elif "quality_tier" not in columns:
        op.execute(QUALITY_TIER_ADD_COLUMN)


def downgrade() -> None:
    columns = _column_names("channels")
    if columns is None or "quality_tier" in columns:
        # SQLite >= 3.35 supports DROP COLUMN; wrap defensively so a
        # downgrade on an older SQLite / already-dropped column does
        # not abort the whole migration (mirrors 0002's last4 drop).
        try:
            op.execute("ALTER TABLE channels DROP COLUMN quality_tier")
        except Exception:
            pass
