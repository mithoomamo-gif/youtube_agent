"""Learning Loop (Round 5) — bounded, per-niche learning rules.

* :mod:`app.learning_loop.contracts` — documented adapter contracts
  (analytics evidence + Learning Store) and the seam status.
* :mod:`app.learning_loop.engine` — the update engine: batch
  evaluation at D+7 / D+28, empirical-Bayes shrinkage, minimum-
  evidence gating, versioned supersession, deterministic +
  idempotent re-runs.
* :mod:`app.learning_loop.adapters` — a reference in-memory Learning
  Store (stub seam), a list analytics source, and a partial real
  SQLite analytics adapter.
"""

from app.learning_loop.contracts import (
    MIN_EVIDENCE,
    RULE_KINDS,
    SNAPSHOT_POINTS,
    ExpectationBand,
    VideoEvidence,
)
from app.learning_loop.engine import (
    BatchReport,
    RuleOutcome,
    UpdateEngine,
    VideoEvaluation,
)

__all__ = [
    "MIN_EVIDENCE",
    "RULE_KINDS",
    "SNAPSHOT_POINTS",
    "ExpectationBand",
    "VideoEvidence",
    "BatchReport",
    "RuleOutcome",
    "UpdateEngine",
    "VideoEvaluation",
]
