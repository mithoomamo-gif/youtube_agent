"""Learning Store rules persistence (Round 5 — Growth + Learning).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-23

Changes:
- New ``learning_rules`` table: the per-niche Learning Store.
  Learning is bounded by design (MASTER_ROADMAP §7): analytics are
  converted into versioned, evidence-counted rules, and learning
  cannot silently rewrite prompts or pool creative conclusions
  across niches.

  Scope: ``channel_id`` (REFERENCES channels ON DELETE RESTRICT) and
  ``niche`` are BOTH NOT NULL on every row — a rule belongs to
  exactly one channel and exactly one niche, never pooled across
  either.  ``niche`` is the niche scope identifier (niche profile
  id / key); it is deliberately not a FOREIGN KEY so a rule's scope
  survives niche profile edits and custom ("Other") niches.

  Versioning: ``version`` is per (channel_id, niche, kind,
  ``statement_family``).  A published rule is NEVER mutated in
  place: superseding writes a new row with ``version + 1`` and
  ``supersedes_id`` pointing at the replaced row, whose ``status``
  flips to 'superseded'.  ``status`` is 'candidate' / 'active' /
  'superseded'; at most one active row per family is a store-level
  rule, not a schema constraint.

  Evidence: ``kind`` ∈ topic_affinity / hook_pattern /
  duration_band / title_pattern / thumbnail_style / publish_window;
  ``statement`` is the human-readable rule; ``effect_value`` +
  ``effect_unit`` carry the effect estimate; ``evidence_count``
  counts the snapshots/videos behind the rule and ``thin_evidence``
  labels rules below the minimum-evidence threshold (roadmap
  anti-overfitting: n>=5 for hook/packaging rankings);
  ``confidence`` is 0–1; ``created_from_json`` is a JSON list of the
  snapshot/video ids the rule was derived from.  Timestamps are
  INTEGER Unix epoch milliseconds, matching the section-15 global
  conventions in app.schema_ddl.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept in sync with app.learning.schema (the runtime module ensures
# the table exists whether or not alembic ran).
LEARNING_RULES_DDL = """
CREATE TABLE IF NOT EXISTS learning_rules (
    id                TEXT PRIMARY KEY,
    channel_id        TEXT NOT NULL
                      REFERENCES channels (id) ON DELETE RESTRICT,
    niche             TEXT NOT NULL,
    kind              TEXT NOT NULL
                      CHECK (kind IN ('topic_affinity', 'hook_pattern',
                             'duration_band', 'title_pattern',
                             'thumbnail_style', 'publish_window')),
    statement_family  TEXT NOT NULL,
    version           INTEGER NOT NULL DEFAULT 1
                      CHECK (version >= 1),
    statement         TEXT NOT NULL,
    effect_value      REAL NULL,
    effect_unit       TEXT NULL,
    evidence_count    INTEGER NOT NULL DEFAULT 0
                      CHECK (evidence_count >= 0),
    confidence        REAL NULL
                      CHECK (confidence IS NULL
                             OR (confidence >= 0 AND confidence <= 1)),
    thin_evidence     INTEGER NOT NULL DEFAULT 1
                      CHECK (thin_evidence IN (0, 1)),
    status            TEXT NOT NULL DEFAULT 'candidate'
                      CHECK (status IN ('candidate', 'active',
                             'superseded')),
    supersedes_id     TEXT NULL,
    created_from_json TEXT NOT NULL DEFAULT '[]',
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
)
"""


def upgrade() -> None:
    op.execute(LEARNING_RULES_DDL)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_learning_rules_scope_status "
        "ON learning_rules (channel_id, niche, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_learning_rules_family "
        "ON learning_rules (channel_id, niche, kind, statement_family, version)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_learning_rules_family")
    op.execute("DROP INDEX IF EXISTS idx_learning_rules_scope_status")
    op.execute("DROP TABLE IF EXISTS learning_rules")
