"""Scene manifest models — Worker 1 (Scene Planning), roadmap Part 6.4 / §12.

Design rules encoded here (from the master roadmap):

* Every scene is a *record*, not a file: the manifest row is the edit
  decision list for FFmpeg and the audit trail for QC / Copyright /
  Analytics.  It therefore carries the chosen source, the licence-record
  reference, the content hash and the reuse count.
* The atomic unit of segmentation is the *sentence* (never cut
  mid-sentence), and ``start``/``end`` come from the measured narration
  timing spine (see :mod:`.timing`), never from text-length estimates.
* Exactly one primary scene class per scene (the 8 classes below) and
  HERO scenes are capped at :data:`MAX_HERO_SCENES` per video.

Beat-duration, motion-alternation, budget and safety *policy* checks are
deliberately NOT model-level rejects: they live in :mod:`.beats`,
:mod:`.motion`, :mod:`.decision` and :mod:`.safety` as validators that
*flag* violations, so a planner can report and re-plan instead of
crashing.  The model itself only enforces structural invariants
(``end > start``, non-negative reuse, overlay word limit, hero cap,
unique ids inside a manifest).
"""

from __future__ import annotations

import enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Constants (roadmap §12 / Part 6.4 defaults)
# ---------------------------------------------------------------------------

#: HERO (thumbnail / climax) scenes per video: "max 2-3 per video".
MAX_HERO_SCENES = 3

#: Overlay text is rendered programmatically and capped at 6 words.
MAX_OVERLAY_WORDS = 6

#: Stock relevance floor: candidates below this cosine are rejected.
STOCK_RELEVANCE_FLOOR = 0.22

#: Per-video AI budget ceilings (fractions of all scenes).
AI_SCENE_BUDGET_FRACTION = 0.40
AI_VIDEO_BUDGET_FRACTION = 0.10


class SceneClass(str, enum.Enum):
    """The 8 primary scene classes — exactly one per scene."""

    REAL_WORLD = "REAL_WORLD"
    SPECIFIC_EVENT = "SPECIFIC_EVENT"
    ABSTRACT_CONCEPT = "ABSTRACT_CONCEPT"
    DATA = "DATA"
    TEXTUAL = "TEXTUAL"
    CHARACTER_MOMENT = "CHARACTER_MOMENT"
    TRANSITION = "TRANSITION"
    HERO = "HERO"


class BeatClass(str, enum.Enum):
    """Narration beat a scene serves; drives the duration rules in beats.py."""

    HOOK = "HOOK"
    STANDARD = "STANDARD"
    EXPLANATORY = "EXPLANATORY"
    EMOTIONAL = "EMOTIONAL"
    LIST = "LIST"


class SourceKind(str, enum.Enum):
    """Where a scene's visual comes from (the asset decision)."""

    STOCK = "stock"
    AI_IMAGE = "ai_image"
    AI_VIDEO = "ai_video"
    #: Programmatic graphic (chart / timeline / map rendered deterministically).
    GRAPHIC = "graphic"
    #: Programmatic text card (quotes / verses — text is never generated).
    TEXT_CARD = "text_card"


#: Source kinds that consume the per-video AI budget.
AI_SOURCE_KINDS = frozenset({SourceKind.AI_IMAGE, SourceKind.AI_VIDEO})

#: Source kinds rendered programmatically (deterministic, no generation).
PROGRAMMATIC_SOURCE_KINDS = frozenset({SourceKind.GRAPHIC, SourceKind.TEXT_CARD})


class ReasonCode(str, enum.Enum):
    """Decision reason codes — every sourcing decision is logged with one."""

    STOCK_MATCH = "STOCK_MATCH"
    STOCK_BELOW_THRESHOLD = "STOCK_BELOW_THRESHOLD"
    STOCK_UNVERIFIED_NAMED = "STOCK_UNVERIFIED_NAMED"
    NO_STOCK_EXISTS = "NO_STOCK_EXISTS"
    SAFETY_ROUTE = "SAFETY_ROUTE"
    HERO_UPGRADE = "HERO_UPGRADE"
    HERO_CAP_EXCEEDED = "HERO_CAP_EXCEEDED"
    PROGRAMMATIC_DATA = "PROGRAMMATIC_DATA"
    PROGRAMMATIC_TEXT = "PROGRAMMATIC_TEXT"
    TRANSITION_TEMPLATE = "TRANSITION_TEMPLATE"
    ABSTRACT_AI = "ABSTRACT_AI"
    NAMED_SUBJECT_AI = "NAMED_SUBJECT_AI"
    SPECIFIC_EVENT_AI = "SPECIFIC_EVENT_AI"
    CHARACTER_AI = "CHARACTER_AI"
    AI_VIDEO_PREMIUM = "AI_VIDEO_PREMIUM"
    AI_VIDEO_CAP_EXCEEDED = "AI_VIDEO_CAP_EXCEEDED"
    AI_BUDGET_EXCEEDED = "AI_BUDGET_EXCEEDED"
    CREDIT_CAP_EXCEEDED = "CREDIT_CAP_EXCEEDED"
    FALLBACK_GRAPHIC = "FALLBACK_GRAPHIC"
    FALLBACK_TEXT_CARD = "FALLBACK_TEXT_CARD"
    UNDECIDED = "UNDECIDED"


class SceneStatus(str, enum.Enum):
    PLANNED = "planned"
    SOURCED = "sourced"
    READY = "ready"
    SAFETY_HOLD = "safety_hold"
    BLOCKED = "blocked"


class DeclaredCondition(str, enum.Enum):
    """Visual conditions a planner declares *in advance* for a scene.

    A declaration is a prediction, not an exemption: QC still detects,
    flags and judges the condition exactly as if it had not been
    declared (no threshold, verdict or flag is ever changed by it).
    The declaration only lets QC *annotate* a matching minor flag as
    expected — see :mod:`app.pipeline.qc.declarations`.

    Mapping to the QC flags each condition pre-declares:

    * ``DARK``  -> ``BLACK_SEGMENT_BRIEF`` (a briefly black segment is
      expected because the scene is intentionally dark)
    * ``STILL`` -> ``FREEZE_DECLARED_STILL`` (a still/frozen passage is
      expected because the scene is intentionally still)
    """

    DARK = "dark"
    STILL = "still"


class MotionKind(str, enum.Enum):
    """Motion instruction kinds (see :mod:`.motion`)."""

    ZOOM_IN = "zoom_in"
    ZOOM_OUT = "zoom_out"
    PAN_LEFT = "pan_left"
    PAN_RIGHT = "pan_right"
    STATIC = "static"
    PARALLAX = "parallax"
    REVEAL = "reveal"


class MotionInstruction(BaseModel):
    """The motion a scene's visual must perform.

    Every still leaves the planner with one of these.  ``kind=static``
    means *no* internal motion event — which is what makes a long still
    a beat-rule violation (see :mod:`.beats`).
    """

    model_config = ConfigDict(extra="forbid")

    kind: MotionKind = MotionKind.ZOOM_IN
    #: Optional Ken Burns scale endpoints (1.0 = native framing).
    start_scale: Optional[float] = Field(default=None, gt=0)
    end_scale: Optional[float] = Field(default=None, gt=0)

    @property
    def has_motion(self) -> bool:
        """True when the scene contains an internal motion event."""
        return self.kind is not MotionKind.STATIC

    @property
    def direction(self) -> str:
        """Coarse direction group used by the alternation rule."""
        return {
            MotionKind.ZOOM_IN: "in",
            MotionKind.ZOOM_OUT: "out",
            MotionKind.PAN_LEFT: "left",
            MotionKind.PAN_RIGHT: "right",
            MotionKind.PARALLAX: "depth",
            MotionKind.REVEAL: "reveal",
            MotionKind.STATIC: "none",
        }[self.kind]


class SceneRecord(BaseModel):
    """One scene = one manifest record (the atomic unit is the sentence)."""

    model_config = ConfigDict(extra="forbid")

    # Identity + narration spine -------------------------------------------
    scene_id: str = Field(min_length=1)
    #: The source sentence(s) this scene visualises (atomic unit = sentence).
    source_sentence: str = Field(min_length=1)
    #: Script sentence ids covered by this scene (usually exactly one;
    #: two short sentences may share one visual idea if < 3 s combined).
    sentence_ids: list[str] = Field(default_factory=list)
    #: Start/end seconds on the *measured* narration timing spine.
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)

    # Classification --------------------------------------------------------
    beat_class: BeatClass = BeatClass.STANDARD
    scene_class: SceneClass

    # Sourcing decision -----------------------------------------------------
    chosen_source: SourceKind
    decision_reason: ReasonCode = ReasonCode.UNDECIDED
    #: Reference to the acquired asset (registry id / path), if sourced yet.
    asset_ref: Optional[str] = None
    #: Reference into the licence ledger — no reference, no render.
    licence_ref: Optional[str] = None
    #: Content hash (SHA-256 hex) of the asset bytes, once acquired.
    content_hash: Optional[str] = None
    #: How many published videos have already used this asset.
    reuse_count: int = Field(default=0, ge=0)
    #: Stock search query OR generation prompt id (exactly one is used).
    stock_query: Optional[str] = None
    generation_prompt: Optional[str] = None

    # Presentation ----------------------------------------------------------
    motion: MotionInstruction = Field(default_factory=MotionInstruction)
    overlay_text: Optional[str] = None

    # Beat / safety facts ---------------------------------------------------
    #: True when the visual is a still image (vs a moving clip).
    is_still: bool = False
    #: Number of list items spoken during this scene (LIST beats).
    list_item_count: int = Field(default=1, ge=1)
    safety_flags: list[str] = Field(default_factory=list)
    #: Visual conditions declared in advance for this scene (planner
    #: pre-declaration for QC annotation).  Default empty: every
    #: existing caller / manifest is unaffected.  Annotation only —
    #: never suppresses a QC flag and never changes a QC verdict.
    declared_conditions: list[DeclaredCondition] = Field(default_factory=list)
    similarity_score: Optional[float] = Field(default=None, ge=0, le=1)
    status: SceneStatus = SceneStatus.PLANNED

    # Scripture handling (TEXTUAL scenes in religious niches) ---------------
    is_scripture: bool = False
    surah_ref: Optional[str] = None
    ayah_ref: Optional[str] = None

    # -- structural invariants ----------------------------------------------
    @model_validator(mode="after")
    def _end_after_start(self) -> "SceneRecord":
        if self.end_s <= self.start_s:
            raise ValueError(
                f"scene {self.scene_id!r}: end_s ({self.end_s}) must be "
                f"greater than start_s ({self.start_s})"
            )
        return self

    @field_validator("overlay_text")
    @classmethod
    def _overlay_word_cap(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and len(value.split()) > MAX_OVERLAY_WORDS:
            raise ValueError(
                f"overlay_text is capped at {MAX_OVERLAY_WORDS} words, "
                f"got {len(value.split())}: {value!r}"
            )
        return value

    @field_validator("content_hash")
    @classmethod
    def _hash_shape(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            v = value.strip().lower()
            if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
                raise ValueError(
                    "content_hash must be a 64-char SHA-256 hex digest"
                )
        return value

    # -- derived values -------------------------------------------------------
    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def start_ms(self) -> int:
        return round(self.start_s * 1000)

    @property
    def end_ms(self) -> int:
        return round(self.end_s * 1000)

    @property
    def is_ai(self) -> bool:
        return self.chosen_source in AI_SOURCE_KINDS

    @property
    def narration_text(self) -> str:
        """Roadmap alias: the narration sentence this scene covers."""
        return self.source_sentence


class SceneManifest(BaseModel):
    """The full scene plan for one video (``scenes.json``)."""

    model_config = ConfigDict(extra="forbid")

    video_id: str = Field(min_length=1)
    scenes: list[SceneRecord] = Field(min_length=1)
    niche_id: Optional[str] = None
    #: Version-pinned to the locked script + narration that produced it.
    spine_version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _unique_ids_and_hero_cap(self) -> "SceneManifest":
        ids = [s.scene_id for s in self.scenes]
        if len(ids) != len(set(ids)):
            raise ValueError("scene_id values must be unique in a manifest")
        heroes = sum(1 for s in self.scenes if s.scene_class is SceneClass.HERO)
        if heroes > MAX_HERO_SCENES:
            raise ValueError(
                f"HERO scenes are capped at {MAX_HERO_SCENES} per video, "
                f"got {heroes}"
            )
        return self

    # -- derived values -------------------------------------------------------
    @property
    def total_duration_s(self) -> float:
        return max(s.end_s for s in self.scenes) - min(s.start_s for s in self.scenes)

    @property
    def hero_count(self) -> int:
        return sum(1 for s in self.scenes if s.scene_class is SceneClass.HERO)

    @property
    def ai_count(self) -> int:
        return sum(1 for s in self.scenes if s.is_ai)

    @property
    def ai_video_count(self) -> int:
        return sum(
            1 for s in self.scenes if s.chosen_source is SourceKind.AI_VIDEO
        )

    def scene_by_id(self, scene_id: str) -> Optional[SceneRecord]:
        for scene in self.scenes:
            if scene.scene_id == scene_id:
                return scene
        return None
