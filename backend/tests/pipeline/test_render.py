"""Render-engine tests — REAL ffmpeg work, tiny media (<=2 s per segment).

Covers: the locked segment contract + hash cache key, the real-encode
encoder probe, video/still(Ken Burns)/solid segment rendering, Pillow
text overlays, stream-copy concat + verification gates, cache-hit resume,
progress callbacks, mux helper, draft tier and QC by-products.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Repo layout: backend/ holds the `app` package (namespace packages above
# `render`, which is a regular package).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.pipeline.render import (  # noqa: E402
    EncoderChoice,
    SceneSpec,
    SegmentRenderer,
    SourceType,
    TextOverlay,
    check_narration_frame_gate,
    check_stream_contract,
    compute_segment_hash,
    concat_segments,
    ffprobe,
    find_default_font,
    make_contact_sheet,
    mux_final,
    probe_duration,
    probe_encoder,
    render_draft,
    render_text_png,
    reset_probe_cache,
    segment_filename,
    verify_concat,
    write_probe_report,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg not installed",
)


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-500:]


# ---------------------------------------------------------------------------
# Fixtures: tiny generated source assets + one shared probed encoder
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def assets(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("assets")
    clip = d / "clip.mp4"
    _run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
          "-i", "testsrc2=s=640x360:r=30:d=3",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)])
    still = d / "still.png"
    _run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
          "-i", "testsrc2=s=1600x1200:d=1:r=1",
          "-frames:v", "1", str(still)])
    audio = d / "voice.wav"
    _run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
          "-i", "sine=frequency=440:d=3",
          "-ar", "48000", str(audio)])
    return {"clip": clip, "still": still, "audio": audio}


@pytest.fixture(scope="module")
def encoder(tmp_path_factory) -> EncoderChoice:
    reset_probe_cache()
    cache = tmp_path_factory.mktemp("probe") / "encoder.json"
    return probe_encoder(cache_file=cache)


@pytest.fixture(scope="module")
def seg_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("segments")


def _renderer(seg_dir: Path, encoder: EncoderChoice, **kw) -> SegmentRenderer:
    return SegmentRenderer(seg_dir, encoder_choice=encoder, **kw)


# ---------------------------------------------------------------------------
# Contract + hashing (no ffmpeg needed, but kept with the suite)
# ---------------------------------------------------------------------------

def test_segment_filename_is_spec_hash(assets):
    spec = SceneSpec(scene_id="s001", source_type=SourceType.VIDEO,
                     duration_seconds=2.0, source_path=str(assets["clip"]),
                     asset_hash="abc123")
    name = segment_filename(spec)
    digest = compute_segment_hash(spec)
    assert name == f"seg_s001_v{digest}.ts"
    assert len(digest) == 64  # full sha256 hex
    # Same spec -> same name (stable cache key); changed spec -> new name.
    assert segment_filename(spec) == name
    changed = SceneSpec(scene_id="s001", source_type=SourceType.VIDEO,
                        duration_seconds=1.0, source_path=str(assets["clip"]),
                        asset_hash="abc123")
    assert segment_filename(changed) != name
    other_asset = SceneSpec(scene_id="s001", source_type=SourceType.VIDEO,
                            duration_seconds=2.0,
                            source_path=str(assets["clip"]),
                            asset_hash="different")
    assert segment_filename(other_asset) != name


def test_narration_frame_gate():
    check_narration_frame_gate([60, 30], 3.0)          # exact
    check_narration_frame_gate([60, 31], 3.0)          # +1 frame: allowed
    with pytest.raises(ValueError):
        check_narration_frame_gate([60, 60], 3.0)      # +30 frames: FAIL


# ---------------------------------------------------------------------------
# Encoder probe — must genuinely encode, not just list
# ---------------------------------------------------------------------------

def test_encoder_probe_selects_libx264_and_really_encodes(encoder, tmp_path):
    # This VM has no GPU: nvenc/qsv are listed by `ffmpeg -encoders` but
    # fail real encodes; amf is not usable either.  libx264 must win.
    assert encoder.encoder == "libx264"
    assert encoder.probe_ran_encode is True
    by_name = {a.encoder: a for a in encoder.attempts}
    assert by_name["h264_nvenc"].listed is True
    assert by_name["h264_nvenc"].encode_ok is False  # listed != usable
    assert by_name["h264_qsv"].encode_ok is False
    assert by_name["h264_amf"].encode_ok is False
    assert by_name["libx264"].encode_ok is True
    # Priority order was respected in the attempts log.
    order = [a.encoder for a in encoder.attempts]
    assert order.index("h264_nvenc") < order.index("libx264")


def test_encoder_probe_cache_file(encoder, tmp_path):
    reset_probe_cache()
    cache = tmp_path / "enc.json"
    first = probe_encoder(cache_file=cache)
    assert cache.exists()
    saved = json.loads(cache.read_text())
    assert saved["encoder"] == first.encoder
    reset_probe_cache()
    second = probe_encoder(cache_file=cache)  # served from cache file
    assert second.encoder == first.encoder
    reset_probe_cache()


# ---------------------------------------------------------------------------
# Segment rendering + concat + gates
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def rendered(seg_dir, assets, encoder) -> dict[str, Path]:
    renderer = _renderer(seg_dir, encoder)
    video = renderer.render_segment(SceneSpec(
        scene_id="v001", source_type=SourceType.VIDEO, duration_seconds=2.0,
        source_path=str(assets["clip"]), asset_hash="cliphash1"))
    solid = renderer.render_segment(SceneSpec(
        scene_id="v002", source_type=SourceType.SOLID, duration_seconds=1.0,
        color="0x203040"))
    return {"video": video, "solid": solid}


def test_segments_match_contract(rendered):
    for path in rendered.values():
        assert path.suffix == ".ts" and path.exists()
        report = check_stream_contract(path)  # raises on any violation
        stream = report["streams"][0]
        assert stream["codec_name"] == "h264"
        assert stream["width"] == 1920 and stream["height"] == 1080
        assert stream["pix_fmt"] == "yuv420p"
        # No audio in segments (D2).
        assert all(s["codec_type"] == "video" for s in report["streams"])
    assert abs(probe_duration(rendered["video"]) - 2.0) < 0.2
    assert abs(probe_duration(rendered["solid"]) - 1.0) < 0.2


def test_concat_stream_copy_and_gates(rendered, tmp_path):
    out = concat_segments([rendered["video"], rendered["solid"]],
                          tmp_path / "joined.ts")
    report = verify_concat([rendered["video"], rendered["solid"]], out)
    assert report.passed, [(g.name, g.detail) for g in report.gates]
    seg_sum = probe_duration(rendered["video"]) + probe_duration(rendered["solid"])
    assert abs(seg_sum - probe_duration(out)) <= 0.5
    assert abs(probe_duration(out) - 3.0) <= 0.5


def test_render_skips_existing_segment_cache_hit(seg_dir, assets, encoder):
    spec = SceneSpec(scene_id="cache1", source_type=SourceType.SOLID,
                     duration_seconds=1.0, color="0x112233")
    renderer = _renderer(seg_dir, encoder)
    first = renderer.render_segment_detailed(spec)
    assert first.cache_hit is False
    digest_before = hashlib.sha256(first.path.read_bytes()).hexdigest()
    mtime_before = first.path.stat().st_mtime_ns

    second = renderer.render_segment_detailed(spec)
    assert second.cache_hit is True
    assert second.path == first.path
    assert hashlib.sha256(second.path.read_bytes()).hexdigest() == digest_before
    assert second.path.stat().st_mtime_ns == mtime_before  # untouched


def test_progress_callback_fires(seg_dir, assets, encoder):
    fractions: list[float] = []
    renderer = _renderer(
        seg_dir, encoder,
        progress=lambda frac, _raw: fractions.append(frac))
    renderer.render_segment(SceneSpec(
        scene_id="prog1", source_type=SourceType.SOLID,
        duration_seconds=1.0, color="0x334455"))
    assert fractions, "progress callback was never called"
    assert fractions[-1] == 1.0
    assert all(0.0 <= f <= 1.0 for f in fractions)


# ---------------------------------------------------------------------------
# Ken Burns still + Pillow text overlay
# ---------------------------------------------------------------------------

def test_ken_burns_still_segment(seg_dir, assets, encoder):
    renderer = _renderer(seg_dir, encoder)
    path = renderer.render_segment(SceneSpec(
        scene_id="kb1", source_type=SourceType.STILL, duration_seconds=2.0,
        source_path=str(assets["still"]), asset_hash="stillhash",
        ken_burns=True, zoom_start=1.0, zoom_end=1.12))
    check_stream_contract(path)
    assert abs(probe_duration(path) - 2.0) < 0.2
    # Motion actually happened: first and last frames must differ.
    frames = seg_dir / "kb_frames"
    frames.mkdir(exist_ok=True)
    for label, seek in (("first", "0"), ("last", "1.9")):
        # Output-side seek: .ts timestamps start at ~1.4 s, so an
        # input-side -ss would overshoot the content.
        _run(["ffmpeg", "-y", "-v", "error", "-i", str(path),
              "-ss", seek, "-frames:v", "1", str(frames / f"{label}.png")])
    assert (frames / "first.png").read_bytes() != (frames / "last.png").read_bytes()


def test_pillow_text_overlay_png_and_segment(seg_dir, assets, encoder, tmp_path):
    png = render_text_png("Hello 42", tmp_path / "overlay.png",
                          font_path=find_default_font(), font_size=72)
    assert png.exists()
    from PIL import Image
    with Image.open(png) as img:
        assert img.mode == "RGBA"
        assert img.width > 50 and img.height > 20

    renderer = _renderer(seg_dir, encoder)
    path = renderer.render_segment(SceneSpec(
        scene_id="txt1", source_type=SourceType.SOLID, duration_seconds=1.0,
        color="0x000000",
        text_overlays=[TextOverlay(png_path=str(png), x="(W-w)/2", y="(H-h)/2")]))
    check_stream_contract(path)


def test_ken_burns_zoom_cap_enforced(assets):
    with pytest.raises(ValueError):
        SceneSpec(scene_id="bad", source_type=SourceType.STILL,
                  duration_seconds=1.0, source_path=str(assets["still"]),
                  zoom_end=1.30)  # > 1.15x cap


# ---------------------------------------------------------------------------
# Mux helper, draft tier, QC by-products
# ---------------------------------------------------------------------------

def test_mux_final_and_qc_byproducts(rendered, assets, tmp_path):
    joined = concat_segments([rendered["video"], rendered["solid"]],
                             tmp_path / "joined.ts")
    final = mux_final(joined, assets["audio"], tmp_path / "final.mp4")
    report = ffprobe(final)
    kinds = {s["codec_type"] for s in report["streams"]}
    assert kinds == {"video", "audio"}
    video = next(s for s in report["streams"] if s["codec_type"] == "video")
    assert video["codec_name"] == "h264"  # stream-copied, not re-encoded
    assert abs(probe_duration(final) - 3.0) <= 0.5

    probe_json = write_probe_report(final, tmp_path / "probe.json")
    assert json.loads(probe_json.read_text())["streams"]

    sheet = make_contact_sheet(final, tmp_path / "sheet.png",
                               interval_seconds=1.0, columns=3)
    assert sheet.exists() and sheet.stat().st_size > 0


def test_draft_tier_is_720p(rendered, encoder, tmp_path):
    draft = render_draft([rendered["video"], rendered["solid"]],
                         tmp_path / "draft.mp4", encoder_choice=encoder)
    stream = ffprobe(draft)["streams"][0]
    assert (stream["width"], stream["height"]) == (1280, 720)
    assert stream["codec_name"] == "h264"


# ---------------------------------------------------------------------------
# Colour-range regression: full-range sources must come out yuv420p
#
# Bug (Round 4 automation proof blocker): the filter graph ended with
# ``format=yuv420p``, which converts the pixel format but NOT the colour
# range — any full-range source (a yuvj420p stock clip, or a JPEG still;
# JPEG is inherently full-range) rendered a segment still tagged
# yuvj420p, and check_stream_contract correctly rejected it.  The graph
# now range-normalises with ``scale=in_range=auto:out_range=tv`` before
# format=.  These tests pin BOTH halves of the fix: the tag (contract)
# and the levels (a relabel-only "fix" such as setrange=tv leaves
# full-range white at Y=255 instead of remapping it to limited Y~235;
# a double conversion of a limited source would drag its white from
# 235 down to ~218).
# ---------------------------------------------------------------------------

def _video_stream(path: Path) -> dict:
    return next(s for s in ffprobe(path)["streams"]
                if s["codec_type"] == "video")


def _mean_luma(path: Path) -> float:
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-vf", "extractplanes=y,format=gray", "-f", "rawvideo", "-"],
        capture_output=True, timeout=120)
    assert proc.returncode == 0 and proc.stdout
    return sum(proc.stdout) / len(proc.stdout)


@pytest.fixture(scope="module")
def range_assets(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("range_assets")

    def raw_clip(name: str, y: int, pix_fmt: str, extra: list[str]) -> Path:
        w, h, frames = 64, 64, 60
        raw = d / f"{name}.yuv"
        frame = bytes([y]) * (w * h) + bytes([128]) * (w * h // 2)
        raw.write_bytes(frame * frames)
        out = d / f"{name}.mp4"
        _run(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo",
              "-pix_fmt", pix_fmt, "-s", f"{w}x{h}", "-r", "30",
              "-i", str(raw), "-c:v", "libx264", "-pix_fmt", pix_fmt,
              *extra, str(out)])
        return out

    # Full-range white: Y=255 in yuvj420p (levels only valid as pc).
    full_white = raw_clip("white_full", 255, "yuvj420p",
                          ["-color_range", "pc"])
    # Limited-range white: Y=235 in yuv420p (already tv levels).
    limited_white = raw_clip("white_lim", 235, "yuv420p",
                             ["-color_range", "tv"])
    # A realistic patterned full-range clip (the stock-clip case).
    full_clip = d / "full_clip.mp4"
    _run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
          "-i", "testsrc2=s=640x360:r=30:d=3",
          "-vf", "setrange=pc,format=yuvj420p",
          "-c:v", "libx264", "-pix_fmt", "yuvj420p", str(full_clip)])
    # A JPEG still (the Pixabay-still case): inherently yuvj420p.
    jpeg = d / "still.jpg"
    _run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
          "-i", "testsrc2=s=1280x853:d=1:r=1",
          "-frames:v", "1", str(jpeg)])
    return {"full_white": full_white, "limited_white": limited_white,
            "full_clip": full_clip, "jpeg": jpeg}


def test_full_range_video_segment_is_yuv420p_with_converted_levels(
        seg_dir, range_assets, encoder):
    # Fixture sanity: the sources really are full-range, or the test
    # would pass vacuously on limited inputs.
    assert _video_stream(range_assets["full_clip"])["pix_fmt"] == "yuvj420p"
    assert _video_stream(range_assets["full_white"])["pix_fmt"] == "yuvj420p"

    renderer = _renderer(seg_dir, encoder)
    path = renderer.render_segment(SceneSpec(
        scene_id="fr1", source_type=SourceType.VIDEO, duration_seconds=1.0,
        source_path=str(range_assets["full_clip"]), asset_hash="fullclip1"))
    check_stream_contract(path)  # raises on any violation (the old bug)
    assert _video_stream(path)["pix_fmt"] == "yuv420p"

    white = renderer.render_segment(SceneSpec(
        scene_id="frwhite", source_type=SourceType.VIDEO,
        duration_seconds=1.0,
        source_path=str(range_assets["full_white"]), asset_hash="fullwhite1"))
    check_stream_contract(white)
    # Genuine level remap: full-range white 255 -> limited ~235.
    # A tag-only fix would leave ~255 here.
    assert 228 <= _mean_luma(white) <= 242


def test_jpeg_still_segment_is_yuv420p(seg_dir, range_assets, encoder):
    assert _video_stream(range_assets["jpeg"])["pix_fmt"] == "yuvj420p"
    renderer = _renderer(seg_dir, encoder)
    for scene_id, ken_burns in (("jpgkb", True), ("jpgflat", False)):
        path = renderer.render_segment(SceneSpec(
            scene_id=scene_id, source_type=SourceType.STILL,
            duration_seconds=1.0,
            source_path=str(range_assets["jpeg"]), asset_hash="jpeg1",
            ken_burns=ken_burns))
        check_stream_contract(path)
        assert _video_stream(path)["pix_fmt"] == "yuv420p"


def test_limited_range_segment_levels_unchanged(
        seg_dir, range_assets, assets, encoder):
    assert _video_stream(
        range_assets["limited_white"])["pix_fmt"] == "yuv420p"
    renderer = _renderer(seg_dir, encoder)
    white = renderer.render_segment(SceneSpec(
        scene_id="limwhite", source_type=SourceType.VIDEO,
        duration_seconds=1.0,
        source_path=str(range_assets["limited_white"]),
        asset_hash="limwhite1"))
    check_stream_contract(white)
    assert _video_stream(white)["pix_fmt"] == "yuv420p"
    # No double conversion: limited white stays ~235 (a second pc->tv
    # remap would drag it to ~218).
    assert 230 <= _mean_luma(white) <= 242

    clip = renderer.render_segment(SceneSpec(
        scene_id="limclip", source_type=SourceType.VIDEO,
        duration_seconds=1.0, source_path=str(assets["clip"]),
        asset_hash="limclip1"))
    check_stream_contract(clip)
    assert _video_stream(clip)["pix_fmt"] == "yuv420p"
