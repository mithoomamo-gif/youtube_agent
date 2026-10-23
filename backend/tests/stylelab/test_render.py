"""Markdown renderer tests — especially honest uncertainty rendering."""
from __future__ import annotations

from app.style_analysis import StyleLabAnalysis, render_breakdown
from app.style_analysis.schema import SceneVisualLabel, VisualMix

from .test_schema import full_analysis

SECTIONS = [
    "## Overview",
    "## Scenes & Pacing",
    "## Visual Mix",
    "## Speech & Structure",
    "## Audio & Editing Dressing",
    "## Thumbnail",
]


def test_all_sections_present():
    md = render_breakdown(full_analysis())
    for section in SECTIONS:
        assert section in md


def test_probabilistic_label_rendered_with_confidence():
    md = render_breakdown(full_analysis())
    assert "likely stock-like (0.62)" in md


def test_all_uncertain_renders_honestly():
    md = render_breakdown(StyleLabAnalysis.make_uncertain("/tmp/x.mp4"))
    assert "uncertain / not assessed" in md
    # No label may be upgraded into a bare fact.
    assert "likely stock" not in md
    assert "very likely" not in md
    # Missing measurements are "not measured", never fabricated 0s.
    assert "not measured" in md
    # Null thumbnail is stated as absent, not invented.
    assert "No thumbnail was supplied" in md
    for section in SECTIONS:
        assert section in md


def test_low_confidence_flagged_as_low():
    a = full_analysis()
    a.visual_mix = VisualMix(
        stock_like=100.0, uncertain=0.0, confidence=0.3,
        scene_labels=[SceneVisualLabel(scene_index=0, label="stock_like", confidence=0.3)],
    )
    md = render_breakdown(a)
    assert "possibly stock-like (0.30 — low confidence)" in md


def test_thumbnail_section_renders_structure_only():
    md = render_breakdown(full_analysis())
    thumb = md.split("## Thumbnail", 1)[1]
    assert "Text word count: 3" in thumb
    assert "structure only" in thumb
