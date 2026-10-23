"""Deterministic Fact-Check ledger service (Agent 9 support).

This module contains NO LLM calls. It defines the schemas and the
deterministic rule engine that Agent 9's (LLM-assisted) claim extraction
and source retrieval outputs are validated against.

Spec source: roadmap section 33 (Scripting Workflow):

* Atomic claims are typed (statistic, date, name/entity, quote, causal,
  religious-reference, historical-event, product/version).
* Every claim becomes a row in a permanent Fact Ledger.
* Tiered source policy:
    - Tier 1: primary / official (original study, official record,
      scripture text with verified translation, .gov/.edu, official
      announcements)
    - Tier 2: reputable secondary
    - Tier 3: general web -- NEVER sufficient alone for a load-bearing
      claim.
* Two-source rule: a load-bearing claim requires TWO INDEPENDENT
  confirmations, at least one of Tier 1 or 2. Sources copying each
  other (same ``url_or_name``) count as ONE independent source.
* Traffic-light verdicts:
    - GREEN  -- multi-source, high authority -> passes automatically.
    - YELLOW -- single source, ambiguity, or only Tier-3 sources ->
      soften wording or flag for review.
    - RED    -- unverifiable (zero sources for a load-bearing claim),
      contradicted, or citation cannot be located -> BLOCKED. A script
      with ANY RED claim cannot reach Gate A.
* Originality (anti-reused-content): a video needs >= 3 independent
  sources in total, and no single source may supply more than ~40% of
  the video's factual claims (measured from the ledger's source
  distribution).
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

__all__ = [
    "Claim",
    "ClaimType",
    "FactLedger",
    "SourceRef",
    "Verdict",
    "evaluate_claim",
    "extract_claim_candidates",
]


class ClaimType(StrEnum):
    """Atomic claim types from roadmap section 33."""

    STATISTIC = "statistic"
    DATE = "date"
    NAME_ENTITY = "name_entity"
    QUOTE = "quote"
    CAUSAL = "causal"
    RELIGIOUS_REFERENCE = "religious_reference"
    HISTORICAL_EVENT = "historical_event"
    PRODUCT_VERSION = "product_version"


class Verdict(StrEnum):
    """Traffic-light fact-check verdicts."""

    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


class SourceRef(BaseModel):
    """A single source citation attached to a claim."""

    url_or_name: str
    tier: Literal[1, 2, 3]
    retrieved_at: datetime | None = None


class Claim(BaseModel):
    """One atomic claim in the Fact Ledger.

    ``contradicted`` marks a claim that a retrieved source directly
    contradicts. For backwards compatibility with callers that signal
    contradiction via a negative confidence, :func:`evaluate_claim`
    also treats ``confidence < 0`` as contradicted (and normalises it
    into ``contradicted=True`` on the returned claim).
    """

    claim_id: str
    text: str
    claim_type: ClaimType
    script_location: str  # chapter / section id, e.g. "ch03"
    load_bearing: bool
    sources: list[SourceRef] = Field(default_factory=list)
    verdict: Verdict | None = None
    confidence: float = 0.0
    rationale: str = ""
    contradicted: bool = False


def _independent_source_names(claim: Claim) -> set[str]:
    """Distinct source identities (normalised); copies count as one."""
    return {s.url_or_name.strip().lower() for s in claim.sources if s.url_or_name.strip()}


def _best_tier(claim: Claim) -> int | None:
    if not claim.sources:
        return None
    return min(s.tier for s in claim.sources)


def evaluate_claim(claim: Claim) -> Claim:
    """Apply the deterministic verdict rules to ``claim``.

    Returns a NEW Claim (the input is not mutated) with ``verdict``,
    ``confidence`` and ``rationale`` set.

    Rules:
      * Contradicted (``contradicted=True`` or ``confidence < 0``)
        -> RED, regardless of source count.
      * Load-bearing:
          - zero sources -> RED
          - >= 2 independent sources AND >= 1 source of tier 1 or 2
            -> GREEN
          - exactly one independent source, or only tier-3 sources
            -> YELLOW
      * Non-load-bearing:
          - >= 1 source of tier <= 2 -> GREEN
          - only tier-3 sources -> YELLOW
          - zero sources -> YELLOW
    """
    contradicted = claim.contradicted or claim.confidence < 0

    if contradicted:
        return claim.model_copy(
            update={
                "verdict": Verdict.RED,
                "confidence": 0.0,
                "contradicted": True,
                "rationale": "Contradicted by a retrieved source; claim must be cut or rewritten.",
            }
        )

    independent = _independent_source_names(claim)
    n_independent = len(independent)
    n_sources = len(claim.sources)
    best_tier = _best_tier(claim)
    has_tier_1_or_2 = best_tier is not None and best_tier <= 2

    if claim.load_bearing:
        if n_sources == 0:
            return claim.model_copy(
                update={
                    "verdict": Verdict.RED,
                    "confidence": 0.0,
                    "rationale": "Load-bearing claim with zero sources; unverifiable and blocked at Gate A.",
                }
            )
        if n_independent >= 2 and has_tier_1_or_2:
            confidence = 0.95 if best_tier == 1 else 0.90
            return claim.model_copy(
                update={
                    "verdict": Verdict.GREEN,
                    "confidence": confidence,
                    "rationale": (
                        f"Two-source rule satisfied: {n_independent} independent sources, "
                        f"best tier {best_tier}."
                    ),
                }
            )
        # Exactly one (independent) source, or only tier-3 sources.
        if n_independent <= 1:
            rationale = "Load-bearing claim has only one independent source; two-source rule not met."
        else:
            rationale = "Load-bearing claim is supported only by tier-3 sources; tier 3 alone is never sufficient."
        return claim.model_copy(
            update={
                "verdict": Verdict.YELLOW,
                "confidence": 0.5,
                "rationale": rationale,
            }
        )

    # Non-load-bearing claims.
    if n_sources == 0:
        return claim.model_copy(
            update={
                "verdict": Verdict.YELLOW,
                "confidence": 0.5,
                "rationale": "Non-load-bearing claim with no source; soften wording or add a source.",
            }
        )
    if has_tier_1_or_2:
        confidence = 0.90 if best_tier == 1 else 0.85
        return claim.model_copy(
            update={
                "verdict": Verdict.GREEN,
                "confidence": confidence,
                "rationale": f"Supported by a tier-{best_tier} source.",
            }
        )
    return claim.model_copy(
        update={
            "verdict": Verdict.YELLOW,
            "confidence": 0.5,
            "rationale": "Supported only by tier-3 source(s); single-source / low-authority ambiguity.",
        }
    )


def _effective_verdict(claim: Claim) -> Verdict:
    """Stored verdict if present, otherwise the deterministic verdict."""
    if claim.verdict is not None:
        return claim.verdict
    return evaluate_claim(claim).verdict


class FactLedger(BaseModel):
    """Permanent per-project ledger of claims and their verdicts."""

    project_id: str
    claims: list[Claim] = Field(default_factory=list)

    def red_count(self) -> int:
        """Number of claims whose (effective) verdict is RED."""
        return sum(1 for c in self.claims if _effective_verdict(c) is Verdict.RED)

    def yellow_claims(self) -> list[Claim]:
        """All claims whose (effective) verdict is YELLOW."""
        return [c for c in self.claims if _effective_verdict(c) is Verdict.YELLOW]

    def source_distribution(self) -> dict[str, float]:
        """Share of claims supplied by each source.

        Returns ``{source url_or_name: share}`` where share is the
        fraction of ledger claims citing that source (a claim citing a
        source twice counts once for that source). Empty ledger -> {}.
        Used for the <= 40%-from-one-source originality rule.
        """
        if not self.claims:
            return {}
        counts: dict[str, int] = {}
        for claim in self.claims:
            for name in {s.url_or_name for s in claim.sources}:
                counts[name] = counts.get(name, 0) + 1
        total = len(self.claims)
        return {name: count / total for name, count in counts.items()}

    def gate_a_blockers(self) -> list[str]:
        """Human-readable blocker strings; any RED claim blocks Gate A."""
        blockers: list[str] = []
        for claim in self.claims:
            if _effective_verdict(claim) is Verdict.RED:
                evaluated = claim if claim.verdict is not None else evaluate_claim(claim)
                reason = evaluated.rationale or "RED verdict"
                blockers.append(
                    f"RED claim {claim.claim_id} at {claim.script_location}: "
                    f"{claim.text!r} -- {reason}"
                )
        return blockers

    def originality_violations(self) -> list[str]:
        """Violations of the multi-source synthesis rules.

        * A single source supplying > 40% of claims.
        * Fewer than 3 independent sources in total.
        """
        violations: list[str] = []
        for source, share in self.source_distribution().items():
            if share > 0.40:
                violations.append(
                    f"Source {source!r} supplies {share:.0%} of claims, exceeding the 40% limit."
                )
        independent_total = len(
            {s.url_or_name.strip().lower() for c in self.claims for s in c.sources if s.url_or_name.strip()}
        )
        if independent_total < 3:
            violations.append(
                f"Only {independent_total} independent source(s) in ledger; minimum is 3."
            )
        return violations


# ---------------------------------------------------------------------------
# Candidate extraction (regex only -- candidates, NOT verdicts)
# ---------------------------------------------------------------------------

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|"
    "October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)

# Order matters: earlier patterns win when spans overlap.
_CANDIDATE_PATTERNS: list[tuple[str, ClaimType, re.Pattern[str]]] = [
    (
        "quote",
        ClaimType.QUOTE,
        re.compile(r'"[^"\n]{2,}"|“[^”\n]{2,}”|\'[^\'\n]{2,}\''),
    ),
    (
        "full_date",
        ClaimType.DATE,
        re.compile(
            rf"\b(?:\d{{1,2}}\s+(?:{_MONTHS})\s+\d{{4}}|(?:{_MONTHS})\s+\d{{1,2}},?\s+\d{{4}})\b"
        ),
    ),
    (
        "iso_date",
        ClaimType.DATE,
        re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    ),
    (
        "percentage",
        ClaimType.STATISTIC,
        re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|percent\b)", re.IGNORECASE),
    ),
    (
        "year",
        ClaimType.DATE,
        re.compile(r"\b(?:1[0-9]{3}|20[0-9]{2})\b"),
    ),
    (
        "number",
        ClaimType.STATISTIC,
        re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+\.\d+\b"),
    ),
    (
        "version",
        ClaimType.PRODUCT_VERSION,
        re.compile(r"\bv\d+(?:\.\d+){1,3}\b|\bversion\s+\d+(?:\.\d+)*\b", re.IGNORECASE),
    ),
    (
        "proper_name",
        ClaimType.NAME_ENTITY,
        re.compile(r"\b[A-Z][a-z]+(?:\s+(?:[A-Z][a-z]+|[A-Z]\.)){1,3}\b"),
    ),
]


def extract_claim_candidates(script_text: str) -> list[dict]:
    """Extract raw claim CANDIDATES from script text via regex.

    IMPORTANT: these are candidates only, NOT verdicts. This function
    makes no judgement about truth. It merely surfaces spans that look
    like checkable assertions (numbers/percentages, years/dates, quoted
    strings, capitalised multi-word names, version strings) so the
    Agent 9 LLM pass can type, split and verify them into proper
    :class:`Claim` rows.

    Returns a list of dicts sorted by position, with overlapping spans
    de-duplicated (earliest pattern in ``_CANDIDATE_PATTERNS`` wins).
    Each dict has: ``candidate_text`` / ``text``, ``suggested_type``
    (a ClaimType value), ``pattern``, ``start`` and ``end`` offsets.
    """
    found: list[tuple[int, int, dict]] = []
    occupied: list[tuple[int, int]] = []

    for pattern_name, suggested_type, pattern in _CANDIDATE_PATTERNS:
        for match in pattern.finditer(script_text):
            start, end = match.start(), match.end()
            if any(start < occ_end and end > occ_start for occ_start, occ_end in occupied):
                continue
            occupied.append((start, end))
            text = match.group(0)
            found.append(
                (
                    start,
                    end,
                    {
                        "candidate_text": text,
                        "text": text,
                        "suggested_type": suggested_type.value,
                        "pattern": pattern_name,
                        "start": start,
                        "end": end,
                    },
                )
            )

    found.sort(key=lambda item: (item[0], item[1]))
    return [candidate for _, _, candidate in found]
