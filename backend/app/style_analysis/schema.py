"""Style Lab — per-video competitor analysis schema (the shared contract).

This module is the CONTRACT every other Style Lab worker codes against
(scenes, visual classification, speech, dressing, thumbnail, engine).
Conventions follow the codebase standard: Pydantic v2 ``BaseModel`` with
``ConfigDict(extra="forbid", str_strip_whitespace=True)`` — see
``app.niches.models`` and ``app.integration.models``.

Honesty rule (encoded, not just documented)
-------------------------------------------
Style Lab analysis is *probabilistic*: whether a scene looks stock,
AI-generated or raw footage can never be known for certain from the
pixels alone.  Therefore:

* ``"uncertain"`` is a first-class value in EVERY analytic enum
  (:data:`VisualClass`, :data:`PresenceLabel`, :data:`MotionLabel`).
  No analytic field forces a hard claim — every sub-analysis defaults
  to its uncertain state.
* Every analytic judgement carries a ``confidence`` in ``[0.0, 1.0]``.
  ``0.0`` means "no evidence / not assessed"; renderers must surface
  it, never hide it or round it up into a fact.
* Measured facts (duration, width/height, fps, scene boundaries from a
  detector) are plain values — they do NOT carry a confidence, because
  they are observations, not inferences.  When a measurement is
  unavailable the field is ``None``, never a fabricated number.

Copyright / reuse boundary
--------------------------
The thumbnail model stores STRUCTURE ONLY: word count, the *role* of
the text (e.g. "curiosity-gap question"), layout and contrast classes.
It never stores the competitor's actual thumbnail wording as reusable
copy, and ``SpeechBeat.summary`` / ``HookAnalysis.pattern_notes`` are
structural summaries, never verbatim transcript excerpts.  The full
transcript, if produced, lives outside this schema and is referenced
by ``SpeechAnalysis.transcript_ref`` only.

Expected producer contracts (other workers)
-------------------------------------------
* ``scenes.detect_scenes(video_path) -> SceneAnalysis``
  (or a ``list[SceneSegment]`` the engine wraps via
  :meth:`SceneAnalysis.from_segments`).
* ``visual_classify.classify_visuals(video, scenes) -> VisualMix``
* ``speech.analyse_speech(video, scenes) -> SpeechAnalysis``
* ``dressing.analyse_dressing(video, scenes) -> DressingAnalysis``
* ``thumbnail.analyse_thumbnail(path_or_None) -> ThumbnailAnalysis | None``
* ``engine.analyse_video(...) -> StyleLabAnalysis`` — assembles the
  above into the top-level result and persists via ``storage``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "SCHEMA_VERSION",
    "CTAAnalysis",
    "CTAPlacement",
    "Confidence",
    "DressingAnalysis",
    "HookAnalysis",
    "MotionEstimate",
    "PresenceAssessment",
    "PresenceLabel",
    "SceneAnalysis",
    "SceneSegment",
    "SceneVisualLabel",
    "SpeechAnalysis",
    "SpeechBeat",
    "StyleLabAnalysis",
    "ThumbnailAnalysis",
    "VideoAnalysisResult",
    "VideoMetadata",
    "VisualClass",
    "VisualMix",
    "VISUAL_CLASSES",
]

#: Bump on any breaking field change; stored in every persisted result.
SCHEMA_VERSION: str = "1.0"

#: A probability / confidence score. 0.0 = no evidence, 1.0 = certain.
Confidence = float

# ---------------------------------------------------------------------------
# Closed vocabularies — "uncertain" is always representable
# ---------------------------------------------------------------------------

#: Per-scene / aggregate visual provenance classes. These are *likelihood*
#: labels ("stock_like"), never provenance facts.
VisualClass = Literal[
    "stock_like",
    "ai_generated_like",
    "raw_footage_like",
    "screen_capture",
    "text_card",
    "other",
    "uncertain",
]

VISUAL_CLASSES: tuple[str, ...] = (
    "stock_like",
    "ai_generated_like",
    "raw_footage_like",
    "screen_capture",
    "text_card",
    "other",
    "uncertain",
)

#: For binary-ish dressing judgements (music present? captions burned in?).
PresenceLabel = Literal["present", "absent", "uncertain"]

#: Coarse camera/editing motion vocabulary.
MotionLabel = Literal[
    "static", "zoom", "pan", "zoom_pan", "mixed", "uncertain"
]


def _check_confidence(value: float) -> float:
    if not (0.0 <= float(value) <= 1.0):
        raise ValueError(f"confidence must be in [0.0, 1.0], got {value!r}")
    return float(value)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Video metadata — measured facts (no confidence; None when unmeasured)
# ---------------------------------------------------------------------------


class VideoMetadata(_Base):
    """Measured facts about the analysed video file."""

    #: Local path (or opaque ref) of the analysed video. Required.
    path: str = Field(..., min_length=1)
    #: Runtime in seconds; None when probing failed / not yet probed.
    duration_s: float | None = Field(default=None, ge=0)
    #: Pixel width / height; None when unknown.
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    #: Human resolution string, e.g. "1920x1080". Derived from
    #: width/height when omitted (see validator).
    resolution: str | None = None
    #: Frames per second; None when unknown.
    fps: float | None = Field(default=None, gt=0)
    #: Container/codec label if probed, e.g. "h264". Optional colour.
    codec: str | None = None
    file_size_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _derive_resolution(self) -> "VideoMetadata":
        if self.resolution is None and self.width and self.height:
            self.resolution = f"{self.width}x{self.height}"
        return self


# ---------------------------------------------------------------------------
# Scenes & pacing
# ---------------------------------------------------------------------------


class SceneSegment(_Base):
    """One detected shot/scene. Boundaries are detector observations."""

    index: int = Field(..., ge=0)
    start_s: float = Field(..., ge=0)
    end_s: float = Field(..., ge=0)
    #: Duration in seconds. Derived as end-start when omitted (0.0).
    duration_s: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def _check_span(self) -> "SceneSegment":
        if self.end_s < self.start_s:
            raise ValueError(
                f"scene {self.index}: end_s ({self.end_s}) < start_s ({self.start_s})"
            )
        if self.duration_s == 0.0 and self.end_s > self.start_s:
            self.duration_s = round(self.end_s - self.start_s, 6)
        return self


class SceneAnalysis(_Base):
    """Scene list plus pacing aggregates.

    Prefer :meth:`from_segments` so aggregates are always consistent
    with the segment list; direct construction is validated loosely
    (aggregates may be supplied by a detector that knows more than the
    stored segment sample).
    """

    scenes: list[SceneSegment] = Field(default_factory=list)
    scene_count: int = Field(default=0, ge=0)
    avg_shot_length_s: float | None = Field(default=None, ge=0)
    cuts_per_minute: float | None = Field(default=None, ge=0)
    #: Confidence in the *segmentation* itself (e.g. detector quality).
    confidence: Confidence = 0.0
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fill_count(self) -> "SceneAnalysis":
        if self.scene_count == 0 and self.scenes:
            self.scene_count = len(self.scenes)
        return self

    @classmethod
    def from_segments(
        cls,
        segments: list[SceneSegment],
        *,
        total_duration_s: float | None = None,
        confidence: Confidence = 0.0,
        notes: list[str] | None = None,
    ) -> "SceneAnalysis":
        """Build a SceneAnalysis with aggregates derived from segments."""
        count = len(segments)
        total = (
            total_duration_s
            if total_duration_s is not None
            else (segments[-1].end_s if segments else 0.0)
        )
        avg = (total / count) if count and total > 0 else None
        cpm = (count / (total / 60.0)) if count and total > 0 else None
        return cls(
            scenes=list(segments),
            scene_count=count,
            avg_shot_length_s=round(avg, 4) if avg is not None else None,
            cuts_per_minute=round(cpm, 4) if cpm is not None else None,
            confidence=confidence,
            notes=list(notes or []),
        )

    @classmethod
    def make_uncertain(cls, notes: list[str] | None = None) -> "SceneAnalysis":
        return cls(confidence=0.0, notes=list(notes or ["scene detection not run"]))


# ---------------------------------------------------------------------------
# Visual mix — probabilistic provenance labels
# ---------------------------------------------------------------------------


class SceneVisualLabel(_Base):
    """Probabilistic visual class for ONE scene."""

    scene_index: int = Field(..., ge=0)
    label: VisualClass = "uncertain"
    confidence: Confidence = 0.0
    #: Short signal names that drove the label, e.g.
    #: ["watermark_like", "smooth_camera", "no_sensor_noise"].
    #: Signals are evidence descriptors, never claims of fact.
    signals: list[str] = Field(default_factory=list)

    _conf = field_validator("confidence")(_check_confidence)


class VisualMix(_Base):
    """Aggregate visual provenance mix (percentages) + per-scene labels.

    Percentages are duration-weighted shares in ``[0, 100]`` and must
    sum to ~100 (tolerance 1.5 for rounding) when any class is set.
    The default is 100% ``uncertain`` — the honest starting state.
    """

    stock_like: float = Field(default=0.0, ge=0, le=100)
    ai_generated_like: float = Field(default=0.0, ge=0, le=100)
    raw_footage_like: float = Field(default=0.0, ge=0, le=100)
    screen_capture: float = Field(default=0.0, ge=0, le=100)
    text_card: float = Field(default=0.0, ge=0, le=100)
    other: float = Field(default=0.0, ge=0, le=100)
    uncertain: float = Field(default=100.0, ge=0, le=100)

    scene_labels: list[SceneVisualLabel] = Field(default_factory=list)
    #: Overall confidence in the mix as a whole.
    confidence: Confidence = 0.0
    notes: list[str] = Field(default_factory=list)

    _conf = field_validator("confidence")(_check_confidence)

    @model_validator(mode="after")
    def _check_sum(self) -> "VisualMix":
        total = sum(self.percentages().values())
        if total == 0.0:
            # All-zero is only acceptable as an explicit "nothing
            # assessed" state; normalise it to fully uncertain so no
            # consumer can misread it as a measured empty mix.
            self.uncertain = 100.0
            return self
        if abs(total - 100.0) > 1.5:
            raise ValueError(
                f"visual mix percentages must sum to ~100, got {total:.2f}"
            )
        return self

    def percentages(self) -> dict[str, float]:
        """Per-class percentages keyed by VisualClass name, in order."""
        return {name: float(getattr(self, name)) for name in VISUAL_CLASSES}

    def dominant_label(self) -> str:
        """Highest-percentage class name (excluding none)."""
        pct = self.percentages()
        return max(pct, key=lambda k: pct[k])

    @classmethod
    def from_scene_labels(
        cls,
        labels: list[SceneVisualLabel],
        scenes: list[SceneSegment] | None = None,
        *,
        confidence: Confidence = 0.0,
        notes: list[str] | None = None,
    ) -> "VisualMix":
        """Build duration-weighted percentages from per-scene labels.

        When ``scenes`` is given, each label is weighted by its scene's
        duration; otherwise labels are weighted equally. Scenes without
        a label count as ``uncertain``.
        """
        durations: dict[int, float] = {}
        if scenes:
            durations = {s.index: max(s.duration_s, 0.0) for s in scenes}
        weights: dict[str, float] = {name: 0.0 for name in VISUAL_CLASSES}
        total_weight = 0.0
        labelled = {lbl.scene_index: lbl for lbl in labels}
        indices = set(durations) | set(labelled)
        if not indices:
            return cls(confidence=confidence, notes=list(notes or []))
        for idx in sorted(indices):
            w = durations.get(idx, 1.0) or 1.0
            lbl = labelled.get(idx)
            weights[lbl.label if lbl else "uncertain"] += w
            total_weight += w
        pct = {
            name: round(100.0 * w / total_weight, 2) if total_weight else 0.0
            for name, w in weights.items()
        }
        # Fix rounding drift on the largest bucket so the sum validates.
        drift = round(100.0 - sum(pct.values()), 2)
        if drift:
            biggest = max(pct, key=lambda k: pct[k])
            pct[biggest] = round(pct[biggest] + drift, 2)
        return cls(
            **pct,  # type: ignore[arg-type]
            scene_labels=list(labels),
            confidence=confidence,
            notes=list(notes or []),
        )

    @classmethod
    def make_uncertain(cls, notes: list[str] | None = None) -> "VisualMix":
        return cls(confidence=0.0, notes=list(notes or ["visual classification not run"]))


# ---------------------------------------------------------------------------
# Speech & structure
# ---------------------------------------------------------------------------


class HookAnalysis(_Base):
    """Structure of the opening hook — pattern, never verbatim copy."""

    #: Hook window in seconds (e.g. first 30.0s). None = not assessed.
    window_s: float | None = Field(default=None, ge=0)
    #: Structural pattern notes, e.g. "opens with a question, then a
    #: bold claim; no channel intro". Never a transcript excerpt.
    pattern_notes: str = ""
    #: Coarse hook family label if recognised, e.g. "question",
    #: "bold_claim", "story_cold_open", "list_tease", "uncertain".
    pattern_label: str = "uncertain"
    confidence: Confidence = 0.0

    _conf = field_validator("confidence")(_check_confidence)


class SpeechBeat(_Base):
    """One structural beat/segment of the narration."""

    start_s: float = Field(..., ge=0)
    end_s: float = Field(..., ge=0)
    #: Structural summary, e.g. "introduces second example". Never
    #: verbatim transcript text.
    summary: str = ""
    confidence: Confidence = 0.0

    _conf = field_validator("confidence")(_check_confidence)


class CTAPlacement(_Base):
    """One call-to-action placement (structural position only)."""

    start_s: float | None = Field(default=None, ge=0)
    end_s: float | None = Field(default=None, ge=0)
    #: e.g. "subscribe", "comment_prompt", "watch_next", "link", "other".
    kind: str = "other"
    notes: str = ""
    confidence: Confidence = 0.0

    _conf = field_validator("confidence")(_check_confidence)


class CTAAnalysis(_Base):
    present: bool | None = None  # None = not assessed / uncertain
    placements: list[CTAPlacement] = Field(default_factory=list)
    confidence: Confidence = 0.0

    _conf = field_validator("confidence")(_check_confidence)


class SpeechAnalysis(_Base):
    """Narration analysis: language, pace, hook, beats, CTA."""

    #: Detected language codes, e.g. ["ur"], ["en"], ["ur", "en"].
    #: Empty = not assessed.
    transcript_languages: list[str] = Field(default_factory=list)
    language_confidence: Confidence = 0.0
    #: Spoken words per minute; None when no transcript / not measured.
    wpm: float | None = Field(default=None, ge=0)
    wpm_confidence: Confidence = 0.0
    hook: HookAnalysis = Field(default_factory=HookAnalysis)
    beats: list[SpeechBeat] = Field(default_factory=list)
    cta: CTAAnalysis = Field(default_factory=CTAAnalysis)
    #: Reference (path / artefact id) to the full transcript, stored
    #: OUTSIDE this schema. None when no transcript was produced.
    transcript_ref: str | None = None
    confidence: Confidence = 0.0
    notes: list[str] = Field(default_factory=list)

    _confs = field_validator(
        "language_confidence", "wpm_confidence", "confidence"
    )(_check_confidence)

    @classmethod
    def make_uncertain(cls, notes: list[str] | None = None) -> "SpeechAnalysis":
        return cls(notes=list(notes or ["speech analysis not run"]))


# ---------------------------------------------------------------------------
# Dressing — music, burned-in captions, overlays, motion
# ---------------------------------------------------------------------------


class PresenceAssessment(_Base):
    """A present/absent/uncertain judgement with confidence."""

    label: PresenceLabel = "uncertain"
    confidence: Confidence = 0.0
    notes: str = ""

    _conf = field_validator("confidence")(_check_confidence)


class MotionEstimate(_Base):
    label: MotionLabel = "uncertain"
    confidence: Confidence = 0.0
    #: e.g. estimated zoom/pan share: "slow zoom on ~40% of scenes".
    notes: str = ""

    _conf = field_validator("confidence")(_check_confidence)


class DressingAnalysis(_Base):
    """Audio/editing dressing layered over the core visuals + speech."""

    music_presence: PresenceAssessment = Field(default_factory=PresenceAssessment)
    caption_burn_in: PresenceAssessment = Field(default_factory=PresenceAssessment)
    #: Text overlays per minute; None = not assessed.
    text_overlay_frequency: float | None = Field(default=None, ge=0)
    text_overlay_confidence: Confidence = 0.0
    motion: MotionEstimate = Field(default_factory=MotionEstimate)
    confidence: Confidence = 0.0
    notes: list[str] = Field(default_factory=list)

    _confs = field_validator("text_overlay_confidence", "confidence")(
        _check_confidence
    )

    @classmethod
    def make_uncertain(cls, notes: list[str] | None = None) -> "DressingAnalysis":
        return cls(notes=list(notes or ["dressing analysis not run"]))


# ---------------------------------------------------------------------------
# Thumbnail — STRUCTURE ONLY (never the competitor's actual text)
# ---------------------------------------------------------------------------


class ThumbnailAnalysis(_Base):
    """Structural thumbnail analysis. The whole model is nullable at
    the top level: ``StyleLabAnalysis.thumbnail is None`` means no
    thumbnail was supplied / analysed.

    HARD RULE: ``text_role_notes`` describes the ROLE/structure of any
    thumbnail text (e.g. "3-word curiosity-gap question, top-left").
    The actual wording must never be stored here as reusable copy.
    """

    text_word_count: int | None = Field(default=None, ge=0)
    #: Structure/role ONLY — see class docstring.
    text_role_notes: str = ""
    face_present: bool | None = None  # None = uncertain / not assessed
    face_confidence: Confidence = 0.0
    #: Dominant colours as hex strings, e.g. ["#E53935", "#111111"].
    dominant_colours: list[str] = Field(default_factory=list)
    #: e.g. "text_left_subject_right", "full_bleed_subject",
    #: "split_screen", "text_only", "uncertain".
    layout_class: str = "uncertain"
    #: e.g. "high", "medium", "low", "uncertain".
    contrast_class: str = "uncertain"
    confidence: Confidence = 0.0
    notes: list[str] = Field(default_factory=list)

    _confs = field_validator("face_confidence", "confidence")(_check_confidence)

    @classmethod
    def make_uncertain(cls, notes: list[str] | None = None) -> "ThumbnailAnalysis":
        return cls(notes=list(notes or ["thumbnail analysis not run"]))


# ---------------------------------------------------------------------------
# Top-level result
# ---------------------------------------------------------------------------


class StyleLabAnalysis(_Base):
    """Complete per-video Style Lab analysis result.

    This is the object ``storage.save_analysis`` persists and
    ``render.render_breakdown`` renders.
    """

    schema_version: str = SCHEMA_VERSION
    #: Stable id for THIS analysis run (uuid4 hex when generated).
    analysis_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    #: Intake-side competitor video id, when known. The storage seam
    #: keys directories by this (falling back to ``analysis_id``).
    competitor_video_id: str | None = None
    #: User's own channel this competitor was attached to, when known.
    #: Style profiles are scoped per channel — carry it through.
    channel_id: str | None = None
    #: Public source URL of the competitor video, when known.
    #: Metadata only; analysis itself runs on a local file.
    source_url: str | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    #: Free label for the analyser build, e.g. "stylelab-0.1".
    analyzer_version: str = "stylelab-0.1"

    video: VideoMetadata
    scenes: SceneAnalysis = Field(default_factory=lambda: SceneAnalysis.make_uncertain())
    visual_mix: VisualMix = Field(default_factory=lambda: VisualMix.make_uncertain())
    speech: SpeechAnalysis = Field(default_factory=lambda: SpeechAnalysis.make_uncertain())
    dressing: DressingAnalysis = Field(default_factory=lambda: DressingAnalysis.make_uncertain())
    #: None = no thumbnail supplied / not analysed (honest absence,
    #: distinct from an all-uncertain ThumbnailAnalysis).
    thumbnail: ThumbnailAnalysis | None = None

    #: Roll-up confidence across all sub-analyses (producer-set).
    overall_confidence: Confidence = 0.0
    notes: list[str] = Field(default_factory=list)

    _conf = field_validator("overall_confidence")(_check_confidence)

    # -- JSON round-trip helpers (thin, explicit wrappers) ---------------
    def to_json(self, **kwargs) -> str:
        """Serialise to JSON (``model_dump(mode="json")``)."""
        return self.model_dump_json(**kwargs)

    @classmethod
    def from_json(cls, raw: str) -> "StyleLabAnalysis":
        return cls.model_validate_json(raw)

    @classmethod
    def make_uncertain(
        cls,
        video_path: str,
        *,
        competitor_video_id: str | None = None,
        channel_id: str | None = None,
        source_url: str | None = None,
        notes: list[str] | None = None,
    ) -> "StyleLabAnalysis":
        """An honest all-uncertain result for ``video_path``.

        Used as the engine's fallback when every sub-analysis failed /
        was skipped: the result exists, but claims nothing.
        """
        return cls(
            competitor_video_id=competitor_video_id,
            channel_id=channel_id,
            source_url=source_url,
            video=VideoMetadata(path=video_path),
            notes=list(notes or ["analysis not run — all sections uncertain"]),
        )


#: Alias — some workers refer to the top level as the "video analysis
#: result". Same class, one contract.
VideoAnalysisResult = StyleLabAnalysis
