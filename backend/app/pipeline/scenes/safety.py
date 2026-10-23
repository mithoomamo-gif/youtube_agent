"""Faceless-safety hard gates — roadmap §12 / Part 9.3.

Safety rules are hard gates, not prompts: they run as data + validators
*after* sourcing/generation decisions, and a blocked scene can never
enter assembly (it becomes a SAFETY_HOLD and is re-planned to a safe
alternative — symbolic / graphic / text card).

Two layers:

* **Global gates** (every niche): recognisable real-person AI likeness,
  gore, misleading photorealism of real named subjects, logos /
  watermarks, AI photoreal children, generated in-image text.
* **Niche overlays**: a per-niche taboo list is a REQUIRED input (an
  "Other" niche cannot be planned until its taboo list exists, even if
  it is "none known").  The Islamic overlay is the strictest launch
  rule: no depiction of Prophets in any form, and scripture only as
  programmatic text cards carrying a surah/ayah reference.
"""

from __future__ import annotations

import enum
import re
from typing import Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .models import SceneClass, SceneRecord, SourceKind


class SafetyFlag(str, enum.Enum):
    """Classifier / detector flags that BLOCK a scene when present."""

    RECOGNISABLE_REAL_PERSON_LIKENESS = "recognisable_real_person_likeness"
    GORE = "gore"
    MISLEADING_PHOTOREAL_NAMED_SUBJECT = "misleading_photoreal_named_subject"
    LOGO_OR_WATERMARK = "logo_or_watermark"
    AI_PHOTOREAL_CHILD = "ai_photoreal_child"
    GENERATED_IN_IMAGE_TEXT = "generated_in_image_text"
    DEPICTS_PROPHET = "depicts_prophet"
    ANTHROPOMORPHIC_DEITY = "anthropomorphic_deity"
    HUMAN_FIGURE_FORBIDDEN = "human_figure_forbidden"


#: Every global flag blocks.  (Kept as data so Policy/QC can read it.)
GLOBAL_BLOCKING_FLAGS: frozenset[SafetyFlag] = frozenset(SafetyFlag)


class SafetyViolationCode(str, enum.Enum):
    BLOCKING_FLAG = "BLOCKING_FLAG"
    TABOO_TERM = "TABOO_TERM"
    PROPHET_DEPICTION = "PROPHET_DEPICTION"
    SCRIPTURE_NOT_TEXT_CARD = "SCRIPTURE_NOT_TEXT_CARD"
    SCRIPTURE_MISSING_REFERENCE = "SCRIPTURE_MISSING_REFERENCE"


class NicheSafetyProfile(BaseModel):
    """Per-niche safety overlay input to scene planning.

    ``taboo_terms`` is the per-niche taboo list: scene prompts/queries
    matching any term are blocked.  An empty list is a *recorded
    decision* ("none known"), not a missing input — planning a niche
    without a profile object at all is what the roadmap forbids.
    """

    model_config = ConfigDict(extra="forbid")

    niche_id: str = Field(min_length=1)
    is_islamic: bool = False
    taboo_terms: list[str] = Field(default_factory=list)
    #: Religious scenes in this niche must contain no human figures.
    forbid_human_figures: bool = False


class SceneSafetyFacts(BaseModel):
    """Everything the safety validators need to judge one scene.

    Boolean facts come from pre-generation screening and
    post-acquisition classifiers; ``detected_flags`` carries classifier
    output verbatim.  All default to the safe value.
    """

    model_config = ConfigDict(extra="forbid")

    scene_id: str = Field(min_length=1)
    scene_class: SceneClass
    chosen_source: SourceKind
    #: Generation prompt or stock query for this scene (screened text).
    visual_prompt: str = ""
    narration_text: str = ""
    # Global gate facts ------------------------------------------------------
    has_recognisable_real_person_likeness: bool = False
    has_gore: bool = False
    #: Photoreal AI presenting a real named place/event/person as footage.
    misleading_photoreal_named_subject: bool = False
    has_logo_or_watermark: bool = False
    has_ai_photoreal_child: bool = False
    has_generated_in_image_text: bool = False
    detected_flags: list[SafetyFlag] = Field(default_factory=list)
    # Religious overlay facts --------------------------------------------------
    depicts_prophet: bool = False
    depicts_human_figure: bool = False
    is_scripture: bool = False
    surah_ref: Optional[str] = None
    ayah_ref: Optional[str] = None


class SafetyViolation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: SafetyViolationCode
    scene_id: str
    flag: Optional[str] = None
    message: str


class SafetyAssessment(BaseModel):
    """Result of judging one scene: blocked scenes never assemble."""

    model_config = ConfigDict(extra="forbid")

    scene_id: str
    blocked: bool
    violations: list[SafetyViolation] = Field(default_factory=list)

    @property
    def flags(self) -> list[str]:
        return [v.flag for v in self.violations if v.flag]


# ---------------------------------------------------------------------------
# Prophet-depiction screening (Islamic overlay)
# ---------------------------------------------------------------------------

#: Names / titles that refer to Prophets in Islamic tradition.  Matching
#: is on the *visual prompt* (what would be drawn/searched), not on the
#: narration: mentioning a Prophet in narration is normal for the niche;
#: asking an image pipeline to depict one is the hard block.
_PROPHET_TERMS = (
    "prophet muhammad",
    "muhammad",
    "prophet musa",
    "musa (as)",
    "prophet isa",
    "prophet jesus",
    "prophet ibrahim",
    "prophet abraham",
    "prophet yusuf",
    "prophet joseph",
    "prophet nuh",
    "prophet noah",
    "prophet adam",
    "the prophet",
    "holy prophet",
    "messenger of allah",
)

_DEPICTION_MARKERS = (
    "portrait",
    "face",
    "figure",
    "silhouette",
    "image of",
    "picture of",
    "depict",
    "illustration of",
    "painting of",
    "drawing of",
    "man in",
    "bearded man",
    "person",
    "body",
)


def indicates_prophet_depiction(visual_prompt: str) -> bool:
    """True when a visual prompt asks to depict a Prophet.

    Deterministic screening: a prophet term together with a depiction
    marker, or a bare prophet term in a prompt for a *figural* scene —
    the caller decides the latter via the explicit ``depicts_prophet``
    fact.  Here, term + marker is the textual hard gate.
    """
    text = f" {visual_prompt.lower()} "
    has_term = any(term in text for term in _PROPHET_TERMS)
    if not has_term:
        return False
    return any(marker in text for marker in _DEPICTION_MARKERS)


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

# Facts -> blocking flag, for the six global gates.
_GLOBAL_FACT_FLAGS: tuple[tuple[str, SafetyFlag, str], ...] = (
    (
        "has_recognisable_real_person_likeness",
        SafetyFlag.RECOGNISABLE_REAL_PERSON_LIKENESS,
        "recognisable real-person likeness (AI or otherwise) is forbidden",
    ),
    (
        "has_gore",
        SafetyFlag.GORE,
        "graphic gore / real death / real suffering is forbidden; "
        "violence may only be symbolic",
    ),
    (
        "misleading_photoreal_named_subject",
        SafetyFlag.MISLEADING_PHOTOREAL_NAMED_SUBJECT,
        "photoreal AI must not depict a real named place/event/person "
        "as if it were footage",
    ),
    (
        "has_logo_or_watermark",
        SafetyFlag.LOGO_OR_WATERMARK,
        "third-party branding / watermarks / logos are forbidden",
    ),
    (
        "has_ai_photoreal_child",
        SafetyFlag.AI_PHOTOREAL_CHILD,
        "AI photoreal children are forbidden; child characters default "
        "to illustration",
    ),
    (
        "has_generated_in_image_text",
        SafetyFlag.GENERATED_IN_IMAGE_TEXT,
        "generated in-image text is forbidden; all words are "
        "programmatic overlays / cards",
    ),
)


def validate_scene_safety(
    facts: SceneSafetyFacts,
    niche: Optional[NicheSafetyProfile] = None,
) -> SafetyAssessment:
    """Judge one scene against the global gates + the niche overlay."""
    violations: list[SafetyViolation] = []

    def block(code: SafetyViolationCode, message: str, flag: Optional[str] = None):
        violations.append(
            SafetyViolation(
                code=code, scene_id=facts.scene_id, flag=flag, message=message
            )
        )

    # -- Global gates: boolean facts ---------------------------------------
    for attr, flag, message in _GLOBAL_FACT_FLAGS:
        if getattr(facts, attr):
            block(
                SafetyViolationCode.BLOCKING_FLAG,
                f"scene {facts.scene_id}: {message}",
                flag=flag.value,
            )

    # -- Global gates: classifier flags --------------------------------------
    for flag in facts.detected_flags:
        if flag in GLOBAL_BLOCKING_FLAGS:
            block(
                SafetyViolationCode.BLOCKING_FLAG,
                f"scene {facts.scene_id}: classifier flag {flag.value} blocks "
                f"this scene from assembly",
                flag=flag.value,
            )

    # -- Niche taboo list ------------------------------------------------------
    if niche is not None and facts.visual_prompt:
        prompt_lower = facts.visual_prompt.lower()
        for term in niche.taboo_terms:
            term_clean = term.strip().lower()
            if term_clean and term_clean in prompt_lower:
                block(
                    SafetyViolationCode.TABOO_TERM,
                    f"scene {facts.scene_id}: visual prompt matches niche "
                    f"taboo term {term!r} (niche {niche.niche_id})",
                    flag=f"taboo:{term_clean}",
                )

    # -- Islamic overlay --------------------------------------------------------
    if niche is not None and niche.is_islamic:
        if facts.depicts_prophet or SafetyFlag.DEPICTS_PROPHET in facts.detected_flags:
            block(
                SafetyViolationCode.PROPHET_DEPICTION,
                f"scene {facts.scene_id}: depiction of Prophets is "
                f"forbidden in Islamic niches in any form (face, figure, "
                f"silhouette-as-person); use calligraphy text cards, "
                f"architecture, nature or light symbolism instead",
                flag=SafetyFlag.DEPICTS_PROPHET.value,
            )
        elif facts.chosen_source in (
            SourceKind.AI_IMAGE,
            SourceKind.AI_VIDEO,
            SourceKind.STOCK,
        ) and indicates_prophet_depiction(facts.visual_prompt):
            block(
                SafetyViolationCode.PROPHET_DEPICTION,
                f"scene {facts.scene_id}: visual prompt indicates "
                f"depiction of a Prophet, which is forbidden in Islamic "
                f"niches",
                flag=SafetyFlag.DEPICTS_PROPHET.value,
            )
        if niche.forbid_human_figures and facts.depicts_human_figure:
            block(
                SafetyViolationCode.BLOCKING_FLAG,
                f"scene {facts.scene_id}: human figures are forbidden for "
                f"this niche's religious scenes",
                flag=SafetyFlag.HUMAN_FIGURE_FORBIDDEN.value,
            )

    # -- Scripture handling (any niche; strictest in Islamic) ------------------
    if facts.is_scripture:
        if facts.chosen_source is not SourceKind.TEXT_CARD:
            block(
                SafetyViolationCode.SCRIPTURE_NOT_TEXT_CARD,
                f"scene {facts.scene_id}: scripture may only appear as a "
                f"programmatic text card, not as {facts.chosen_source.value}",
                flag="scripture_not_text_card",
            )
        if not facts.surah_ref or not facts.ayah_ref:
            block(
                SafetyViolationCode.SCRIPTURE_MISSING_REFERENCE,
                f"scene {facts.scene_id}: scripture text card must carry "
                f"a surah/ayah reference (surah={facts.surah_ref!r}, "
                f"ayah={facts.ayah_ref!r})",
                flag="scripture_missing_reference",
            )

    return SafetyAssessment(
        scene_id=facts.scene_id,
        blocked=len(violations) > 0,
        violations=violations,
    )


def validate_plan_safety(
    facts_list: Sequence[SceneSafetyFacts],
    niche: Optional[NicheSafetyProfile] = None,
) -> list[SafetyAssessment]:
    """Judge every scene; returns the assessments of *blocked* scenes only."""
    return [
        a
        for a in (validate_scene_safety(f, niche) for f in facts_list)
        if a.blocked
    ]


def facts_from_scene(scene: SceneRecord) -> SceneSafetyFacts:
    """Build safety facts from a manifest record's own fields.

    Classifier-derived booleans are not stored on the record, so they
    default to safe here; the record's ``safety_flags`` are mapped into
    ``detected_flags`` when they name a known :class:`SafetyFlag`.
    """
    known = {f.value: f for f in SafetyFlag}
    detected = [known[f] for f in scene.safety_flags if f in known]
    return SceneSafetyFacts(
        scene_id=scene.scene_id,
        scene_class=scene.scene_class,
        chosen_source=scene.chosen_source,
        visual_prompt=scene.generation_prompt or scene.stock_query or "",
        narration_text=scene.source_sentence,
        detected_flags=detected,
        depicts_prophet="depicts_prophet" in scene.safety_flags,
        is_scripture=scene.is_scripture,
        surah_ref=scene.surah_ref,
        ayah_ref=scene.ayah_ref,
    )


# Re-export helper for callers that screen raw prompt text for taboos.
def prompt_matches_taboo(prompt: str, niche: NicheSafetyProfile) -> Optional[str]:
    """Return the first taboo term found in ``prompt``, or None."""
    lowered = prompt.lower()
    for term in niche.taboo_terms:
        clean = term.strip().lower()
        if clean and re.search(re.escape(clean), lowered):
            return term
    return None
