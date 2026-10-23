"""SL-006 stock brightness selection filter — unit + fetcher tests.

Synthetic media is generated with ffmpeg lavfi under the test's
tmp dir (basetemp is redirected to the workspace .tmp-yta by the
assignment's pytest invocation; nothing here writes to /tmp).

Contract under test:
* ``measure_brightness_yavg`` returns a measured mean luma for real
  media and ``None`` (abstain) for non-media / missing files;
* ``BRIGHTNESS_FLOOR_YAVG`` rejects pure black and near-black but
  passes ordinary bright content;
* ``StockBrightnessFilter`` tracks checked / rejected stats;
* ``StockFetcher`` rejects a measured near-black candidate at
  SELECTION time and serves the next ranked candidate instead —
  and when every candidate is too dark it returns ``None`` (the
  visuals stage then falls back to a local plate, as before).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.integration.brightness import (
    BRIGHTNESS_FLOOR_YAVG,
    StockBrightnessFilter,
    is_too_dark,
    measure_brightness_yavg,
)
from app.integration.stock_fetch import StockFetcher


def _lavfi_video(path: Path, lavfi_spec: str) -> Path:
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", lavfi_spec,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
    return path


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("brightness-media")
    return {
        "black": _lavfi_video(d / "black.mp4", "color=c=black:s=320x180:d=2:r=10"),
        "near_black": _lavfi_video(
            d / "near_black.mp4", "color=c=0x0a0a0a:s=320x180:d=2:r=10"),
        "bright": _lavfi_video(d / "bright.mp4", "color=c=white:s=320x180:d=2:r=10"),
        "testsrc": _lavfi_video(
            d / "testsrc.mp4", "testsrc2=size=320x180:duration=2:rate=10"),
    }


# ---------------------------------------------------------------------------
# Measurement + floor
# ---------------------------------------------------------------------------

def test_floor_is_the_documented_constant():
    # Calibrated: pure black measures 16, the v1 blocking stock clip
    # (Pixabay 25613) ~18.2, QC-passing stock >= ~54.  The floor must
    # sit between the near-black cluster and passing content.
    assert BRIGHTNESS_FLOOR_YAVG == 30.0
    assert 18.2 < BRIGHTNESS_FLOOR_YAVG < 54.0


def test_measure_black_is_at_limited_range_black(media):
    yavg = measure_brightness_yavg(media["black"])
    assert yavg is not None
    assert yavg == pytest.approx(16.0, abs=1.5)
    assert is_too_dark(yavg)


def test_measure_near_black_is_rejected(media):
    yavg = measure_brightness_yavg(media["near_black"])
    assert yavg is not None
    assert yavg < BRIGHTNESS_FLOOR_YAVG
    assert is_too_dark(yavg)


def test_measure_bright_passes(media):
    yavg = measure_brightness_yavg(media["bright"])
    assert yavg is not None
    assert yavg > 200
    assert not is_too_dark(yavg)


def test_measure_testsrc_passes(media):
    yavg = measure_brightness_yavg(media["testsrc"])
    assert yavg is not None
    assert yavg > BRIGHTNESS_FLOOR_YAVG
    assert not is_too_dark(yavg)


def test_measure_unmeasurable_abstains(tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"this is not a media file at all")
    assert measure_brightness_yavg(junk) is None
    assert measure_brightness_yavg(tmp_path / "missing.mp4") is None
    # Abstention is never a rejection.
    assert not is_too_dark(None)
    assert not is_too_dark(measure_brightness_yavg(junk))


def test_measure_still_image(tmp_path):
    from PIL import Image

    dark = tmp_path / "dark.png"
    Image.new("RGB", (64, 36), (0, 0, 0)).save(dark)
    bright = tmp_path / "bright.png"
    Image.new("RGB", (64, 36), (240, 240, 240)).save(bright)
    assert is_too_dark(measure_brightness_yavg(dark))
    assert not is_too_dark(measure_brightness_yavg(bright))


# ---------------------------------------------------------------------------
# Filter bookkeeping
# ---------------------------------------------------------------------------

def test_filter_tracks_stats(media, tmp_path):
    filt = StockBrightnessFilter()
    assert filt.floor == BRIGHTNESS_FLOOR_YAVG
    v_dark = filt.check(media["black"], asset_id="dark-1")
    v_bright = filt.check(media["bright"], asset_id="bright-1")
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"nope")
    v_junk = filt.check(junk, asset_id="junk-1")
    assert v_dark.too_dark and v_dark.measured
    assert not v_bright.too_dark and v_bright.measured
    assert not v_junk.too_dark and not v_junk.measured  # abstain = pass
    stats = filt.stats()
    assert stats["floor_yavg"] == BRIGHTNESS_FLOOR_YAVG
    assert stats["checked"] == 3
    assert stats["rejected_too_dark"] == 1
    assert stats["unmeasurable"] == 1
    assert stats["rejections"][0]["asset_id"] == "dark-1"
    assert stats["rejections"][0]["yavg"] == pytest.approx(16.0, abs=1.5)


def test_filter_custom_floor(media):
    filt = StockBrightnessFilter(floor=250.0)
    # Even a white clip (YAVG 235) is below a 250 floor.
    assert filt.check(media["bright"]).too_dark


# ---------------------------------------------------------------------------
# Fetcher integration: rejection happens at selection time
# ---------------------------------------------------------------------------

def _candidate(asset_id: str, url: str, *, downloads: int = 10) -> dict:
    # Shape after the Pixabay adapter's normalisation, which is what
    # StockFetcher._normalise_candidate consumes.
    return {
        "id": asset_id,
        "width": 1280,
        "height": 720,
        "duration": 30,
        "downloads": downloads,
        "tags": ["antarctica", "ice"],
        "download_url": url,
        "download_tiers": [
            {"quality": "large", "url": url, "width": 1280, "height": 720},
        ],
    }


class _StubSearchAdapter:
    """Adapter returning fixed raw hits; downloads via fetcher's
    injected downloader (URL -> real lavfi bytes)."""

    cache = object()  # pretend the adapter has its own search cache

    def __init__(self, hits: list[dict]):
        self._hits = hits

    async def search(self, query, media_type, filters=None):
        return list(self._hits)


def _fetcher(tmp_path: Path, hits: list[dict], files: dict[str, Path]) -> StockFetcher:
    def downloader(url: str) -> bytes:
        for key, path in files.items():
            if key in url:
                return path.read_bytes()
        raise AssertionError(f"unexpected download url {url}")

    return StockFetcher(
        adapter=_StubSearchAdapter(hits),
        cache_dir=tmp_path / "cache",
        downloader=downloader,
    )


def test_fetcher_rejects_dark_and_serves_next_candidate(media, tmp_path):
    hits = [
        _candidate("dark1", "https://cdn.example/dark1.mp4", downloads=5),
        _candidate("bright1", "https://cdn.example/bright1.mp4", downloads=9),
    ]
    files = {"dark1": media["black"], "bright1": media["testsrc"]}
    fetcher = _fetcher(tmp_path, hits, files)
    asset = fetcher.fetch_for_scene(5.0, ["antarctica ice"])
    assert asset is not None
    assert asset.provider_asset_id == "bright1"
    stats = fetcher.brightness_stats
    assert stats["checked"] >= 2
    assert stats["rejected_too_dark"] == 1
    assert stats["rejections"][0]["asset_id"] == "dark1"


def test_fetcher_all_dark_returns_none(media, tmp_path):
    hits = [_candidate("dark1", "https://cdn.example/dark1.mp4")]
    fetcher = _fetcher(tmp_path, hits, {"dark1": media["black"]})
    asset = fetcher.fetch_for_scene(5.0, ["antarctica ice"])
    assert asset is None
    assert fetcher.brightness_stats["rejected_too_dark"] == 1


def test_fetcher_unmeasurable_bytes_still_served(tmp_path):
    hits = [_candidate("junk1", "https://cdn.example/junk1.mp4")]
    fetcher = StockFetcher(
        adapter=_StubSearchAdapter(hits),
        cache_dir=tmp_path / "cache",
        downloader=lambda url: b"not a real video",
    )
    asset = fetcher.fetch_for_scene(5.0, ["antarctica ice"])
    assert asset is not None
    assert asset.provider_asset_id == "junk1"
    assert fetcher.brightness_stats["rejected_too_dark"] == 0
    assert fetcher.brightness_stats["unmeasurable"] == 1
