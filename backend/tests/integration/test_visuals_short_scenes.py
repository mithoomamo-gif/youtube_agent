"""Stage-level regression tests for the Round 3 short-scene beat fix.

The centrepiece is the EXACT Round 2 failing case, hermetically
rebuilt: ``ProjectSpec(niche="facts", topic="Ocean depth secrets",
minutes=0.5, seed=0)`` through the real deterministic content stage,
with per-chapter voice durations *measured* at 275 effective WPM —
the middle of the ~250-300 WPM range real edge-tts produced in
Round 2.  In Round 2 that narration measured only ~17.45 s for the
whole video: 10 sentences, most of them far under the old STANDARD
>= 4 s/scene beat minimum, and it died inside
``plan_and_generate_visuals`` with
``SceneRecord: end_s (17.455) must be greater than start_s (30.109)``
because beat-minimum clamping inflated the timeline to 30.1 s and
the last scene was force-ended at the measured total.  Since Round 4
the bundle itself is planned from calibrated WPM, so its word count
(and therefore the narration measured at this fixture's fixed
275 WPM) has moved twice: ~33.16 s under Roman-row planning, and
19.2 s since the R4-002 fix made the default ur-PK production plan
from the Urdu-script row (see the fixture-sanity pin in the test).

Plate rendering is stubbed with a tiny PNG: the regression lives in
scene *planning/timing*, and rendering 10 full plates would make
this test take ~45 s for no extra signal.  Stock is forced off by
clearing the env key (no keys exist in the test environment).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.integration import produce, visuals_stage
from app.integration.content import build_content
from app.integration.models import ProjectSpec
from app.integration.visuals_stage import plan_and_generate_visuals

#: Real edge-tts effective speed observed in Round 2 (~250-300 WPM).
ROUND2_EDGE_TTS_WPM = 275.0


@pytest.fixture(autouse=True)
def _fast_local_plates(monkeypatch):
    """Stub plate pixels; keep every planning/timing code path real."""
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)

    def fake_render(out_path, *, palette, seed, phrase, scene_class, hero):
        Image.new("RGB", (8, 8), palette[2]).save(out_path, format="PNG")

    monkeypatch.setattr(visuals_stage, "_render_plate", fake_render)


def _ctx(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        visuals_dir=str(tmp_path / "visuals"),
        video_id="round2-regression",
        project_id="round2-regression",
    )


def _voice_for(bundle, wpm: float) -> SimpleNamespace:
    """Voice result whose chapter durations are *measured* at ``wpm``."""
    durations = [
        len(chapter.text.split()) / wpm * 60.0 for chapter in bundle.chapters
    ]
    return SimpleNamespace(
        duration_s=sum(durations),
        chapter_durations_s=durations,
        chapter_files=[],
    )


def _assert_valid_timeline(scenes, total_s):
    assert scenes, "expected at least one scene"
    for scene in scenes:
        assert scene["end_s"] > scene["start_s"], scene
        assert scene["duration_s"] > 0, scene
        assert scene["duration_s"] == pytest.approx(
            scene["end_s"] - scene["start_s"], abs=0.002
        )
    for previous, current in zip(scenes, scenes[1:]):
        assert current["start_s"] >= previous["end_s"] - 1e-9, (
            "scenes must not overlap"
        )
    total_planned = sum(s["duration_s"] for s in scenes)
    assert total_planned == pytest.approx(total_s, abs=0.05)
    assert scenes[-1]["end_s"] == pytest.approx(total_s, abs=0.05)


# ---------------------------------------------------------------------------
# The exact Round 2 failing case
# ---------------------------------------------------------------------------


def test_round2_half_minute_facts_spec_now_builds_valid_scenes(tmp_path):
    spec = ProjectSpec(
        niche="facts", topic="Ocean depth secrets", minutes=0.5, seed=0
    )
    profile = produce.resolve_profile(spec)
    bundle = build_content(spec, profile)
    voice = _voice_for(bundle, ROUND2_EDGE_TTS_WPM)

    # Fixture sanity: this really is the Round 2 shape — a short
    # measured narration for a 0.5-minute spec.  RE-PINNED (R4-002
    # fix pass, ex N-1): the original 17.455 s pin encoded the
    # pre-calibration (Round 2/3) bundle word count.  Under Round 4
    # calibrated planning the same spec measured 33.16 s at this
    # fixture's fixed 275 WPM; since the R4-002 fix the default ur-PK
    # production (this spec: ur-PK-UzmaNeural, tts_input_script now
    # defaults to "urdu_script") plans from the Urdu-script
    # calibration row — 90 target words @179.58 WPM, bundle = 88
    # words — so the fixture measures 88 / 275 x 60 = 19.2 s exactly
    # (measured 2026-10-23).  At the calibrated Urdu WPM itself the
    # same bundle would measure ~29.4 s, i.e. on the 30 s target;
    # the 275 WPM fixture speed is kept because this test pins scene
    # planning on a short measured timeline, not TTS speed.
    assert voice.duration_s == pytest.approx(19.2, abs=0.5)
    assert len(bundle.chapters) == 4

    result = plan_and_generate_visuals(bundle, voice, profile, _ctx(tmp_path))

    scenes = result.scene_manifest
    # Scene-count pin moved 10 -> 11 with the same R4-002 re-pin: the
    # Urdu-script-planned bundle has 11 sentences (deterministic).
    assert len(scenes) == 11  # pinned fixture (deterministic)
    _assert_valid_timeline(scenes, voice.duration_s)

    # Sub-4 s scenes exist and are legal now — most STANDARD scenes
    # measure 1-2.2 s at this narration speed.
    sub4 = [s for s in scenes if s["duration_s"] < 4.0]
    assert len(sub4) >= 5
    assert any(s["duration_s"] < 2.0 for s in scenes)

    # The hook scenes sit in the legal HOOK band (2-4 s).
    hooks = [s for s in scenes if s["beat_class"] == "HOOK"]
    assert len(hooks) == 2
    assert all(2.0 <= h["duration_s"] <= 4.0 for h in hooks)

    # The stage's own aggregate validation (measured-narration mode)
    # accepts the plan: short measured scenes are not violations.
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["validation"]["beat_violations"] == []
    assert manifest["validation"]["is_valid"] is True
    assert result.total_duration_s == pytest.approx(voice.duration_s, abs=0.01)


# ---------------------------------------------------------------------------
# Normal (>= 4 s) planning is unaffected
# ---------------------------------------------------------------------------


def test_normal_length_scenes_still_respect_beat_ranges(tmp_path):
    # 10 plain sentences x 10 words, one chapter, 60 s measured at a
    # calm pace: every beat range is jointly feasible, so the plan
    # must respect them exactly as before the policy change.
    # (Alpha-only tokens: digits would classify the scenes as DATA.)
    bank = [
        "samundar", "lehren", "kinara", "musafir", "chandni",
        "sitaare", "khamoshi", "manzar", "qissa", "safar",
        "dariya", "hawaein", "roshni", "parcham", "mohabbat",
        "duniya", "zamaana", "tasveer", "khushboo", "awaaz",
    ]
    sentences = [
        " ".join(bank[(i + w) % len(bank)] for w in range(10)) + "."
        for i in range(10)
    ]
    bundle = SimpleNamespace(
        chapters=[SimpleNamespace(index=1, title="Main", text=" ".join(sentences))],
        niche_id="facts",
    )
    voice = SimpleNamespace(
        duration_s=60.0, chapter_durations_s=[60.0], chapter_files=[]
    )
    profile = SimpleNamespace(profile_id="facts", visual_style="", taboo=[])

    result = plan_and_generate_visuals(bundle, voice, profile, _ctx(tmp_path))

    scenes = result.scene_manifest
    assert len(scenes) == 10
    _assert_valid_timeline(scenes, 60.0)
    # Hooks clamp to their 4 s ceiling, STANDARD scenes land in 4-8 s:
    # no scene comes out below the 4 s planning floor on a normal video.
    assert min(s["duration_s"] for s in scenes) >= 4.0 - 1e-9
    for scene in scenes:
        if scene["beat_class"] == "HOOK":
            assert scene["duration_s"] <= 4.0 + 1e-9
        elif scene["beat_class"] == "STANDARD":
            assert 4.0 <= scene["duration_s"] <= 8.0


# ---------------------------------------------------------------------------
# Truly invalid timings are explicit errors, never silent fallbacks
# ---------------------------------------------------------------------------


def test_zero_length_measured_chapter_is_an_explicit_error(tmp_path):
    bundle = SimpleNamespace(
        chapters=[
            SimpleNamespace(index=1, title="A", text="Pehla chapter bol raha hai."),
            SimpleNamespace(index=2, title="B", text="Dusra chapter bhi bol raha hai."),
        ],
        niche_id="facts",
    )
    voice = SimpleNamespace(
        duration_s=20.0,
        chapter_durations_s=[20.0, 0.0],  # zero-length measured segment
        chapter_files=[],
    )
    profile = SimpleNamespace(profile_id="facts", visual_style="", taboo=[])

    with pytest.raises(ValueError, match="zero-length measured"):
        plan_and_generate_visuals(bundle, voice, profile, _ctx(tmp_path))


def test_zero_total_voice_duration_is_an_explicit_error(tmp_path):
    bundle = SimpleNamespace(
        chapters=[SimpleNamespace(index=1, title="A", text="Kuch bol raha hai.")],
        niche_id="facts",
    )
    voice = SimpleNamespace(duration_s=0.0, chapter_durations_s=[0.0], chapter_files=[])
    profile = SimpleNamespace(profile_id="facts", visual_style="", taboo=[])

    with pytest.raises(ValueError, match="no positive duration_s"):
        plan_and_generate_visuals(bundle, voice, profile, _ctx(tmp_path))
