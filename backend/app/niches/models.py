"""Niche Profile data model.

A niche is DATA, not code (roadmap Section 33): every scripting prompt is
assembled at run time from a :class:`NicheProfile`, so built-in niches and
user-created "Other" niches run through the identical pipeline.

The field set and the deterministic clamp/validate rules mirror the
OTHER-niche auto-build step 4 in the roadmap:

* ``target_wpm`` is *clamped* to 110..160 (never rejected for being out of
  range — the archetype/inference value is pulled into the safe band).
* ``structure_template`` must open with a hook section and end with a
  conclusion section.
* ``sensitive_flags`` is a closed subset of
  health / finance / law / religion / politics; any flag present forces the
  corresponding hard-rule pack on downstream, regardless of tone.
* ``hook_families`` holds Hook Bank formula IDs (``"H1"`` .. ``"H8"``).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "ArchetypeName",
    "BuiltinProfileId",
    "HookFamilyId",
    "NicheProfile",
    "RiskClass",
    "SensitiveFlag",
    "SUPPORTED_SENSITIVE_FLAGS",
    "WPM_MAX",
    "WPM_MIN",
]

#: Deterministic WPM clamp band (roadmap Section 33, auto-build step 4).
WPM_MIN: int = 110
WPM_MAX: int = 160

#: The 8 built-in profile ids, in library order.
BuiltinProfileId = Literal[
    "facts",
    "islamic_stories",
    "horror_stories",
    "history",
    "motivation",
    "tech",
    "crime_stories",
    "health",
]

#: Archetypes a profile can be classified against: the 8 built-ins plus the
#: 3 generic fallbacks used when an "Other" niche matches nothing closely
#: (Narrative-Story, Explainer, Listicle).
ArchetypeName = Literal[
    "facts",
    "islamic_stories",
    "horror_stories",
    "history",
    "motivation",
    "tech",
    "crime_stories",
    "health",
    "narrative_story",
    "explainer",
    "listicle",
]

#: Hook Bank formula IDs (roadmap Section 33, Hook Bank H1..H8).
HookFamilyId = Literal["H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8"]

#: Closed set of sensitive categories. Any flag forces its hard-rule pack on.
SensitiveFlag = Literal["health", "finance", "law", "religion", "politics"]

SUPPORTED_SENSITIVE_FLAGS: tuple[str, ...] = (
    "health",
    "finance",
    "law",
    "religion",
    "politics",
)

RiskClass = Literal["low", "sensitive"]

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_HOOK_FAMILY_RE = re.compile(r"^H[1-8]$")


class NicheProfile(BaseModel):
    """One complete, versioned niche profile.

    Instances are the single source of truth injected into the Niche
    Profile Layer of every scripting prompt (language, tone, WPM,
    structure, hook families, voice/visual hints, CTA style, sensitive
    rules, forbidden patterns).
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # -- identity ---------------------------------------------------------
    profile_id: str = Field(
        ...,
        description="Slug id, e.g. 'facts' or 'islamic_stories'.",
    )
    name: str = Field(..., description="Human-readable label, e.g. 'Facts / Curiosity'.")
    source: Literal["builtin", "other"] = Field(
        ...,
        description="'builtin' for the 8 shipped profiles, 'other' for auto-built ones.",
    )
    status: Literal["established", "provisional"] = Field(
        default="established",
        description=(
            "'provisional' for a freshly auto-built Other profile; it graduates "
            "to 'established' after 5 videos or one user confirmation."
        ),
    )
    version: int = Field(default=1, ge=1, description="Profile version (profiles are versioned like scripts).")

    # -- language & voice of the writing ----------------------------------
    language: str = Field(
        ...,
        description=(
            "Narration language code/mix, e.g. 'roman_urdu', 'urdu', 'english', "
            "'roman_urdu+english', 'english+roman_urdu'."
        ),
    )
    tone: list[str] = Field(
        ...,
        description="Exactly 3 tone adjectives, e.g. ['energetic', 'curious', 'fast-paced'].",
    )
    target_wpm: int = Field(
        ...,
        description=f"Target narration speed in words per minute; clamped to {WPM_MIN}..{WPM_MAX}.",
    )
    sentence_style: str = Field(
        ...,
        description="Sentence-length / spoken-style guidance for the writer and TTS.",
    )

    # -- structure & retention ---------------------------------------------
    structure_template: list[str] = Field(
        ...,
        description=(
            "Ordered section template. The first section must be a hook "
            "section and the last section must be a conclusion section."
        ),
    )
    hook_families: list[str] = Field(
        ...,
        description="Allowed Hook Bank formula IDs, each 'H1'..'H8'.",
    )

    # -- production hints ---------------------------------------------------
    voice_style: str = Field(..., description="Voiceover delivery hint for TTS / voice selection.")
    visual_style: str = Field(..., description="Visual / B-roll style hint for scene planning.")
    cta_style: str = Field(..., description="Call-to-action wording style used at the conclusion.")

    # -- safety ---------------------------------------------------------------
    risk_class: RiskClass = Field(
        default="low",
        description="'sensitive' for niches whose rule pack is enforced as a gate (religion, health, crime...).",
    )
    sensitive_flags: list[SensitiveFlag] = Field(
        default_factory=list,
        description="Subset of health/finance/law/religion/politics; each flag forces its hard-rule pack on.",
    )
    taboo: list[str] = Field(
        default_factory=list,
        description="Topics / depictions that must never appear in this niche.",
    )
    forbidden_patterns: list[str] = Field(
        default_factory=list,
        description="Language patterns the writer must never produce (e.g. fabricated claims, cure claims).",
    )
    special_rules: list[str] = Field(
        default_factory=list,
        description="Niche hard rules, e.g. mandatory fact-check classes and structural disclaimer rules.",
    )

    # -- validators -----------------------------------------------------------
    @field_validator("profile_id")
    @classmethod
    def _profile_id_is_slug(cls, value: str) -> str:
        slug = value.strip().lower()
        if not _SLUG_RE.match(slug):
            raise ValueError(
                f"profile_id must be a lowercase slug (a-z, 0-9, '_'), got {value!r}"
            )
        return slug

    @field_validator("target_wpm", mode="before")
    @classmethod
    def _clamp_target_wpm(cls, value: object) -> object:
        """Clamp (do not reject) WPM into the 110..160 band."""
        if isinstance(value, bool):  # bool is an int subclass; never a WPM
            raise ValueError("target_wpm must be an integer")
        if isinstance(value, (int, float)):
            return max(WPM_MIN, min(WPM_MAX, int(value)))
        return value

    @field_validator("tone")
    @classmethod
    def _tone_is_three_adjectives(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if len(cleaned) != 3:
            raise ValueError(
                f"tone must contain exactly 3 adjectives, got {len(cleaned)}: {value!r}"
            )
        return cleaned

    @field_validator("hook_families")
    @classmethod
    def _hook_families_are_valid_ids(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            formula = item.strip().upper()
            if not _HOOK_FAMILY_RE.match(formula):
                raise ValueError(
                    f"hook_families entries must be Hook Bank IDs 'H1'..'H8', got {item!r}"
                )
            if formula not in cleaned:
                cleaned.append(formula)
        if not cleaned:
            raise ValueError("hook_families must contain at least one Hook Bank ID")
        return cleaned

    @field_validator("sensitive_flags")
    @classmethod
    def _sensitive_flags_deduped(cls, value: list[SensitiveFlag]) -> list[SensitiveFlag]:
        # The Literal type already restricts entries to the supported subset;
        # here we only normalise ordering/duplicates away.
        seen: list[SensitiveFlag] = []
        for flag in value:
            if flag not in seen:
                seen.append(flag)
        return seen

    @model_validator(mode="after")
    def _structure_has_hook_first_and_conclusion_last(self) -> "NicheProfile":
        structure = [section.strip() for section in self.structure_template]
        if len(structure) < 2:
            raise ValueError(
                "structure_template must contain at least a hook section and a conclusion section"
            )
        if "hook" not in structure[0].lower():
            raise ValueError(
                "structure_template must start with a hook section, "
                f"got first section {structure[0]!r}"
            )
        if "conclusion" not in structure[-1].lower():
            raise ValueError(
                "structure_template must end with a conclusion section, "
                f"got last section {structure[-1]!r}"
            )
        self.structure_template = structure
        return self

    # -- helpers ----------------------------------------------------------------
    def target_words(self, target_minutes: float) -> int:
        """Roadmap formula: ``target_words = target_minutes x profile.WPM``.

        This is the UNMEASURED planning formula (the profile's
        ``target_wpm`` constant, 110..160).  It remains for callers
        that have no voice context; production planning should prefer
        :meth:`calibrated_target_words`, which plans with the selected
        voice's measured WPM when calibration data exists.
        """
        if target_minutes <= 0:
            raise ValueError("target_minutes must be positive")
        return int(round(target_minutes * self.target_wpm))

    # -- calibrated planning (Round 4) ------------------------------------
    def planning_wpm(
        self,
        voice_id: str | None = None,
        rate_pct: float = 0.0,
        *,
        store: object | None = None,
        input_script: str | None = None,
    ):
        """The WPM to plan script length with for ``voice_id``/``rate_pct``.

        Returns a ``CalibratedWpm`` (from
        :mod:`app.providers.tts.calibration_store`): the voice's
        MEASURED WPM with provenance (version / measured_at / method)
        when calibration data exists, otherwise this profile's
        ``target_wpm`` planning constant with provenance explicitly
        labelled ``"unmeasured-default"`` — never silently presented
        as measured.  With ``voice_id=None`` no voice can be measured,
        so the unmeasured default is returned.  The store import is
        lazy so the niches package keeps its pydantic-only top level.
        """
        from app.providers.tts.calibration_store import resolve_planning_wpm

        return resolve_planning_wpm(
            voice_id or "",
            rate_pct,
            fallback_wpm=float(self.target_wpm),
            store=store,  # type: ignore[arg-type]
            input_script=input_script,
        )

    def calibrated_target_words(
        self,
        target_minutes: float,
        voice_id: str | None = None,
        rate_pct: float = 0.0,
        *,
        store: object | None = None,
        input_script: str | None = None,
    ) -> tuple[int, object]:
        """``(target_words, CalibratedWpm)`` planned with measured WPM.

        ``target_words = calibrated_wpm x target_minutes`` — with real
        calibration data (Round 3 measured Urdu edge-tts at ~199–225
        WPM vs the 110..160 planning band) the plan lands on the
        duration target at the voice's natural rate, with no post-hoc
        rate override.  The second tuple element carries the WPM's
        provenance for logging/persistence.
        """
        if target_minutes <= 0:
            raise ValueError("target_minutes must be positive")
        calibrated = self.planning_wpm(
            voice_id, rate_pct, store=store, input_script=input_script)
        return int(round(target_minutes * calibrated.wpm)), calibrated
