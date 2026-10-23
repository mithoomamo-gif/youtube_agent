"""Learning Store table — runtime DDL + lazy schema creation.

This module holds the DDL for the ``learning_rules`` table and
:func:`ensure_learning_schema`, which creates it lazily on a
connection — the pattern used by :mod:`app.style_lab.schema`
(``ensure_style_lab_schema``) and
:mod:`app.execution.budget_reservations`
(``ensure_reservations_table``) — so the Learning Store works on
databases that have not run migrations.

The DDL constant below is kept in sync with Alembic migration
``0005_learning_rules`` (``backend/migrations/versions/
0005_learning_rules.py``), which carries it under the same name.

Schema notes (the Learning Store contract):

* TEXT primary keys are uuid4 hex; timestamps are INTEGER Unix epoch
  milliseconds, matching the section-15 global conventions in
  :mod:`app.schema_ddl`.
* ``channel_id`` (REFERENCES channels ON DELETE RESTRICT) and
  ``niche`` are BOTH ``NOT NULL`` on every row: a rule belongs to
  exactly one channel and exactly one niche and is never pooled
  across either.  ``niche`` is the niche scope identifier (niche
  profile id / key, as used interchangeably by the routers); it is
  deliberately not a FOREIGN KEY so a rule's scope survives niche
  profile edits and custom ("Other") niches.
* ``version`` is per (channel_id, niche, kind, statement_family):
  version 1 is the first statement of a family; a superseding rule
  carries ``version + 1`` and ``supersedes_id`` pointing at the row
  it replaced.  Published rows are never mutated in place — the old
  row's status flips to ``'superseded'`` and a new row is written.
* ``kind`` ∈ topic_affinity / hook_pattern / duration_band /
  title_pattern / thumbnail_style / publish_window; ``status`` ∈
  candidate / active / superseded (CHECK-enforced).
* ``effect_value`` + ``effect_unit`` carry the effect estimate
  (e.g. ``18.0`` + ``'pct_retention_first_30s'``); both nullable —
  a candidate may be recorded before its effect is estimated.
* ``thin_evidence`` is the thin-evidence label: 1 while
  ``evidence_count`` is below :data:`THIN_EVIDENCE_THRESHOLD`,
  else 0.  The store maintains it on write; readers surface it as
  ``is_thin_evidence``.

Evidence policy (UNIFIED — Round 5 closure, R5-005 / R5L-004)
-------------------------------------------------------------
Two different numbers, two different jobs; they are NOT two gates:

* :data:`MIN_EVIDENCE_FOR_ACTIVATION` (= 3) is THE activation
  threshold, everywhere: the update engine
  (``app.learning_loop.contracts.MIN_EVIDENCE``, imported from
  here so there is exactly one constant) only activates at
  ``evidence_count >= 3``, and :meth:`app.learning.store.
  LearningStore.activate_rule` itself refuses activation below 3
  with a typed error — the store is the last line of defence, so
  no caller can activate a thin rule by bypassing the engine.
* :data:`THIN_EVIDENCE_THRESHOLD` (= 5) is a DISPLAY BAND ONLY:
  a rule backed by fewer than 5 videos is labelled
  ``is_thin_evidence`` so readers can see the evidence is still
  thin.  The label never blocks activation — a rule with n=3–4
  is legitimately ``active`` AND labelled thin at the same time,
  and that combination is the policy working as decided, not an
  inconsistency.
* ``created_from_json`` is a JSON list of the snapshot / video ids
  the rule was derived from (its evidence provenance).
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "LEARNING_RULES_TABLE",
    "LEARNING_RULES_DDL",
    "LEARNING_DDL",
    "INDEX_STATEMENTS",
    "RULE_KINDS",
    "RULE_STATUSES",
    "MIN_EVIDENCE_FOR_ACTIVATION",
    "THIN_EVIDENCE_THRESHOLD",
    "ensure_learning_schema",
]

LEARNING_RULES_TABLE = "learning_rules"

#: Rule kinds (mirror the schema CHECK).
RULE_KINDS = (
    "topic_affinity",
    "hook_pattern",
    "duration_band",
    "title_pattern",
    "thumbnail_style",
    "publish_window",
)

#: learning_rules.status values (mirror the schema CHECK).
RULE_STATUSES = ("candidate", "active", "superseded")

#: THE activation threshold (unified evidence policy — see the
#: module docstring): a rule may become ``active`` only with at
#: least this many videos/snapshots of evidence.  Enforced by the
#: update engine AND by ``LearningStore.activate_rule`` itself.
#: ``app.learning_loop.contracts.MIN_EVIDENCE`` is this constant,
#: imported — there is exactly one activation threshold.
MIN_EVIDENCE_FOR_ACTIVATION = 3

#: A rule backed by fewer snapshots/videos than this is labelled
#: thin-evidence.  DISPLAY BAND ONLY (unified evidence policy):
#: the label says "evidence still thin", it does NOT gate
#: activation — activation is governed solely by
#: :data:`MIN_EVIDENCE_FOR_ACTIVATION` (n>=3), so an active rule
#: with n=3–4 is correctly labelled thin at the same time.
THIN_EVIDENCE_THRESHOLD = 5

# Kept in sync with migrations/versions/0005_learning_rules.py (the
# migration carries the same constant under the same name).
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

#: All Learning Store table DDL statements, in creation order.
LEARNING_DDL = (LEARNING_RULES_DDL,)

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_learning_rules_scope_status "
    "ON learning_rules (channel_id, niche, status)",
    "CREATE INDEX IF NOT EXISTS idx_learning_rules_family "
    "ON learning_rules (channel_id, niche, kind, statement_family, version)",
)

# Alias following the budget_reservations naming pattern.
_INDEX_STATEMENTS = INDEX_STATEMENTS


def _commit_quietly(conn: Any) -> None:
    try:
        conn.commit()
    except Exception:
        pass


def ensure_learning_schema(conn: Any) -> None:
    """Create the Learning Store table + indexes on ``conn`` if absent.

    Idempotent (every statement is IF NOT EXISTS) and safe to call on
    every store entry point, mirroring
    :func:`app.style_lab.schema.ensure_style_lab_schema`.
    """
    for statement in LEARNING_DDL:
        conn.execute(statement)
    for statement in INDEX_STATEMENTS:
        conn.execute(statement)
    _commit_quietly(conn)
