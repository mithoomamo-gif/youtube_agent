"""Schema round-trip + honesty-rule tests for Style Lab."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.style_analysis.schema import (
    CTAAnalysis,
    CTAPlacement,
    DressingAnalysis,
    HookAnalysis,
    MotionEstimate,
    PresenceAssessment,
    SceneAnalysis,
    SceneSegment,
    SceneVisualLabel,
    SpeechAnalysis,
    SpeechBeat,
    StyleLabAnalysis,
    ThumbnailAnalysis,
    VideoMetadata,
    VisualMix,
)


def full_analysis() -> StyleLabAnalysis:
    return StyleLabAnalysis(
        competitor_video_id="comp-vid-1",
        channel_id="chan-1",
        source_url="https://www.youtube.com/watch?v=example",
        overall_confidence=0.61,
        video=VideoMetadata(
            path="/tmp/comp.mp4", duration_s=185.0, width=1920, height=1080, fps=30.0
        ),
        scenes=SceneAnalysis.from_segments(
            [
                SceneSegment(index=0, start_s=0, end_s=10),
                SceneSegment(index=1, start_s=10, end_s=25),
                SceneSegment(index=2, start_s=25, end_s=60),
            ],
            confidence=0.9,
        ),
        visual_mix=VisualMix.from_scene_labels(
            [
                SceneVisualLabel(scene_index=0, label="stock_like", confidence=0.62,
                                   signals=["smooth_camera"]),
                SceneVisualLabel(scene_index=1, label="ai_generated_like", confidence=0.4),
                SceneVisualLabel(scene_index=2, label="uncertain", confidence=0.0),
            ],
            scenes=[
                SceneSegment(index=0, start_s=0, end_s=10),
                SceneSegment(index=1, start_s=10, end_s=25),
                SceneSegment(index=2, start_s=25, end_s=60),
            ],
            confidence=0.55,
        ),
        speech=SpeechAnalysis(
            transcript_languages=["ur", "en"],
            language_confidence=0.8,
            wpm=142.0,
            wpm_confidence=0.7,
            hook=HookAnalysis(window_s=30.0, pattern_label="question",
                              pattern_notes="opens with a question", confidence=0.66),
            beats=[SpeechBeat(start_s=0, end_s=30, summary="hook + premise", confidence=0.6)],
            cta=CTAAnalysis(present=True, confidence=0.9,
                            placements=[CTAPlacement(start_s=170, kind="subscribe", confidence=0.85)]),
            transcript_ref="artefacts/transcript-1.txt",
            confidence=0.7,
        ),
        dressing=DressingAnalysis(
            music_presence=PresenceAssessment(label="present", confidence=0.77),
            caption_burn_in=PresenceAssessment(label="absent", confidence=0.88),
            text_overlay_frequency=2.5,
            text_overlay_confidence=0.5,
            motion=MotionEstimate(label="zoom", confidence=0.58, notes="slow zoom"),
            confidence=0.6,
        ),
        thumbnail=ThumbnailAnalysis(
            text_word_count=3,
            text_role_notes="3-word curiosity-gap question, top-left",
            face_present=True,
            face_confidence=0.9,
            dominant_colours=["#E53935", "#111111"],
            layout_class="text_left_subject_right",
            contrast_class="high",
            confidence=0.82,
        ),
    )


def test_video_metadata_derives_resolution():
    v = VideoMetadata(path="x.mp4", width=1280, height=720)
    assert v.resolution == "1280x720"


def test_scene_segment_derives_duration_and_rejects_reversed():
    assert SceneSegment(index=0, start_s=2, end_s=7).duration_s == 5
    with pytest.raises(ValidationError):
        SceneSegment(index=0, start_s=7, end_s=2)


def test_scene_aggregates():
    s = full_analysis().scenes
    assert s.scene_count == 3
    assert s.avg_shot_length_s == 20.0
    assert s.cuts_per_minute == 3.0


def test_json_round_trip_full():
    a = full_analysis()
    restored = StyleLabAnalysis.from_json(a.to_json())
    assert restored == a
    assert restored.model_dump(mode="json") == a.model_dump(mode="json")


def test_json_round_trip_uncertain_and_null_thumbnail():
    a = StyleLabAnalysis.make_uncertain("/tmp/x.mp4", competitor_video_id="c1")
    assert a.thumbnail is None
    assert a.visual_mix.uncertain == 100.0
    assert StyleLabAnalysis.from_json(a.to_json()) == a
    # dict round-trip too
    assert StyleLabAnalysis.model_validate(a.model_dump()) == a


def test_confidence_out_of_range_rejected():
    with pytest.raises(ValidationError):
        SceneVisualLabel(scene_index=0, label="stock_like", confidence=1.5)
    with pytest.raises(ValidationError):
        PresenceAssessment(label="present", confidence=-0.1)


def test_visual_mix_sum_enforced_and_uncertain_representable():
    with pytest.raises(ValidationError):
        VisualMix(stock_like=50.0, uncertain=0.0)
    vm = VisualMix()  # default = fully uncertain, must be valid
    assert vm.uncertain == 100.0 and vm.dominant_label() == "uncertain"


def test_visual_mix_from_labels_weighted_by_duration():
    vm = full_analysis().visual_mix
    pct = vm.percentages()
    assert abs(sum(pct.values()) - 100.0) < 0.01
    # scene 2 is longest (35s) and uncertain -> largest share
    assert pct["uncertain"] == max(pct.values())


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        VideoMetadata(path="x.mp4", bogus_field=1)  # type: ignore[call-arg]


def test_thumbnail_stores_structure_not_text():
    t = full_analysis().thumbnail
    assert t is not None
    assert not hasattr(t, "text")  # no field can hold actual thumbnail wording
    assert "curiosity" in t.text_role_notes
