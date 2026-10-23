"""Unit tests for app.style_analysis.visual_classify.

No network, no real video files except where ffmpeg is used on a
synthetic clip generated in-test. Vision is mocked via the injected
``vision_fn`` callable and via a MockTransport on the Gemini path.
"""

from __future__ import annotations

import io
import json
import subprocess

import httpx
import numpy as np
import pytest
from PIL import Image

from app.style_analysis import visual_classify as vc


# ---------------------------------------------------------------------------
# Helpers: synthetic frames
# ---------------------------------------------------------------------------
def _jpeg(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr.astype(np.uint8)).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _solid_frame(rng_seed: int = 0, colour=(30, 90, 160), size=(90, 160)) -> bytes:
    arr = np.zeros((size[0], size[1], 3), dtype=np.uint8)
    arr[:, :] = colour
    return _jpeg(arr)


def _noisy_frame(seed: int = 1, size=(90, 160)) -> bytes:
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 256, size=(size[0], size[1], 3))
    return _jpeg(arr)


def _make_video(path, colour="navy", duration=3) -> str:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c={colour}:s=320x180:d={duration}:r=5",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True, timeout=60,
    )
    return str(path)


# ---------------------------------------------------------------------------
# Frame signals / heuristics
# ---------------------------------------------------------------------------
def test_solid_colour_card_has_high_flatness():
    sig = vc.frame_signals(_solid_frame())
    assert sig["flat_ratio"] > 0.9
    assert sig["palette_breadth"] < 0.05


def test_noisy_frame_has_broad_palette_and_high_noise():
    sig = vc.frame_signals(_noisy_frame())
    assert sig["palette_breadth"] > 0.05
    assert sig["noise"] > 5.0
    assert sig["flat_ratio"] < 0.2


def test_solid_card_heuristics_point_to_text_card():
    sig = vc.frame_signals(_solid_frame())
    label, conf, signals = vc.heuristics_judgement([sig], motion=0.0)
    assert label == "text_card"
    assert conf >= 0.55
    assert "large_flat_colour_area" in signals


def test_noisy_natural_frame_heuristics_stay_a_weak_prior():
    sig = vc.frame_signals(_noisy_frame())
    label, conf, _ = vc.heuristics_judgement([sig], motion=0.05)
    # Heuristics alone must NOT be confident on natural imagery.
    assert conf < vc.UNCERTAIN_THRESHOLD


# ---------------------------------------------------------------------------
# Combination rule
# ---------------------------------------------------------------------------
def test_combine_agreement_boosts_confidence():
    out = vc.combine_judgements(
        "stock_like", 0.40,
        {"label": "stock_like", "confidence": 0.9, "signals": ["graded"]},
    )
    assert out["label"] == "stock_like"
    assert out["confidence"] == pytest.approx(min(0.95, 0.7 * 0.9 + 0.3 * 0.4 + 0.10))
    assert out["provenance"] == "vision+heuristics"


def test_combine_disagreement_penalises_and_takes_vision_label():
    out = vc.combine_judgements(
        "stock_like", 0.40,
        {"label": "ai_generated_like", "confidence": 0.8, "signals": []},
    )
    assert out["candidate_label"] == "ai_generated_like"
    assert out["confidence"] == pytest.approx(0.48)
    # 0.48 < threshold -> uncertain, candidate preserved in signals.
    assert out["label"] == "uncertain"
    assert "candidate:ai_generated_like" in out["signals"]


def test_combine_heuristics_only_is_discounted_and_capped():
    out = vc.combine_judgements("text_card", 0.58, None)
    assert out["provenance"] == "heuristics_only"
    assert out["confidence"] == pytest.approx(0.8 * 0.58)
    assert out["label"] == "uncertain"  # 0.464 < 0.55


def test_combine_low_vision_confidence_becomes_uncertain():
    out = vc.combine_judgements(
        "raw_footage_like", 0.38,
        {"label": "raw_footage_like", "confidence": 0.4, "signals": ["unsure"]},
    )
    assert out["label"] == "uncertain"
    assert out["candidate_label"] == "raw_footage_like"


def test_combine_unrecognised_vision_label_falls_back_to_heuristics():
    out = vc.combine_judgements(
        "text_card", 0.58, {"label": "definitely_stock", "confidence": 0.99}
    )
    assert out["provenance"] == "heuristics_only"


# ---------------------------------------------------------------------------
# Aggregation math
# ---------------------------------------------------------------------------
def test_aggregate_mix_percentages_and_uncertain_separate():
    scenes = [
        {"label": "stock_like"}, {"label": "stock_like"},
        {"label": "text_card"}, {"label": "uncertain"},
    ]
    mix = vc.aggregate_mix(scenes)
    assert mix["stock_like"] == 50.0
    assert mix["text_card"] == 25.0
    assert mix["uncertain"] == 25.0
    assert mix["ai_generated_like"] == 0.0
    assert sum(mix.values()) == pytest.approx(100.0)


def test_aggregate_mix_empty_is_all_zeros():
    mix = vc.aggregate_mix([])
    assert set(mix) == set(vc.CLASSES)
    assert all(v == 0.0 for v in mix.values())


# ---------------------------------------------------------------------------
# End-to-end classify_scenes with mocked vision (synthetic video)
# ---------------------------------------------------------------------------
def test_classify_scenes_with_mocked_vision(tmp_path):
    video = _make_video(tmp_path / "card.mp4", colour="navy")
    scenes = [{"scene_index": 0, "start": 0.0, "end": 3.0}]

    def fake_vision(frames):
        assert frames, "expected sampled frames"
        return {"label": "text_card", "confidence": 0.9,
                "signals": ["flat navy background", "no photographic content"]}

    out = vc.classify_scenes(video, scenes, vision_fn=fake_vision)
    assert out["scene_count"] == 1
    scene = out["scenes"][0]
    assert scene["scene_index"] == 0
    assert scene["label"] == "text_card"
    assert scene["provenance"] == "vision+heuristics"
    assert any(s.startswith("vision:") for s in scene["signals"])
    assert out["visual_mix"]["text_card"] == 100.0


def test_classify_scenes_vision_failure_degrades_to_heuristics_only(tmp_path):
    video = _make_video(tmp_path / "card.mp4", colour="darkred")

    def failing_vision(frames):
        raise RuntimeError("simulated 429")

    out = vc.classify_scenes(
        video, [{"start": 0.0, "end": 3.0}], vision_fn=failing_vision
    )
    scene = out["scenes"][0]
    assert scene["provenance"] == "heuristics_only"
    # Heuristics-only confidence is capped/discounted: a flat card's
    # heuristic 0.58 becomes 0.464 -> below threshold -> uncertain.
    assert scene["label"] == "uncertain"
    assert scene["candidate_label"] == "text_card"
    assert out["visual_mix"]["uncertain"] == 100.0


def test_classify_scenes_low_confidence_vision_yields_uncertain(tmp_path):
    video = _make_video(tmp_path / "v.mp4", colour="teal")

    def unsure_vision(frames):
        return {"label": "stock_like", "confidence": 0.3,
                "signals": ["cannot distinguish from raw footage"]}

    out = vc.classify_scenes(
        video, [{"start": 0.0, "end": 3.0}], vision_fn=unsure_vision
    )
    assert out["scenes"][0]["label"] == "uncertain"
    assert out["visual_mix"]["uncertain"] == 100.0


def test_classify_scenes_missing_video_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        vc.classify_scenes(tmp_path / "nope.mp4", [{"start": 0, "end": 1}])


def test_classify_scenes_multi_scene_mix_math(tmp_path):
    video = _make_video(tmp_path / "multi.mp4", colour="purple", duration=6)
    scenes = [{"start": 0.0, "end": 3.0}, {"start": 3.0, "end": 6.0}]
    judgements = iter([
        {"label": "text_card", "confidence": 0.95, "signals": []},
        {"label": "screen_capture", "confidence": 0.2, "signals": []},
    ])
    out = vc.classify_scenes(
        video, scenes, vision_fn=lambda frames: next(judgements)
    )
    assert out["scenes"][0]["label"] == "text_card"
    assert out["scenes"][1]["label"] == "uncertain"
    assert out["visual_mix"]["text_card"] == 50.0
    assert out["visual_mix"]["uncertain"] == 50.0


# ---------------------------------------------------------------------------
# Gemini path via MockTransport (no network)
# ---------------------------------------------------------------------------
def _gemini_response(payload: dict) -> httpx.Response:
    body = {
        "candidates": [
            {"content": {"parts": [{"text": json.dumps(payload)}]}}
        ]
    }
    return httpx.Response(200, json=body)


@pytest.mark.asyncio
async def test_gemini_judge_parses_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert vc.VISION_MODEL in str(request.url)
        return _gemini_response(
            {"label": "stock_like", "confidence": 0.7, "signals": ["graded"]}
        )

    transport = httpx.MockTransport(handler)
    out = await vc._gemini_vision_judge(
        [_solid_frame()], "test-key", transport=transport
    )
    assert out is not None
    assert out["label"] == "stock_like"


@pytest.mark.asyncio
async def test_gemini_judge_429_returns_none():
    transport = httpx.MockTransport(lambda req: httpx.Response(429, text="quota"))
    out = await vc._gemini_vision_judge(
        [_solid_frame()], "test-key", transport=transport
    )
    assert out is None
