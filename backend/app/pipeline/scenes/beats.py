"""Beat-duration + visual-density validation — roadmap §12 / Part 6.4.

Rules (defaults, per-niche overridable upstream, never silently changed):

==================  =====================================================
Beat                Duration rule
==================  =====================================================
HOOK                2-4 s per visual (fastest cutting)
STANDARD            4-8 s
EXPLANATORY         8-15 s
EMOTIONAL           6-12 s
LIST                >= 2.5 s *per spoken item*
(any)               no scene < 1.5 s
(any still)         no still shown > 15 s without an internal motion event
(whole video)       visual-change density 6-20 changes/min
==================  =====================================================

The validator *flags* violations; it never mutates the plan.  The
planner re-segments / merges / re-plans in response.

Measured-narration policy (Round 3 fix)
---------------------------------------
The ranges above are *preferred* planning targets, not hard legality
floors.  When a plan's timings come from measured narration (the
normal case — see :mod:`.timing`: audio is the master clock), the
measured durations WIN: a scene shorter than its beat minimum is
LEGAL when the narration segment itself is that short.  Real TTS at
~250-300 effective WPM routinely produces sub-4 s sentences (a whole
0.5-minute video can measure ~17 s), and padding or stretching such
scenes to satisfy the table would fabricate durations the narration
never had — in Round 2 it inflated the timeline past the measured
total and collapsed the final scene to ``end_s <= start_s``.

So :func:`validate_beat_durations` accepts ``measured_narration=True``:
in that mode the *minimum*-duration flags (BEAT_TOO_SHORT,
SCENE_TOO_SHORT, LIST_ITEM_TOO_FAST) and DENSITY_TOO_HIGH — the direct
mathematical shadow of short measured scenes — are not emitted.
What remains enforced in every mode is structural, not stylistic:
scenes must not overlap (SCENE_OVERLAP), and the model layer already
guarantees ``end_s > start_s``.  Maximum-duration flags (too-long
beats / stills, density too low) also remain: an over-long visual can
be fixed with motion or a sub-visual without touching the narration
clock, so flagging it fabricates nothing.
"""

from __future__ import annotations

import enum
from typing import Iterable, Optional, Sequence

from pydantic import BaseModel, ConfigDict

from .models import BeatClass, SceneRecord

# ---------------------------------------------------------------------------
# Rule table
# ---------------------------------------------------------------------------

#: (min_s, max_s) per beat class.  LIST has no fixed max — its floor is
#: per-item (see LIST_MIN_SECONDS_PER_ITEM).
BEAT_DURATION_RANGES: dict[BeatClass, tuple[Optional[float], Optional[float]]] = {
    BeatClass.HOOK: (2.0, 4.0),
    BeatClass.STANDARD: (4.0, 8.0),
    BeatClass.EXPLANATORY: (8.0, 15.0),
    BeatClass.EMOTIONAL: (6.0, 12.0),
    BeatClass.LIST: (None, None),
}

LIST_MIN_SECONDS_PER_ITEM = 2.5
#: Preferred scene floor for *planning audits* (non-measured mode).
#: It is NOT a legality floor for measured narration — a measured
#: scene may be shorter (see the module docstring).  The only absolute
#: floor a measured scene has is positivity: ``end_s > start_s``.
MIN_SCENE_DURATION_S = 1.5
MAX_STILL_DURATION_S = 15.0
DENSITY_MIN_PER_MIN = 6.0
DENSITY_MAX_PER_MIN = 20.0


class BeatViolationCode(str, enum.Enum):
    BEAT_TOO_SHORT = "BEAT_TOO_SHORT"
    BEAT_TOO_LONG = "BEAT_TOO_LONG"
    SCENE_TOO_SHORT = "SCENE_TOO_SHORT"
    LIST_ITEM_TOO_FAST = "LIST_ITEM_TOO_FAST"
    STILL_TOO_LONG = "STILL_TOO_LONG"
    DENSITY_TOO_LOW = "DENSITY_TOO_LOW"
    DENSITY_TOO_HIGH = "DENSITY_TOO_HIGH"
    #: Structural (every mode): a scene starts before the previous
    #: scene (by start order) has ended.
    SCENE_OVERLAP = "SCENE_OVERLAP"


class BeatViolation(BaseModel):
    """One flagged beat/density problem. ``scene_id=None`` = video-level."""

    model_config = ConfigDict(extra="forbid")

    code: BeatViolationCode
    scene_id: Optional[str] = None
    message: str
    actual: float
    limit: float


def validate_beat_durations(
    scenes: Sequence[SceneRecord],
    *,
    density_min_per_min: float = DENSITY_MIN_PER_MIN,
    density_max_per_min: float = DENSITY_MAX_PER_MIN,
    check_density: bool = True,
    measured_narration: bool = False,
) -> list[BeatViolation]:
    """Flag every beat-duration / still / density violation in ``scenes``.

    Pure function: returns a (possibly empty) list, never raises on bad
    *durations* — flagging is its whole job.

    ``measured_narration=True`` declares that the scene timings are
    measured narration timings (the master clock).  Minimum-duration
    and density-too-high flags are then suppressed — a short measured
    scene is legal, never padded (see the module docstring) — while
    overlap, too-long and density-too-low flags still apply.
    """
    violations: list[BeatViolation] = []

    # Structural invariant, every mode: in start order, no scene may
    # begin before the previous scene ends (contiguity is fine).
    ordered = sorted(scenes, key=lambda s: (s.start_s, s.end_s))
    for previous, current in zip(ordered, ordered[1:]):
        overlap = previous.end_s - current.start_s
        if overlap > 1e-9:
            violations.append(
                BeatViolation(
                    code=BeatViolationCode.SCENE_OVERLAP,
                    scene_id=current.scene_id,
                    message=(
                        f"scene {current.scene_id}: starts at "
                        f"{current.start_s:.3f}s, {overlap:.3f}s before "
                        f"scene {previous.scene_id} ends at "
                        f"{previous.end_s:.3f}s — scenes must not overlap"
                    ),
                    actual=overlap,
                    limit=0.0,
                )
            )

    for scene in scenes:
        duration = scene.duration_s

        if not measured_narration and duration < MIN_SCENE_DURATION_S:
            violations.append(
                BeatViolation(
                    code=BeatViolationCode.SCENE_TOO_SHORT,
                    scene_id=scene.scene_id,
                    message=(
                        f"scene {scene.scene_id}: {duration:.2f}s is below the "
                        f"{MIN_SCENE_DURATION_S}s planning minimum"
                    ),
                    actual=duration,
                    limit=MIN_SCENE_DURATION_S,
                )
            )

        if scene.beat_class is BeatClass.LIST:
            floor = LIST_MIN_SECONDS_PER_ITEM * scene.list_item_count
            if not measured_narration and duration < floor:
                violations.append(
                    BeatViolation(
                        code=BeatViolationCode.LIST_ITEM_TOO_FAST,
                        scene_id=scene.scene_id,
                        message=(
                            f"scene {scene.scene_id}: LIST beat with "
                            f"{scene.list_item_count} item(s) needs >= {floor:.1f}s "
                            f"({LIST_MIN_SECONDS_PER_ITEM}s/item), got {duration:.2f}s"
                        ),
                        actual=duration,
                        limit=floor,
                    )
                )
        else:
            low, high = BEAT_DURATION_RANGES[scene.beat_class]
            if not measured_narration and low is not None and duration < low:
                violations.append(
                    BeatViolation(
                        code=BeatViolationCode.BEAT_TOO_SHORT,
                        scene_id=scene.scene_id,
                        message=(
                            f"scene {scene.scene_id}: {scene.beat_class.value} beat "
                            f"is {duration:.2f}s, below the {low:.0f}s minimum"
                        ),
                        actual=duration,
                        limit=low,
                    )
                )
            if high is not None and duration > high:
                violations.append(
                    BeatViolation(
                        code=BeatViolationCode.BEAT_TOO_LONG,
                        scene_id=scene.scene_id,
                        message=(
                            f"scene {scene.scene_id}: {scene.beat_class.value} beat "
                            f"is {duration:.2f}s, above the {high:.0f}s maximum"
                        ),
                        actual=duration,
                        limit=high,
                    )
                )

        # No still > 15 s without an internal motion event.
        if (
            scene.is_still
            and duration > MAX_STILL_DURATION_S
            and not scene.motion.has_motion
        ):
            violations.append(
                BeatViolation(
                    code=BeatViolationCode.STILL_TOO_LONG,
                    scene_id=scene.scene_id,
                    message=(
                        f"scene {scene.scene_id}: still shown for "
                        f"{duration:.2f}s with no motion event "
                        f"(limit {MAX_STILL_DURATION_S:.0f}s)"
                    ),
                    actual=duration,
                    limit=MAX_STILL_DURATION_S,
                )
            )

    if check_density and scenes:
        violations.extend(
            _density_violations(
                scenes,
                density_min_per_min,
                density_max_per_min,
                measured_narration=measured_narration,
            )
        )

    return violations


def _density_violations(
    scenes: Sequence[SceneRecord],
    density_min: float,
    density_max: float,
    *,
    measured_narration: bool = False,
) -> list[BeatViolation]:
    """Visual-change density for the whole video + each full 60 s window."""
    violations: list[BeatViolation] = []
    start = min(s.start_s for s in scenes)
    end = max(s.end_s for s in scenes)
    total = end - start
    if total <= 0:
        return violations

    def _check(count: int, span_s: float, label: str) -> None:
        density = count / (span_s / 60.0)
        if measured_narration and density > density_max:
            # High density is the direct shadow of short *measured*
            # scenes; the narration clock makes it legal (module
            # docstring), so it is not flagged in measured mode.
            return
        if density < density_min:
            violations.append(
                BeatViolation(
                    code=BeatViolationCode.DENSITY_TOO_LOW,
                    message=(
                        f"{label}: visual-change density {density:.1f}/min is "
                        f"below the {density_min:.0f}/min floor"
                    ),
                    actual=density,
                    limit=density_min,
                )
            )
        elif density > density_max:
            violations.append(
                BeatViolation(
                    code=BeatViolationCode.DENSITY_TOO_HIGH,
                    message=(
                        f"{label}: visual-change density {density:.1f}/min is "
                        f"above the {density_max:.0f}/min ceiling"
                    ),
                    actual=density,
                    limit=density_max,
                )
            )

    _check(len(scenes), total, "whole video")
    # Per-60 s windows once the video is long enough for them to be
    # meaningful (a short video is judged on the whole-video figure only).
    if total >= 120.0:
        window_start = start
        while window_start + 60.0 <= end + 1e-9:
            window_end = window_start + 60.0
            count = sum(
                1 for s in scenes if window_start <= s.start_s < window_end
            )
            _check(count, 60.0, f"window {window_start:.0f}-{window_end:.0f}s")
            window_start += 60.0
    return violations


def visual_change_density(scenes: Iterable[SceneRecord]) -> float:
    """Changes per minute for a scene list (0.0 for an empty/zero plan)."""
    scenes = list(scenes)
    if not scenes:
        return 0.0
    total = max(s.end_s for s in scenes) - min(s.start_s for s in scenes)
    if total <= 0:
        return 0.0
    return len(scenes) / (total / 60.0)
