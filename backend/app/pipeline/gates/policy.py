"""Policy gates: cross-video similarity and source synthesis.

Implements the variation/similarity interface from roadmap section 13
(inauthentic-content safeguard).  The current video's fingerprints are
compared against the channel's last 20 published videos on multiple axes:

* script text (word n-gram / shingle Jaccard overlap),
* structure template id,
* hook formula id,
* thumbnail template id,
* title formula id.

Verdict rules (thresholds are named constants -- internal engineering
defaults, tunable per channel, deliberately conservative; YouTube
publishes no numeric thresholds):

* ``PASS``  -- no axis elevated.
* ``WARN``  -- one axis elevated; auto-fixable once (rewrite the hook,
  rotate the structure, swap the template), then re-check.
* ``BLOCK`` -- a hard rule fired: a near-duplicate script, high mean
  script overlap, an identical structure in >= 3 of the last 5 videos
  (rotation required), or a combined packaging pattern (hook + thumbnail
  + title sameness across the last 5).

Also included: the ">= 3-source synthesis" helper -- given the
fact-ledger source distribution for a video, BLOCK if any single source
supplied > 40% of factual claims (single-source paraphrase fails the
reused-content safeguard), or if fewer than 3 independent sources
contributed at all.

``previous`` fingerprints must be ordered NEWEST FIRST; the "last 5" is
``previous[:5]`` and the comparison window is ``previous[:20]``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from .tokens import (
    GATE_POLICY_SIMILARITY,
    GATE_POLICY_SYNTHESIS,
    GateToken,
    Verdict,
    issue_token,
)

__all__ = [
    "VideoFingerprint",
    "PerVideoSimilarity",
    "SimilarityReport",
    "SynthesisReport",
    "word_shingles",
    "jaccard",
    "script_similarity",
    "check_similarity",
    "check_source_synthesis",
    "NGRAM_SIZE",
    "COMPARISON_WINDOW",
    "RECENT_WINDOW",
    "SCRIPT_NEAR_DUPLICATE_THRESHOLD",
    "SCRIPT_MEAN_BLOCK_THRESHOLD",
    "SCRIPT_MEAN_WARN_THRESHOLD",
    "STRUCTURE_BLOCK_COUNT",
    "PACKAGING_AXIS_WARN_COUNT",
    "PACKAGING_AXES_BLOCK",
    "MAX_SINGLE_SOURCE_SHARE",
    "MIN_INDEPENDENT_SOURCES",
]

# ---------------------------------------------------------------------------
# Thresholds (named constants; policy config in production, per section 13)
# ---------------------------------------------------------------------------

NGRAM_SIZE = 5
COMPARISON_WINDOW = 20
RECENT_WINDOW = 5

# Script axis (Jaccard over word 5-gram shingles).
SCRIPT_NEAR_DUPLICATE_THRESHOLD = 0.60  # vs any single prior video -> BLOCK
SCRIPT_MEAN_BLOCK_THRESHOLD = 0.30  # mean vs window -> BLOCK
SCRIPT_MEAN_WARN_THRESHOLD = 0.15  # mean vs window -> WARN

# Structure axis: identical structure in >= 3 of last 5 -> BLOCK / rotate.
STRUCTURE_BLOCK_COUNT = 3
# Packaging axes (hook / thumbnail / title): same id in >= 3 of last 5
# elevates that axis; >= 2 elevated packaging axes -> BLOCK (combined
# pattern), exactly 1 -> WARN.
PACKAGING_AXIS_WARN_COUNT = 3
PACKAGING_AXES_BLOCK = 2

# Source synthesis (section 13, reused-content safeguard).
MAX_SINGLE_SOURCE_SHARE = 0.40  # strictly greater than this -> BLOCK
MIN_INDEPENDENT_SOURCES = 3


# ---------------------------------------------------------------------------
# Fingerprints and script similarity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VideoFingerprint:
    """The comparison fingerprint of one video (current or published)."""

    video_id: str
    script_text: str = ""
    structure_template_id: Optional[str] = None
    hook_formula_id: Optional[str] = None
    thumbnail_template_id: Optional[str] = None
    title_formula_id: Optional[str] = None


_WORD_RE = re.compile(r"[a-z0-9']+")


def _words(text: str) -> list:
    return _WORD_RE.findall(text.lower())


def word_shingles(text: str, n: int = NGRAM_SIZE) -> frozenset:
    """Set of consecutive word n-grams.  Falls back to the word set when
    the text has fewer than ``n`` words (very short scripts)."""
    words = _words(text)
    if len(words) < n:
        return frozenset((w,) for w in words)
    return frozenset(tuple(words[i : i + n]) for i in range(len(words) - n + 1))


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def script_similarity(text_a: str, text_b: str, n: int = NGRAM_SIZE) -> float:
    """Jaccard overlap of word n-gram shingles between two scripts."""
    return jaccard(word_shingles(text_a, n), word_shingles(text_b, n))


# ---------------------------------------------------------------------------
# Similarity report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PerVideoSimilarity:
    video_id: str
    script_score: float
    same_structure: bool
    same_hook_formula: bool
    same_thumbnail_template: bool
    same_title_formula: bool


@dataclass(frozen=True)
class SimilarityReport:
    verdict: Verdict
    reason_codes: tuple
    per_video: tuple  # tuple[PerVideoSimilarity, ...], newest first
    mean_script_score: float
    max_script_score: float
    most_similar_video_id: Optional[str]
    recent_counts: Mapping[str, int]  # axis -> matches within last RECENT_WINDOW
    auto_fixable: bool  # True only for WARN (fix once, then re-check)

    def to_token(self, manifest_hash: str, **kwargs) -> GateToken:
        return issue_token(
            GATE_POLICY_SIMILARITY,
            self.verdict,
            manifest_hash,
            reason_codes=self.reason_codes,
            evidence={
                "mean_script_score": round(self.mean_script_score, 4),
                "max_script_score": round(self.max_script_score, 4),
                "most_similar_video_id": self.most_similar_video_id,
                "recent_counts": dict(self.recent_counts),
                "auto_fixable": self.auto_fixable,
                "per_video": [
                    {
                        "video_id": p.video_id,
                        "script_score": round(p.script_score, 4),
                        "same_structure": p.same_structure,
                        "same_hook_formula": p.same_hook_formula,
                        "same_thumbnail_template": p.same_thumbnail_template,
                        "same_title_formula": p.same_title_formula,
                    }
                    for p in self.per_video
                ],
            },
            **kwargs,
        )


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return a is not None and b is not None and a == b


def check_similarity(
    current: VideoFingerprint,
    previous: Sequence[VideoFingerprint],
    *,
    window: int = COMPARISON_WINDOW,
    recent_window: int = RECENT_WINDOW,
) -> SimilarityReport:
    """Compare ``current`` against the channel's recent videos.

    ``previous`` must be ordered newest-first.
    """
    considered = list(previous)[:window]
    recent = considered[:recent_window]

    per_video = tuple(
        PerVideoSimilarity(
            video_id=p.video_id,
            script_score=script_similarity(current.script_text, p.script_text),
            same_structure=_same(current.structure_template_id, p.structure_template_id),
            same_hook_formula=_same(current.hook_formula_id, p.hook_formula_id),
            same_thumbnail_template=_same(
                current.thumbnail_template_id, p.thumbnail_template_id
            ),
            same_title_formula=_same(current.title_formula_id, p.title_formula_id),
        )
        for p in considered
    )

    scores = [p.script_score for p in per_video]
    max_score = max(scores) if scores else 0.0
    mean_score = sum(scores) / len(scores) if scores else 0.0
    most_similar_id = (
        max(per_video, key=lambda p: p.script_score).video_id if per_video else None
    )

    recent_counts = {
        "structure": sum(
            1 for p in recent if _same(current.structure_template_id, p.structure_template_id)
        ),
        "hook_formula": sum(
            1 for p in recent if _same(current.hook_formula_id, p.hook_formula_id)
        ),
        "thumbnail_template": sum(
            1
            for p in recent
            if _same(current.thumbnail_template_id, p.thumbnail_template_id)
        ),
        "title_formula": sum(
            1 for p in recent if _same(current.title_formula_id, p.title_formula_id)
        ),
    }

    reason_codes: list = []
    verdict = Verdict.PASS

    def escalate(new: Verdict, code: str) -> None:
        nonlocal verdict
        reason_codes.append(code)
        if new.severity > verdict.severity:
            verdict = new

    # -- script axis ------------------------------------------------------
    if max_score >= SCRIPT_NEAR_DUPLICATE_THRESHOLD:
        escalate(Verdict.BLOCK, "SCRIPT_NEAR_DUPLICATE")
    if mean_score >= SCRIPT_MEAN_BLOCK_THRESHOLD:
        escalate(Verdict.BLOCK, "SCRIPT_MEAN_SIMILARITY_HIGH")
    elif mean_score >= SCRIPT_MEAN_WARN_THRESHOLD:
        escalate(Verdict.WARN, "SCRIPT_MEAN_SIMILARITY_ELEVATED")

    # -- structure axis ---------------------------------------------------
    if recent_counts["structure"] >= STRUCTURE_BLOCK_COUNT:
        escalate(Verdict.BLOCK, "STRUCTURE_ROTATION_REQUIRED")
    elif recent and _same(
        current.structure_template_id, considered[0].structure_template_id
    ):
        escalate(Verdict.WARN, "STRUCTURE_CONSECUTIVE_REPEAT")

    # -- packaging axes (hook / thumbnail / title) ------------------------
    elevated_axes = [
        axis
        for axis in ("hook_formula", "thumbnail_template", "title_formula")
        if recent_counts[axis] >= PACKAGING_AXIS_WARN_COUNT
    ]
    if len(elevated_axes) >= PACKAGING_AXES_BLOCK:
        escalate(Verdict.BLOCK, "COMBINED_PACKAGING_PATTERN")
    elif elevated_axes:
        escalate(Verdict.WARN, "PACKAGING_AXIS_REPEATED")

    return SimilarityReport(
        verdict=verdict,
        reason_codes=tuple(reason_codes),
        per_video=per_video,
        mean_script_score=mean_score,
        max_script_score=max_score,
        most_similar_video_id=most_similar_id,
        recent_counts=recent_counts,
        auto_fixable=verdict is Verdict.WARN,
    )


# ---------------------------------------------------------------------------
# >= 3-source synthesis check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SynthesisReport:
    verdict: Verdict
    reason_codes: tuple
    total_claims: int
    source_shares: Mapping[str, float]
    dominant_source: Optional[str]
    dominant_share: float

    def to_token(self, manifest_hash: str, **kwargs) -> GateToken:
        return issue_token(
            GATE_POLICY_SYNTHESIS,
            self.verdict,
            manifest_hash,
            reason_codes=self.reason_codes,
            evidence={
                "total_claims": self.total_claims,
                "source_shares": {
                    k: round(v, 4) for k, v in sorted(self.source_shares.items())
                },
                "dominant_source": self.dominant_source,
                "dominant_share": round(self.dominant_share, 4),
            },
            **kwargs,
        )


def check_source_synthesis(
    claim_counts: Mapping[str, int],
    *,
    max_single_source_share: float = MAX_SINGLE_SOURCE_SHARE,
    min_sources: int = MIN_INDEPENDENT_SOURCES,
) -> SynthesisReport:
    """BLOCK if one source supplied > ``max_single_source_share`` of the
    video's factual claims, or fewer than ``min_sources`` independent
    sources contributed.  An empty fact ledger fails closed (BLOCK)."""
    counts = {src: int(n) for src, n in claim_counts.items() if int(n) > 0}
    total = sum(counts.values())
    shares = {src: n / total for src, n in counts.items()} if total else {}
    dominant_source = max(shares, key=shares.get) if shares else None
    dominant_share = shares[dominant_source] if dominant_source else 0.0

    reason_codes: list = []
    if total == 0:
        reason_codes.append("EMPTY_FACT_LEDGER")
    else:
        if dominant_share > max_single_source_share:
            reason_codes.append("SINGLE_SOURCE_DOMINANT")
        if len(counts) < min_sources:
            reason_codes.append("INSUFFICIENT_INDEPENDENT_SOURCES")

    return SynthesisReport(
        verdict=Verdict.BLOCK if reason_codes else Verdict.PASS,
        reason_codes=tuple(reason_codes),
        total_claims=total,
        source_shares=shares,
        dominant_source=dominant_source,
        dominant_share=dominant_share,
    )
