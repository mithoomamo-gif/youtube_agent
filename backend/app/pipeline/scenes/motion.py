"""Motion planning rules — roadmap §12 ("Motion, planned, not afterthought").

* Every still leaves the planner with a motion instruction.
* Direction must alternate across consecutive scenes.
* Identical motion on 3+ consecutive scenes is a *planner error* and is
  flagged by :func:`validate_motion_sequence`.
"""

from __future__ import annotations

import enum
from typing import Optional, Sequence

from pydantic import BaseModel, ConfigDict

from .models import MotionInstruction, MotionKind, SceneRecord

#: Longest run of one identical motion the planner may emit.
MAX_IDENTICAL_MOTION_RUN = 2


class MotionViolationCode(str, enum.Enum):
    MOTION_REPEAT = "MOTION_REPEAT"
    MOTION_DIRECTION_REPEAT = "MOTION_DIRECTION_REPEAT"


class MotionViolation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: MotionViolationCode
    #: Scene ids forming the offending run, in plan order.
    scene_ids: list[str]
    motion: MotionKind
    message: str
    run_length: int


def validate_motion_sequence(
    scenes: Sequence[SceneRecord],
) -> list[MotionViolation]:
    """Flag identical motion on 3+ consecutive scenes.

    A *run* is a maximal streak of consecutive scenes sharing one motion
    kind.  Any run of length >= 3 produces exactly one violation.
    """
    violations: list[MotionViolation] = []
    if not scenes:
        return violations

    run_start = 0
    for idx in range(1, len(scenes) + 1):
        run_ended = idx == len(scenes) or (
            scenes[idx].motion.kind is not scenes[run_start].motion.kind
        )
        if run_ended:
            run = scenes[run_start:idx]
            if len(run) > MAX_IDENTICAL_MOTION_RUN:
                kind = run[0].motion.kind
                violations.append(
                    MotionViolation(
                        code=MotionViolationCode.MOTION_REPEAT,
                        scene_ids=[s.scene_id for s in run],
                        motion=kind,
                        message=(
                            f"motion {kind.value} repeats on {len(run)} "
                            f"consecutive scenes "
                            f"({run[0].scene_id}..{run[-1].scene_id}); "
                            f"identical motion on 3+ consecutive scenes is "
                            f"a planner error — directions must alternate"
                        ),
                        run_length=len(run),
                    )
                )
            run_start = idx
    return violations


#: Deterministic alternation partner for each motion kind.  The planner
#: uses this to pick the next scene's motion; STATIC has no direction,
#: so it alternates to a gentle zoom-in.
_ALTERNATION_PARTNER: dict[MotionKind, MotionKind] = {
    MotionKind.ZOOM_IN: MotionKind.ZOOM_OUT,
    MotionKind.ZOOM_OUT: MotionKind.ZOOM_IN,
    MotionKind.PAN_LEFT: MotionKind.PAN_RIGHT,
    MotionKind.PAN_RIGHT: MotionKind.PAN_LEFT,
    MotionKind.PARALLAX: MotionKind.ZOOM_IN,
    MotionKind.REVEAL: MotionKind.ZOOM_OUT,
    MotionKind.STATIC: MotionKind.ZOOM_IN,
}


def suggest_next_motion(
    previous: Optional[MotionInstruction],
) -> MotionInstruction:
    """Return a motion instruction that alternates against ``previous``."""
    if previous is None:
        return MotionInstruction(kind=MotionKind.ZOOM_IN)
    return MotionInstruction(kind=_ALTERNATION_PARTNER[previous.kind])


def directions_alternate(scenes: Sequence[SceneRecord]) -> bool:
    """True when no motion kind repeats on 3+ consecutive scenes."""
    return not validate_motion_sequence(scenes)
