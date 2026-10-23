"""Round-5 quality-tier tests for the render engine (master encode).

Covers: tier resolution/default (``draft`` unless asked), the tier
spec table, ffmpeg argument construction per tier (draft unchanged,
1080p / 1440p scale params present), and REAL tiny encodes through
:func:`render_final` verifying output dimensions + the report fields
(quality tier, render wall time, output file size).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.pipeline.render import (  # noqa: E402
    EncoderChoice,
    SceneSpec,
    SegmentRenderer,
    SourceType,
    ffprobe,
    final_encode_command,
    probe_encoder,
    quality_tier_spec,
    render_final,
    render_master,
    resolve_quality_tier,
)

FFMPEG_MISSING = shutil.which("ffmpeg") is None

#: A hand-built choice so command-construction tests never probe.
LIBX264 = EncoderChoice(encoder="libx264", label="CPU libx264")


# ---------------------------------------------------------------------------
# Tier resolution / defaults (no ffmpeg needed)
# ---------------------------------------------------------------------------

def test_resolve_quality_tier_defaults_to_draft():
    assert resolve_quality_tier() == "draft"
    assert resolve_quality_tier(None) == "draft"
    assert resolve_quality_tier("") == "draft"
    assert resolve_quality_tier("draft") == "draft"


def test_resolve_quality_tier_normalises_and_accepts_all_tiers():
    assert resolve_quality_tier("master_1080p") == "master_1080p"
    assert resolve_quality_tier("master_1440p_upscale") == "master_1440p_upscale"
    assert resolve_quality_tier("  Master_1080P ") == "master_1080p"


def test_resolve_quality_tier_unknown_fails_loud():
    with pytest.raises(ValueError):
        resolve_quality_tier("master_4k")
    with pytest.raises(ValueError):
        quality_tier_spec("ultra")


def test_quality_tier_spec_table():
    draft = quality_tier_spec("draft")
    assert (draft.width, draft.height) == (1280, 720)
    assert draft.encoder_tier == "draft"
    assert draft.audio_bitrate == "128k"

    m1080 = quality_tier_spec("master_1080p")
    assert (m1080.width, m1080.height) == (1920, 1080)
    assert m1080.encoder_tier == "master"
    assert m1080.audio_bitrate == "320k"

    m1440 = quality_tier_spec("master_1440p_upscale")
    assert (m1440.width, m1440.height) == (2560, 1440)
    assert m1440.encoder_tier == "master"
    assert m1440.audio_bitrate == "320k"

    # Default spec is the draft spec.
    assert quality_tier_spec() == draft


# ---------------------------------------------------------------------------
# FFmpeg argument construction per tier (no encoding performed)
# ---------------------------------------------------------------------------

def _scale_arg(cmd: list[str]) -> str:
    vf_index = cmd.index("-vf")
    return cmd[vf_index + 1]


def test_final_encode_command_draft_unchanged():
    cmd = final_encode_command(
        Path("joined.ts"), Path("out.mp4"), quality_tier="draft",
        encoder_choice=LIBX264, audio=Path("master.wav"))
    # The historical draft construction, verbatim.
    assert _scale_arg(cmd) == (
        "scale=1280:720:flags=lanczos:in_range=auto:out_range=tv")
    assert cmd[cmd.index("-preset") + 1] == "superfast"
    assert cmd[cmd.index("-crf") + 1] == "24"
    assert cmd[cmd.index("-b:a") + 1] == "128k"
    assert cmd[-1] == "out.mp4"


def test_final_encode_command_master_1080p():
    cmd = final_encode_command(
        Path("joined.ts"), Path("out.mp4"), quality_tier="master_1080p",
        encoder_choice=LIBX264, audio=Path("master.wav"))
    assert "scale=1920:1080:" in _scale_arg(cmd)
    assert cmd[cmd.index("-crf") + 1] == "17"  # master encoder tier
    assert cmd[cmd.index("-b:a") + 1] == "320k"


def test_final_encode_command_master_1440p_upscale():
    cmd = final_encode_command(
        Path("joined.ts"), Path("out.mp4"),
        quality_tier="master_1440p_upscale",
        encoder_choice=LIBX264, audio=Path("master.wav"))
    assert "scale=2560:1440:" in _scale_arg(cmd)
    assert cmd[cmd.index("-crf") + 1] == "17"
    assert cmd[cmd.index("-b:a") + 1] == "320k"


def test_final_encode_command_default_is_draft_and_video_only():
    cmd = final_encode_command(
        Path("joined.ts"), Path("out.mp4"), encoder_choice=LIBX264)
    assert "scale=1280:720:" in _scale_arg(cmd)
    assert "-an" in cmd and "-c:a" not in cmd


# ---------------------------------------------------------------------------
# Real tiny encodes through render_final (ffmpeg required)
# ---------------------------------------------------------------------------

pytestmark_real = pytest.mark.skipif(
    FFMPEG_MISSING, reason="ffmpeg not installed")


@pytest.fixture(scope="module")
def encoder() -> EncoderChoice:
    return probe_encoder()


@pytest.fixture(scope="module")
def segment(tmp_path_factory, encoder) -> Path:
    seg_dir = tmp_path_factory.mktemp("tier_segments")
    renderer = SegmentRenderer(seg_dir, encoder_choice=encoder)
    return renderer.render_segment(SceneSpec(
        scene_id="tier1", source_type=SourceType.SOLID,
        duration_seconds=1.0, color="0x203040"))


@pytest.fixture(scope="module")
def audio_file(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("tier_audio")
    audio = d / "voice.wav"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:d=1", "-ar", "48000", str(audio)],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-500:]
    return audio


def _video_stream(path: Path) -> dict:
    return next(s for s in ffprobe(path)["streams"]
                if s["codec_type"] == "video")


@pytestmark_real
def test_render_final_default_is_draft_720p(segment, encoder, tmp_path):
    result = render_final([segment], tmp_path / "final.mp4",
                          encoder_choice=encoder)
    stream = _video_stream(result.path)
    assert (stream["width"], stream["height"]) == (1280, 720)
    assert result.quality_tier == "draft"
    assert result.render_wall_time_s > 0
    assert result.output_file_size_bytes == result.path.stat().st_size > 0


@pytestmark_real
def test_render_final_master_1080p(segment, encoder, tmp_path):
    result = render_final(
        [segment], tmp_path / "final.mp4", quality_tier="master_1080p",
        encoder_choice=encoder)
    stream = _video_stream(result.path)
    assert (stream["width"], stream["height"]) == (1920, 1080)
    assert result.quality_tier == "master_1080p"
    assert (result.width, result.height) == (1920, 1080)
    assert result.render_wall_time_s > 0
    assert result.output_file_size_bytes == result.path.stat().st_size > 0
    as_dict = result.to_dict()
    assert as_dict["quality_tier"] == "master_1080p"
    assert as_dict["output_file_size_bytes"] == result.output_file_size_bytes


@pytestmark_real
def test_render_final_master_1440p_upscale(segment, encoder, tmp_path):
    result = render_final(
        [segment], tmp_path / "final.mp4",
        quality_tier="master_1440p_upscale", encoder_choice=encoder)
    stream = _video_stream(result.path)
    assert (stream["width"], stream["height"]) == (2560, 1440)
    assert result.quality_tier == "master_1440p_upscale"
    assert result.render_wall_time_s > 0
    assert result.output_file_size_bytes == result.path.stat().st_size > 0


@pytestmark_real
def test_render_final_master_with_audio(segment, encoder, audio_file,
                                        tmp_path):
    result = render_final(
        [segment], tmp_path / "final.mp4", quality_tier="master_1080p",
        audio=audio_file, encoder_choice=encoder)
    kinds = {s["codec_type"] for s in ffprobe(result.path)["streams"]}
    assert kinds == {"video", "audio"}
    assert _video_stream(result.path)["width"] == 1920


@pytestmark_real
def test_render_master_delegates_to_tiers(segment, encoder, tmp_path):
    plain = render_master([segment], tmp_path / "m1080.mp4", upscale=False,
                          encoder_choice=encoder)
    assert (_video_stream(plain)["width"],
            _video_stream(plain)["height"]) == (1920, 1080)
    up = render_master([segment], tmp_path / "m1440.mp4", upscale=True,
                       encoder_choice=encoder)
    assert (_video_stream(up)["width"],
            _video_stream(up)["height"]) == (2560, 1440)
