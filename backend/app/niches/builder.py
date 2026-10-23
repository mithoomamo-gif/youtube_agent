"""OTHER / custom-niche profile builder (Agent 3 logic, roadmap section 33).

Turns a one-line user idea into a complete, SAFE :class:`NicheProfile`
with zero manual prompt engineering by the user.  Pipeline:

1. **Validate** -- ``name`` 2..40 chars, ``idea`` 10..500 chars
   (hard limits; :class:`ValueError` otherwise).
2. **Infer + inherit** -- run :func:`inference.infer_profile_fields`,
   then inherit the FULL parameter set of the nearest archetype from
   :data:`builtins.BUILTIN_PROFILES`.  A generic archetype
   (``narrative_story`` / ``explainer`` / ``listicle``) is mapped to a
   built-in base via :data:`ARCHETYPE_BASE`.
3. **Apply overrides selectively** -- an inferred field replaces the
   archetype default ONLY when its confidence is
   ``>= CONFIDENCE_THRESHOLD`` (0.6).  Below-threshold fields keep the
   archetype default; in the :class:`ProfileInference` record they are
   the fields tagged ``source="archetype_default"`` (or simply not
   applied here).
4. **Clamp & validate (deterministic, non-LLM)** -- WPM clamped to
   110..160; structure forced to start with a hook section and end
   with a conclusion section; language normalised to the supported
   set; sensitive flags detected in the idea (health / finance / law /
   religion / politics) force ``risk_class="sensitive"`` and their
   hard-rule pack ON *regardless of tone or inference*; a stats-heavy
   idea forces the mandatory fact-check rule for statistics.
5. **Provisional profile v1** -- returned with ``source="other"``,
   ``status="provisional"``, ``version=1`` and
   ``profile_id = "other_" + slugified name``.

The final object is a real :class:`models.NicheProfile`, so it passes
that model's own validators (exactly-3 tone adjectives, H1..H8 hook
ids, hook-first / conclusion-last structure, slug profile_id) and runs
through the identical scripting pipeline as the 8 built-ins.
"""

from __future__ import annotations

import re
from typing import Any

from .builtins import BUILTIN_PROFILES, get_profile
from .inference import (
    STATS_RULE,
    ProfileInference,
    detect_sensitive_flags,
    detect_stats_heavy,
    infer_profile_fields,
)
from .models import (
    SUPPORTED_SENSITIVE_FLAGS,
    WPM_MAX,
    WPM_MIN,
    ArchetypeName,
    NicheProfile,
)

__all__ = [
    "ARCHETYPE_BASE",
    "CONFIDENCE_THRESHOLD",
    "SENSITIVE_FORBIDDEN_PATTERNS",
    "SENSITIVE_HARD_RULES",
    "build_other_profile",
    "profile_summary_lines",
]

#: Minimum inference confidence for a field to override the archetype
#: default (roadmap step 4: below threshold -> silent fallback).
CONFIDENCE_THRESHOLD: float = 0.6

#: Generic archetype -> built-in base profile (roadmap step 2/3).
#: A generic archetype has no built-in of its own, so it borrows the
#: full parameter set (pacing, structure, hooks, cadence) of the
#: nearest built-in:
#:   narrative_story -> horror_stories (story pacing / structure)
#:   explainer       -> facts          (explainer pacing / structure)
#:   listicle        -> facts          (countdown-friendly defaults)
ARCHETYPE_BASE: dict[str, str] = {
    "narrative_story": "horror_stories",
    "explainer": "facts",
    "listicle": "facts",
}

#: Hard-rule pack forced ON per sensitive flag (roadmap step 4 and the
#: niche-specific fact-check packs in section 33).  Applied regardless
#: of the inferred tone, and merged with - never replacing - the base
#: archetype's own rules.
SENSITIVE_HARD_RULES: dict[str, list[str]] = {
    "health": [
        "Disclaimer block is structural and cannot be edited out",
        "No diagnosis or cure claims",
        "Health claims are a mandatory fact-check class",
    ],
    "finance": [
        "No guaranteed-return or guaranteed-income language",
        "Risk disclaimer is mandatory: investments can lose value",
        "Financial figures and return claims are a mandatory fact-check class",
    ],
    "law": [
        "General legal information only - no definitive legal advice; "
        "advise consulting a qualified lawyer",
        "Laws, sections and case outcomes are a mandatory fact-check class",
        "Use 'alleged'/'suspected' language until conviction",
    ],
    "religion": [
        "No fabricated narrations",
        "Religious references need a named source + verified translation",
        "Religious references are a mandatory fact-check class",
    ],
    "politics": [
        "Neutral, non-partisan framing - verified facts only, "
        "no party or candidate endorsement",
        "Political claims, dates and figures are a mandatory fact-check class",
    ],
}

#: Forbidden patterns forced ON per sensitive flag (merged, deduped).
SENSITIVE_FORBIDDEN_PATTERNS: dict[str, list[str]] = {
    "health": ["diagnosis claims", "cure claims", "dosage instructions"],
    "finance": ["guaranteed returns", "guaranteed income promises"],
    "law": ["presenting allegations as proven guilt"],
    "religion": [
        "fabricated narrations presented as authentic",
        "paraphrase presented as a direct quotation",
    ],
    "politics": ["partisan endorsement presented as fact"],
}

#: Languages the pipeline supports (mirrors the built-in profiles).
_SUPPORTED_LANGUAGES: frozenset[str] = frozenset(
    {
        "roman_urdu",
        "roman_urdu+english",
        "english+roman_urdu",
        "roman_urdu+medical_terms",
        "urdu",
        "english",
        "roman_hindi",
        "hindi",
    }
)

#: Friendly labels for the one-screen review summary.
_LANGUAGE_LABELS: dict[str, str] = {
    "roman_urdu+english": "Roman Urdu + English mix",
    "english+roman_urdu": "English + Roman Urdu mix",
    "roman_urdu+medical_terms": "Roman Urdu + medical terms",
    "roman_urdu": "Roman Urdu",
    "roman_hindi": "Roman Hindi",
    "urdu": "Urdu",
    "english": "English",
    "hindi": "Hindi",
}

_HOOK_ID_RE = re.compile(r"^H[1-8]$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    """Slugify a niche name for use inside ``profile_id``."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if not slug:
        slug = "custom"
    if slug[0].isdigit():
        slug = f"n_{slug}"
    return slug[:48].strip("_") or "custom"


def _resolve_base_profile(
    archetype: str, secondary_archetype: str | None = None
) -> NicheProfile:
    """Return the built-in profile an archetype inherits from.

    Lookup order: the archetype itself when it is a built-in id, its
    :data:`ARCHETYPE_BASE` mapping, then the same two steps for the
    secondary (hybrid) archetype, finally the ``facts`` explainer
    defaults - so a sane base is always found.
    """
    candidates: list[str] = []
    for arch in (archetype, secondary_archetype):
        if arch:
            candidates.append(arch)
            mapped = ARCHETYPE_BASE.get(arch)
            if mapped:
                candidates.append(mapped)
    candidates.append("facts")
    for candidate in candidates:
        if candidate in BUILTIN_PROFILES:
            return get_profile(candidate)
    # BUILTIN_PROFILES is a static library of 8; reaching here means it
    # was emptied/mocked - fail loudly rather than inventing a profile.
    raise LookupError(
        f"no built-in base profile found for archetype {archetype!r} "
        f"(BUILTIN_PROFILES keys: {sorted(BUILTIN_PROFILES)})"
    )


def _union(first: list[str], second: list[str]) -> list[str]:
    """Order-preserving union of two string lists (deduped)."""
    out = list(first)
    for item in second:
        if item not in out:
            out.append(item)
    return out


def _normalise_language(value: Any, fallback: str) -> str:
    """Normalise an inferred language to the supported set."""
    if not isinstance(value, str):
        return fallback
    lang = value.strip().lower().replace(" ", "")
    if lang in _SUPPORTED_LANGUAGES:
        return lang
    if "urdu" in lang and "english" in lang:
        return "roman_urdu+english"
    if "urdu" in lang:
        return "roman_urdu"
    if "hindi" in lang:
        return "roman_hindi"
    if "english" in lang:
        return "english"
    return fallback


def _normalise_tone(value: Any, base_tone: list[str]) -> list[str]:
    """Force exactly 3 tone adjectives (NicheProfile's contract)."""
    items: list[str] = []
    if isinstance(value, list):
        for raw in value:
            word = str(raw).strip()
            if word and word not in items:
                items.append(word)
    if len(items) > 3:
        items = items[:3]
    if len(items) < 3:
        for filler in list(base_tone) + ["engaging", "clear", "focused"]:
            if len(items) >= 3:
                break
            if filler not in items:
                items.append(filler)
    return items[:3]


def _normalise_hooks(value: Any, base_hooks: list[str]) -> list[str]:
    """Keep only valid H1..H8 ids, deduped, at most 3, never empty."""
    items: list[str] = []
    if isinstance(value, list):
        for raw in value:
            hook = str(raw).strip().upper()
            if _HOOK_ID_RE.match(hook) and hook not in items:
                items.append(hook)
    if not items:
        items = list(base_hooks)
    return items[:3]


def _fix_structure(value: Any, base_structure: list[str]) -> list[str]:
    """Force hook-first / conclusion-last (NicheProfile's contract)."""
    if isinstance(value, list):
        sections = [str(s).strip() for s in value if str(s).strip()]
    else:
        sections = []
    if not sections:
        sections = list(base_structure)
    if "hook" not in sections[0].lower():
        sections.insert(0, "Hook")
    if "conclusion" not in sections[-1].lower():
        sections.append("Conclusion + CTA")
    if len(sections) < 2:  # pragma: no cover - inserts above prevent it
        sections.append("Conclusion")
    return sections


def _clamp_wpm(value: Any, fallback: int) -> int:
    """Clamp WPM into WPM_MIN..WPM_MAX (110..160); fallback if unusable."""
    try:
        wpm = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        wpm = fallback
    return max(WPM_MIN, min(WPM_MAX, wpm))


def _ordered_flags(flags: list[str]) -> list[str]:
    """Dedupe flags and order them by the canonical supported order."""
    wanted = {f for f in flags if f in SUPPORTED_SENSITIVE_FLAGS}
    return [f for f in SUPPORTED_SENSITIVE_FLAGS if f in wanted]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def build_other_profile(
    name: str,
    idea: str,
    llm_result: dict[str, Any] | None = None,
) -> NicheProfile:
    """Build a provisional OTHER-niche profile from a one-line idea.

    Args:
        name: User-given niche name (2..40 characters).
        idea: One-line niche idea (10..500 characters), e.g.
            ``"cricket analysis - matches ki kahani aur stats Urdu mein"``.
        llm_result: Optional structured JSON from an LLM caller; merged
            over the deterministic heuristics by
            :func:`inference.infer_profile_fields`.

    Returns:
        A validated :class:`NicheProfile` with ``source="other"``,
        ``status="provisional"``, ``version=1``.

    Raises:
        ValueError: if ``name`` or ``idea`` violate the hard length
            limits.
    """
    # -- 1. Validate (hard limits) -----------------------------------------
    clean_name = (name or "").strip()
    clean_idea = (idea or "").strip()
    if not (2 <= len(clean_name) <= 40):
        raise ValueError(
            f"name must be 2..40 characters, got {len(clean_name)}: {clean_name!r}"
        )
    if not (10 <= len(clean_idea) <= 500):
        raise ValueError(
            f"idea must be 10..500 characters, got {len(clean_idea)}"
        )

    # -- 2. Inference + archetype inheritance -------------------------------
    inference: ProfileInference = infer_profile_fields(
        clean_name, clean_idea, llm_result=llm_result
    )
    # ArchetypeName is the closed vocabulary inference draws from; the
    # resolver accepts any string and falls back deterministically.
    archetype: str = str(inference.archetype)
    _archetype_check: ArchetypeName | str = archetype  # typing aid only
    base = _resolve_base_profile(archetype, inference.secondary_archetype)
    data: dict[str, Any] = base.model_dump()
    base_is_direct_builtin = archetype in BUILTIN_PROFILES

    inferred = inference.field_map()

    def applied(field: str) -> Any | None:
        """Inferred value for ``field`` when it clears the threshold."""
        item = inferred.get(field)
        if item is not None and item.confidence >= CONFIDENCE_THRESHOLD:
            return item.value
        return None

    # -- 3. Selective overrides (confidence >= threshold only) --------------
    language_override = applied("language")
    if language_override is not None:
        data["language"] = language_override

    tone_override = applied("tone")
    if tone_override is not None:
        data["tone"] = tone_override

    wpm_override = applied("target_wpm")
    if wpm_override is not None:
        data["target_wpm"] = wpm_override

    for text_field in ("sentence_style", "voice_style", "visual_style", "cta_style"):
        override = applied(text_field)
        if isinstance(override, str) and override.strip():
            data[text_field] = override.strip()

    structure_override = applied("structure_template")
    if structure_override is not None:
        data["structure_template"] = structure_override

    hooks_override = applied("hook_families")
    if hooks_override is not None:
        data["hook_families"] = hooks_override

    # List fields that must never *lose* archetype protections merge
    # instead of replacing.
    rules_override = applied("special_rules")
    if isinstance(rules_override, list) and rules_override:
        data["special_rules"] = _union(
            list(data.get("special_rules", [])),
            [str(r).strip() for r in rules_override if str(r).strip()],
        )
    for merge_field in ("taboo", "forbidden_patterns"):
        override = applied(merge_field)
        if isinstance(override, list) and override:
            data[merge_field] = _union(
                list(data.get(merge_field, [])),
                [str(r).strip() for r in override if str(r).strip()],
            )

    # -- 4. Clamp & validate (deterministic) --------------------------------
    data["language"] = _normalise_language(data["language"], base.language)
    data["tone"] = _normalise_tone(data.get("tone"), base.tone)
    data["target_wpm"] = _clamp_wpm(data.get("target_wpm"), base.target_wpm)
    data["structure_template"] = _fix_structure(
        data.get("structure_template"), base.structure_template
    )
    data["hook_families"] = _normalise_hooks(
        data.get("hook_families"), base.hook_families
    )

    # Sensitive flags: detected in the idea text directly (never left
    # to tone/inference), plus any high-confidence inferred flags, plus
    # the base flags when the archetype *is* a sensitive built-in.
    detected = detect_sensitive_flags(f"{clean_name} {clean_idea}")
    flag_sources: list[str] = list(detected)
    inferred_flags = applied("sensitive_flags")
    if isinstance(inferred_flags, list):
        flag_sources.extend(str(f) for f in inferred_flags)
    if llm_result:
        # An LLM flagging a category counts even at heuristic level:
        # safety packs err on the side of ON.
        llm_flags = inferred.get("sensitive_flags")
        if llm_flags is not None and isinstance(llm_flags.value, list):
            flag_sources.extend(str(f) for f in llm_flags.value)
    if base_is_direct_builtin:
        flag_sources.extend(str(f) for f in base.sensitive_flags)
    flags = _ordered_flags(flag_sources)

    if flags:
        data["risk_class"] = "sensitive"
        data["sensitive_flags"] = flags
        for flag in flags:
            data["special_rules"] = _union(
                list(data.get("special_rules", [])),
                SENSITIVE_HARD_RULES.get(flag, []),
            )
            data["forbidden_patterns"] = _union(
                list(data.get("forbidden_patterns", [])),
                SENSITIVE_FORBIDDEN_PATTERNS.get(flag, []),
            )
    else:
        data["risk_class"] = "low"
        data["sensitive_flags"] = []

    # Stats-heavy: statistics become a mandatory fact-check class.
    stats_field = inferred.get("stats_heavy")
    stats_heavy = bool(
        detect_stats_heavy(f"{clean_name} {clean_idea}")
        or (stats_field is not None and stats_field.value is True)
    )
    if stats_heavy:
        data["special_rules"] = _union(list(data.get("special_rules", [])), [STATS_RULE])

    # -- 5. Identity: provisional Other profile v1 ---------------------------
    data["profile_id"] = f"other_{_slugify(clean_name)}"
    data["name"] = clean_name
    data["source"] = "other"
    data["status"] = "provisional"
    data["version"] = 1

    # Construct with exactly the model's fields (extra="forbid").
    model_fields = set(NicheProfile.model_fields)
    return NicheProfile(**{k: v for k, v in data.items() if k in model_fields})


# ---------------------------------------------------------------------------
# One-screen review summary
# ---------------------------------------------------------------------------


def _pace_label(wpm: int) -> str:
    if wpm <= 118:
        return "slow"
    if wpm <= 128:
        return "medium-slow"
    if wpm <= 138:
        return "medium"
    if wpm <= 148:
        return "medium-fast"
    return "fast"


def profile_summary_lines(profile: NicheProfile) -> list[str]:
    """One-screen review lines for a (provisional) profile.

    Mirrors the roadmap review-first summary, e.g.::

        Tone: passionate · analytical · fan-friendly · Language: Roman Urdu + English mix · Speed: medium-fast
        Structure: Hook (...) → Context → ... → Verdict + conclusion + ...
        Hooks: H3, H2
        Risk: low
    """
    language_label = _LANGUAGE_LABELS.get(
        profile.language, profile.language.replace("_", " ").replace("+", " + ")
    )
    lines: list[str] = [
        (
            f"Tone: {' · '.join(profile.tone)}"
            f" · Language: {language_label}"
            f" · Speed: {_pace_label(profile.target_wpm)} ({profile.target_wpm} WPM)"
        ),
        f"Name: {profile.name} ({profile.source}, {profile.status}, v{profile.version})",
        f"Structure: {' → '.join(profile.structure_template)}",
        f"Hooks: {', '.join(profile.hook_families)}",
        f"Voice: {profile.voice_style}",
        f"CTA: {profile.cta_style}",
    ]
    if profile.risk_class == "sensitive" or profile.sensitive_flags:
        lines.append(
            "Risk: sensitive - flags: "
            + (", ".join(profile.sensitive_flags) or "none listed")
            + " (hard rules ON)"
        )
    else:
        lines.append("Risk: low")
    if any("statistic" in rule.lower() for rule in profile.special_rules):
        lines.append(
            "Fact-check: all statistics are a mandatory fact-check class"
        )
    if profile.special_rules:
        lines.append(f"Special rules: {len(profile.special_rules)} active")
    return lines
