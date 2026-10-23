"""Contracts for the Round 5 Learning Loop update engine.

ADAPTER SEAM (loud, deliberate)
===============================
This engine was first written against the documented contracts
below while the siblings were still in flight (HEAD ``aaabab4``).
Both have since **landed in the working tree** during the round:

* migration ``0005_learning_rules`` + ``app.learning.store.
  LearningStore`` — real, but with scoped method signatures that
  differ from the protocol below; the sibling service layer
  (``app.learning.service._EngineStoreAdapter``) bridges it to this
  protocol, so the engine itself needed no changes;
* expectation bands live in ``calibration_log`` (migration 0008,
  ``app.calibration``), which the SQLite analytics adapter now
  reads directly (see :mod:`app.learning_loop.adapters`).

The protocol below remains the engine's own contract:

Learning Store contract (sibling-owned)
---------------------------------------
Rules are scoped ``(channel_id, niche)`` — never pooled across niches
or channels.  ``kind`` is one of :data:`RULE_KINDS`.  Fields:
``statement``, ``effect_estimate``, ``evidence_count``,
``confidence``, ``status`` (``candidate`` / ``active`` /
``superseded``), ``version``, ``created_from``.  Operations:

* ``get_latest(channel_id, niche, kind, statement_family)``
  -> the newest non-superseded rule dict for that family, or ``None``;
* ``create_candidate(fields)`` -> rule dict (status ``candidate``);
* ``activate(rule_id)`` -> rule dict (status ``active``);
* ``supersede(old_rule_id, fields)`` -> new rule dict whose
  ``version`` is the old rule's ``version + 1``; the old rule becomes
  ``superseded`` and is never mutated in any other respect.  The new
  rule is written under ``fields['statement_family']`` — which MAY
  differ from the old rule's family (a direction flip supersedes the
  opposite-direction rule and the replacement belongs to the NEW
  family, never the old one).

When the sibling store lands, wiring it in is a constructor argument
(:class:`~app.learning_loop.engine.UpdateEngine` accepts any object
satisfying :class:`LearningRuleStore`); no engine code changes.

Analytics contract (sibling-owned read models)
----------------------------------------------
The engine consumes :class:`VideoEvidence` records produced by an
:class:`AnalyticsSource`: one record per (published video, snapshot
point), carrying the actuals, the *issued expectation band* recorded
at publish time, and the project metadata the rule kinds are built
from.  See :mod:`app.learning_loop.adapters` for the sources that
exist today (in-memory synthetic + a partial real-SQLite reader).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence

# The activation threshold is ONE shared constant (unified evidence
# policy, Round 5 closure R5-005 / R5L-004): it is defined by the
# Learning Store's schema module and imported here, so the engine
# and the store can never drift onto different gates again.  The
# store's ``THIN_EVIDENCE_THRESHOLD`` (= 5) is a separate, purely
# display band and does not gate activation.
from app.learning.schema import (
    MIN_EVIDENCE_FOR_ACTIVATION as MIN_EVIDENCE,
)

__all__ = [
    "RULE_KINDS",
    "SNAPSHOT_POINTS",
    "MIN_EVIDENCE",
    "ExpectationBand",
    "VideoEvidence",
    "LearningRuleStore",
    "AnalyticsSource",
]

#: Rule kinds (documented Learning Store contract).
RULE_KINDS = (
    "topic_affinity",
    "hook_pattern",
    "duration_band",
    "title_pattern",
    "thumbnail_style",
    "publish_window",
)

#: Batch evaluation happens only at these post-publish snapshot points.
SNAPSHOT_POINTS = ("D+7", "D+28")

# ``MIN_EVIDENCE`` (= 3) is imported at the top of this module from
# ``app.learning.schema`` (``MIN_EVIDENCE_FOR_ACTIVATION``): a rule
# needs at least this many videos of evidence to activate, and the
# store enforces the same minimum inside ``activate_rule``.


@dataclass(frozen=True)
class ExpectationBand:
    """Performance range issued (predicted) at publish time.

    ``metric`` names the actual the band applies to; the engine's
    primary outcome metric is ``views`` (see engine docstring), so
    bands on any other metric are recorded but not graded.
    """

    low: float
    high: float
    metric: str = "views"

    def verdict(self, actual: float) -> str:
        if actual < self.low:
            return "below_band"
        if actual > self.high:
            return "above_band"
        return "within_band"


@dataclass(frozen=True)
class VideoEvidence:
    """One published video's actuals at one snapshot point.

    SYNTHETIC-FIXTURE NOTE: in tests every instance is synthetic and
    labelled as such; in production instances come from an
    :class:`AnalyticsSource` over real analytics snapshots.

    Optional metadata fields (``hook_type``, ``title_pattern``,
    ``thumbnail_style``) are ``None`` when the source cannot supply
    them; the engine simply emits no rule for that dimension for
    that video instead of guessing.
    """

    video_id: str
    channel_id: str
    niche: str
    snapshot_point: str  # one of SNAPSHOT_POINTS
    published_at_ms: Optional[int] = None
    snapshot_at_ms: Optional[int] = None
    views: int = 0
    watch_hours: float = 0.0
    ctr: Optional[float] = None
    avg_view_duration_sec: Optional[float] = None
    expectation_band: Optional[ExpectationBand] = None
    # project metadata
    topic: Optional[str] = None
    hook_type: Optional[str] = None
    duration_sec: Optional[float] = None
    title_pattern: Optional[str] = None
    thumbnail_style: Optional[str] = None

    @property
    def outcome(self) -> float:
        """Primary outcome metric (views) as a float."""
        return float(self.views)


class LearningRuleStore(Protocol):
    """Documented Learning Store contract (see module docstring)."""

    def get_latest(
        self, channel_id: str, niche: str, kind: str, statement_family: str
    ) -> Optional[dict]:
        ...

    def create_candidate(self, fields: Mapping[str, Any]) -> dict:
        ...

    def activate(self, rule_id: str) -> dict:
        ...

    def supersede(self, old_rule_id: str, fields: Mapping[str, Any]) -> dict:
        ...


class AnalyticsSource(Protocol):
    """Produces :class:`VideoEvidence` for one snapshot point."""

    def fetch(
        self, snapshot_point: str, *, channel_id: Optional[str] = None
    ) -> Sequence[VideoEvidence]:
        ...
