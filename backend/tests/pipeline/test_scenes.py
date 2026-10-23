"""Tests for Worker 1 — Scene Planning (app.pipeline.scenes).

Covers: manifest model validation, beat-duration validator (too-long
still, too-fast list, density), the deterministic stock-vs-AI decision
order, motion alternation, the measured timing spine (real ffprobe
durations from ffmpeg-generated WAVs), and the faceless-safety gates
including the Islamic-niche overlay.
"""

import shutil
import subprocess

import pytest
from pydantic import ValidationError

from app.pipeline.scenes import (
    BeatClass,
    MotionKind,
    NarrationSegment,
    NicheSafetyProfile,
    PlanBudgetState,
    ProbeError,
    ReasonCode,
    SceneClass,
    SceneFeatures,
    SceneManifest,
    SceneRecord,
    SceneSafetyFacts,
    SourceKind,
    StockCandidate,
    WordAlignment,
    build_timing_spine,
    decide_source,
    facts_from_scene,
    probe_duration_s,
    validate_ai_budget,
    validate_beat_durations,
    validate_motion_sequence,
    validate_plan_safety,
    validate_scene_plan,
    validate_scene_safety,
)
from app.pipeline.scenes.beats import BeatViolationCode
from app.pipeline.scenes.motion import suggest_next_motion
from app.pipeline.scenes.models import MotionInstruction
from app.pipeline.scenes.safety import SafetyViolationCode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_scene(
    scene_id="s1",
    start=0.0,
    dur=6.0,
    beat=BeatClass.STANDARD,
    cls=SceneClass.REAL_WORLD,
    source=SourceKind.STOCK,
    motion=MotionKind.ZOOM_IN,
    **kwargs,
):
    kwargs.setdefault("source_sentence", f"Sentence for {scene_id}.")
    return SceneRecord(
        scene_id=scene_id,
        start_s=start,
        end_s=start + dur,
        beat_class=beat,
        scene_class=cls,
        chosen_source=source,
        motion=MotionInstruction(kind=motion),
        **kwargs,
    )


def budget(**kwargs):
    kwargs.setdefault("total_scenes", 10)
    return PlanBudgetState(**kwargs)


def features(cls=SceneClass.REAL_WORLD, **kwargs):
    return SceneFeatures(scene_class=cls, **kwargs)


def good_stock(**kwargs):
    kwargs.setdefault("candidate_id", "cand-1")
    kwargs.setdefault("relevance_cosine", 0.85)
    return StockCandidate(**kwargs)


# ---------------------------------------------------------------------------
# 1. Manifest model validation
# ---------------------------------------------------------------------------


class TestManifestModel:
    def test_valid_scene_record_roundtrip(self):
        scene = make_scene(
            decision_reason=ReasonCode.STOCK_MATCH,
            asset_ref="asset-123",
            licence_ref="lic-456",
            content_hash="a" * 64,
            reuse_count=1,
        )
        assert scene.duration_s == 6.0
        assert scene.start_ms == 0 and scene.end_ms == 6000
        assert scene.narration_text == scene.source_sentence
        assert SceneRecord.model_validate(scene.model_dump()) == scene

    def test_all_eight_scene_classes_exist(self):
        assert {c.value for c in SceneClass} == {
            "REAL_WORLD",
            "SPECIFIC_EVENT",
            "ABSTRACT_CONCEPT",
            "DATA",
            "TEXTUAL",
            "CHARACTER_MOMENT",
            "TRANSITION",
            "HERO",
        }

    def test_end_must_exceed_start(self):
        with pytest.raises(ValidationError):
            make_scene(start=5.0, dur=0.0)
        with pytest.raises(ValidationError):
            SceneRecord(
                scene_id="x",
                source_sentence="Hi.",
                start_s=5.0,
                end_s=4.0,
                scene_class=SceneClass.DATA,
                chosen_source=SourceKind.GRAPHIC,
            )

    def test_overlay_text_capped_at_six_words(self):
        with pytest.raises(ValidationError):
            make_scene(overlay_text="one two three four five six seven")
        assert make_scene(overlay_text="one two three four five six")

    def test_content_hash_must_be_sha256_hex(self):
        with pytest.raises(ValidationError):
            make_scene(content_hash="not-a-hash")
        assert make_scene(content_hash="0123456789abcdef" * 4)

    def test_reuse_count_cannot_be_negative(self):
        with pytest.raises(ValidationError):
            make_scene(reuse_count=-1)

    def test_manifest_rejects_duplicate_scene_ids(self):
        with pytest.raises(ValidationError):
            SceneManifest(
                video_id="v1", scenes=[make_scene("dup"), make_scene("dup", start=6.0)]
            )

    def test_manifest_hero_cap_is_three(self):
        heroes = [
            make_scene(f"h{i}", start=i * 6.0, cls=SceneClass.HERO, source=SourceKind.AI_IMAGE)
            for i in range(3)
        ]
        manifest = SceneManifest(video_id="v1", scenes=heroes)
        assert manifest.hero_count == 3
        with pytest.raises(ValidationError):
            SceneManifest(
                video_id="v1",
                scenes=heroes
                + [make_scene("h4", start=18.0, cls=SceneClass.HERO, source=SourceKind.AI_IMAGE)],
            )

    def test_manifest_derived_counts(self):
        scenes = [
            make_scene("a", start=0.0, source=SourceKind.AI_IMAGE),
            make_scene("b", start=6.0, source=SourceKind.AI_VIDEO),
            make_scene("c", start=12.0, source=SourceKind.STOCK),
        ]
        manifest = SceneManifest(video_id="v1", scenes=scenes)
        assert manifest.total_duration_s == 18.0
        assert manifest.ai_count == 2 and manifest.ai_video_count == 1
        assert manifest.scene_by_id("b").scene_id == "b"
        assert manifest.scene_by_id("nope") is None


# ---------------------------------------------------------------------------
# 2. Beat-duration validator
# ---------------------------------------------------------------------------


class TestBeatValidator:
    def codes(self, scenes, **kw):
        kw.setdefault("check_density", False)
        return [v.code for v in validate_beat_durations(scenes, **kw)]

    def test_too_long_still_is_flagged(self):
        scene = make_scene(
            dur=20.0,
            beat=BeatClass.EXPLANATORY,
            source=SourceKind.AI_IMAGE,
            motion=MotionKind.STATIC,
            is_still=True,
        )
        violations = validate_beat_durations([scene], check_density=False)
        assert BeatViolationCode.STILL_TOO_LONG in [v.code for v in violations]

    def test_long_still_with_motion_event_is_fine(self):
        scene = make_scene(
            dur=20.0,
            beat=BeatClass.EXPLANATORY,
            source=SourceKind.AI_IMAGE,
            motion=MotionKind.ZOOM_IN,
            is_still=True,
        )
        codes = self.codes([scene])
        assert BeatViolationCode.STILL_TOO_LONG not in codes

    def test_long_moving_clip_is_not_a_still_violation(self):
        scene = make_scene(dur=20.0, beat=BeatClass.EXPLANATORY, is_still=False)
        assert BeatViolationCode.STILL_TOO_LONG not in self.codes([scene])

    def test_too_fast_list_is_flagged(self):
        # 3 items at >= 2.5 s/item need >= 7.5 s; 5 s is too fast.
        scene = make_scene(dur=5.0, beat=BeatClass.LIST, list_item_count=3)
        violations = validate_beat_durations([scene], check_density=False)
        assert BeatViolationCode.LIST_ITEM_TOO_FAST in [v.code for v in violations]

    def test_list_at_exact_per_item_floor_is_fine(self):
        scene = make_scene(dur=7.5, beat=BeatClass.LIST, list_item_count=3)
        assert self.codes([scene]) == []

    @pytest.mark.parametrize(
        "beat,dur,ok",
        [
            (BeatClass.HOOK, 3.0, True),
            (BeatClass.HOOK, 1.8, False),
            (BeatClass.HOOK, 5.0, False),
            (BeatClass.STANDARD, 6.0, True),
            (BeatClass.STANDARD, 3.0, False),
            (BeatClass.STANDARD, 9.0, False),
            (BeatClass.EXPLANATORY, 10.0, True),
            (BeatClass.EXPLANATORY, 5.0, False),
            (BeatClass.EMOTIONAL, 9.0, True),
            (BeatClass.EMOTIONAL, 13.0, False),
        ],
    )
    def test_beat_ranges(self, beat, dur, ok):
        codes = self.codes([make_scene(dur=dur, beat=beat)])
        range_codes = {BeatViolationCode.BEAT_TOO_SHORT, BeatViolationCode.BEAT_TOO_LONG}
        assert (not range_codes.intersection(codes)) == ok

    def test_scene_below_hard_minimum_flagged(self):
        scene = make_scene(dur=1.0, beat=BeatClass.HOOK)
        assert BeatViolationCode.SCENE_TOO_SHORT in self.codes([scene])

    def test_density_too_low_flagged(self):
        scenes = [make_scene("a", start=0.0, dur=30.0), make_scene("b", start=30.0, dur=30.0)]
        codes = [v.code for v in validate_beat_durations(scenes)]
        assert BeatViolationCode.DENSITY_TOO_LOW in codes  # 2 changes/min < 6

    def test_density_too_high_flagged(self):
        scenes = [
            make_scene(f"s{i}", start=i * 2.0, dur=2.0, beat=BeatClass.HOOK)
            for i in range(30)
        ]
        codes = [v.code for v in validate_beat_durations(scenes)]
        assert BeatViolationCode.DENSITY_TOO_HIGH in codes  # 30/min > 20

    def test_healthy_plan_has_no_beat_violations(self):
        scenes = [
            make_scene(
                f"s{i}",
                start=i * 6.0,
                dur=6.0,
                motion=MotionKind.ZOOM_IN if i % 2 == 0 else MotionKind.ZOOM_OUT,
            )
            for i in range(10)
        ]
        assert validate_beat_durations(scenes) == []  # 10 changes/min


# ---------------------------------------------------------------------------
# 3. Stock-vs-AI decision order
# ---------------------------------------------------------------------------


class TestDecisionOrder:
    def test_data_goes_programmatic_even_with_great_stock(self):
        decision = decide_source(features(SceneClass.DATA), [good_stock()], budget())
        assert decision.choice is SourceKind.GRAPHIC
        assert decision.reason_code is ReasonCode.PROGRAMMATIC_DATA

    def test_textual_goes_programmatic_text_card(self):
        decision = decide_source(features(SceneClass.TEXTUAL), [good_stock()], budget())
        assert decision.choice is SourceKind.TEXT_CARD
        assert decision.reason_code is ReasonCode.PROGRAMMATIC_TEXT

    def test_safety_precheck_runs_before_everything(self):
        decision = decide_source(
            features(SceneClass.DATA, safety_restricted=True), [good_stock()], budget()
        )
        assert decision.choice is SourceKind.TEXT_CARD
        assert decision.reason_code is ReasonCode.SAFETY_ROUTE

    def test_real_world_stock_first_when_relevant(self):
        decision = decide_source(features(), [good_stock()], budget())
        assert decision.choice is SourceKind.STOCK
        assert decision.reason_code is ReasonCode.STOCK_MATCH

    def test_low_relevance_stock_rejected_then_ai(self):
        weak = good_stock(relevance_cosine=0.10)  # below the 0.22 floor
        decision = decide_source(features(), [weak], budget())
        assert decision.choice is SourceKind.AI_IMAGE
        assert decision.reason_code is ReasonCode.STOCK_BELOW_THRESHOLD

    def test_stock_at_threshold_is_accepted(self):
        edge = good_stock(relevance_cosine=0.22)
        decision = decide_source(features(), [edge], budget())
        assert decision.choice is SourceKind.STOCK

    def test_watermarked_stock_rejected(self):
        bad = good_stock(has_watermark=True)
        decision = decide_source(features(), [bad], budget())
        assert decision.choice is SourceKind.AI_IMAGE
        assert decision.reason_code is ReasonCode.STOCK_BELOW_THRESHOLD

    def test_no_stock_goes_ai_with_no_stock_reason(self):
        decision = decide_source(features(), [], budget())
        assert decision.choice is SourceKind.AI_IMAGE
        assert decision.reason_code is ReasonCode.NO_STOCK_EXISTS

    def test_named_subject_unverified_stock_rejected(self):
        unverified = good_stock(relevance_cosine=0.95, subject_verified=None)
        decision = decide_source(
            features(is_named_subject=True), [unverified], budget()
        )
        assert decision.choice is SourceKind.AI_IMAGE
        assert decision.reason_code is ReasonCode.STOCK_UNVERIFIED_NAMED

    def test_named_subject_verified_stock_accepted(self):
        verified = good_stock(relevance_cosine=0.95, subject_verified=True)
        decision = decide_source(features(is_named_subject=True), [verified], budget())
        assert decision.choice is SourceKind.STOCK

    def test_abstract_goes_ai(self):
        decision = decide_source(
            features(SceneClass.ABSTRACT_CONCEPT), [good_stock()], budget()
        )
        assert decision.choice is SourceKind.AI_IMAGE
        assert decision.reason_code is ReasonCode.ABSTRACT_AI

    def test_specific_event_and_character_go_ai(self):
        event = decide_source(features(SceneClass.SPECIFIC_EVENT), [good_stock()], budget())
        assert event.choice is SourceKind.AI_IMAGE
        assert event.reason_code is ReasonCode.SPECIFIC_EVENT_AI
        char = decide_source(features(SceneClass.CHARACTER_MOMENT), [], budget())
        assert char.choice is SourceKind.AI_IMAGE
        assert char.reason_code is ReasonCode.CHARACTER_AI

    def test_transition_uses_template(self):
        decision = decide_source(features(SceneClass.TRANSITION), [], budget())
        assert decision.choice is SourceKind.GRAPHIC
        assert decision.reason_code is ReasonCode.TRANSITION_TEMPLATE

    def test_hero_upgrade_to_ai_image(self):
        decision = decide_source(features(SceneClass.HERO), [], budget())
        assert decision.choice is SourceKind.AI_IMAGE
        assert decision.reason_code is ReasonCode.HERO_UPGRADE

    def test_hero_cap_blocks_fourth_hero_upgrade(self):
        decision = decide_source(
            features(SceneClass.HERO), [], budget(hero_scenes_so_far=3)
        )
        assert decision.choice is SourceKind.GRAPHIC
        assert decision.reason_code is ReasonCode.HERO_CAP_EXCEEDED

    def test_ai_budget_cap_degrades_to_graphic(self):
        # 4/10 AI scenes already used = the 40% cap is full.
        decision = decide_source(
            features(SceneClass.ABSTRACT_CONCEPT), [], budget(ai_scenes_so_far=4)
        )
        assert decision.choice is SourceKind.GRAPHIC
        assert decision.reason_code is ReasonCode.AI_BUDGET_EXCEEDED

    def test_credit_cap_degrades_to_graphic(self):
        decision = decide_source(
            features(SceneClass.ABSTRACT_CONCEPT),
            [],
            budget(credit_cap=2, credits_used=2),
        )
        assert decision.choice is SourceKind.GRAPHIC
        assert decision.reason_code is ReasonCode.CREDIT_CAP_EXCEEDED

    def test_ai_video_hero_premium_within_cap(self):
        decision = decide_source(
            features(SceneClass.HERO, wants_ai_video=True),
            [],
            budget(total_scenes=20, premium_enabled=True),
        )
        assert decision.choice is SourceKind.AI_VIDEO
        assert decision.reason_code is ReasonCode.HERO_UPGRADE

    def test_ai_video_requires_premium(self):
        decision = decide_source(
            features(SceneClass.HERO, wants_ai_video=True),
            [],
            budget(total_scenes=20, premium_enabled=False),
        )
        assert decision.choice is SourceKind.AI_IMAGE  # degrades, never AI video

    def test_ai_video_ten_percent_cap(self):
        # 1/10 AI-video scenes already used = the 10% cap is full.
        decision = decide_source(
            features(SceneClass.HERO, wants_ai_video=True),
            [],
            budget(premium_enabled=True, ai_video_scenes_so_far=1),
        )
        assert decision.choice is SourceKind.AI_IMAGE

    def test_decision_is_deterministic_and_unpackable(self):
        args = (features(), [good_stock()], budget())
        first, second = decide_source(*args), decide_source(*args)
        assert first == second
        choice, reason_code = first[:2]  # the roadmap's (choice, reason) pair
        assert choice is SourceKind.STOCK and reason_code is ReasonCode.STOCK_MATCH

    def test_plan_level_ai_budget_validator(self):
        scenes = [
            make_scene(f"s{i}", start=i * 6.0, source=SourceKind.AI_IMAGE if i < 5 else SourceKind.STOCK)
            for i in range(10)
        ]
        problems = validate_ai_budget(scenes)
        assert any("AI budget exceeded" in p for p in problems)  # 50% > 40%
        ok = [make_scene(f"s{i}", start=i * 6.0) for i in range(10)]
        assert validate_ai_budget(ok) == []


# ---------------------------------------------------------------------------
# 4. Motion alternation
# ---------------------------------------------------------------------------


class TestMotionRules:
    def test_three_identical_motions_in_a_row_flagged(self):
        scenes = [
            make_scene(f"s{i}", start=i * 6.0, motion=MotionKind.ZOOM_IN)
            for i in range(3)
        ]
        violations = validate_motion_sequence(scenes)
        assert len(violations) == 1
        assert violations[0].run_length == 3
        assert violations[0].scene_ids == ["s0", "s1", "s2"]

    def test_two_identical_motions_are_allowed(self):
        scenes = [
            make_scene("a", start=0.0, motion=MotionKind.PAN_LEFT),
            make_scene("b", start=6.0, motion=MotionKind.PAN_LEFT),
        ]
        assert validate_motion_sequence(scenes) == []

    def test_alternating_motions_are_clean(self):
        kinds = [MotionKind.ZOOM_IN, MotionKind.ZOOM_OUT, MotionKind.PAN_LEFT, MotionKind.PAN_RIGHT]
        scenes = [
            make_scene(f"s{i}", start=i * 6.0, motion=kinds[i % 4]) for i in range(8)
        ]
        assert validate_motion_sequence(scenes) == []

    def test_long_run_reports_one_violation_per_run(self):
        scenes = [
            make_scene(f"s{i}", start=i * 6.0, motion=MotionKind.STATIC)
            for i in range(5)
        ]
        violations = validate_motion_sequence(scenes)
        assert len(violations) == 1 and violations[0].run_length == 5

    def test_suggest_next_motion_alternates_direction(self):
        nxt = suggest_next_motion(MotionInstruction(kind=MotionKind.ZOOM_IN))
        assert nxt.kind is MotionKind.ZOOM_OUT
        nxt = suggest_next_motion(MotionInstruction(kind=MotionKind.PAN_LEFT))
        assert nxt.kind is MotionKind.PAN_RIGHT
        assert suggest_next_motion(None).kind is MotionKind.ZOOM_IN


# ---------------------------------------------------------------------------
# 5. Timing spine from measured narration
# ---------------------------------------------------------------------------


def _make_wav(path, seconds, freq=440):
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
            "-ar", "48000", "-ac", "1", str(path),
        ],
        check=True,
    )
    return path


ffmpeg_required = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not available",
)


@ffmpeg_required
class TestTimingSpineWithFfprobe:
    def test_probe_duration_of_generated_wav(self, tmp_path):
        wav = _make_wav(tmp_path / "tone.wav", 1.0)
        assert probe_duration_s(wav) == pytest.approx(1.0, abs=0.1)

    def test_probe_missing_file_raises(self, tmp_path):
        with pytest.raises(ProbeError):
            probe_duration_s(tmp_path / "nope.wav")

    def test_spine_built_from_real_probed_durations(self, tmp_path):
        wav_a = _make_wav(tmp_path / "a.wav", 1.0, freq=440)
        wav_b = _make_wav(tmp_path / "b.wav", 0.5, freq=660)
        spine = build_timing_spine(
            [
                NarrationSegment(sentence_id="s1", text="First.", audio_path=str(wav_a)),
                NarrationSegment(sentence_id="s2", text="Second.", audio_path=str(wav_b)),
            ]
        )
        first, second = spine.entries
        assert first.duration_source == "ffprobe"
        assert first.start_s == pytest.approx(0.0)
        assert first.end_s == pytest.approx(1.0, abs=0.1)
        assert second.start_s == pytest.approx(first.end_s)  # contiguous
        assert second.end_s == pytest.approx(1.5, abs=0.15)
        assert spine.total_duration_s == pytest.approx(1.5, abs=0.15)

    def test_probed_duration_overrides_stale_figure(self, tmp_path):
        wav = _make_wav(tmp_path / "a.wav", 1.0)
        spine = build_timing_spine(
            [
                NarrationSegment(
                    sentence_id="s1",
                    text="Measured wins.",
                    audio_path=str(wav),
                    measured_duration_s=9.9,  # stale: ffprobe is truth
                )
            ]
        )
        assert spine.entries[0].measured_duration_s == pytest.approx(1.0, abs=0.1)


class TestTimingSpineMeasured:
    def test_spine_from_measured_durations_with_gap(self):
        spine = build_timing_spine(
            [
                NarrationSegment(sentence_id="s1", text="One.", measured_duration_s=2.0),
                NarrationSegment(sentence_id="s2", text="Two.", measured_duration_s=3.0),
            ],
            gap_s=0.3,
        )
        first, second = spine.entries
        assert (first.start_s, first.end_s) == (0.0, 2.0)
        assert (second.start_s, second.end_s) == (2.3, 5.3)
        assert spine.total_duration_s == 5.3
        assert spine.timing_for("s2") is second

    def test_refuses_to_estimate_from_text(self):
        with pytest.raises(ValueError, match="refusing to estimate"):
            build_timing_spine(
                [NarrationSegment(sentence_id="s1", text="No timing at all.")]
            )

    def test_drift_against_estimate_is_reported_not_used(self):
        spine = build_timing_spine(
            [
                NarrationSegment(
                    sentence_id="s1",
                    text="Drift.",
                    measured_duration_s=2.0,
                    estimated_duration_s=1.5,
                )
            ]
        )
        entry = spine.entries[0]
        assert entry.end_s == 2.0  # measured, not the 1.5 s estimate
        assert entry.drift_s == pytest.approx(0.5)

    def test_word_alignment_is_rebased_onto_spine(self):
        spine = build_timing_spine(
            [
                NarrationSegment(sentence_id="s1", text="One.", measured_duration_s=2.0),
                NarrationSegment(
                    sentence_id="s2",
                    text="Two words.",
                    measured_duration_s=2.0,
                    words=[
                        WordAlignment(word="Two", start_s=0.1, end_s=0.4),
                        WordAlignment(word="words", start_s=0.5, end_s=1.1),
                    ],
                ),
            ]
        )
        words = spine.entries[1].words
        assert (words[0].start_s, words[0].end_s) == (2.1, 2.4)
        assert (words[1].start_s, words[1].end_s) == (2.5, 3.1)

    def test_alignment_outside_measured_span_rejected(self):
        with pytest.raises(ValueError, match="does not match"):
            build_timing_spine(
                [
                    NarrationSegment(
                        sentence_id="s1",
                        text="Short.",
                        measured_duration_s=1.0,
                        words=[WordAlignment(word="Late", start_s=0.9, end_s=2.0)],
                    )
                ]
            )

    def test_empty_and_duplicate_segments_rejected(self):
        with pytest.raises(ValueError):
            build_timing_spine([])
        dup = NarrationSegment(sentence_id="s1", text="x", measured_duration_s=1.0)
        with pytest.raises(ValueError, match="duplicate"):
            build_timing_spine([dup, dup])


# ---------------------------------------------------------------------------
# 6. Faceless-safety hard gates
# ---------------------------------------------------------------------------


def facts(**kwargs):
    kwargs.setdefault("scene_id", "s1")
    kwargs.setdefault("scene_class", SceneClass.REAL_WORLD)
    kwargs.setdefault("chosen_source", SourceKind.AI_IMAGE)
    return SceneSafetyFacts(**kwargs)


ISLAMIC = NicheSafetyProfile(niche_id="islamic_stories", is_islamic=True)
GENERIC = NicheSafetyProfile(niche_id="facts", taboo_terms=["casino", "gambling"])


class TestSafetyGates:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"has_recognisable_real_person_likeness": True},
            {"has_gore": True},
            {"misleading_photoreal_named_subject": True},
            {"has_logo_or_watermark": True},
            {"has_ai_photoreal_child": True},
            {"has_generated_in_image_text": True},
        ],
    )
    def test_each_global_gate_blocks(self, kwargs):
        assessment = validate_scene_safety(facts(**kwargs), GENERIC)
        assert assessment.blocked
        assert assessment.violations[0].code is SafetyViolationCode.BLOCKING_FLAG

    def test_clean_scene_passes(self):
        assessment = validate_scene_safety(
            facts(visual_prompt="mountain valley at dawn, wide shot"), GENERIC
        )
        assert not assessment.blocked and assessment.violations == []

    def test_taboo_term_in_prompt_blocks(self):
        assessment = validate_scene_safety(
            facts(visual_prompt="neon casino sign at night"), GENERIC
        )
        assert assessment.blocked
        assert SafetyViolationCode.TABOO_TERM in [v.code for v in assessment.violations]

    def test_islamic_prophet_depiction_fact_blocks(self):
        assessment = validate_scene_safety(facts(depicts_prophet=True), ISLAMIC)
        assert assessment.blocked
        assert SafetyViolationCode.PROPHET_DEPICTION in [
            v.code for v in assessment.violations
        ]

    def test_islamic_prophet_depiction_prompt_blocks(self):
        assessment = validate_scene_safety(
            facts(visual_prompt="portrait of the Prophet Muhammad, face visible"),
            ISLAMIC,
        )
        assert assessment.blocked
        assert SafetyViolationCode.PROPHET_DEPICTION in [
            v.code for v in assessment.violations
        ]

    def test_islamic_safe_symbolism_passes(self):
        assessment = validate_scene_safety(
            facts(
                visual_prompt="mosque architecture at sunset, desert light, geometric patterns",
                narration_text="The Prophet Muhammad (peace be upon him) travelled at night.",
            ),
            ISLAMIC,
        )
        assert not assessment.blocked  # narration may mention; visuals may not depict

    def test_prophet_prompt_only_blocked_for_islamic_niche(self):
        # The depiction gate is an Islamic-overlay rule; the generic
        # niche here has no such overlay (global gates still apply).
        assessment = validate_scene_safety(
            facts(visual_prompt="portrait of the Prophet Muhammad, face visible"),
            GENERIC,
        )
        assert SafetyViolationCode.PROPHET_DEPICTION not in [
            v.code for v in assessment.violations
        ]

    def test_scripture_must_be_text_card_with_reference(self):
        not_card = validate_scene_safety(
            facts(
                scene_class=SceneClass.TEXTUAL,
                chosen_source=SourceKind.AI_IMAGE,
                is_scripture=True,
                surah_ref="Al-Baqarah",
                ayah_ref="255",
            ),
            ISLAMIC,
        )
        assert SafetyViolationCode.SCRIPTURE_NOT_TEXT_CARD in [
            v.code for v in not_card.violations
        ]

        no_ref = validate_scene_safety(
            facts(
                scene_class=SceneClass.TEXTUAL,
                chosen_source=SourceKind.TEXT_CARD,
                is_scripture=True,
            ),
            ISLAMIC,
        )
        assert SafetyViolationCode.SCRIPTURE_MISSING_REFERENCE in [
            v.code for v in no_ref.violations
        ]

        proper = validate_scene_safety(
            facts(
                scene_class=SceneClass.TEXTUAL,
                chosen_source=SourceKind.TEXT_CARD,
                is_scripture=True,
                surah_ref="Al-Baqarah",
                ayah_ref="255",
            ),
            ISLAMIC,
        )
        assert not proper.blocked

    def test_facts_from_scene_maps_manifest_flags(self):
        scene = make_scene(safety_flags=["gore"])
        assessment = validate_scene_safety(facts_from_scene(scene), GENERIC)
        assert assessment.blocked

    def test_plan_safety_returns_only_blocked(self):
        plan = [
            facts(scene_id="ok", visual_prompt="forest path, morning light"),
            facts(scene_id="bad", has_gore=True),
        ]
        blocked = validate_plan_safety(plan, GENERIC)
        assert [a.scene_id for a in blocked] == ["bad"]


# ---------------------------------------------------------------------------
# 7. Aggregate plan validation
# ---------------------------------------------------------------------------


class TestAggregateValidation:
    def test_clean_plan_is_valid(self):
        scenes = [
            make_scene(
                f"s{i}",
                start=i * 6.0,
                motion=MotionKind.ZOOM_IN if i % 2 == 0 else MotionKind.ZOOM_OUT,
            )
            for i in range(10)
        ]
        report = validate_scene_plan(scenes)
        assert report.is_valid

    def test_dirty_plan_reports_every_layer(self):
        scenes = [
            make_scene(
                f"s{i}",
                start=i * 20.0,
                dur=20.0,
                beat=BeatClass.EXPLANATORY,
                source=SourceKind.AI_IMAGE,
                motion=MotionKind.ZOOM_IN,
                is_still=True,
            )
            for i in range(3)
        ]
        report = validate_scene_plan(scenes)
        assert not report.is_valid
        assert report.beat_violations  # still too long / density
        assert report.motion_violations  # 3x zoom_in in a row
