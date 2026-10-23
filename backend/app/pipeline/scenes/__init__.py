"""Scene Planning (Worker 1 / Agent 14) — roadmap Part 6.4, section 12.

Public interface:

* Models: :class:`SceneRecord`, :class:`SceneManifest`,
  :class:`SceneClass` (the 8 classes), :class:`BeatClass`,
  :class:`SourceKind`, :class:`ReasonCode`, :class:`MotionInstruction`.
* Beat rules: :func:`validate_beat_durations`, :class:`BeatViolation`.
* Motion rules: :func:`validate_motion_sequence`,
  :func:`suggest_next_motion`.
* Sourcing decision (pure): :func:`decide_source`,
  :class:`SceneFeatures`, :class:`StockCandidate`,
  :class:`PlanBudgetState`, :class:`SourceDecision`.
* Timing spine (measured narration is truth): :func:`probe_duration_s`,
  :func:`build_timing_spine`, :class:`NarrationSegment`,
  :class:`TimingSpine`.
* Faceless safety gates: :func:`validate_scene_safety`,
  :func:`validate_plan_safety`, :class:`NicheSafetyProfile`,
  :class:`SceneSafetyFacts`, :class:`SafetyFlag`.
* Everything at once: :func:`validate_scene_plan`,
  :class:`PlanValidationReport`.
"""

from .beats import (
    BeatViolation,
    BeatViolationCode,
    validate_beat_durations,
    visual_change_density,
)
from .decision import (
    PlanBudgetState,
    SceneFeatures,
    SourceDecision,
    StockCandidate,
    decide_source,
    validate_ai_budget,
)
from .models import (
    MAX_HERO_SCENES,
    BeatClass,
    DeclaredCondition,
    MotionInstruction,
    MotionKind,
    ReasonCode,
    SceneClass,
    SceneManifest,
    SceneRecord,
    SceneStatus,
    SourceKind,
)
from .motion import (
    MotionViolation,
    directions_alternate,
    suggest_next_motion,
    validate_motion_sequence,
)
from .safety import (
    NicheSafetyProfile,
    SafetyAssessment,
    SafetyFlag,
    SafetyViolation,
    SceneSafetyFacts,
    facts_from_scene,
    indicates_prophet_depiction,
    prompt_matches_taboo,
    validate_plan_safety,
    validate_scene_safety,
)
from .timing import (
    NarrationSegment,
    ProbeError,
    SceneTiming,
    TimingSpine,
    WordAlignment,
    build_timing_spine,
    probe_duration_s,
)
from .validators import PlanValidationReport, validate_scene_plan

__all__ = [
    # models
    "MAX_HERO_SCENES",
    "BeatClass",
    "DeclaredCondition",
    "MotionInstruction",
    "MotionKind",
    "ReasonCode",
    "SceneClass",
    "SceneManifest",
    "SceneRecord",
    "SceneStatus",
    "SourceKind",
    # beats
    "BeatViolation",
    "BeatViolationCode",
    "validate_beat_durations",
    "visual_change_density",
    # motion
    "MotionViolation",
    "directions_alternate",
    "suggest_next_motion",
    "validate_motion_sequence",
    # decision
    "PlanBudgetState",
    "SceneFeatures",
    "SourceDecision",
    "StockCandidate",
    "decide_source",
    "validate_ai_budget",
    # timing
    "NarrationSegment",
    "ProbeError",
    "SceneTiming",
    "TimingSpine",
    "WordAlignment",
    "build_timing_spine",
    "probe_duration_s",
    # safety
    "NicheSafetyProfile",
    "SafetyAssessment",
    "SafetyFlag",
    "SafetyViolation",
    "SceneSafetyFacts",
    "facts_from_scene",
    "indicates_prophet_depiction",
    "prompt_matches_taboo",
    "validate_plan_safety",
    "validate_scene_safety",
    # aggregate
    "PlanValidationReport",
    "validate_scene_plan",
]
