"""Unit tests for app.style_analysis.dressing.

No network and no real API key: the Gemini vision path is exercised
through an httpx MockTransport and through the injected ``vision_fn``
callable. Synthetic media is generated in-test with FFmpeg (tone /
gap / chord building blocks) and PIL (frames with caption/title bars).
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import httpx
import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.style_analysis import dressing
from app.style_analysis.dressing import (
    FrameVerdict,
    aggregate_caption_verdicts,
    aggregate_overlay_verdicts,
    analyse_dressing,
    estimate_music_presence,
    heuristic_frame_verdicts,
    music_signals,
)
from app.style_analysis.schema import DressingAnalysis


# ---------------------------------------------------------------------------
# Helpers: synthetic audio (FFmpeg-generated building blocks)
# ---------------------------------------------------------------------------
def _ffmpeg(args: list[str]) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True, capture_output=True, timeout=120,
    )


@pytest.fixture(scope="module")
def audio_dir(tmp_path_factory) -> Path:
    """Synthetic audio fixtures:

    * ``silence.wav``    — digital silence
    * ``voice_only.wav`` — sine "speech" bursts (0.9 s on / 0.9 s off),
      gaps are digital silence
    * ``chord.wav``      — continuous 3-tone chord (music-like)
    * ``mix.wav``        — voice bursts over a chord bed (0.04 / tone)
    * ``mix_quiet.wav``  — same, quieter bed (0.015 / tone)
    """
    d = tmp_path_factory.mktemp("dressing-audio")
    _ffmpeg(["-f", "lavfi", "-i",
             "aevalsrc=0.5*sin(2*PI*160*t):duration=0.9:sample_rate=16000",
             str(d / "tone.wav")])
    _ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
             "-t", "0.9", str(d / "gap.wav")])
    _ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
             "-t", "6", str(d / "silence.wav")])
    _ffmpeg(["-f", "lavfi", "-i",
             "aevalsrc=0.18*sin(2*PI*220*t)+0.18*sin(2*PI*261.63*t)"
             "+0.18*sin(2*PI*329.63*t):duration=9:sample_rate=16000",
             str(d / "chord.wav")])
    for name, amp in (("bed.wav", "0.04"), ("bed_quiet.wav", "0.015")):
        _ffmpeg(["-f", "lavfi", "-i",
                 f"aevalsrc={amp}*sin(2*PI*220*t)+{amp}*sin(2*PI*261.63*t)"
                 f"+{amp}*sin(2*PI*329.63*t):duration=9:sample_rate=16000",
                 str(d / name)])
    listing = "".join("file 'tone.wav'\nfile 'gap.wav'\n" for _ in range(5))
    (d / "list.txt").write_text(listing)
    _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(d / "list.txt"),
             "-c", "copy", str(d / "voice_only.wav")])
    for bed, out in (("bed.wav", "mix.wav"), ("bed_quiet.wav", "mix_quiet.wav")):
        _ffmpeg(["-i", str(d / "voice_only.wav"), "-i", str(d / bed),
                 "-filter_complex", "[0:a][1:a]amix=inputs=2:normalize=0[a]",
                 "-map", "[a]", str(d / out)])
    return d


# ---------------------------------------------------------------------------
# Helpers: synthetic frames / videos (PIL + FFmpeg)
# ---------------------------------------------------------------------------
def _jpeg(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr.astype(np.uint8)).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _gradient_frame() -> np.ndarray:
    h, w = 180, 320
    y = np.linspace(40, 110, h)[:, None]
    x = np.linspace(0, 25, w)[None, :]
    base = y + x
    return np.clip(np.stack([base * 0.6, base * 0.85, base], axis=-1), 0, 255)


def _with_caption_bars(arr: np.ndarray) -> np.ndarray:
    img = Image.fromarray(arr.astype(np.uint8))
    d = ImageDraw.Draw(img)
    for line, y0 in enumerate((140, 156)):
        x = 70
        while x < 250:
            wl = 14 + (x * 7 + line * 13) % 22
            d.rectangle([x, y0, x + wl, y0 + 9], fill=(245, 245, 245))
            x += wl + 6
    return np.asarray(img)


def _with_title_bars(arr: np.ndarray) -> np.ndarray:
    img = Image.fromarray(arr.astype(np.uint8))
    d = ImageDraw.Draw(img)
    x = 40
    while x < 260:
        wl = 18 + (x * 5) % 26
        d.rectangle([x, 18, x + wl, 34], fill=(250, 240, 120))
        x += wl + 8
    return np.asarray(img)


def _make_video(path: Path, *, caption: bool, frames: int = 16, fps: int = 2) -> str:
    frames_dir = path.parent / (path.stem + "_frames")
    frames_dir.mkdir(parents=True, exist_ok=True)
    base = _with_caption_bars(_gradient_frame()) if caption else _gradient_frame()
    for i in range(frames):
        Image.fromarray(base.astype(np.uint8)).save(frames_dir / f"f_{i:03d}.png")
    _ffmpeg([
        "-framerate", str(fps), "-i", str(frames_dir / "f_%03d.png"),
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(path),
    ])
    return str(path)


# ---------------------------------------------------------------------------
# Music estimator — synthetic audio direction (FFmpeg fixtures)
# ---------------------------------------------------------------------------
def test_silence_is_no_music(audio_dir):
    r = estimate_music_presence(audio_dir / "silence.wav")
    assert r["label"] == "no_music_detected"
    assert r["music_score"] == 0.0
    assert r["confidence"] >= 0.7


def test_voice_only_is_no_music(audio_dir):
    r = estimate_music_presence(audio_dir / "voice_only.wav")
    assert r["label"] == "no_music_detected"
    assert r["method"] == "pause_analysis"
    assert r["music_score"] < 0.38
    # Pauses fall to (digital) silence: fill is huge, tonal/steady 0.
    assert r["signals"]["pause_fill_db"] > 34.0


def test_voice_over_bed_is_music_likely(audio_dir):
    r = estimate_music_presence(audio_dir / "mix.wav")
    assert r["label"] == "music_likely"
    assert r["music_score"] > 0.62
    assert r["signals"]["pause_peak_ratio"] > 100.0


def test_quiet_bed_still_detected_but_scores_lower(audio_dir):
    loud = estimate_music_presence(audio_dir / "mix.wav")
    quiet = estimate_music_presence(audio_dir / "mix_quiet.wav")
    assert quiet["music_score"] < loud["music_score"]
    assert quiet["label"] in ("music_likely", "music_possible")
    assert quiet["signals"]["pause_fill_db"] > loud["signals"]["pause_fill_db"]


def test_continuous_chord_scores_high_via_global_path(audio_dir):
    chord = estimate_music_presence(audio_dir / "chord.wav")
    voice = estimate_music_presence(audio_dir / "voice_only.wav")
    assert chord["method"] == "global_tonality_only"
    assert chord["music_score"] > voice["music_score"] + 0.5
    # Global-only evidence is capped — never a confident claim.
    assert chord["confidence"] <= 0.60


def test_music_score_direction_ordering(audio_dir):
    silence = estimate_music_presence(audio_dir / "silence.wav")
    voice = estimate_music_presence(audio_dir / "voice_only.wav")
    quiet = estimate_music_presence(audio_dir / "mix_quiet.wav")
    loud = estimate_music_presence(audio_dir / "mix.wav")
    assert silence["music_score"] <= voice["music_score"]
    assert voice["music_score"] < quiet["music_score"] < loud["music_score"]


def test_undecodable_file_is_not_assessed(tmp_path):
    r = estimate_music_presence(tmp_path / "does_not_exist.wav")
    assert r["label"] == "no_music_detected"
    assert r["confidence"] == 0.0  # 0.0 == not assessed, honestly
    assert "error" in r["signals"]


def test_music_signals_on_pure_arrays():
    sr = 16000
    t = np.arange(sr * 9) / sr
    gate = ((t % 1.8) < 0.9).astype(float)
    voice = 0.5 * np.sin(2 * np.pi * 160 * t) * gate
    bed = sum(
        0.04 * np.sin(2 * np.pi * f * t) for f in (220.0, 261.63, 329.63)
    )
    voice_only = music_signals(voice, sr)
    mixed = music_signals(voice + bed, sr)
    assert voice_only["label"] == "no_music_detected"
    assert mixed["label"] == "music_likely"
    assert mixed["music_score"] > voice_only["music_score"]


# ---------------------------------------------------------------------------
# Caption / overlay aggregation — pure decision rules
# ---------------------------------------------------------------------------
def _verdicts(captions: list[str], overlays: list[str] | None = None):
    overlays = overlays or ["no"] * len(captions)
    return [FrameVerdict(i, c, o) for i, (c, o) in enumerate(zip(captions, overlays))]


def test_caption_all_yes_is_likely():
    out = aggregate_caption_verdicts(_verdicts(["yes"] * 16), provenance="vision")
    assert out["label"] == "likely"
    assert out["confidence"] >= 0.9
    assert out["yes_fraction"] == 1.0


def test_caption_all_no_is_unlikely():
    out = aggregate_caption_verdicts(_verdicts(["no"] * 16), provenance="vision")
    assert out["label"] == "unlikely"
    assert out["confidence"] >= 0.9


def test_caption_quarter_yes_is_possible_boundary():
    out = aggregate_caption_verdicts(
        _verdicts(["yes"] * 4 + ["no"] * 12), provenance="vision"
    )
    assert out["label"] == "possible"
    assert out["yes_fraction"] == 0.25


def test_caption_unsure_drags_confidence_down():
    sure = aggregate_caption_verdicts(_verdicts(["yes"] * 16), provenance="vision")
    unsure = aggregate_caption_verdicts(
        _verdicts(["yes"] * 8 + ["unsure"] * 8), provenance="vision"
    )
    assert unsure["label"] == "likely"  # yes_fraction still 0.5
    assert unsure["confidence"] < sure["confidence"]


def test_caption_heuristic_provenance_caps_confidence():
    out = aggregate_caption_verdicts(
        _verdicts(["yes"] * 16), provenance="heuristic_only"
    )
    assert out["label"] == "likely"
    assert out["confidence"] <= 0.45


def test_overlay_events_are_runs_divided_by_minutes():
    overlays = ["no", "no", "yes", "yes", "no", "no", "no", "no",
                "no", "no", "yes", "no", "no", "no", "no", "no"]
    out = aggregate_overlay_verdicts(
        _verdicts(["no"] * 16, overlays), duration_s=120.0, provenance="vision"
    )
    assert out["events_observed"] == 2
    assert out["overlays_per_minute"] == pytest.approx(1.0)
    assert out["overlay_frame_fraction"] == pytest.approx(3 / 16, abs=0.001)


def test_overlay_none_observed_is_zero_per_minute():
    out = aggregate_overlay_verdicts(
        _verdicts(["no"] * 16), duration_s=120.0, provenance="vision"
    )
    assert out["overlays_per_minute"] == 0.0
    assert out["events_observed"] == 0


def test_overlay_empty_verdicts_not_assessed():
    out = aggregate_overlay_verdicts([], duration_s=120.0, provenance="vision")
    assert out["overlays_per_minute"] is None
    assert out["confidence"] == 0.0


# ---------------------------------------------------------------------------
# Heuristic fallback — frame-level flags on synthetic frames
# ---------------------------------------------------------------------------
def test_heuristic_plain_frame_is_clean():
    v = heuristic_frame_verdicts([_jpeg(_gradient_frame())])
    assert v[0].caption == "no"
    assert v[0].overlay == "no"


def test_heuristic_caption_bars_flagged():
    v = heuristic_frame_verdicts([_jpeg(_with_caption_bars(_gradient_frame()))])
    assert v[0].caption == "yes"
    assert v[0].overlay == "no"


def test_heuristic_title_bars_flagged_as_overlay():
    v = heuristic_frame_verdicts([_jpeg(_with_title_bars(_gradient_frame()))])
    assert v[0].overlay == "yes"
    assert v[0].caption == "no"


def test_heuristic_noise_frame_not_flagged():
    rng = np.random.default_rng(3)
    noise = rng.integers(60, 190, size=(180, 320, 3))
    v = heuristic_frame_verdicts([_jpeg(noise)])
    assert v[0].caption == "no"
    assert v[0].overlay == "no"


# ---------------------------------------------------------------------------
# Verdict normalisation
# ---------------------------------------------------------------------------
def test_normalise_verdicts_accepts_mapping_and_list():
    raw_map = {"frames": [{"frame": 0, "caption": "YES", "overlay": "no"}]}
    out = dressing._normalise_verdicts(raw_map, expected=1)
    assert out == [FrameVerdict(0, "yes", "no")]
    out2 = dressing._normalise_verdicts(
        [{"caption": "maybe", "overlay": "yes"}], expected=1
    )
    assert out2 == [FrameVerdict(0, "unsure", "yes")]
    assert dressing._normalise_verdicts(None, expected=4) is None
    assert dressing._normalise_verdicts({"nope": 1}, expected=4) is None
    assert dressing._normalise_verdicts([], expected=4) is None


@pytest.mark.asyncio
async def test_gemini_judge_garbage_response_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "not json at all"}]}}]
        })

    transport = httpx.MockTransport(handler)
    out = await dressing._gemini_dressing_judge(
        [b"\xff\xd8fake"], "test-key", transport=transport
    )
    assert out is None


# ---------------------------------------------------------------------------
# Full analyse_dressing — mocked vision, fallback, injection
# ---------------------------------------------------------------------------
def _gemini_response(verdict_items: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={
        "candidates": [{
            "content": {"parts": [{"text": json.dumps(verdict_items)}]}
        }]
    })


def test_analyse_with_mocked_vision(tmp_path):
    video = _make_video(tmp_path / "cap.mp4", caption=True)
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key_header"] = request.headers.get("x-goog-api-key", "")
        items = [
            {
                "frame": i,
                "caption": "yes",
                "overlay": "yes" if i in (2, 3, 10) else "no",
            }
            for i in range(16)
        ]
        return _gemini_response(items)

    result = analyse_dressing(
        video, api_key="test-key", transport=httpx.MockTransport(handler)
    )
    assert isinstance(result, DressingAnalysis)
    assert "gemini-3.1-flash-lite" in seen["url"]
    assert seen["key_header"] == "test-key"
    # Captions on every frame -> present; silent audio track -> absent.
    assert result.caption_burn_in.label == "present"
    assert result.caption_burn_in.confidence >= 0.9
    assert "vision" in result.caption_burn_in.notes
    assert result.music_presence.label == "absent"
    # Overlay runs at frames 2-3 and 10 -> 2 events over ~8 s.
    duration = dressing.probe_duration_s(video)
    assert result.text_overlay_frequency == pytest.approx(
        round(2 / (duration / 60.0), 2), abs=0.6
    )
    assert result.text_overlay_confidence > 0.0
    # Motion is NOT this module's job.
    assert result.motion.label == "uncertain"


def test_analyse_vision_failure_falls_back_to_heuristic(tmp_path):
    video = _make_video(tmp_path / "cap2.mp4", caption=True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    result = analyse_dressing(
        video, api_key="test-key", transport=httpx.MockTransport(handler)
    )
    assert result.caption_burn_in.confidence <= 0.45
    assert "heuristic_only" in result.caption_burn_in.notes
    assert any("heuristic_only" in n for n in result.notes)
    # The synthetic video really does carry lower-third bars.
    assert result.caption_burn_in.label == "present"


def test_analyse_no_key_uses_heuristic_and_plain_video_is_absent(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    video = _make_video(tmp_path / "plain.mp4", caption=False)
    result = analyse_dressing(video)
    assert result.caption_burn_in.label == "absent"
    assert result.caption_burn_in.confidence <= 0.45
    assert result.text_overlay_frequency == 0.0


def test_analyse_vision_fn_injection_takes_precedence(tmp_path):
    video = _make_video(tmp_path / "plain2.mp4", caption=False)

    def fake_vision(frames: list[bytes]):
        assert len(frames) >= 12
        return {
            "frames": [
                {"frame": i, "caption": "yes", "overlay": "no"}
                for i in range(len(frames))
            ]
        }

    result = analyse_dressing(video, vision_fn=fake_vision)
    # Pixels say "no captions", the injected vision says yes -> vision wins.
    assert result.caption_burn_in.label == "present"
    assert "vision" in result.caption_burn_in.notes


def test_analyse_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        analyse_dressing(tmp_path / "nope.mp4")
