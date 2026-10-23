"""Learning Loop — bounded, per-niche Learning Store (Round 5).

Learning in this app is bounded by design (MASTER_ROADMAP §7):
analytics are converted into versioned, evidence-counted rules in a
per-niche Learning Store.  A rule is always scoped to exactly one
``channel_id`` **and** one ``niche`` — rules are never pooled across
channels or niches, and learning never silently rewrites prompts.

This package holds the persistence layer only:

* :mod:`app.learning.schema` — runtime DDL + lazy schema creation
  (kept in sync with Alembic migration ``0005_learning_rules``);
* :mod:`app.learning.store` — the scoped store (candidates,
  activation, supersession/versioning, history).

Consumption (sibling Round 5 team):

* :mod:`app.learning.consumption` — the research seam: active rules
  for one (channel_id, niche) returned as structured DATA beside
  ranked, provenance-carrying topic suggestions (never injected
  into prompts);
* :mod:`app.learning.lessons` — the production-lessons view:
  approval/gate reject reasons aggregated per (channel_id, niche),
  a structure fully separate from performance rules.
"""

from __future__ import annotations

__all__: list[str] = []
