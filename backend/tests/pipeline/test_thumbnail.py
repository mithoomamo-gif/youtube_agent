"""Tests for the thumbnail builder (worker 5).

Run: pytest backend/tests/pipeline/test_thumbnail.py
Backgrounds are generated locally with Pillow -- no network, no AI images.
"""

from __future__ import annotations

import hashlib
import os
import sys

import pytest
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.pipeline.thumbnail import (  # noqa: E402
    BUILTIN_LAYOUTS,
    ChannelStyleProfile,
    ThumbnailBuilder,
    build_thumbnail,
    check_legibility,
    default_profile,
    generate_variants,
    shaping_available,
)
from app.pipeline.thumbnail.text_render import find_fallback_font  # noqa: E402

FONT = find_fallback_font()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def make_background(path, colour=(30, 90, 160), size=(800, 600)):
    """A simple generated 'photo plate': solid sky + sun disc + ground."""
    img = Image.new("RGB", size, colour)
    d = ImageDraw.Draw(img)
    w, h = size
    d.ellipse((w * 0.62, h * 0.12, w * 0.82, h * 0.42), fill=(240, 200, 90))
    d.rectangle((0, h * 0.72, w, h), fill=(40, 120, 60))
    img.save(path)
    return str(path)


def make_profile(**overrides) -> ChannelStyleProfile:
    kwargs = dict(
        name="test-channel",
        palette=["#10243E", "#FFD500", "#FF3B30", "#FFFFFF"],
        accent_color="#FFD500",
        font_path_latin=FONT,
    )
    kwargs.update(overrides)
    return ChannelStyleProfile(**kwargs)


@pytest.fixture()
def bg(tmp_path):
    return make_background(tmp_path / "bg.png")


def mean_abs_diff(a: Image.Image, b: Image.Image, box) -> float:
    ca = a.convert("L").crop(box)
    cb = b.convert("L").crop(box)
    pa, pb = list(ca.getdata()), list(cb.getdata())
    return sum(abs(x - y) for x, y in zip(pa, pb)) / len(pa)


# ---------------------------------------------------------------------------
# Basic build
# ---------------------------------------------------------------------------
def test_build_is_1280x720_valid_png(tmp_path, bg):
    out = tmp_path / "thumb.png"
    result = build_thumbnail(bg, "Amazing Facts", out, profile=make_profile())
    assert result.width == 1280 and result.height == 720
    assert result.image_format == "PNG"
    with Image.open(out) as img:
        img.verify()
    with Image.open(out) as img:
        assert img.size == (1280, 720)


def test_build_jpeg(tmp_path, bg):
    out = tmp_path / "thumb.jpg"
    result = build_thumbnail(bg, "Amazing Facts", out, profile=make_profile())
    assert result.image_format == "JPEG"
    with Image.open(out) as img:
        assert img.size == (1280, 720) and img.format == "JPEG"


def test_background_is_cover_fitted_from_any_size(tmp_path):
    weird = make_background(tmp_path / "weird.png", size=(500, 900))  # portrait
    out = tmp_path / "t.png"
    build_thumbnail(weird, "Tall Image", out, profile=make_profile())
    with Image.open(out) as img:
        assert img.size == (1280, 720)


def test_text_is_present_in_text_region(tmp_path, bg):
    builder = ThumbnailBuilder(make_profile())
    with_text = builder.build(bg, "Amazing Facts", tmp_path / "with.png")
    without = builder.build(bg, "", tmp_path / "without.png")
    assert with_text.text_bbox is not None
    a = Image.open(with_text.path)
    b = Image.open(without.path)
    # The headline region must differ strongly from the no-text render.
    assert mean_abs_diff(a, b, with_text.text_bbox) > 8.0


def test_foreground_cutout_is_placed(tmp_path, bg):
    cutout = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    d = ImageDraw.Draw(cutout)
    d.ellipse((10, 10, 290, 290), fill=(255, 0, 0, 255))
    fg_path = tmp_path / "cutout.png"
    cutout.save(fg_path)
    result = build_thumbnail(
        bg, "Red Subject", tmp_path / "t.png",
        profile=make_profile(), foreground=str(fg_path),
    )
    img = Image.open(result.path).convert("RGB")
    zone = img.crop((716, 90, 1232, 690))
    reds = sum(1 for p in zone.getdata() if p[0] > 200 and p[1] < 60 and p[2] < 60)
    assert reds > 5000


# ---------------------------------------------------------------------------
# Style profile / palette / layouts
# ---------------------------------------------------------------------------
def test_builtin_layouts_exist():
    assert {"text-left", "text-bottom-band", "big-number"} <= set(BUILTIN_LAYOUTS)


def test_palette_band_colour_is_applied_verbatim(tmp_path, bg):
    profile = make_profile(palette=["#123456", "#FFD500", "#FF3B30"])
    result = build_thumbnail(
        bg, "Band Layout", tmp_path / "band.png",
        profile=profile, layout_id="text-bottom-band",
    )
    img = Image.open(result.path).convert("RGB")
    # Bottom-left corner sits inside the band, away from text/logo.
    assert img.getpixel((10, 712)) == (0x12, 0x34, 0x56)
    # The accent colour appears verbatim too (accent bar above the headline).
    pixels = set(img.resize((320, 180)).getdata())
    assert (0xFF, 0xD5, 0x00) in pixels or (0xFF, 0xD5, 0x00) in set(img.getdata())


def test_profile_validation():
    with pytest.raises(ValueError):
        ChannelStyleProfile(name="x", palette=["#000000", "#FFFFFF"])  # < 3
    with pytest.raises(ValueError):
        ChannelStyleProfile(
            name="x", palette=["#000000", "#111111", "#222222", "#333333",
                               "#444444", "#555555"]  # > 5
        )
    with pytest.raises(ValueError):
        ChannelStyleProfile(name="x", palette=["red", "#111111", "#222222"])
    with pytest.raises(ValueError):
        make_profile(default_layout_id="no-such-layout")


def test_long_headline_raises_warning_flag(tmp_path, bg):
    headline = "This headline has way too many words in it"  # 9 words
    result = build_thumbnail(bg, headline, tmp_path / "t.png", profile=make_profile())
    assert result.word_count == 9
    assert any("HEADLINE_TOO_LONG" in w for w in result.warnings)


def test_short_headline_has_no_length_warning(tmp_path, bg):
    result = build_thumbnail(bg, "Amazing Space Facts", tmp_path / "t.png",
                             profile=make_profile())
    assert not any("HEADLINE_TOO_LONG" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Legibility
# ---------------------------------------------------------------------------
def test_legibility_passes_bold_high_contrast(tmp_path, bg):
    result = build_thumbnail(bg, "SHOCKING TRUTH", tmp_path / "t.png",
                             profile=make_profile())
    assert result.legibility is not None
    assert result.legibility.passed, result.legibility.reasons
    assert result.legibility.score >= 0.5
    assert result.legibility.checked_size == (128, 72)


def _planted_bad_thumbnail(tmp_path, *, font_size, fg, bg_colour):
    """Hand-drawn (not builder-made) thumbnail with a text mask, for the
    legibility checker: tiny and/or low-contrast text must FAIL."""
    img = Image.new("RGB", (1280, 720), bg_colour)
    mask = Image.new("L", (1280, 720), 0)
    d = ImageDraw.Draw(img)
    md = ImageDraw.Draw(mask)
    font = ImageFont.truetype(FONT, font_size) if FONT else ImageFont.load_default()
    d.text((60, 60), "faint tiny text", font=font, fill=fg)
    md.text((60, 60), "faint tiny text", font=font, fill=255)
    bbox = mask.getbbox()
    return img, mask, bbox


def test_legibility_fails_tiny_low_contrast_text(tmp_path):
    img, mask, bbox = _planted_bad_thumbnail(
        tmp_path, font_size=18, fg=(136, 136, 136), bg_colour=(119, 119, 119)
    )
    result = check_legibility(img, text_bbox=bbox, text_mask=mask)
    assert not result.passed
    assert result.details["contrast_spread"] < 0.30


def test_legibility_fails_large_low_contrast_text(tmp_path):
    img, mask, bbox = _planted_bad_thumbnail(
        tmp_path, font_size=90, fg=(140, 140, 140), bg_colour=(119, 119, 119)
    )
    result = check_legibility(img, text_bbox=bbox, text_mask=mask)
    assert not result.passed


def test_legibility_passes_direct_bold_text(tmp_path):
    img = Image.new("RGB", (1280, 720), (200, 200, 200))
    mask = Image.new("L", (1280, 720), 0)
    d = ImageDraw.Draw(img)
    md = ImageDraw.Draw(mask)
    font = ImageFont.truetype(FONT, 110) if FONT else ImageFont.load_default()
    d.text((60, 60), "BIG NEWS", font=font, fill=(255, 255, 255),
           stroke_width=8, stroke_fill=(0, 0, 0))
    md.text((60, 60), "BIG NEWS", font=font, fill=255,
            stroke_width=8, stroke_fill=255)
    result = check_legibility(img, text_bbox=mask.getbbox(), text_mask=mask)
    assert result.passed, result.reasons


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------
def test_three_variants_produced_and_distinct(tmp_path):
    bgs = [
        make_background(tmp_path / f"bg{i}.png", colour=c)
        for i, c in enumerate([(30, 90, 160), (120, 40, 140), (20, 120, 90)])
    ]
    headlines = ["Amazing Space Facts", "You Won't Believe This", "7 Ocean Secrets"]
    variant_set = generate_variants(
        bgs, headlines, make_profile(), tmp_path / "out", base_name="ep42"
    )
    variants = variant_set.variants
    assert len(variants) == 3
    assert [v.variant_id for v in variants] == ["v1", "v2", "v3"]
    assert {v.layout_id for v in variants} == {
        "text-left", "text-bottom-band", "big-number",
    }
    # Deterministic file names.
    names = [os.path.basename(v.path) for v in variants]
    assert names == [
        "ep42_v1_text-left.png",
        "ep42_v2_text-bottom-band.png",
        "ep42_v3_big-number.png",
    ]
    hashes = set()
    for v in variants:
        assert os.path.isfile(v.path)
        with Image.open(v.path) as img:
            assert img.size == (1280, 720)
        assert v.legibility is not None
        assert v.metadata["variant_id"] == v.variant_id
        assert v.metadata["layout"] == v.layout_id
        assert v.metadata["headline"] == v.headline
        hashes.add(hashlib.sha256(open(v.path, "rb").read()).hexdigest())
    assert len(hashes) == 3  # all distinct


def test_variants_work_with_single_background_and_headline(tmp_path, bg):
    variant_set = generate_variants([bg], ["Only Option"], make_profile(), tmp_path / "o")
    assert len(variant_set.variants) == 3


def test_variants_reject_empty_inputs(tmp_path, bg):
    with pytest.raises(ValueError):
        generate_variants([], ["x"], make_profile(), tmp_path / "o")
    with pytest.raises(ValueError):
        generate_variants([bg], [], make_profile(), tmp_path / "o")


def test_big_number_layout_renders(tmp_path, bg):
    result = build_thumbnail(bg, "7 Ocean Secrets", tmp_path / "n.png",
                             profile=make_profile(), layout_id="big-number")
    assert result.layout_id == "big-number"
    assert result.legibility is not None and result.legibility.passed


# ---------------------------------------------------------------------------
# Urdu / Arabic script
# ---------------------------------------------------------------------------
def test_urdu_headline_builds_with_graceful_shaping_flag(tmp_path, bg):
    profile = make_profile(font_path_nastaliq=None)
    result = build_thumbnail(bg, "دلچسپ سچی کہانی", tmp_path / "urdu.png",
                             profile=profile)
    with Image.open(result.path) as img:
        assert img.size == (1280, 720)
    # shaped is True only when reshaper+bidi are actually installed; in this
    # environment they are not, so the fallback must report False, not crash.
    assert result.shaped == shaping_available()
    assert result.metadata["arabic_script"] is True


def test_latin_headline_not_flagged_arabic(tmp_path, bg):
    result = build_thumbnail(bg, "Plain Latin Title", tmp_path / "t.png",
                             profile=make_profile())
    assert result.metadata["arabic_script"] is False
    assert result.shaped is False
