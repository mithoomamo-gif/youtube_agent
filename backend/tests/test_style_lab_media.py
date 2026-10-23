"""Tests for Style Lab media intake (``app.style_analysis.media``).

Real-media tests: tiny genuine files are generated with ffmpeg lavfi
(a 1s ``testsrc`` clip) in a pytest basetemp — no fixtures are faked,
ffprobe/Pillow validate real bytes. Skipped wholesale when ffmpeg /
ffprobe are not on PATH (same pattern as the pipeline test suites).
"""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
from pathlib import Path

import pytest

from app.style_analysis.media import (
    MAX_THUMBNAIL_BYTES,
    MAX_VIDEO_BYTES,
    REASON_EMPTY_FILE,
    REASON_FFPROBE_FAILED,
    REASON_INVALID_PATH_SEGMENT,
    REASON_MISSING_FILE,
    REASON_NOT_AN_IMAGE,
    REASON_NO_VIDEO_STREAM,
    REASON_TOO_LARGE,
    REASON_ZERO_DURATION,
    MediaValidationError,
    probe_media,
    store_thumbnail,
    store_video_file,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not available",
)

OWNER = "chan-owner-1"
COMPETITOR = "comp-1"


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", *args],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.fixture(scope="module")
def media_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("style-lab-media-src")


@pytest.fixture(scope="module")
def sample_mp4(media_dir: Path) -> Path:
    out = media_dir / "sample.mp4"
    _ffmpeg(["-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)])
    return out


@pytest.fixture(scope="module")
def sample_mkv(media_dir: Path) -> Path:
    out = media_dir / "sample.mkv"
    _ffmpeg(["-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)])
    return out


@pytest.fixture(scope="module")
def sample_png(media_dir: Path) -> Path:
    out = media_dir / "frame.png"
    _ffmpeg(["-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
             "-frames:v", "1", str(out)])
    return out


@pytest.fixture(scope="module")
def audio_only(media_dir: Path) -> Path:
    out = media_dir / "audio.m4a"
    _ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=1",
             "-c:a", "aac", str(out)])
    return out


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Caps are the documented ones
# ---------------------------------------------------------------------------


def test_documented_caps():
    assert MAX_VIDEO_BYTES == 2 * 1024 * 1024 * 1024
    assert MAX_THUMBNAIL_BYTES == 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# probe_media
# ---------------------------------------------------------------------------


def test_probe_media_fields(sample_mp4: Path):
    info = probe_media(sample_mp4)
    assert info.duration_s == pytest.approx(1.0, abs=0.6)
    assert info.width == 320
    assert info.height == 240
    assert info.mime_type == "video/mp4"
    assert info.size_bytes == sample_mp4.stat().st_size
    assert info.video_codec == "h264"
    assert "mp4" in info.format_name


def test_probe_media_missing_file(tmp_path: Path):
    with pytest.raises(MediaValidationError) as excinfo:
        probe_media(tmp_path / "nope.mp4")
    assert excinfo.value.reason == REASON_MISSING_FILE
    assert excinfo.value.code == REASON_MISSING_FILE


def test_probe_media_empty_file(tmp_path: Path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(MediaValidationError) as excinfo:
        probe_media(empty)
    assert excinfo.value.reason == REASON_EMPTY_FILE


def test_probe_media_text_file_is_ffprobe_failure(tmp_path: Path):
    fake = tmp_path / "fake.mp4"
    fake.write_text("this is not a video at all", encoding="utf-8")
    with pytest.raises(MediaValidationError) as excinfo:
        probe_media(fake)
    assert excinfo.value.reason == REASON_FFPROBE_FAILED


def test_probe_media_no_video_stream(audio_only: Path):
    with pytest.raises(MediaValidationError) as excinfo:
        probe_media(audio_only)
    assert excinfo.value.reason == REASON_NO_VIDEO_STREAM


def test_probe_media_image_has_zero_duration(sample_png: Path):
    with pytest.raises(MediaValidationError) as excinfo:
        probe_media(sample_png)
    assert excinfo.value.reason == REASON_ZERO_DURATION


# ---------------------------------------------------------------------------
# store_video_file — happy paths
# ---------------------------------------------------------------------------


def test_store_video_from_path(sample_mp4: Path, data_dir: Path):
    stored = store_video_file(OWNER, COMPETITOR, sample_mp4, "clip.mp4", data_dir)
    expected_sha = _sha256(sample_mp4)
    assert stored.sha256 == expected_sha
    assert stored.size_bytes == sample_mp4.stat().st_size
    # storage_path is RELATIVE to data_dir and inside the style_lab root
    assert not Path(stored.storage_path).is_absolute()
    assert stored.storage_path == f"style_lab/{OWNER}/{COMPETITOR}/{expected_sha}.mp4"
    on_disk = data_dir / stored.storage_path
    assert on_disk.is_file()
    assert _sha256(on_disk) == expected_sha
    assert on_disk.resolve().is_relative_to((data_dir / "style_lab").resolve())
    # probed info rides along
    assert stored.media_info.width == 320
    assert stored.media_info.height == 240
    assert stored.media_info.duration_s == pytest.approx(1.0, abs=0.6)
    assert stored.info is stored.media_info
    assert stored.resolve(data_dir) == on_disk


def test_store_video_dedupes_same_content(sample_mp4: Path, data_dir: Path):
    first = store_video_file(OWNER, COMPETITOR, sample_mp4, "a.mp4", data_dir)
    # Same bytes again, this time as an in-memory stream + another name.
    second = store_video_file(
        OWNER, COMPETITOR, io.BytesIO(sample_mp4.read_bytes()), "b.mp4", data_dir)
    assert second.storage_path == first.storage_path
    assert second.sha256 == first.sha256
    competitor_dir = data_dir / "style_lab" / OWNER / COMPETITOR
    files = [p for p in competitor_dir.iterdir() if p.is_file()]
    assert files == [data_dir / first.storage_path]  # exactly one copy


def test_store_video_from_chunk_iterable(sample_mp4: Path, data_dir: Path):
    payload = sample_mp4.read_bytes()
    chunks = [payload[i:i + 1024] for i in range(0, len(payload), 1024)]
    stored = store_video_file(OWNER, COMPETITOR, iter(chunks), "clip.mp4", data_dir)
    assert stored.sha256 == _sha256(sample_mp4)


def test_store_video_extension_comes_from_probe_not_filename(
        sample_mp4: Path, sample_mkv: Path, data_dir: Path):
    # mp4 content uploaded under a nonsense name -> canonical .mp4
    stored = store_video_file(OWNER, COMPETITOR, sample_mp4, "movie.txt", data_dir)
    assert stored.storage_path.endswith(".mp4")
    assert stored.media_info.mime_type == "video/mp4"
    # mkv content named .mkv keeps .mkv within the matroska family
    stored_mkv = store_video_file(OWNER, COMPETITOR, sample_mkv, "movie.mkv", data_dir)
    assert stored_mkv.storage_path.endswith(".mkv")
    assert stored_mkv.media_info.mime_type == "video/x-matroska"


def test_store_video_same_hash_other_competitor_is_separate(
        sample_mp4: Path, data_dir: Path):
    one = store_video_file(OWNER, "comp-a", sample_mp4, "c.mp4", data_dir)
    two = store_video_file(OWNER, "comp-b", sample_mp4, "c.mp4", data_dir)
    assert one.storage_path != two.storage_path
    assert (data_dir / one.storage_path).is_file()
    assert (data_dir / two.storage_path).is_file()


# ---------------------------------------------------------------------------
# store_video_file — rejection matrix
# ---------------------------------------------------------------------------


def test_store_video_rejects_text_renamed_mp4(tmp_path: Path, data_dir: Path):
    fake = tmp_path / "fake.mp4"
    fake.write_text("definitely not video bytes", encoding="utf-8")
    with pytest.raises(MediaValidationError) as excinfo:
        store_video_file(OWNER, COMPETITOR, fake, "fake.mp4", data_dir)
    assert excinfo.value.reason == REASON_FFPROBE_FAILED
    _assert_no_leftovers(data_dir)


def test_store_video_rejects_empty_bytes(data_dir: Path):
    with pytest.raises(MediaValidationError) as excinfo:
        store_video_file(OWNER, COMPETITOR, b"", "x.mp4", data_dir)
    assert excinfo.value.reason == REASON_EMPTY_FILE
    _assert_no_leftovers(data_dir)


def test_store_video_rejects_missing_path(tmp_path: Path, data_dir: Path):
    with pytest.raises(MediaValidationError) as excinfo:
        store_video_file(OWNER, COMPETITOR, tmp_path / "gone.mp4", "gone.mp4", data_dir)
    assert excinfo.value.reason == REASON_MISSING_FILE


def test_store_video_rejects_image_as_video(sample_png: Path, data_dir: Path):
    with pytest.raises(MediaValidationError) as excinfo:
        store_video_file(OWNER, COMPETITOR, sample_png, "still.mp4", data_dir)
    assert excinfo.value.reason == REASON_ZERO_DURATION
    _assert_no_leftovers(data_dir)


def test_store_video_rejects_audio_only(audio_only: Path, data_dir: Path):
    with pytest.raises(MediaValidationError) as excinfo:
        store_video_file(OWNER, COMPETITOR, audio_only, "song.mp4", data_dir)
    assert excinfo.value.reason == REASON_NO_VIDEO_STREAM
    _assert_no_leftovers(data_dir)


def test_store_video_oversize_stream_rejected_with_small_cap(
        sample_mp4: Path, data_dir: Path):
    payload = sample_mp4.read_bytes()
    chunks = [payload[i:i + 512] for i in range(0, len(payload), 512)]
    with pytest.raises(MediaValidationError) as excinfo:
        store_video_file(OWNER, COMPETITOR, iter(chunks), "big.mp4", data_dir,
                         max_bytes=len(payload) - 1)
    assert excinfo.value.reason == REASON_TOO_LARGE
    _assert_no_leftovers(data_dir)  # no partial copy survives


def test_store_video_exactly_at_cap_is_accepted(sample_mp4: Path, data_dir: Path):
    size = sample_mp4.stat().st_size
    stored = store_video_file(OWNER, COMPETITOR, sample_mp4, "c.mp4", data_dir,
                              max_bytes=size)
    assert stored.size_bytes == size


def _assert_no_leftovers(data_dir: Path) -> None:
    root = data_dir / "style_lab"
    if not root.exists():
        return
    leftovers = [p for p in root.rglob("*") if p.is_file()]
    assert leftovers == []


# ---------------------------------------------------------------------------
# store_thumbnail
# ---------------------------------------------------------------------------


def test_store_thumbnail_accepts_png(sample_png: Path, data_dir: Path):
    stored = store_thumbnail(OWNER, COMPETITOR, sample_png, data_dir)
    expected_sha = _sha256(sample_png)
    assert stored.sha256 == expected_sha
    assert stored.storage_path == (
        f"style_lab/{OWNER}/{COMPETITOR}/{expected_sha}-thumb.png")
    assert stored.media_info.mime_type == "image/png"
    assert stored.media_info.width == 320
    assert stored.media_info.height == 240
    assert stored.media_info.duration_s == 0.0
    assert (data_dir / stored.storage_path).is_file()


def test_store_thumbnail_accepts_jpeg_bytes_and_dedupes(
        sample_png: Path, tmp_path: Path, data_dir: Path):
    from PIL import Image

    jpg_path = tmp_path / "thumb.jpg"
    Image.open(sample_png).convert("RGB").save(jpg_path, "JPEG")
    payload = jpg_path.read_bytes()
    first = store_thumbnail(OWNER, COMPETITOR, payload, data_dir)
    second = store_thumbnail(OWNER, COMPETITOR, io.BytesIO(payload), data_dir)
    assert first.storage_path.endswith("-thumb.jpg")
    assert first.media_info.mime_type == "image/jpeg"
    assert second.storage_path == first.storage_path
    competitor_dir = data_dir / "style_lab" / OWNER / COMPETITOR
    assert len([p for p in competitor_dir.iterdir() if p.is_file()]) == 1


def test_store_thumbnail_rejects_non_image(data_dir: Path):
    with pytest.raises(MediaValidationError) as excinfo:
        store_thumbnail(OWNER, COMPETITOR, b"plain text, not an image", data_dir)
    assert excinfo.value.reason == REASON_NOT_AN_IMAGE
    _assert_no_leftovers(data_dir)


def test_store_thumbnail_rejects_video_bytes(sample_mp4: Path, data_dir: Path):
    with pytest.raises(MediaValidationError) as excinfo:
        store_thumbnail(OWNER, COMPETITOR, sample_mp4.read_bytes(), data_dir)
    assert excinfo.value.reason == REASON_NOT_AN_IMAGE


def test_store_thumbnail_rejects_unlisted_image_format(
        sample_png: Path, tmp_path: Path, data_dir: Path):
    from PIL import Image

    bmp_path = tmp_path / "thumb.bmp"
    Image.open(sample_png).convert("RGB").save(bmp_path, "BMP")
    with pytest.raises(MediaValidationError) as excinfo:
        store_thumbnail(OWNER, COMPETITOR, bmp_path, data_dir)
    assert excinfo.value.reason == REASON_NOT_AN_IMAGE


def test_store_thumbnail_oversize_rejected_with_small_cap(
        sample_png: Path, data_dir: Path):
    with pytest.raises(MediaValidationError) as excinfo:
        store_thumbnail(OWNER, COMPETITOR, sample_png.read_bytes(), data_dir,
                        max_bytes=64)
    assert excinfo.value.reason == REASON_TOO_LARGE
    _assert_no_leftovers(data_dir)


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def test_path_traversal_ids_are_sanitised_inside_root(
        sample_mp4: Path, data_dir: Path):
    stored = store_video_file("../../evil", "comp/../../x", sample_mp4,
                              "c.mp4", data_dir)
    parts = Path(stored.storage_path).parts
    assert parts[0] == "style_lab"
    assert ".." not in parts
    resolved = (data_dir / stored.storage_path).resolve()
    assert resolved.is_relative_to((data_dir / "style_lab").resolve())
    assert resolved.is_file()


def test_slash_in_id_becomes_single_segment(sample_mp4: Path, data_dir: Path):
    stored = store_video_file("owner/with/slashes", COMPETITOR, sample_mp4,
                              "c.mp4", data_dir)
    parts = Path(stored.storage_path).parts
    # style_lab / <one owner segment> / <competitor> / <file>
    assert len(parts) == 4
    assert parts[1] == "owner-with-slashes"


def test_dot_only_id_is_rejected(sample_mp4: Path, data_dir: Path):
    with pytest.raises(MediaValidationError) as excinfo:
        store_video_file("..", COMPETITOR, sample_mp4, "c.mp4", data_dir)
    assert excinfo.value.reason == REASON_INVALID_PATH_SEGMENT
    with pytest.raises(MediaValidationError) as excinfo:
        store_thumbnail(OWNER, "", sample_mp4, data_dir)
    assert excinfo.value.reason == REASON_INVALID_PATH_SEGMENT
