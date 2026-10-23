"""Aggregate plan validation — one call, every planner rule.

Combines the beat/density rules (:mod:`.beats`), the motion-alternation
rule (:mod:`.motion`), the AI-budget / HERO caps (:mod:`.decision`) and —
when safety facts + a niche profile are supplied — the faceless-safety
hard gates (:mod:`.safety`).  A plan is *valid* only when every list in
the report is empty; safety blocks are additionally surfaced as a flat
``blocked_scene_ids`` so Gate B can refuse a PASS token.
"""

from __future__ import annotations

from typing import Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .beats import BeatViolation, validate_beat_durations
from .decision import validate_ai_budget
from .models import SceneRecord
from .motion import MotionViolation, validate_motion_sequence
from .safety import (
    NicheSafetyProfile,
    SafetyAssessment,
    SceneSafetyFacts,
    validate_plan_safety,
)


class PlanValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beat_violations: list[BeatViolation] = Field(default_factory=list)
    motion_violations: list[MotionViolation] = Field(default_factory=list)
    budget_violations: list[str] = Field(default_factory=list)
    safety_blocks: list[SafetyAssessment] = Field(default_factory=list)

    @property
    def blocked_scene_ids(self) -> list[str]:
        return [a.scene_id for a in self.safety_blocks]

    @property
    def is_valid(self) -> bool:
        return not (
            self.beat_violations
            or self.motion_violations
            or self.budget_violations
            or self.safety_blocks
        )


def validate_scene_plan(
    scenes: Sequence[SceneRecord],
    *,
    safety_facts: Optional[Sequence[SceneSafetyFacts]] = None,
    niche: Optional[NicheSafetyProfile] = None,
    measured_narration: bool = False,
) -> PlanValidationReport:
    """Run every planner validator over ``scenes`` (pure, no I/O).

    ``measured_narration`` is forwarded to the beat validator: when the
    plan's timings are measured narration timings, sub-minimum scene
    durations are legal and are not flagged (see :mod:`.beats`).
    """
    return PlanValidationReport(
        beat_violations=validate_beat_durations(
            list(scenes), measured_narration=measured_narration
        ),
        motion_violations=validate_motion_sequence(list(scenes)),
        budget_violations=validate_ai_budget(list(scenes)),
        safety_blocks=(
            validate_plan_safety(safety_facts, niche)
            if safety_facts is not None
            else []
        ),
    )
