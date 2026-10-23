"""Update engine — the batch learner (Agent 30's grading step).

Turns analytics snapshots at D+7 / D+28 into *bounded* learning
rules: versioned, evidence-counted, scoped per ``(channel_id,
niche)``, never pooled across niches or channels, never a silent
prompt rewrite (this engine only writes rules to the Learning
Store; it edits no prompt, profile or published artefact).

Math (documented, deterministic)
================================
Primary outcome: ``views`` at the snapshot point.

Channel baseline: for one batch, the baseline for a scope
``(channel_id, niche, snapshot_point)`` is the **median** outcome of
*all* videos in that scope in the batch — same channel, same niche
only.  Cross-niche / cross-channel pooling is structurally
impossible: grouping keys always include both ids.

Raw effect of an attribute group ``g`` (e.g. hook_type="question")::

    raw_effect = (mean(outcome_g) - baseline) / baseline     (0 if baseline == 0)

Shrinkage (empirical-Bayes, roadmap Part on learning guardrails:
``score_used = (n * score_observed + k * score_baseline) / (n + k)``).
Applied to the group mean that identity is exactly::

    shrunk_effect = raw_effect * n / (n + k)

with ``k = 4`` for ``topic_affinity`` and ``k = 5`` for every other
kind (the roadmap's own constants: k=4 topic clusters, k=5
hooks/packaging).  Rationale: ``k`` is the number of "phantom"
baseline videos the prior is worth.  With n=1, a group keeps only
1/6 (or 1/5) of its raw lift — one spectacular video moves a rule
modestly; the claimed effect grows toward the raw effect only as
real evidence accumulates.  Small samples therefore *cannot*
produce large claimed effects: ``effect_estimate`` stored on a rule
is always the shrunk value, never the raw one.

Confidence::

    agreement  = fraction of group videos whose outcome is on the
                 same side of the baseline as the group mean
                 (outcome == baseline counts as agreeing)
    confidence = (n / (n + k)) * agreement          in [0, 1]

Minimum evidence: ``evidence_count >= 3`` (and >= 2 distinct topics
for non-topic rules, mirroring the roadmap's pacing rule so one
topic repeated cannot masquerade as a pattern) is required before a
rule may be ``active``; below that it stays ``candidate`` and is
labelled ``thin_evidence=True`` in ``created_from`` and in the
batch report.  The n>=3 threshold is the UNIFIED activation policy
(Round 5 closure, R5-005 / R5L-004): ``MIN_EVIDENCE`` is the single
shared constant owned by ``app.learning.schema``
(``MIN_EVIDENCE_FOR_ACTIVATION``), and the Learning Store enforces
the same minimum inside ``activate_rule`` itself, so the gate holds
even for callers that bypass this engine.  (The store's separate
n<5 ``is_thin_evidence`` label is a display band only and never
blocks an n=3–4 activation.)

Versioning: rules are never mutated.  A re-run whose snapshot-set
(the sorted video ids + snapshot point in ``created_from``) is
identical to the latest rule's is an idempotent no-op.  A different
snapshot-set creates a new version via ``store.supersede`` **only**
when the change is material:

* ``|new_shrunk_effect - old_effect_estimate| >= 0.05`` (5 points), or
* the direction (positive/negative) flips, or
* activation eligibility changes (evidence crosses the gate).

Otherwise the existing rule stands and the batch report says why.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.learning_loop.contracts import (
    MIN_EVIDENCE,
    RULE_KINDS,
    SNAPSHOT_POINTS,
    AnalyticsSource,
    LearningRuleStore,
    VideoEvidence,
)

__all__ = [
    "UpdateEngine",
    "BatchReport",
    "VideoEvaluation",
    "RuleOutcome",
    "SHRINKAGE_K",
    "MATERIAL_EFFECT_DELTA",
    "MIN_DISTINCT_TOPICS",
    "duration_band_for",
    "publish_window_for",
    "statement_family_for",
]

#: Empirical-Bayes shrinkage constants (see module docstring).
SHRINKAGE_K: dict[str, float] = {
    "topic_affinity": 4.0,
    "hook_pattern": 5.0,
    "duration_band": 5.0,
    "title_pattern": 5.0,
    "thumbnail_style": 5.0,
    "publish_window": 5.0,
}

#: Minimum |change in shrunk effect| that counts as material.
MATERIAL_EFFECT_DELTA = 0.05

#: Non-topic rules also need this many distinct topics (anti one-topic
#: masquerade; topic_affinity rules are *about* one topic, so exempt).
MIN_DISTINCT_TOPICS = 2


def duration_band_for(duration_sec: Optional[float]) -> Optional[str]:
    """Bucket a duration into a rule attribute, or ``None``."""
    if duration_sec is None or duration_sec <= 0:
        return None
    if duration_sec < 480:
        return "under_8m"
    if duration_sec < 720:
        return "8_12m"
    if duration_sec < 1200:
        return "12_20m"
    return "over_20m"


def publish_window_for(published_at_ms: Optional[int]) -> Optional[str]:
    """Bucket a publish timestamp (UTC hour) into a window label."""
    if published_at_ms is None:
        return None
    hour = (int(published_at_ms) // 3_600_000) % 24
    if hour < 6:
        return "00_06_utc"
    if hour < 12:
        return "06_12_utc"
    if hour < 18:
        return "12_18_utc"
    return "18_24_utc"


def _normalise(value: str) -> str:
    return " ".join(str(value).strip().lower().split())


def statement_family_for(kind: str, attribute: str, direction: str) -> str:
    """Stable dedupe identity of a rule family within a scope.

    Dedupe key (with scope) = ``(channel_id, niche, kind,
    statement_family)``; the snapshot-set lives in ``created_from``
    and decides idempotency vs. supersession.
    """
    return f"{kind}:{_normalise(attribute)}:{direction}"


@dataclass
class VideoEvaluation:
    video_id: str
    channel_id: str
    niche: str
    snapshot_point: str
    outcome: float
    band_verdict: str  # below_band / within_band / above_band / no_band_recorded
    baseline: Optional[float]
    vs_baseline: Optional[float]  # relative, e.g. 0.42 = +42% vs baseline


@dataclass
class RuleOutcome:
    channel_id: str
    niche: str
    kind: str
    statement_family: str
    action: str  # created_candidate / created_active / superseded_candidate /
    #              superseded_active / unchanged_idempotent / unchanged_not_material
    rule_id: Optional[str] = None
    version: Optional[int] = None
    status: Optional[str] = None
    evidence_count: int = 0
    thin_evidence: bool = True
    raw_effect: float = 0.0
    effect_estimate: float = 0.0
    confidence: float = 0.0
    reason: str = ""


@dataclass
class BatchReport:
    snapshot_point: str
    videos_evaluated: list[VideoEvaluation] = field(default_factory=list)
    rule_outcomes: list[RuleOutcome] = field(default_factory=list)
    scopes: list[tuple[str, str]] = field(default_factory=list)

    @property
    def rules_created(self) -> int:
        return sum(
            1
            for o in self.rule_outcomes
            if o.action.startswith(("created", "superseded"))
        )


def _attributes(video: VideoEvidence) -> Iterable[tuple[str, str]]:
    """(kind, attribute) pairs this video is evidence for."""
    if video.topic:
        yield "topic_affinity", _normalise(video.topic)
    if video.hook_type:
        yield "hook_pattern", _normalise(video.hook_type)
    band = duration_band_for(video.duration_sec)
    if band:
        yield "duration_band", band
    if video.title_pattern:
        yield "title_pattern", _normalise(video.title_pattern)
    if video.thumbnail_style:
        yield "thumbnail_style", _normalise(video.thumbnail_style)
    window = publish_window_for(video.published_at_ms)
    if window:
        yield "publish_window", window


class UpdateEngine:
    """Batch learner.  Constructed with a Learning Store (contract in
    :mod:`app.learning_loop.contracts`); optionally an analytics
    source for :meth:`run_from_source`."""

    def __init__(
        self,
        store: LearningRuleStore,
        source: Optional[AnalyticsSource] = None,
    ) -> None:
        self._store = store
        self._source = source

    # ------------------------------------------------------------------
    def run_from_source(
        self, snapshot_point: str, *, channel_id: Optional[str] = None
    ) -> BatchReport:
        if self._source is None:
            raise RuntimeError("UpdateEngine has no analytics source configured")
        return self.run_batch(
            snapshot_point, self._source.fetch(snapshot_point, channel_id=channel_id)
        )

    # ------------------------------------------------------------------
    def run_batch(
        self, snapshot_point: str, videos: Iterable[VideoEvidence]
    ) -> BatchReport:
        if snapshot_point not in SNAPSHOT_POINTS:
            raise ValueError(
                f"snapshot_point must be one of {SNAPSHOT_POINTS}, "
                f"got {snapshot_point!r}"
            )
        batch = sorted(
            (v for v in videos if v.snapshot_point == snapshot_point),
            key=lambda v: (v.channel_id, v.niche, v.video_id),
        )
        report = BatchReport(snapshot_point=snapshot_point)

        # Group strictly by (channel_id, niche): baselines and effects
        # are computed inside one scope, never across scopes.
        scopes: dict[tuple[str, str], list[VideoEvidence]] = {}
        for video in batch:
            scopes.setdefault((video.channel_id, video.niche), []).append(video)
        report.scopes = sorted(scopes)

        for (channel_id, niche), scoped in sorted(scopes.items()):
            baseline = float(statistics.median(v.outcome for v in scoped))
            for video in scoped:
                report.videos_evaluated.append(
                    self._evaluate(video, baseline)
                )
            report.rule_outcomes.extend(
                self._learn_scope(snapshot_point, channel_id, niche, scoped, baseline)
            )
        report.rule_outcomes.sort(
            key=lambda o: (o.channel_id, o.niche, o.statement_family)
        )
        return report

    # ------------------------------------------------------------------
    @staticmethod
    def _evaluate(video: VideoEvidence, baseline: float) -> VideoEvaluation:
        band = video.expectation_band
        if band is None or band.metric != "views":
            verdict = "no_band_recorded"
        else:
            verdict = band.verdict(video.outcome)
        vs = None
        if baseline > 0:
            vs = (video.outcome - baseline) / baseline
        return VideoEvaluation(
            video_id=video.video_id,
            channel_id=video.channel_id,
            niche=video.niche,
            snapshot_point=video.snapshot_point,
            outcome=video.outcome,
            band_verdict=verdict,
            baseline=baseline,
            vs_baseline=vs,
        )

    # ------------------------------------------------------------------
    def _learn_scope(
        self,
        snapshot_point: str,
        channel_id: str,
        niche: str,
        scoped: list[VideoEvidence],
        baseline: float,
    ) -> list[RuleOutcome]:
        groups: dict[tuple[str, str], list[VideoEvidence]] = {}
        for video in scoped:
            for kind, attribute in _attributes(video):
                groups.setdefault((kind, attribute), []).append(video)

        outcomes: list[RuleOutcome] = []
        for (kind, attribute), members in sorted(groups.items()):
            outcomes.append(
                self._learn_group(
                    snapshot_point, channel_id, niche, kind, attribute,
                    members, baseline,
                )
            )
        return outcomes

    # ------------------------------------------------------------------
    def _learn_group(
        self,
        snapshot_point: str,
        channel_id: str,
        niche: str,
        kind: str,
        attribute: str,
        members: list[VideoEvidence],
        baseline: float,
    ) -> RuleOutcome:
        members = sorted(members, key=lambda v: v.video_id)
        n = len(members)
        k = SHRINKAGE_K[kind]
        mean_outcome = statistics.fmean(v.outcome for v in members)
        raw_effect = (mean_outcome - baseline) / baseline if baseline > 0 else 0.0
        shrunk_effect = raw_effect * n / (n + k)
        direction = "positive" if shrunk_effect >= 0 else "negative"
        family = statement_family_for(kind, attribute, direction)

        if baseline > 0:
            agreeing = sum(
                1
                for v in members
                if (v.outcome - baseline) * (mean_outcome - baseline) >= 0
            )
        else:
            agreeing = n
        agreement = agreeing / n if n else 0.0
        confidence = round((n / (n + k)) * agreement, 4)

        distinct_topics = len(
            {_normalise(v.topic) for v in members if v.topic}
        )
        eligible = n >= MIN_EVIDENCE and (
            kind == "topic_affinity" or distinct_topics >= MIN_DISTINCT_TOPICS
        )
        thin = not eligible

        snapshot_set = sorted(v.video_id for v in members)
        created_from: dict[str, Any] = {
            "snapshot_point": snapshot_point,
            "snapshot_set": snapshot_set,
            "video_ids": snapshot_set,
            "baseline_views_median": baseline,
            "group_mean_views": mean_outcome,
            "raw_effect": round(raw_effect, 6),
            "shrinkage_k": k,
            "shrinkage_formula": "shrunk = raw * n/(n+k)",
            "distinct_topics": distinct_topics,
            "thin_evidence": thin,
            "band_verdicts": {
                v.video_id: (
                    v.expectation_band.verdict(v.outcome)
                    if v.expectation_band is not None
                    and v.expectation_band.metric == "views"
                    else "no_band_recorded"
                )
                for v in members
            },
            "engine": "learning_loop.update_engine.v1",
        }
        pct = shrunk_effect * 100
        verb = "outperforms" if direction == "positive" else "underperforms"
        statement = (
            f"{kind} '{attribute}' {verb} the {niche} channel baseline "
            f"by {abs(pct):.1f}% (shrunk, n={n})"
        )
        fields: dict[str, Any] = {
            "channel_id": channel_id,
            "niche": niche,
            "kind": kind,
            "statement_family": family,
            "statement": statement,
            "effect_estimate": round(shrunk_effect, 6),
            "evidence_count": n,
            "confidence": confidence,
            "created_from": created_from,
        }

        outcome = RuleOutcome(
            channel_id=channel_id, niche=niche, kind=kind,
            statement_family=family, action="",
            evidence_count=n, thin_evidence=thin,
            raw_effect=round(raw_effect, 6),
            effect_estimate=round(shrunk_effect, 6),
            confidence=confidence,
        )

        latest = self._store.get_latest(channel_id, niche, kind, family)
        if latest is None:
            # Direction flip: the opposite-direction family for this
            # attribute is the rule this evidence contradicts.  A flip
            # is material by definition — supersede that rule (the new
            # rule carries the new family) rather than leaving two
            # opposing active rules for one attribute.
            opposite = statement_family_for(
                kind, attribute,
                "negative" if direction == "positive" else "positive",
            )
            flipped = self._store.get_latest(channel_id, niche, kind, opposite)
            if flipped is not None:
                rule = self._store.supersede(str(flipped["id"]), fields)
                if eligible:
                    rule = self._store.activate(str(rule["id"]))
                outcome.action = (
                    "superseded_active" if eligible else "superseded_candidate"
                )
                outcome.rule_id = rule.get("id")
                outcome.version = rule.get("version")
                outcome.status = rule.get("status")
                outcome.reason = "direction flip supersedes opposite rule"
                return outcome
        if latest is not None:
            prev_from = latest.get("created_from") or {}
            if (
                prev_from.get("snapshot_point") == snapshot_point
                and list(prev_from.get("snapshot_set") or []) == snapshot_set
            ):
                outcome.action = "unchanged_idempotent"
                outcome.rule_id = latest.get("id")
                outcome.version = latest.get("version")
                outcome.status = latest.get("status")
                outcome.reason = "identical snapshot-set already learned"
                return outcome
            prev_effect = float(latest.get("effect_estimate") or 0.0)
            prev_eligible = not bool(
                prev_from.get(
                    "thin_evidence",
                    int(latest.get("evidence_count") or 0) < MIN_EVIDENCE,
                )
            )
            material = (
                abs(shrunk_effect - prev_effect) >= MATERIAL_EFFECT_DELTA
                or eligible != prev_eligible
            )
            if not material:
                outcome.action = "unchanged_not_material"
                outcome.rule_id = latest.get("id")
                outcome.version = latest.get("version")
                outcome.status = latest.get("status")
                outcome.reason = (
                    f"effect change {abs(shrunk_effect - prev_effect):.4f} "
                    f"< {MATERIAL_EFFECT_DELTA} and eligibility unchanged"
                )
                return outcome
            rule = self._store.supersede(str(latest["id"]), fields)
            action_prefix = "superseded"
        else:
            rule = self._store.create_candidate(fields)
            action_prefix = "created"

        if eligible:
            rule = self._store.activate(str(rule["id"]))
            outcome.action = f"{action_prefix}_active"
        else:
            outcome.action = f"{action_prefix}_candidate"
        outcome.rule_id = rule.get("id")
        outcome.version = rule.get("version")
        outcome.status = rule.get("status")
        outcome.reason = (
            "evidence gate passed (n>=3)"
            if eligible
            else f"thin evidence: n={n} (needs >={MIN_EVIDENCE}"
            + (
                f" and >={MIN_DISTINCT_TOPICS} distinct topics)"
                if kind != "topic_affinity"
                else ")"
            )
        )
        return outcome
