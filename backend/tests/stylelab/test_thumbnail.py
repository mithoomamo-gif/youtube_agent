"""Tests for Style Lab thumbnail analysis (app.style_analysis.thumbnail).

Deterministic Pillow work (palette / contrast / layout signals) is
tested on synthetic images built in-test; the vision paths (text
count/role, face presence) are tested with injected ``vision_fn``
doubles — including the failure path, which must degrade to
``text_word_count is None`` / ``face_present == "unknown"`` and never
guess. No network in this suite: the one live-Gemini check is a
separate manual verification, not a unit test.

Run: pytest backend/tests/stylelab/test_thumbnail.py
"""

from __future__ import annotations

import json
import os
import sys

import pytest
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.style_analysis.thumbnail import (  # noqa: E402
    analyse_thumbnail,
    classify_layout,
    compute_image_stats,
    extract_dominant_colours,
    parse_vision_payload,
)

NO_VISION = {"GEMINI_API_KEY": None}


def _no_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def _save(img: Image.Image, tmp_path, name="t.png") -> str:
    p = tmp_path / name
    img.save(p)
    return str(p)


def _solid(colour, size=(640, 360)):
    return Image.new("RGB", size, colour)


def _two_tone(size=(640, 360)):
    img = Image.new("RGB", size, (10, 20, 120))
    d = ImageDraw.Draw(img)
    d.rectangle((size[0] // 2, 0, size[0], size[1]), fill=(250, 250, 250))
    return img


def _checker(size=(640, 360), cell=16):
    img = Image.new("RGB", size, (0, 0, 0))
    d = ImageDraw.Draw(img)
    for y in range(0, size[1], cell):
        for x in range(0, size[0], cell):
            if (x // cell + y // cell) % 2 == 0:
                d.rectangle((x, y, x + cell - 1, y + cell - 1), fill=(255, 255, 255))
    return img


def _text_image(tmp_path, words="SEVEN OCEAN SECRETS REVEALED NOW"):
    """Bold rendered text over a flat background (Pillow ImageDraw)."""
    img = Image.new("RGB", (1280, 720), (20, 40, 90))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=90)
    except TypeError:  # very old Pillow: bitmap default only
        font = ImageFont.load_default()
    d.text((60, 280), words, font=font, fill=(255, 230, 0))
    return _save(img, tmp_path, "text.png")


def _vision_ok(**overrides):
    payload = {
        "text_word_count": 2,
        "text_role": "statement claim",
        "face_present": False,
        "face_size_class": None,
    }
    payload.update(overrides)
    return lambda path: payload


# ---------------------------------------------------------------------------
# Palette (deterministic, Pillow only)
# ---------------------------------------------------------------------------
def test_palette_solid_colour_is_exact():
    colours = extract_dominant_colours(_solid((18, 52, 86)))
    assert len(colours) == 1
    assert colours[0]["hex"] == "#123456"
    assert colours[0]["coverage_pct"] == 100.0


def test_palette_two_tone_split_coverage():
    colours = extract_dominant_colours(_two_tone())
    hexes = {c["hex"] for c in colours}
    assert "#0A1478" in hexes and "#FAFAFA" in hexes
    top = sorted(colours, key=lambda c: -c["coverage_pct"])
    assert all(45.0 <= c["coverage_pct"] <= 55.0 for c in top[:2])
    assert abs(sum(c["coverage_pct"] for c in colours) - 100.0) < 1.5


def test_palette_is_deterministic():
    img = _two_tone()
    assert extract_dominant_colours(img) == extract_dominant_colours(img)


def test_palette_caps_at_five_and_sorted():
    colours = extract_dominant_colours(_checker())
    assert 1 <= len(colours) <= 5
    coverages = [c["coverage_pct"] for c in colours]
    assert coverages == sorted(coverages, reverse=True)


# ---------------------------------------------------------------------------
# Contrast / layout signals (deterministic)
# ---------------------------------------------------------------------------
def test_contrast_high_for_black_white_split():
    signals = compute_image_stats(_two_tone())
    _, contrast = classify_layout(
        signals, text_word_count=0, face_present=False, face_size_class=None
    )
    assert contrast == "high"
    assert signals["contrast_spread"] >= 0.8


def test_contrast_low_for_solid_grey():
    signals = compute_image_stats(_solid((128, 128, 128)))
    _, contrast = classify_layout(
        signals, text_word_count=0, face_present=False, face_size_class=None
    )
    assert contrast == "low"
    assert signals["edge_density"] == 0.0


def test_layout_minimal_for_solid_frame(tmp_path, monkeypatch):
    _no_key(monkeypatch)
    result = analyse_thumbnail(_save(_solid((30, 30, 30)), tmp_path))
    assert result["layout_class"] == "minimal"
    assert result["contrast_class"] == "low"


def test_layout_object_led_for_busy_faceless_textless(tmp_path):
    result = analyse_thumbnail(
        _save(_checker(), tmp_path), vision_fn=_vision_ok(text_word_count=0)
    )
    assert result["layout_class"] == "object_led"
    assert result["signals"]["edge_density"] > 0.1


def test_layout_text_heavy_from_vision_count(tmp_path):
    result = analyse_thumbnail(
        _text_image(tmp_path),
        vision_fn=_vision_ok(text_word_count=7, text_role="curiosity question"),
    )
    assert result["layout_class"] == "text_heavy"
    assert result["text_word_count"] == 7


def test_layout_face_led_from_vision(tmp_path):
    result = analyse_thumbnail(
        _save(_two_tone(), tmp_path),
        vision_fn=_vision_ok(face_present=True, face_size_class="large"),
    )
    assert result["layout_class"] == "face_led"
    assert result["face_present"] is True
    assert result["face_size_class"] == "large"


def test_stats_are_deterministic():
    img = _checker()
    assert compute_image_stats(img) == compute_image_stats(img)


# ---------------------------------------------------------------------------
# Vision paths (mocked) — success, sanitisation, failure
# ---------------------------------------------------------------------------
def test_vision_success_populates_count_role_face(tmp_path):
    result = analyse_thumbnail(
        _save(_solid((200, 30, 30)), tmp_path),
        vision_fn=_vision_ok(
            text_word_count=3, text_role="number-led claim",
            face_present=True, face_size_class="small",
        ),
    )
    assert result["text_word_count"] == 3
    assert result["text_role_notes"] == "number-led claim"
    assert result["face_present"] is True
    assert result["provenance"]["vision"] == "injected vision_fn"


def test_vision_failure_degrades_to_null_and_unknown(tmp_path):
    def boom(path):
        raise RuntimeError("model unavailable")

    result = analyse_thumbnail(_save(_solid((0, 0, 0)), tmp_path), vision_fn=boom)
    assert result["text_word_count"] is None
    assert result["text_role_notes"] is None
    assert result["face_present"] == "unknown"
    assert result["face_size_class"] is None
    assert "unavailable" in result["provenance"]["vision"]
    # Deterministic parts still work despite the vision failure.
    assert result["dominant_colours"][0]["hex"] == "#000000"
    assert result["layout_class"] in (
        "text_heavy", "face_led", "object_led", "mixed", "minimal"
    )


def test_no_api_key_degrades_to_null_and_unknown(tmp_path, monkeypatch):
    _no_key(monkeypatch)
    result = analyse_thumbnail(_save(_solid((9, 9, 9)), tmp_path))
    assert result["text_word_count"] is None
    assert result["face_present"] == "unknown"


def test_transcription_from_model_is_never_stored(tmp_path):
    """A misbehaving model that returns the actual words must not leak
    them: the parser keeps count/role/face only, role coerced to the
    canonical vocabulary."""
    secret_words = "ZXQWPLM hidden competitor headline words"

    def leaky(path):
        return {
            "text_word_count": 5,
            "text_role": "curiosity question",
            "face_present": False,
            "face_size_class": None,
            "transcription": secret_words,
            "text": secret_words,
            "ocr_text": secret_words,
        }

    result = analyse_thumbnail(_save(_solid((1, 2, 3)), tmp_path), vision_fn=leaky)
    blob = json.dumps(result)
    assert "ZXQWPLM" not in blob
    assert "hidden competitor headline" not in blob
    assert result["text_word_count"] == 5
    assert result["text_role_notes"] == "curiosity question"


def test_parse_vision_payload_coerces_and_validates():
    out = parse_vision_payload(
        {
            "text_word_count": 1,
            "text_role": "literally the words BIG SECRET HERE",
            "face_present": "yes",
            "face_size_class": "huge",
            "extra": "drop me",
        }
    )
    assert out["text_word_count"] == 1
    assert out["text_role_notes"] in (
        "curiosity question", "number-led claim", "single keyword",
        "statement claim", "call to action", "no text", "other",
    )
    assert "BIG SECRET" not in json.dumps(out)
    assert out["face_present"] is True
    assert out["face_size_class"] == "medium"  # 'huge' invalid -> neutral
    assert "extra" not in out


def test_parse_vision_payload_zero_words_is_no_text():
    out = parse_vision_payload(
        {"text_word_count": 0, "text_role": "statement claim",
         "face_present": False, "face_size_class": "large"}
    )
    assert out["text_role_notes"] == "no text"
    assert out["face_size_class"] is None  # no face -> no size class


def test_parse_vision_payload_garbage_is_unknown():
    out = parse_vision_payload("not a dict")
    assert out["text_word_count"] is None
    assert out["face_present"] == "unknown"


# ---------------------------------------------------------------------------
# Contract / schema shape
# ---------------------------------------------------------------------------
def test_result_schema_fields_present(tmp_path):
    result = analyse_thumbnail(
        _save(_two_tone(), tmp_path), vision_fn=_vision_ok()
    )
    for field in (
        "text_word_count", "text_role_notes", "face_present",
        "dominant_colours", "layout_class", "contrast_class",
    ):
        assert field in result, field
    assert result["layout_class"] in (
        "text_heavy", "face_led", "object_led", "mixed", "minimal"
    )
    assert result["contrast_class"] in ("high", "medium", "low")
    assert result["layout_is_estimate"] is True
    for entry in result["dominant_colours"]:
        assert set(entry) >= {"hex", "coverage_pct"}
        assert entry["hex"].startswith("#") and len(entry["hex"]) == 7


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        analyse_thumbnail(tmp_path / "nope.png", vision_fn=_vision_ok())
