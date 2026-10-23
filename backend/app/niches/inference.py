"""Field inference for the OTHER / custom-niche auto-build (Agent 3, step 1-3).

Implements steps 1-3 of the five-step auto-build in roadmap section 33
("Other niche auto-build"):

    (1) Parse & normalise  -- candidate name, language signal, content form.
    (2) Archetype matching -- nearest built-in id or one of the generic
        fallbacks (``narrative_story`` / ``explainer`` / ``listicle``),
        plus an optional secondary (hybrid) archetype.
    (3) Field inference    -- every inferred field is returned as a
        :class:`FieldInference` carrying a confidence score in ``0..1``
        and a ``source`` tag.  :mod:`builder` only applies fields at or
        above its confidence threshold; the rest fall back to the
        archetype default.

This module is deliberately *deterministic and heuristic* (keyword based,
no network, no randomness) so it can be unit-tested and can run offline.
``infer_profile_fields`` also accepts an optional ``llm_result`` dict --
the structured JSON a flagship-tier LLM caller would supply in
production (roadmap step 3) -- and merges it over the heuristics, so a
real LLM can be plugged in later without changing callers.

Worked example (roadmap section 33)::

    idea = "cricket analysis - matches ki kahani aur stats Urdu mein"
    -> archetype "explainer", secondary "narrative_story",
       language "roman_urdu+english", WPM 140, hooks ["H3", "H2"],
       tone ["passionate", "analytical", "fan-friendly"],
       special rule: all statistics are a mandatory fact-check class.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "FieldInference",
    "ProfileInference",
    "infer_profile_fields",
    "detect_sensitive_flags",
    "detect_stats_heavy",
    "SENSITIVE_KEYWORDS",
    "STATS_RULE",
    "GENERIC_ARCHETYPES",
]

#: Generic fallback archetypes (roadmap section 33, step 2).
GENERIC_ARCHETYPES: tuple[str, ...] = ("narrative_story", "explainer", "listicle")

#: Special rule injected for stats-heavy niches (inherited from History's
#: rule pack in the roadmap's worked cricket example).
STATS_RULE: str = (
    "All statistics (scores, records, dates, figures) are a mandatory "
    "fact-check class - every statistic needs a fact-log entry with a source."
)

# ---------------------------------------------------------------------------
# Keyword tables (all matched against the lower-cased name + idea text)
# ---------------------------------------------------------------------------

#: Roman-Urdu function words / common vocabulary.  Presence of several of
#: these in Latin script is the strongest Roman-Urdu signal available
#: without a language model.
_ROMAN_URDU_WORDS: frozenset[str] = frozenset(
    {
        "mein", "mai", "ki", "ka", "ke", "aur", "hai", "hain", "tha", "thi",
        "the", "kya", "kyun", "kyu", "kab", "kahan", "kaisa", "kaisi",
        "aap", "tum", "yeh", "ye", "woh", "wo", "bhi", "nahi", "nhi",
        "kar", "karke", "raha", "rahi", "rahe", "hoga", "hogi", "chahiye",
        "kahani", "kahaniyan", "baat", "baatein", "log", "bohat", "bahut",
        "acha", "wala", "wali", "wale", "apna", "apni", "sab", "jo",
        "se", "ko", "ne", "par", "pe", "tak", "liye", "bina", "sath",
        "saath", "andar", "bahar", "pehle", "baad", "phir", "abhi",
        "matlab", "zaroor", "shayad", "lekin", "magar", "isliye",
        "samajh", "batao", "dekho", "suno", "janna", "chahta", "chahti",
    }
)

#: Words that are ambiguous between English and Roman Urdu; excluded from
#: the English-signal count so e.g. "the" inside a Roman-Urdu sentence
#: does not skew detection.
_AMBIGUOUS_WORDS: frozenset[str] = frozenset({"the", "mein", "ki", "ka"})

#: English domain / technical terms that speakers typically keep in
#: English inside Roman-Urdu narration ("cricket", "stats", "analysis"
#: ...).  Their presence next to an Urdu signal means the mix
#: ``roman_urdu+english``, exactly as in the roadmap's cricket example.
_ENGLISH_DOMAIN_TERMS: frozenset[str] = frozenset(
    {
        "analysis", "stats", "statistics", "data", "record", "records",
        "score", "cricket", "football", "tennis", "hockey", "match",
        "matches", "science", "technology", "computer", "software",
        "internet", "mobile", "smartphone", "business", "marketing",
        "fitness", "workout", "protein", "vitamin", "stock", "crypto",
        "trading", "investment", "tutorial", "review", "reviews",
        "guide", "course", "lesson", "lessons", "channel", "video",
        "videos", "youtube",
    }
)

_ENGLISH_MARKERS: frozenset[str] = frozenset(
    {
        "the", "and", "of", "to", "in", "is", "are", "with", "for",
        "how", "why", "what", "when", "explained", "explains", "guide",
        "tutorial", "review", "reviews", "tips", "tricks", "learn",
        "learning", "story", "stories", "history", "science",
    }
)

#: Sensitive categories -> keyword sets (roadmap step 4: health / finance /
#: law / religion / politics force the corresponding hard-rule pack ON).
SENSITIVE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "health": (
        "health", "sehat", "medical", "doctor", "bimari", "bemari",
        "diabetes", "sugar", "cancer", "heart", "blood pressure",
        "weight loss", "wazan", "diet", "fitness", "exercise", "warish",
        "mental health", "depression", "anxiety", "ilaaj", "ilaj",
        "dawa", "dawai", "medicine", "disease", "symptom", "nutrition",
        "khorak", "vitamin", "pregnancy", "hamal",
    ),
    "finance": (
        "finance", "financial", "money", "paisa", "paise", "invest",
        "investment", "investing", "stock", "stocks", "share market",
        "crypto", "bitcoin", "trading", "trader", "business tips",
        "earn money", "earning", "income", "loan", "qarz", "profit",
        "banking", "tax", "ameer", "rich", "wealth",
    ),
    "law": (
        "law", "legal", "qanoon", "qanun", "court", "adalat", "lawyer",
        "wakeel", "vakil", "judge", "case law", "rights", "haqooq",
        "constitution", "aain", "police case",
    ),
    "religion": (
        "religion", "mazhab", "islam", "islamic", "muslim", "quran",
        "hadith", "nabi", "rasool", "allah", "hindu", "hinduism",
        "christian", "christianity", "bible", "church", "sikh",
        "buddhist", "dua", "ibadat", "namaz", "roza", "seerat",
    ),
    "politics": (
        "politics", "siyasat", "siyasi", "election", "intikhab",
        "government", "hukumat", "vote", "voting", "political party",
        "minister", "wazir", "parliament", "president", "prime minister",
        "democracy", "policy",
    ),
}

#: Stats-heavy signals: explicit stats vocabulary or a stats-driven domain.
_STATS_KEYWORDS: tuple[str, ...] = (
    "stats", "statistics", "statistical", "data", "numbers", "records",
    "record", "figures", "score", "scores", "scorecard", "chart",
    "graph", "ranking", "rankings", "percent", "percentage", "average",
    "averages", "economy rate", "strike rate", "aadad", "shumariyat",
)

_STATS_DOMAINS: tuple[str, ...] = (
    "cricket", "football", "sports", "sport", "match", "matches",
    "ipl", "psl", "world cup",
)

#: Domain keyword table: domain -> (archetype hint, tone, wpm, hooks...).
#: Order matters: the first matching domain wins for tone/pace, so the
#: most specific / sensitive domains come first.
_DOMAIN_PROFILES: list[dict[str, Any]] = [
    {
        "domain": "islamic",
        "keywords": ("islamic", "islam", "quran", "hadith", "seerat", "nabi", "muslim"),
        "archetype": "islamic_stories",
        "tone": ["respectful", "calm", "emotional"],
        "wpm": 120,
        "hooks": ["H7", "H3"],
        "sentence_style": "respectful, clear sentences with pauses for reflection",
        "voice_style": "calm, reverent narrator voice",
        "visual_style": "respectful imagery, calligraphy and historic places",
        "cta_style": "gentle reflection question + dua-style closing",
    },
    {
        "domain": "health",
        "keywords": SENSITIVE_KEYWORDS["health"],
        "archetype": "health",
        "tone": ["caring", "responsible", "clear"],
        "wpm": 135,
        "hooks": ["H5", "H6"],
        "sentence_style": "simple, careful sentences; medical terms explained in plain words",
        "voice_style": "warm, responsible expert voice - never alarmist",
        "visual_style": "clean medical diagrams, simple animations and practical demos",
        "cta_style": "practical tip recap + comment question (with disclaimer intact)",
    },
    {
        "domain": "horror",
        "keywords": ("horror", "bhoot", "pret", "jinn", "haunted", "scary", "darawni", "darawna", "ghost", "paranormal", "khoofnak"),
        "archetype": "horror_stories",
        "tone": ["dark", "suspenseful", "slow-build"],
        "wpm": 115,
        "hooks": ["H3", "H4"],
        "sentence_style": "short tense sentences with deliberate pauses and slow build-up",
        "voice_style": "low, slow, suspenseful voice with long pauses",
        "visual_style": "dark, atmospheric scenes; shadows and slow zooms",
        "cta_style": "open-ending question + next-story tease",
    },
    {
        "domain": "crime",
        "keywords": ("crime", "murder", "qatal", "criminal", "police", "investigation", "tafteesh", "mystery case", "true crime", "kidnap", "robbery", "dakaiti"),
        "archetype": "crime_stories",
        "tone": ["serious", "investigative", "measured"],
        "wpm": 130,
        "hooks": ["H3", "H4"],
        "sentence_style": "precise, factual sentences; 'alleged / suspected' until conviction",
        "voice_style": "serious investigative narrator voice",
        "visual_style": "case files, timelines, maps and evidence-style graphics",
        "cta_style": "case question + next-case tease",
    },
    {
        "domain": "history",
        "keywords": ("history", "tareekh", "historical", "ancient", "qadeem", "empire", "saltanat", "king", "badshah", "war", "jang", "civilization"),
        "archetype": "history",
        "tone": ["documentary", "authoritative", "curious"],
        "wpm": 135,
        "hooks": ["H7", "H4"],
        "sentence_style": "documentary sentences; names and dates stated precisely",
        "voice_style": "authoritative documentary voice",
        "visual_style": "maps, timelines, archival-style imagery and artefacts",
        "cta_style": "legacy / lesson question + next-era tease",
    },
    {
        "domain": "motivation",
        "keywords": ("motivation", "motivational", "success", "kamyabi", "self improvement", "self-improvement", "mindset", "habits", "aadat", "inspiration", "inspirational", "life lessons"),
        "archetype": "motivation",
        "tone": ["direct", "emotional", "uplifting"],
        "wpm": 130,
        "hooks": ["H6", "H8"],
        "sentence_style": "direct second-person sentences with emotional peaks",
        "voice_style": "passionate, direct coach voice",
        "visual_style": "real-life stories, bold quotes and before/after imagery",
        "cta_style": "action-plan challenge + comment commitment",
    },
    {
        "domain": "tech",
        "keywords": ("tech", "technology", "software", "computer", "mobile", "smartphone", "gadget", "ai", "artificial intelligence", "programming", "coding", "internet", "app", "apps"),
        "archetype": "tech",
        "tone": ["simple", "friendly", "expert"],
        "wpm": 145,
        "hooks": ["H5", "H1"],
        "sentence_style": "simple friendly sentences; English tech terms explained in plain words",
        "voice_style": "friendly expert voice, clear and upbeat",
        "visual_style": "screen demos, clean diagrams and product close-ups",
        "cta_style": "verdict recap + comment question on viewer use-case",
    },
    {
        "domain": "cricket_sport",
        "keywords": ("cricket", "football", "sports", "sport", "tennis", "hockey", "match analysis", "batting", "bowling", "wicket", "innings"),
        "archetype": "explainer",
        "tone": ["passionate", "analytical", "fan-friendly"],
        "wpm": 140,
        "hooks": ["H3", "H2"],
        "sentence_style": "short spoken sentences; energetic analysis with clear stats call-outs",
        "voice_style": "passionate fan-analyst voice - energetic but clear",
        "visual_style": "match graphics, scorecards, wagon wheels and stat overlays",
        "cta_style": "verdict question + next-match tease; ask viewers for their prediction in comments",
    },
    {
        "domain": "finance",
        "keywords": SENSITIVE_KEYWORDS["finance"],
        "archetype": "explainer",
        "tone": ["practical", "cautious", "direct"],
        "wpm": 135,
        "hooks": ["H6", "H5"],
        "sentence_style": "plain, careful sentences; numbers always stated with context",
        "voice_style": "calm, trustworthy explainer voice - no hype",
        "visual_style": "clean charts, calculators and real examples on screen",
        "cta_style": "recap + risk reminder + comment question",
    },
    {
        "domain": "facts",
        "keywords": ("facts", "fact", "amazing", "interesting", "ajeeb", "hairat", "did you know", "science", "sciency", "knowledge", "maloomat"),
        "archetype": "facts",
        "tone": ["energetic", "curious", "clear"],
        "wpm": 150,
        "hooks": ["H1", "H2"],
        "sentence_style": "short punchy spoken sentences, one idea per sentence",
        "voice_style": "energetic, curious presenter voice",
        "visual_style": "bold visuals, quick cuts and on-screen fact cards",
        "cta_style": "comment question + subscribe prompt",
    },
]

#: Content-form keyword table: form -> keywords (first match wins, the
#: table is ordered so explicit forms beat generic ones).
_FORM_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("listicle", ("listicle", "top 10", "top 5", "top ten", "top five", "countdown", "list of", "cheezein jo", "batain jo", "baatein jo")),
    ("tutorial", ("tutorial", "how to", "how-to", "seekho", "seekhna", "step by step", "guide", "course", "lesson", "lessons")),
    ("news", ("news", "khabar", "khabrein", "latest update", "breaking", "daily update", "updates")),
    ("analysis", ("analysis", "analyse", "analyze", "tajziya", "breakdown", "review", "reviews", "explained", "explainer", "deep dive")),
    ("stories", ("stories", "story", "kahani", "kahaniyan", "katha", "tales", "tale", "narrative", "waqiya", "waqiyat", "qissa", "qissay")),
    ("commentary", ("commentary", "opinion", "my take", "reaction", "react", "views on")),
]

#: Structure templates per content form / archetype flavour.  Every
#: template deliberately contains a Hook block and a Conclusion block
#: (builder re-validates this deterministically in step 4).
_STRUCTURE_TEMPLATES: dict[str, list[str]] = {
    "cricket_sport": [
        "Hook (match turning point)",
        "Context",
        "Innings / phases breakdown",
        "Key stats chapters",
        "Turning-point climax",
        "Verdict + conclusion + next-match tease",
    ],
    "stories": [
        "Hook scene",
        "Setting / background",
        "Escalating events",
        "Climax",
        "Conclusion",
    ],
    "listicle": [
        "Hook",
        "List chapters (countdown)",
        "Biggest item climax",
        "Conclusion + comment question",
    ],
    "tutorial": [
        "Hook (promise of result)",
        "What you need / context",
        "Step-by-step chapters",
        "Common mistakes / pro tips",
        "Conclusion + practice CTA",
    ],
    "news": [
        "Hook (headline moment)",
        "Background / context",
        "What happened - chronological chapters",
        "Why it matters",
        "Conclusion + what to watch next",
    ],
    "analysis": [
        "Hook",
        "Context",
        "Breakdown chapters",
        "Key evidence / examples",
        "Climax - key insight",
        "Conclusion + verdict",
    ],
    "explainer": [
        "Hook",
        "Introduction / context",
        "Main explainer chapters",
        "Key examples",
        "Climax - key insight",
        "Conclusion + CTA",
    ],
}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class FieldInference(BaseModel):
    """One inferred profile field, with its confidence and provenance."""

    field: str
    value: Any
    confidence: float = Field(ge=0.0, le=1.0)
    source: Literal["idea", "archetype_default", "clamp"]

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v: Any) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, f))


class ProfileInference(BaseModel):
    """Result of steps 1-3: parsed idea + archetype match + inferred fields."""

    candidate_name: str
    language_signal: str
    content_form: str
    archetype: str
    secondary_archetype: str | None = None
    fields: list[FieldInference] = Field(default_factory=list)

    def field_map(self) -> dict[str, FieldInference]:
        """Convenience lookup: field name -> inference (last one wins)."""
        return {f.field: f for f in self.fields}

    def get(self, field: str, default: Any = None) -> Any:
        """Value of an inferred field, or ``default`` when absent."""
        item = self.field_map().get(field)
        return item.value if item is not None else default


# ---------------------------------------------------------------------------
# Low-level text helpers
# ---------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _phrase_in_text(phrase: str, text: str) -> bool:
    """Word-boundary phrase match.

    Plain substring matching is unsafe for short keywords: "ai" occurs
    inside "said", "dua" inside "individual", "war" inside "warm".
    Every keyword table in this module is therefore matched on word
    boundaries instead.
    """
    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(_phrase_in_text(p, text) for p in phrases)


def _matched_phrases(text: str, phrases: tuple[str, ...]) -> list[str]:
    return [p for p in phrases if _phrase_in_text(p, text)]


def detect_sensitive_flags(text: str) -> list[str]:
    """Return sensitive categories detected in ``text`` (stable order).

    Categories follow the roadmap order: health, finance, law,
    religion, politics.  Matching is substring based on multi-word-safe
    keyword phrases against the lower-cased text.
    """
    low = text.lower()
    flags: list[str] = []
    for category in ("health", "finance", "law", "religion", "politics"):
        if _contains_any(low, SENSITIVE_KEYWORDS[category]):
            flags.append(category)
    return flags


def detect_stats_heavy(text: str) -> bool:
    """True when the idea is stats-driven (explicit stats words, or a
    stats domain combined with an analysis / stats cue)."""
    low = text.lower()
    if _contains_any(low, _STATS_KEYWORDS):
        return True
    # A match/sport domain on its own is not enough; require an
    # analytical cue as well so plain sports *stories* stay non-stats.
    if _contains_any(low, _STATS_DOMAINS) and _contains_any(
        low, ("analysis", "analyse", "analyze", "tajziya", "breakdown", "review")
    ):
        return True
    return False


def _detect_language(text: str, tokens: list[str]) -> tuple[str, str, float, list[str]]:
    """Return (language value, language_signal, confidence, reasons)."""
    low = text.lower()
    reasons: list[str] = []

    # Urdu script characters (Arabic block) -> proper Urdu script.
    if re.search(r"[؀-ۿ]", text):
        return "urdu", "urdu (Urdu script detected in idea)", 0.95, ["urdu-script"]

    roman_hits = sorted({t for t in tokens if t in _ROMAN_URDU_WORDS})
    english_hits = sorted(
        {t for t in tokens if t in _ENGLISH_MARKERS and t not in _AMBIGUOUS_WORDS}
        | {t for t in tokens if t in _ENGLISH_DOMAIN_TERMS}
    )
    explicit_urdu = "urdu" in low
    explicit_hindi = "hindi" in low
    explicit_english = "english" in low or "angrezi" in low

    if explicit_urdu:
        reasons.append("explicit 'Urdu' mention")
    if roman_hits:
        reasons.append(f"Roman Urdu markers: {', '.join(roman_hits[:6])}")
    if english_hits:
        reasons.append(f"English markers: {', '.join(english_hits[:6])}")

    has_roman = len(roman_hits) >= 2 or explicit_urdu
    has_english_terms = len(english_hits) >= 1 or explicit_english

    if explicit_urdu and has_english_terms:
        # e.g. cricket: Roman Urdu narration + English domain terms.
        return (
            "roman_urdu+english",
            "roman_urdu+english (" + "; ".join(reasons) + ")",
            0.9,
            reasons,
        )
    if has_roman and has_english_terms:
        return (
            "roman_urdu+english",
            "roman_urdu+english (" + "; ".join(reasons) + ")",
            0.8,
            reasons,
        )
    if has_roman:
        return "roman_urdu", "roman_urdu (" + "; ".join(reasons) + ")", 0.85, reasons
    if explicit_hindi:
        return (
            "roman_hindi",
            "roman_hindi (explicit 'Hindi' mention)",
            0.7,
            reasons,
        )
    if has_english_terms or explicit_english:
        return "english", "english (" + "; ".join(reasons) + ")", 0.8, reasons
    # No signal at all: guess the platform default at low confidence so
    # the builder falls back to the archetype default language.
    return (
        "roman_urdu+english",
        "roman_urdu+english (no clear signal - low-confidence default)",
        0.35,
        reasons,
    )


def _detect_form(text: str) -> tuple[str, dict[str, int]]:
    """Return (primary content form, per-form hit counts)."""
    low = text.lower()
    hits: dict[str, int] = {}
    for form, keywords in _FORM_KEYWORDS:
        matched = _matched_phrases(low, keywords)
        if matched:
            hits[form] = len(matched)
    if not hits:
        return "explainer", hits
    # Highest hit count wins; ties break by table order (explicit first).
    order = [form for form, _ in _FORM_KEYWORDS]
    primary = max(hits, key=lambda f: (hits[f], -order.index(f)))
    return primary, hits


def _detect_domain(text: str) -> dict[str, Any] | None:
    low = text.lower()
    for profile in _DOMAIN_PROFILES:
        if _contains_any(low, tuple(profile["keywords"])):
            return profile
    return None


# ---------------------------------------------------------------------------
# LLM-result merging
# ---------------------------------------------------------------------------


def _unwrap_llm_value(raw: Any) -> tuple[Any, float | None]:
    """Unwrap ``{"value": ..., "confidence": ...}`` style LLM entries."""
    if isinstance(raw, dict) and "value" in raw:
        conf = raw.get("confidence")
        return raw["value"], (float(conf) if conf is not None else None)
    return raw, None


def _merge_llm(
    fields: dict[str, FieldInference],
    meta: dict[str, Any],
    llm_result: dict[str, Any],
) -> None:
    """Merge an LLM-supplied structured result over heuristic fields.

    Accepted shapes (all optional, freely combinable):

    * flat fields: ``{"language": "english", "target_wpm": 145, ...}``
    * per-field confidence: ``{"confidences": {"language": 0.9}}`` or
      ``{"language_confidence": 0.9}``
    * wrapped values: ``{"tone": {"value": [...], "confidence": 0.9}}``
    * field list: ``{"fields": [{"field": ..., "value": ..., "confidence": ...}]}``
    * meta keys: ``candidate_name`` / ``name`` / ``label``,
      ``language_signal``, ``content_form``, ``archetype``,
      ``secondary_archetype``
    """
    confidences: dict[str, Any] = {}
    if isinstance(llm_result.get("confidences"), dict):
        confidences.update(llm_result["confidences"])
    if isinstance(llm_result.get("confidence"), dict):
        confidences.update(llm_result["confidence"])

    def _put(field: str, raw: Any) -> None:
        value, inline_conf = _unwrap_llm_value(raw)
        conf: float | None = inline_conf
        if conf is None and field in confidences:
            try:
                conf = float(confidences[field])
            except (TypeError, ValueError):
                conf = None
        if conf is None and f"{field}_confidence" in llm_result:
            try:
                conf = float(llm_result[f"{field}_confidence"])
            except (TypeError, ValueError):
                conf = None
        if conf is None:
            conf = 0.85  # LLM structured output: trusted, not certain.
        fields[field] = FieldInference(
            field=field,
            value=value,
            confidence=max(0.0, min(1.0, conf)),
            source="idea",
        )

    known_fields = {
        "language", "tone", "target_wpm", "sentence_style",
        "structure_template", "hook_families", "voice_style",
        "visual_style", "cta_style", "risk_class", "sensitive_flags",
        "taboo", "forbidden_patterns", "special_rules", "stats_heavy",
    }
    for key, raw in llm_result.items():
        if key in known_fields:
            _put(key, raw)

    raw_fields = llm_result.get("fields")
    if isinstance(raw_fields, dict):
        for key, raw in raw_fields.items():
            _put(str(key), raw)
    elif isinstance(raw_fields, list):
        for item in raw_fields:
            if isinstance(item, dict) and "field" in item and "value" in item:
                _put(str(item["field"]), item)

    for meta_key, aliases in (
        ("candidate_name", ("candidate_name", "name", "label")),
        ("language_signal", ("language_signal",)),
        ("content_form", ("content_form", "form")),
        ("archetype", ("archetype",)),
        ("secondary_archetype", ("secondary_archetype", "secondary")),
    ):
        for alias in aliases:
            if alias in llm_result and llm_result[alias]:
                meta[meta_key] = llm_result[alias]
                break


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def infer_profile_fields(
    name: str,
    idea: str,
    llm_result: dict[str, Any] | None = None,
) -> ProfileInference:
    """Infer a provisional niche profile's fields from a one-line idea.

    Deterministic heuristic implementation of roadmap steps 1-3.  When
    ``llm_result`` is supplied (structured JSON from an LLM caller) its
    values are merged over the heuristics, per field, with their own
    confidence scores.
    """
    candidate_name = (name or "").strip()
    idea_text = (idea or "").strip()
    combined = f"{candidate_name} {idea_text}".strip()
    low = combined.lower()
    tokens = _tokens(combined)

    if not candidate_name:
        # Derive a candidate name from the first words of the idea.
        words = idea_text.split()
        candidate_name = " ".join(words[:4]).strip(" -—:,.") or "Custom Niche"

    # -- Step 1: language + content form -----------------------------------
    language, language_signal, language_conf, _ = _detect_language(combined, tokens)
    content_form, form_hits = _detect_form(combined)

    # -- Step 2: archetype matching ---------------------------------------
    domain = _detect_domain(combined)
    domain_archetype: str | None = domain["archetype"] if domain else None

    story_signal = form_hits.get("stories", 0) > 0 or _contains_any(
        low, ("kahani", "story", "stories", "narrative")
    )
    list_signal = content_form == "listicle"

    analysis_signal = content_form == "analysis" or _contains_any(
        low, ("analysis", "analyse", "analyze", "tajziya", "breakdown", "explained")
    )
    if list_signal:
        archetype = "listicle"
    elif domain_archetype and domain_archetype != "explainer":
        # A specific built-in domain (horror, health, islamic, ...) is
        # the nearest archetype; generic domains already say explainer.
        archetype = domain_archetype
    elif analysis_signal:
        # An explicit analysis form beats a story flavour in the same
        # idea - the story flavour survives as the secondary archetype
        # (the roadmap's Explainer + Narrative hybrid).
        archetype = domain_archetype or "explainer"
    elif story_signal and content_form in ("stories",):
        archetype = "narrative_story"
    elif domain_archetype:
        archetype = domain_archetype
    else:
        archetype = "explainer"

    secondary: str | None = None
    if archetype == "explainer" and story_signal:
        # Explainer + Narrative hybrid (the cricket worked example).
        secondary = "narrative_story"
    elif archetype == "narrative_story" and (
        content_form == "analysis" or "analysis" in low or "explained" in low
    ):
        secondary = "explainer"
    elif archetype not in GENERIC_ARCHETYPES and story_signal and archetype not in (
        "horror_stories", "islamic_stories", "crime_stories",
    ):
        secondary = "narrative_story"
    if secondary == archetype:
        secondary = None

    # Normalise the ProfileInference content_form vocabulary to the
    # roadmap's set: stories / analysis / listicle / news / tutorial /
    # commentary / explainer.
    if content_form == "stories":
        profile_form = "stories"
    elif content_form in ("analysis", "tutorial", "news", "commentary", "listicle"):
        profile_form = content_form
    else:
        profile_form = "explainer"

    # -- Step 3: field inference -------------------------------------------
    fields: dict[str, FieldInference] = {}

    def put(field: str, value: Any, confidence: float, source: Literal["idea", "archetype_default", "clamp"] = "idea") -> None:
        fields[field] = FieldInference(
            field=field, value=value, confidence=confidence, source=source
        )

    put("language", language, language_conf)

    stats_heavy = detect_stats_heavy(combined)
    sensitive = detect_sensitive_flags(combined)

    # Tone / pace / hooks / styles: domain-driven when a domain matched,
    # otherwise form-driven at lower confidence.
    if domain is not None:
        put("tone", list(domain["tone"]), 0.85)
        put("target_wpm", int(domain["wpm"]), 0.85)
        put("hook_families", list(domain["hooks"]), 0.85)
        put("sentence_style", str(domain["sentence_style"]), 0.75)
        put("voice_style", str(domain["voice_style"]), 0.75)
        put("visual_style", str(domain["visual_style"]), 0.7)
        put("cta_style", str(domain["cta_style"]), 0.7)
    elif archetype == "narrative_story":
        put("tone", ["engaging", "emotional", "descriptive"], 0.55, "archetype_default")
        put("target_wpm", 120, 0.55, "archetype_default")
        put("hook_families", ["H3", "H4"], 0.55, "archetype_default")
        put("sentence_style", "descriptive sentences with a slow, immersive build", 0.5, "archetype_default")
        put("voice_style", "warm storytelling voice", 0.5, "archetype_default")
        put("visual_style", "cinematic scenes matching the story beats", 0.5, "archetype_default")
        put("cta_style", "ending question + next-story tease", 0.5, "archetype_default")
    elif archetype == "listicle":
        put("tone", ["energetic", "punchy", "curious"], 0.6)
        put("target_wpm", 150, 0.6)
        put("hook_families", ["H2", "H1"], 0.6)
        put("sentence_style", "short punchy sentences, one item per beat", 0.55, "archetype_default")
        put("voice_style", "energetic countdown presenter voice", 0.55, "archetype_default")
        put("visual_style", "numbered on-screen cards and bold item visuals", 0.55, "archetype_default")
        put("cta_style", "comment question - which item surprised you most?", 0.55, "archetype_default")
    else:  # explainer fallback
        put("tone", ["energetic", "curious", "clear"], 0.55, "archetype_default")
        put("target_wpm", 140, 0.55, "archetype_default")
        put("hook_families", ["H1", "H8"], 0.55, "archetype_default")
        put("sentence_style", "short spoken sentences, one idea per sentence", 0.5, "archetype_default")
        put("voice_style", "clear, friendly explainer voice", 0.5, "archetype_default")
        put("visual_style", "clean graphics, diagrams and bold on-screen text", 0.5, "archetype_default")
        put("cta_style", "comment question + subscribe prompt", 0.5, "archetype_default")

    # Explicit pace words override the domain pace.
    if _contains_any(low, ("slow", "calm", "aahista", "slowly", "relaxing")):
        put("target_wpm", 115, 0.8)
    elif _contains_any(low, ("fast paced", "fast-paced", "tez", "energetic", "high energy")):
        put("target_wpm", 150, 0.8)

    # Structure: domain-specific template at usable confidence,
    # otherwise the form template at fallback confidence.
    if domain is not None and domain["domain"] in _STRUCTURE_TEMPLATES:
        put(
            "structure_template",
            list(_STRUCTURE_TEMPLATES[domain["domain"]]),
            0.8,
        )
    elif content_form in _STRUCTURE_TEMPLATES:
        conf = 0.65 if content_form in ("listicle", "tutorial", "news") else 0.55
        src: Literal["idea", "archetype_default", "clamp"] = "idea" if conf >= 0.6 else "archetype_default"
        put("structure_template", list(_STRUCTURE_TEMPLATES[content_form]), conf, src)
    else:
        put(
            "structure_template",
            list(_STRUCTURE_TEMPLATES["explainer"]),
            0.5,
            "archetype_default",
        )

    # Cricket / sports hybrid refinement: a stats-driven explainer with a
    # story signal uses the sports structure even when the generic
    # 'cricket_sport' domain matched via a single keyword above - this is
    # already handled by the domain branch; keep the hook order H3, H2
    # exactly as in the roadmap example (cold-open first, stats second).
    # (No extra work needed: _DOMAIN_PROFILES encodes it.)

    # Sensitive flags + risk class.
    if sensitive:
        put("sensitive_flags", sensitive, 0.9)
        put("risk_class", "sensitive", 0.9)
    else:
        put("sensitive_flags", [], 0.8)
        put("risk_class", "low", 0.8)

    # Stats-heavy: mandatory fact-check rule (roadmap worked example).
    if stats_heavy:
        put("stats_heavy", True, 0.9)
        put("special_rules", [STATS_RULE], 0.9)
    else:
        put("stats_heavy", False, 0.7)

    meta: dict[str, Any] = {
        "candidate_name": candidate_name,
        "language_signal": language_signal,
        "content_form": profile_form,
        "archetype": archetype,
        "secondary_archetype": secondary,
    }

    # -- Optional LLM merge (production plug-in point) ----------------------
    if llm_result:
        _merge_llm(fields, meta, llm_result)
        # Re-derive aggregates the builder relies on if the LLM changed
        # raw fields but not the aggregates.
        if "sensitive_flags" in llm_result or "fields" in llm_result:
            llm_flags = fields.get("sensitive_flags")
            if llm_flags is not None and isinstance(llm_flags.value, list) and llm_flags.value:
                put("risk_class", "sensitive", max(0.85, llm_flags.confidence))

    return ProfileInference(
        candidate_name=str(meta["candidate_name"]),
        language_signal=str(meta["language_signal"]),
        content_form=str(meta["content_form"]),
        archetype=str(meta["archetype"]),
        secondary_archetype=(
            str(meta["secondary_archetype"]) if meta.get("secondary_archetype") else None
        ),
        fields=list(fields.values()),
    )
