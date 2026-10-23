"""Copyright gates G1, G2 and G6 (roadmap section 16).

All three gates are deterministic joins over registry-style data -- no
network, no heuristics, no LLM.  Posture is DEFAULT-DENY: anything that
cannot be resolved to a complete record blocks publication, and the
offending items are listed so a human (or an automatic replacement job)
can fix exactly what failed.

Implemented here:

* **G1 Licence completeness** -- every rendered second of the final
  timeline must resolve to >= 1 usage segment whose asset has a complete
  licence record (licence id present, commercial use OK, monetised
  YouTube use OK, review approved/auto-cleared).
* **G2 Provenance scan** -- every asset must have provenance (provider +
  acquisition record/hash) from a Tier-1/Tier-2 source.  Unknown
  provenance and Tier-3 sources (label music, other platforms' clips,
  Google Images / Pinterest, watermarked previews, ...) BLOCK.
* **G6 Attribution diff** -- every ``attribution_required`` licence's
  attribution string must appear VERBATIM in the final description.

G3 (fingerprint), G4 (private-upload dry run) and G5 (human originality
review) live elsewhere -- see ``tokens.EXTERNAL_GATES``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from .tokens import (
    GATE_COPYRIGHT,
    GATE_COPYRIGHT_G1,
    GATE_COPYRIGHT_G2,
    GATE_COPYRIGHT_G6,
    GateToken,
    Verdict,
    issue_token,
    worst_verdict,
)

__all__ = [
    "LicenceRecord",
    "UsageSegment",
    "AssetProvenance",
    "G1Report",
    "G2Report",
    "G6Report",
    "CopyrightReport",
    "check_g1_licence_completeness",
    "check_g2_provenance",
    "check_g6_attribution",
    "run_copyright_gates",
    "TIER3_PROVIDERS",
    "PROHIBITED_PROVIDER_KEYWORDS",
    "APPROVED_REVIEW_STATUSES",
    "COVERAGE_EPSILON_S",
]

# A licence only counts toward G1 coverage when review has cleared it.
APPROVED_REVIEW_STATUSES = frozenset({"approved", "auto_cleared"})
# Timeline gaps smaller than this are float dust, not missing coverage.
COVERAGE_EPSILON_S = 0.005


# ---------------------------------------------------------------------------
# Input records (mirrors the section-16 registry tables)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LicenceRecord:
    """Machine-checkable rights layer for one asset."""

    asset_id: str
    licence_id: Optional[str]
    commercial_use_ok: bool
    monetized_youtube_ok: bool = True
    attribution_required: bool = False
    attribution_text: Optional[str] = None
    review_status: str = "approved"


@dataclass(frozen=True)
class UsageSegment:
    """One row of the timeline map (asset_usage).

    ``start_s``/``end_s`` are timeline seconds.  Non-timeline assets
    (thumbnail, font, template) pass ``None`` for both: they are still
    licence-checked by G1 but contribute no timeline coverage.
    """

    asset_id: str
    start_s: Optional[float] = None
    end_s: Optional[float] = None
    role: str = "b_roll"


@dataclass(frozen=True)
class AssetProvenance:
    """Where an asset came from and how that was evidenced."""

    asset_id: str
    provider: Optional[str]
    tier: Optional[int]  # 1 = cleared, 2 = conditional paid, 3 = prohibited
    acquisition_hash: Optional[str] = None  # hash of receipt / API response
    acquisition_record_id: Optional[str] = None
    watermark_detected: bool = False


# ---------------------------------------------------------------------------
# G1 -- licence completeness
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetLicenceProblem:
    asset_id: str
    reasons: tuple  # e.g. ("NO_LICENCE_RECORD", "COMMERCIAL_USE_NOT_OK")


@dataclass(frozen=True)
class G1Report:
    verdict: Verdict
    total_duration_s: float
    uncovered_intervals: tuple  # tuple[tuple[float, float], ...] (start, end)
    incomplete_assets: tuple  # tuple[AssetLicenceProblem, ...]
    reason_codes: tuple = ()

    @property
    def offending_seconds(self) -> float:
        return round(sum(end - start for start, end in self.uncovered_intervals), 3)

    def to_token(self, manifest_hash: str, **kwargs) -> GateToken:
        return issue_token(
            GATE_COPYRIGHT_G1,
            self.verdict,
            manifest_hash,
            reason_codes=self.reason_codes,
            evidence={
                "total_duration_s": self.total_duration_s,
                "offending_seconds": self.offending_seconds,
                "uncovered_intervals": [list(iv) for iv in self.uncovered_intervals],
                "incomplete_assets": [
                    {"asset_id": p.asset_id, "reasons": list(p.reasons)}
                    for p in self.incomplete_assets
                ],
            },
            **kwargs,
        )


def _licence_problems(record: Optional[LicenceRecord]) -> tuple:
    if record is None:
        return ("NO_LICENCE_RECORD",)
    reasons = []
    if not record.licence_id:
        reasons.append("LICENCE_ID_MISSING")
    if not record.commercial_use_ok:
        reasons.append("COMMERCIAL_USE_NOT_OK")
    if not record.monetized_youtube_ok:
        reasons.append("MONETIZED_YOUTUBE_USE_NOT_OK")
    if record.review_status not in APPROVED_REVIEW_STATUSES:
        reasons.append("REVIEW_NOT_APPROVED")
    return tuple(reasons)


def _uncovered(covered: list, total: float) -> tuple:
    """Sweep merged [start, end) intervals; return the gaps in [0, total)."""
    gaps = []
    cursor = 0.0
    for start, end in sorted(covered):
        start = max(0.0, start)
        end = min(total, end)
        if end <= start:
            continue
        if start - cursor > COVERAGE_EPSILON_S:
            gaps.append((round(cursor, 3), round(start, 3)))
        cursor = max(cursor, end)
    if total - cursor > COVERAGE_EPSILON_S:
        gaps.append((round(cursor, 3), round(total, 3)))
    return tuple(gaps)


def check_g1_licence_completeness(
    segments: Iterable[UsageSegment],
    licences: Mapping[str, LicenceRecord],
    total_duration_s: float,
) -> G1Report:
    """G1: every rendered second resolves to >= 1 complete licence record.

    Only segments whose asset's licence is complete count as coverage, so
    a badly licensed segment shows up twice: in ``incomplete_assets`` and
    as uncovered seconds -- exactly the "offending seconds listed"
    behaviour the roadmap requires.
    """
    problems: dict = {}
    covered: list = []
    for seg in segments:
        reasons = _licence_problems(licences.get(seg.asset_id))
        if reasons:
            problems.setdefault(seg.asset_id, reasons)
            continue
        if seg.start_s is not None and seg.end_s is not None and seg.end_s > seg.start_s:
            covered.append((float(seg.start_s), float(seg.end_s)))

    uncovered = _uncovered(covered, float(total_duration_s))
    incomplete = tuple(
        AssetLicenceProblem(asset_id=aid, reasons=rs)
        for aid, rs in sorted(problems.items())
    )
    reason_codes = []
    if incomplete:
        reason_codes.append("G1_LICENCE_INCOMPLETE")
    if uncovered:
        reason_codes.append("G1_TIMELINE_COVERAGE_GAP")
    verdict = Verdict.BLOCK if reason_codes else Verdict.PASS
    return G1Report(
        verdict=verdict,
        total_duration_s=float(total_duration_s),
        uncovered_intervals=uncovered,
        incomplete_assets=incomplete,
        reason_codes=tuple(reason_codes),
    )


# ---------------------------------------------------------------------------
# G2 -- provenance & contamination scan
# ---------------------------------------------------------------------------

# Normalised provider names that are Tier-3 (roadmap section 16).  Rows for
# these exist only to hard-block; there is no override.
TIER3_PROVIDERS = frozenset(
    {
        "record_label_music",
        "commercial_label_music",
        "youtube_clips",
        "youtube",
        "tiktok",
        "instagram",
        "google_images",
        "pinterest",
        "free_download_site",
        "no_copyright_youtube_channel",
        "watermarked_preview",
        "unknown",
    }
)

# Substrings that mark a provider name as Tier-3 even if not listed above.
# (Known-good provider names are matched by exact/tier checks first, so
# e.g. "youtube_audio_library" -- Tier 1 -- is never caught by "youtube".)
PROHIBITED_PROVIDER_KEYWORDS = (
    "google_images",
    "pinterest",
    "tiktok",
    "instagram",
    "record_label",
    "label_music",
    "watermarked",
    "free_download",
)


def _normalize_provider(provider: Optional[str]) -> str:
    if not provider:
        return ""
    return provider.strip().lower().replace(" ", "_").replace("-", "_")


@dataclass(frozen=True)
class AssetProvenanceProblem:
    asset_id: str
    reasons: tuple


@dataclass(frozen=True)
class G2Report:
    verdict: Verdict
    problems: tuple  # tuple[AssetProvenanceProblem, ...]
    reason_codes: tuple = ()

    def to_token(self, manifest_hash: str, **kwargs) -> GateToken:
        return issue_token(
            GATE_COPYRIGHT_G2,
            self.verdict,
            manifest_hash,
            reason_codes=self.reason_codes,
            evidence={
                "problems": [
                    {"asset_id": p.asset_id, "reasons": list(p.reasons)}
                    for p in self.problems
                ]
            },
            **kwargs,
        )


def check_g2_provenance(
    asset_ids: Iterable[str],
    provenances: Mapping[str, AssetProvenance],
) -> G2Report:
    """G2: every asset has Tier-1/2 provenance with an acquisition record."""
    problems: dict = {}
    for asset_id in asset_ids:
        prov = provenances.get(asset_id)
        if prov is None:
            problems[asset_id] = ("UNKNOWN_PROVENANCE",)
            continue
        reasons = []
        provider = _normalize_provider(prov.provider)
        if not provider:
            reasons.append("PROVENANCE_PROVIDER_MISSING")
        if prov.tier == 3 or provider in TIER3_PROVIDERS:
            reasons.append("TIER3_SOURCE_PROHIBITED")
        elif prov.tier not in (1, 2):
            reasons.append("PROVENANCE_TIER_UNKNOWN")
        elif any(keyword in provider for keyword in PROHIBITED_PROVIDER_KEYWORDS):
            reasons.append("TIER3_SOURCE_PROHIBITED")
        if not prov.acquisition_hash and not prov.acquisition_record_id:
            reasons.append("MISSING_ACQUISITION_RECORD")
        if prov.watermark_detected:
            reasons.append("WATERMARK_DETECTED")
        if reasons:
            problems[asset_id] = tuple(reasons)

    result = tuple(
        AssetProvenanceProblem(asset_id=aid, reasons=rs)
        for aid, rs in sorted(problems.items())
    )
    verdict = Verdict.BLOCK if result else Verdict.PASS
    return G2Report(
        verdict=verdict,
        problems=result,
        reason_codes=("G2_PROVENANCE_FAILED",) if result else (),
    )


# ---------------------------------------------------------------------------
# G6 -- attribution diff
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MissingAttribution:
    asset_id: str
    required_text: str


@dataclass(frozen=True)
class G6Report:
    verdict: Verdict
    missing: tuple  # tuple[MissingAttribution, ...]
    unusable: tuple  # asset_ids requiring attribution but with no text on record
    reason_codes: tuple = ()

    def to_token(self, manifest_hash: str, **kwargs) -> GateToken:
        return issue_token(
            GATE_COPYRIGHT_G6,
            self.verdict,
            manifest_hash,
            reason_codes=self.reason_codes,
            evidence={
                "missing_attributions": [
                    {"asset_id": m.asset_id, "required_text": m.required_text}
                    for m in self.missing
                ],
                "attribution_text_missing_for_assets": list(self.unusable),
            },
            **kwargs,
        )


def check_g6_attribution(
    licences: Iterable[LicenceRecord],
    description: str,
) -> G6Report:
    """G6: required attribution strings appear VERBATIM in the description.

    The comparison is an exact, case-sensitive substring check -- an
    altered or re-worded credit is a licence breach, not a pass.
    """
    missing = []
    unusable = []
    for record in licences:
        if not record.attribution_required:
            continue
        if not record.attribution_text:
            unusable.append(record.asset_id)
        elif record.attribution_text not in description:
            missing.append(
                MissingAttribution(
                    asset_id=record.asset_id, required_text=record.attribution_text
                )
            )
    blocked = bool(missing or unusable)
    reason_codes = []
    if missing:
        reason_codes.append("G6_ATTRIBUTION_MISSING_FROM_DESCRIPTION")
    if unusable:
        reason_codes.append("G6_ATTRIBUTION_TEXT_NOT_ON_RECORD")
    return G6Report(
        verdict=Verdict.BLOCK if blocked else Verdict.PASS,
        missing=tuple(missing),
        unusable=tuple(sorted(unusable)),
        reason_codes=tuple(reason_codes),
    )


# ---------------------------------------------------------------------------
# Aggregate copyright verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CopyrightReport:
    """G1+G2+G6 combined -- the single copyright verdict Gate B consumes."""

    g1: G1Report
    g2: G2Report
    g6: G6Report

    @property
    def verdict(self) -> Verdict:
        return worst_verdict([self.g1.verdict, self.g2.verdict, self.g6.verdict])

    @property
    def reason_codes(self) -> tuple:
        return self.g1.reason_codes + self.g2.reason_codes + self.g6.reason_codes

    def to_token(self, manifest_hash: str, **kwargs) -> GateToken:
        return issue_token(
            GATE_COPYRIGHT,
            self.verdict,
            manifest_hash,
            reason_codes=self.reason_codes,
            evidence={
                "g1": self.g1.verdict.value,
                "g2": self.g2.verdict.value,
                "g6": self.g6.verdict.value,
            },
            **kwargs,
        )


def run_copyright_gates(
    *,
    segments: Iterable[UsageSegment],
    licences: Mapping[str, LicenceRecord],
    provenances: Mapping[str, AssetProvenance],
    description: str,
    total_duration_s: float,
) -> CopyrightReport:
    """Run G1, G2 and G6 over one video's registry data."""
    segment_list = list(segments)
    asset_ids = {seg.asset_id for seg in segment_list}
    return CopyrightReport(
        g1=check_g1_licence_completeness(segment_list, licences, total_duration_s),
        g2=check_g2_provenance(asset_ids, provenances),
        g6=check_g6_attribution(licences.values(), description),
    )
