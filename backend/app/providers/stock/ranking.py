"""Hard filters + ranking (§24 Step 2/3).

Ranking weights are contractual: relevance 0.40 + fit 0.20 + FRESHNESS 0.20
+ reliability 0.10 + intra-video diversity 0.10. The freshness term is
deliberate and anti-obvious: it de-ranks the most-downloaded candidate,
because the #1 clip for a popular query is the clip most likely already in
hundreds of other faceless videos.
"""
from __future__ import annotations

import math

from .models import Candidate, MediaType, SearchFilters, query_tokens

PROVIDER_RELIABILITY: dict[str, float] = {
    "pixabay": 0.90,
    "pexels": 0.95,
    "manual": 0.85,
    "generated": 0.70,
}

_BRAND_WORDS = {"logo", "brand", "trademark", "branded"}


def has_brand_or_logo(candidate: Candidate) -> bool:
    if candidate.sensitive_flags.trademark_or_logo:
        return True
    haystack = {t.lower() for t in candidate.tags} | query_tokens(candidate.title)
    return bool(haystack & _BRAND_WORDS)


def rejection_reason(
    candidate: Candidate,
    filters: SearchFilters,
    *,
    used_ids: set[str] | frozenset[str] = frozenset(),
    used_keys: set[str] | frozenset[str] = frozenset(),
) -> str | None:
    """Return a reason string if the candidate fails a hard filter, else None."""
    if candidate.provider_asset_id in used_ids or candidate.asset_key in used_keys:
        return "already_used"
    if candidate.asset_key in filters.exclude_asset_keys or (
        candidate.provider_asset_id in filters.exclude_provider_asset_ids
    ):
        return "already_used"
    # Orientation: landscape only.
    if filters.orientation == "landscape" and not candidate.is_landscape:
        return "not_landscape"
    if candidate.width is not None and candidate.width < filters.effective_min_width():
        return "below_min_width"
    if candidate.width is None:
        return "missing_width"
    if filters.media_type == MediaType.VIDEO:
        if candidate.fps is not None and candidate.fps < filters.min_fps:
            return "below_min_fps"
        min_dur = filters.effective_min_duration_s()
        if min_dur is not None:
            if candidate.duration_s is None or candidate.duration_s < min_dur:
                return "below_min_duration"
    if filters.safesearch and candidate.is_nsfw:
        return "safesearch"
    if has_brand_or_logo(candidate):
        return "brand_or_logo"
    return None


def filter_candidates(
    candidates: list[Candidate],
    filters: SearchFilters,
    *,
    used_ids: set[str] | frozenset[str] = frozenset(),
    used_keys: set[str] | frozenset[str] = frozenset(),
) -> list[Candidate]:
    return [
        c
        for c in candidates
        if rejection_reason(c, filters, used_ids=used_ids, used_keys=used_keys) is None
    ]


# --- scoring terms (each 0..1) -------------------------------------------------

def relevance_score(candidate: Candidate, query: str) -> float:
    tokens = query_tokens(query)
    if not tokens:
        return 0.5
    haystack: set[str] = set()
    for tag in candidate.tags:
        haystack |= query_tokens(tag)
    haystack |= query_tokens(candidate.title)
    if not haystack:
        return 0.0
    return len(tokens & haystack) / len(tokens)


def fit_score(candidate: Candidate, filters: SearchFilters) -> float:
    score = 0.0
    # Width fit: full marks at/above 1920 for video, at min for photo.
    target_w = 1920
    if candidate.width:
        score += 0.6 * min(1.0, candidate.width / target_w)
    # Duration fit: full marks when the clip covers the scene with headroom,
    # without being absurdly long for it.
    min_dur = filters.effective_min_duration_s()
    if min_dur and candidate.duration_s:
        ratio = candidate.duration_s / min_dur
        score += 0.4 * (1.0 if 1.0 <= ratio <= 4.0 else max(0.0, 1.0 - abs(math.log2(max(ratio, 0.01))) / 4.0))
    elif candidate.duration_s:
        score += 0.4
    else:
        score += 0.4  # photos have no duration term
    return min(1.0, score)


def freshness_score(candidate: Candidate, max_downloads: int | None = None) -> float:
    """Less-downloaded = fresher. Unknown downloads get a neutral-high score."""
    if candidate.downloads is None:
        return 0.7
    if max_downloads and max_downloads > 0:
        return 1.0 - (candidate.downloads / max_downloads)
    return 1.0 / (1.0 + math.log10(1.0 + candidate.downloads))


def diversity_score(candidate: Candidate, already_chosen: list[Candidate]) -> float:
    if not already_chosen:
        return 1.0
    cand_tags = {t.lower() for t in candidate.tags}
    worst = 0.0
    for other in already_chosen:
        if other.provider_asset_id == candidate.provider_asset_id and other.provider == candidate.provider:
            return 0.0
        other_tags = {t.lower() for t in other.tags}
        union = cand_tags | other_tags
        if union:
            worst = max(worst, len(cand_tags & other_tags) / len(union))
        if other.author_name and other.author_name == candidate.author_name:
            worst = max(worst, 0.5)
    return 1.0 - worst


def score_candidate(
    candidate: Candidate,
    query: str,
    filters: SearchFilters,
    *,
    already_chosen: list[Candidate] | None = None,
    max_downloads: int | None = None,
) -> float:
    return (
        0.40 * relevance_score(candidate, query)
        + 0.20 * fit_score(candidate, filters)
        + 0.20 * freshness_score(candidate, max_downloads)
        + 0.10 * PROVIDER_RELIABILITY.get(candidate.provider, 0.5)
        + 0.10 * diversity_score(candidate, already_chosen or [])
    )


def score_breakdown(candidate, query, filters, *, already_chosen=None, max_downloads=None) -> dict[str, float]:
    return {
        "relevance": relevance_score(candidate, query),
        "fit": fit_score(candidate, filters),
        "freshness": freshness_score(candidate, max_downloads),
        "reliability": PROVIDER_RELIABILITY.get(candidate.provider, 0.5),
        "diversity": diversity_score(candidate, already_chosen or []),
        "total": score_candidate(
            candidate, query, filters, already_chosen=already_chosen, max_downloads=max_downloads
        ),
    }


def rank_candidates(
    candidates: list[Candidate],
    query: str,
    filters: SearchFilters,
    *,
    already_chosen: list[Candidate] | None = None,
) -> list[Candidate]:
    downloads = [c.downloads for c in candidates if c.downloads is not None]
    max_dl = max(downloads) if downloads else None
    return sorted(
        candidates,
        key=lambda c: score_candidate(c, query, filters, already_chosen=already_chosen, max_downloads=max_dl),
        reverse=True,
    )
