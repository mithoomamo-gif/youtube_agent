"""Regression tests for the Round 3 short-scene beat policy fix.

Round 2 failure being pinned here: a 0.5-minute video whose real
edge-tts narration ran at ~250-300 effective WPM produced chapter
audio far shorter than the visuals stage's beat minimums
(STANDARD >= 4 s/scene).  ``_distribute_durations`` clamped every
scene UP to its beat minimum, inflating the timeline past the
measured voice total; the final scene was then force-ended at the
voice total and died with ``SceneRecord end_s <= start_s``.

Policy after the fix (see ``app.pipeline.scenes.beats``):

* beat ranges are *preferred* targets, applied only when jointly
  feasible for the measured total — durations are never padded or
  fabricated to satisfy them;
* sub-minimum scenes are LEGAL when measured narration requires
  them (hooks may be 2-4 s; any measured scene may be shorter);
* structural invariants stay hard: ``end_s > start_s``, no
  zero/negative durations, no overlapping scenes;
* a zero-length measured segment is an explicit error, never a
  silent floor-up.
"""

import pytest
from pydantic import ValidationError

from app.integration.visuals_stage import _distribute_durations
from app.pipeline.scenes import (
    BeatClass,
    SceneClass,
    SceneRecord,
    SourceKind,
    validate_beat_durations,
    validate_scene_plan,
)
from app.pipeline.scenes.beats import BeatViolationCode
from app.pipeline.scenes.models import MotionInstruction

STANDARD_RANGE = (4.0, 8.0)
HOOK_RANGE = (2.0, 4.0)


def make_scene(scene_id="s1", start=0.0, dur=6.0, beat=BeatClass.STANDARD, **kw):
    kw.setdefault("source_sentence", f"Sentence for {scene_id}.")
    return SceneRecord(
        scene_id=scene_id,
        start_s=start,
        end_s=start + dur,
        beat_class=beat,
        scene_class=SceneClass.REAL_WORLD,
        chosen_source=SourceKind.GRAPHIC,
        motion=MotionInstruction(),
        **kw,
    )


def codes(scenes, **kw):
    kw.setdefault("check_density", False)
    return [v.code for v in validate_beat_durations(scenes, **kw)]


# ---------------------------------------------------------------------------
# _distribute_durations: measured total wins over beat minimums
# ---------------------------------------------------------------------------


class TestDistributeDurationsMeasuredWins:
    def test_infeasible_minimums_return_proportional_split(self):
        # 3 STANDARD scenes want >= 4 s each (>= 12 s) but the measured
        # chapter is only 9 s — the exact Round 2 infeasibility shape.
        durations = _distribute_durations(
            9.0, [1.0, 1.0, 1.0], [STANDARD_RANGE] * 3
        )
        assert durations == pytest.approx([3.0, 3.0, 3.0])
        assert sum(durations) == pytest.approx(9.0)  # never inflated
        assert all(d > 0 for d in durations)
        assert all(d < 4.0 for d in durations)  # sub-minimum, and legal

    def test_infeasible_split_is_word_weighted_not_padded(self):
        durations = _distribute_durations(
            6.0, [3.0, 1.0], [STANDARD_RANGE, STANDARD_RANGE]
        )
        assert durations == pytest.approx([4.5, 1.5])
        assert sum(durations) == pytest.approx(6.0)

    def test_single_scene_shorter_than_its_beat_minimum(self):
        # One measured 1.96 s chapter (Round 2 fixture chapter 3).
        durations = _distribute_durations(1.96, [9.0], [STANDARD_RANGE])
        assert durations == pytest.approx([1.96])

    def test_tiny_measured_total_stays_positive(self):
        durations = _distribute_durations(
            0.9, [1.0, 1.0, 1.0], [STANDARD_RANGE] * 3
        )
        assert durations == pytest.approx([0.3, 0.3, 0.3])
        assert all(d > 0 for d in durations)

    def test_feasible_ranges_still_applied(self):
        # 20 s, weights skewed: raw split [16.7, 1.7, 1.7] violates the
        # ranges, but the ranges ARE jointly feasible (12 <= 20 <= 24),
        # so clamp-and-redistribute must reconcile — normal behaviour
        # is unchanged.
        durations = _distribute_durations(
            20.0, [10.0, 1.0, 1.0], [STANDARD_RANGE] * 3
        )
        assert sum(durations) == pytest.approx(20.0)
        assert all(4.0 <= d <= 8.0 for d in durations)

    def test_normal_in_range_split_is_untouched(self):
        durations = _distribute_durations(
            18.0, [1.0, 1.0, 1.0], [STANDARD_RANGE] * 3
        )
        assert durations == pytest.approx([6.0, 6.0, 6.0])

    def test_hook_range_feasible_split(self):
        durations = _distribute_durations(
            4.145, [13.0, 6.0], [HOOK_RANGE, HOOK_RANGE]
        )
        assert sum(durations) == pytest.approx(4.145)
        assert all(2.0 <= d <= 4.0 for d in durations)

    def test_zero_measured_total_is_an_explicit_error(self):
        with pytest.raises(ValueError, match="non-positive measured"):
            _distribute_durations(0.0, [1.0], [STANDARD_RANGE])
        with pytest.raises(ValueError, match="non-positive measured"):
            _distribute_durations(-2.0, [1.0], [STANDARD_RANGE])

    def test_non_positive_weight_is_an_explicit_error(self):
        with pytest.raises(ValueError, match="weights must be positive"):
            _distribute_durations(10.0, [1.0, 0.0], [STANDARD_RANGE] * 2)

    def test_no_scenes_no_durations(self):
        assert _distribute_durations(10.0, [], []) == []


# ---------------------------------------------------------------------------
# Beat validator: minimums are preferences; measured mode makes them legal
# ---------------------------------------------------------------------------


class TestBeatValidatorMeasuredPolicy:
    def test_sub4s_standard_scene_flagged_in_planning_audit(self):
        # Default (non-measured) mode keeps the Round 1 audit behaviour.
        scene = make_scene(dur=3.0, beat=BeatClass.STANDARD)
        assert BeatViolationCode.BEAT_TOO_SHORT in codes([scene])

    def test_sub4s_standard_scene_legal_when_measured(self):
        scene = make_scene(dur=3.0, beat=BeatClass.STANDARD)
        assert codes([scene], measured_narration=True) == []

    def test_short_hook_legal_when_measured(self):
        # Hook at 2.5 s sits inside the preferred 2-4 s band already.
        assert codes([make_scene(dur=2.5, beat=BeatClass.HOOK)]) == []
        # Even a sub-2 s measured hook is legal in measured mode.
        scene = make_scene(dur=1.3, beat=BeatClass.HOOK)
        assert BeatViolationCode.BEAT_TOO_SHORT in codes([scene])
        assert codes([scene], measured_narration=True) == []

    def test_scene_below_planning_floor_legal_when_measured(self):
        scene = make_scene(dur=1.1, beat=BeatClass.STANDARD)
        assert BeatViolationCode.SCENE_TOO_SHORT in codes([scene])
        assert codes([scene], measured_narration=True) == []

    def test_fast_list_legal_when_measured(self):
        scene = make_scene(dur=3.0, beat=BeatClass.LIST, list_item_count=3)
        assert BeatViolationCode.LIST_ITEM_TOO_FAST in codes([scene])
        assert codes([scene], measured_narration=True) == []

    def test_high_density_from_short_measured_scenes_not_flagged(self):
        scenes = [
            make_scene(f"s{i}", start=i * 2.0, dur=2.0, beat=BeatClass.HOOK)
            for i in range(30)
        ]
        default_codes = [v.code for v in validate_beat_durations(scenes)]
        assert BeatViolationCode.DENSITY_TOO_HIGH in default_codes
        measured_codes = [
            v.code
            for v in validate_beat_durations(scenes, measured_narration=True)
        ]
        assert BeatViolationCode.DENSITY_TOO_HIGH not in measured_codes
        assert measured_codes == []

    def test_too_long_still_flagged_when_measured(self):
        # Maximum-side flags survive measured mode: they can be fixed
        # without touching the narration clock.
        scene = make_scene(dur=9.0, beat=BeatClass.STANDARD)
        assert BeatViolationCode.BEAT_TOO_LONG in codes(
            [scene], measured_narration=True
        )

    def test_validate_scene_plan_forwards_measured_flag(self):
        scenes = [make_scene(dur=3.0, beat=BeatClass.STANDARD)]
        assert not validate_scene_plan(scenes).is_valid
        assert validate_scene_plan(scenes, measured_narration=True).is_valid


# ---------------------------------------------------------------------------
# Structural invariants stay hard in every mode
# ---------------------------------------------------------------------------


class TestStructuralInvariants:
    def test_zero_length_scene_record_rejected_by_model(self):
        with pytest.raises(ValidationError):
            make_scene(start=5.0, dur=0.0)

    def test_overlapping_scenes_flagged_in_both_modes(self):
        scenes = [
            make_scene("a", start=0.0, dur=6.0),
            make_scene("b", start=5.0, dur=6.0),
        ]
        for measured in (False, True):
            found = codes(scenes, measured_narration=measured)
            assert BeatViolationCode.SCENE_OVERLAP in found

    def test_contiguous_scenes_do_not_overlap(self):
        scenes = [
            make_scene("a", start=0.0, dur=6.0),
            make_scene("b", start=6.0, dur=6.0),
        ]
        for measured in (False, True):
            found = codes(scenes, measured_narration=measured)
            assert BeatViolationCode.SCENE_OVERLAP not in found
