"""Style Lab W4: thumbnail-style wiring in the assemble stage.

Covers the pure mapping layer
(:mod:`app.integration.style_thumbnail`) and its wiring in
:func:`app.integration.assemble_stage.assemble_project`:

* each style thumbnail directive -> pipeline field, or honestly
  recorded as unmapped (layout / palette / face);
* headline band adjustment (truncate / fallback / untouched);
* precedence: style profile beats the niche-profile default, the
  niche default remains the fallback, and NO profile means identical
  behaviour (no ``style_applied`` recorded).

Heavy assemble stages are faked exactly like
``test_assemble_subtitles.py``; the style resolver is the REAL W1
contract except where a test explicitly substitutes it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.integration import assemble_stage
from app.integration.style_profile import (
    StyleProfile,
    ThumbnailStyle,
    style_directives,
)
from app.integration.style_thumbnail import (
    adjust_headline_to_band,
    adjust_headlines,
    apply_style_thumbnail,
    extract_thumbnail_directives,
    map_layout_class,
    palette_preset_for,
)
from app.pipeline.gates.tokens import Verdict
from app.pipeline.subtitles.models import WordTiming
from app.pipeline.thumbnail.models import (
    ChannelStyleProfile,
    default_profile,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _niche_profile(**overrides) -> ChannelStyleProfile:
    kwargs = dict(
        name="niche-channel",
        palette=["#111111", "#222222", "#333333", "#444444"],
        accent_color="#444444",
        default_layout_id="big-number",
        font_path_latin="/fonts/niche.ttf",
    )
    kwargs.update(overrides)
    return ChannelStyleProfile(**kwargs)


def _directives(**thumbnail_overrides) -> dict:
    thumbnail = {
        "layout_class": None,
        "text_words_min": None,
        "text_words_max": None,
        "face_expected": False,
        "palette_class": None,
    }
    thumbnail.update(thumbnail_overrides)
    return thumbnail


# ---------------------------------------------------------------------------
# Directive extraction
# ---------------------------------------------------------------------------


def test_extract_from_full_directives_dict():
    full = {"thumbnail": _directives(layout_class="face_right_text_left"),
            "title_formula": {}}
    out = extract_thumbnail_directives(full)
    assert out is not None
    assert out["layout_class"] == "face_right_text_left"


def test_extract_from_bare_thumbnail_dict_and_none():
    assert extract_thumbnail_directives(_directives()) is not None
    assert extract_thumbnail_directives(None) is None
    assert extract_thumbnail_directives({"unrelated": 1}) is None


def test_extract_from_real_style_profile_object():
    profile = StyleProfile(
        channel_id="chan-1",
        thumbnail=ThumbnailStyle(
            layout_class="face_right_text_left", text_words_min=2,
            text_words_max=4, face_expected=True,
            palette_class="high_contrast_warm"),
    )
    # Both the profile object and style_directives(profile) work.
    for source in (profile, style_directives(profile)):
        out = extract_thumbnail_directives(source)
        assert out == {
            "layout_class": "face_right_text_left",
            "text_words_min": 2,
            "text_words_max": 4,
            "face_expected": True,
            "palette_class": "high_contrast_warm",
        }


# ---------------------------------------------------------------------------
# Layout mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("face_right_text_left", "text-left"),
    ("text_left_subject_right", "text-left"),
    ("text-left", "text-left"),
    ("text_bottom_band", "text-bottom-band"),
    ("text-bottom-band", "text-bottom-band"),
    ("big_number", "big-number"),
    ("number_led", "big-number"),
    ("text_heavy", "text-left"),
    ("text_only", "text-left"),
])
def test_layout_class_maps_to_pipeline_layout(raw, expected):
    assert map_layout_class(raw) == expected


@pytest.mark.parametrize("raw", [
    "split_screen", "full_bleed_subject", "face_led", "object_led",
    "mixed", "minimal", "face_left_text_right", "uncertain", None, "",
])
def test_layout_class_without_counterpart_is_unmapped(raw):
    assert map_layout_class(raw) is None
    base = _niche_profile()
    mapping = apply_style_thumbnail(base, _directives(layout_class=raw))
    assert mapping.profile.default_layout_id == "big-number"  # niche kept
    if raw:
        assert "layout_class" in mapping.unmapped
    else:
        assert "layout_class" not in mapping.unmapped


def test_layout_mapping_applies_and_preserves_niche_fields():
    base = _niche_profile()
    mapping = apply_style_thumbnail(
        base, _directives(layout_class="text_bottom_band"))
    assert mapping.profile.default_layout_id == "text-bottom-band"
    assert mapping.layout_applied == "text-bottom-band"
    # Untouched niche fields survive verbatim.
    assert mapping.profile.font_path_latin == "/fonts/niche.ttf"
    assert mapping.profile.name == "niche-channel"
    assert mapping.profile.palette == base.palette
    assert mapping.unmapped == []


# ---------------------------------------------------------------------------
# Palette mapping
# ---------------------------------------------------------------------------


def test_palette_class_maps_to_own_preset_palette():
    preset = palette_preset_for("high_contrast_warm")
    assert preset is not None
    palette, accent = preset
    mapping = apply_style_thumbnail(
        _niche_profile(), _directives(palette_class="high_contrast_warm"))
    assert mapping.palette_applied is True
    assert mapping.profile.palette == palette
    assert mapping.profile.accent_color == accent
    assert "palette_class" not in mapping.unmapped


@pytest.mark.parametrize("raw", ["warm", "cool", "neon", "dark",
                                 "high_contrast", "vibrant", "muted"])
def test_known_palette_classes_have_presets(raw):
    assert palette_preset_for(raw) is not None


def test_unknown_palette_class_is_unmapped_and_niche_palette_kept():
    base = _niche_profile()
    assert palette_preset_for("iridescent_plaid") is None
    mapping = apply_style_thumbnail(
        base, _directives(palette_class="iridescent_plaid"))
    assert mapping.palette_applied is False
    assert mapping.profile.palette == base.palette
    assert mapping.profile.accent_color == base.accent_color
    assert "palette_class" in mapping.unmapped


# ---------------------------------------------------------------------------
# Face directive — no pipeline counterpart
# ---------------------------------------------------------------------------


def test_face_expected_true_is_recorded_unmapped():
    mapping = apply_style_thumbnail(
        _niche_profile(), _directives(face_expected=True))
    assert mapping.face_expected is True
    assert "face_expected" in mapping.unmapped


def test_face_expected_false_is_satisfied_noop_not_unmapped():
    mapping = apply_style_thumbnail(
        _niche_profile(), _directives(face_expected=False))
    assert mapping.face_expected is False
    assert "face_expected" not in mapping.unmapped


def test_text_word_band_is_carried_not_unmapped():
    mapping = apply_style_thumbnail(
        _niche_profile(), _directives(text_words_min=2, text_words_max=5))
    assert (mapping.text_words_min, mapping.text_words_max) == (2, 5)
    assert mapping.unmapped == []
    # The band is a headline-level directive: the profile model has no
    # such field and none was invented.
    assert not hasattr(mapping.profile, "text_words_min")


def test_no_directives_returns_base_profile_unchanged():
    base = _niche_profile()
    mapping = apply_style_thumbnail(base, None)
    assert mapping.profile is base
    assert mapping.unmapped == []


# ---------------------------------------------------------------------------
# Headline band adjustment
# ---------------------------------------------------------------------------


def test_headline_too_long_is_truncated_at_word_boundary():
    adj = adjust_headline_to_band(
        "One Two Three Four Five Six", [], 2, 4)
    assert adj.action == "truncated"
    assert adj.headline == "One Two Three Four..."
    assert adj.words_before == 6
    assert adj.words_after == 4  # ellipsis glued on: not a word itself


def test_headline_too_long_prefers_shortest_in_band_fallback():
    adj = adjust_headline_to_band(
        "One Two Three Four Five Six",
        ["Three Word Fallback", "Two Words", "Way Too Long Fallback Text"],
        2, 4)
    assert adj.action == "fallback_variant"
    assert adj.headline == "Two Words"  # shortest in-band candidate
    assert adj.words_after == 2


def test_headline_inside_band_is_untouched():
    adj = adjust_headline_to_band("Three Good Words", ["Other"], 2, 4)
    assert adj.action == "within_band"
    assert adj.headline == "Three Good Words"
    assert adj.words_before == adj.words_after == 3


def test_headline_too_short_without_fallback_is_kept_never_padded():
    adj = adjust_headline_to_band("Short", ["Also Short"], 3, 5)
    assert adj.action == "below_min_kept"
    assert adj.headline == "Short"


def test_headline_too_short_uses_in_band_fallback():
    adj = adjust_headline_to_band("Short", ["Now Long Enough"], 2, 5)
    assert adj.action == "fallback_variant"
    assert adj.headline == "Now Long Enough"


def test_no_band_and_invalid_band_leave_headline_alone():
    assert adjust_headline_to_band("Any Length At All", [], None, None).action == "no_band"
    bad = adjust_headline_to_band("Any Length", [], 5, 2)
    assert bad.action == "invalid_band"
    assert bad.headline == "Any Length"


def test_adjust_headlines_swaps_fallback_to_front_preserving_list():
    headlines, adj = adjust_headlines(
        ["A B C D E", "X Y", "A B C"], 2, 4)
    assert adj is not None and adj.action == "fallback_variant"
    assert headlines[0] == "X Y"
    assert sorted(headlines) == ["A B C", "A B C D E", "X Y"]


# ---------------------------------------------------------------------------
# Assemble-stage wiring (heavy stages faked, style wiring real)
# ---------------------------------------------------------------------------


class _FakeRenderer:
    def __init__(self, out_dir, encoder_choice=None):
        self.out_dir = Path(out_dir)

    def render_all(self, specs):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / "seg_0001.ts"
        path.write_bytes(b"ts")
        return [path]


def _fake_qc(*args, **kwargs):
    return SimpleNamespace(
        verdict=Verdict.PASS,
        loudness=SimpleNamespace(integrated_lufs=-14.0, true_peak_dbtp=-1.0,
                                 lra_lu=2.0, source="fake"),
        checks=[],
        detections={},
    )


def _patch_heavy_stages(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_generate(backgrounds, headlines, profile, out_dir, base_name="thumbnail"):
        captured["headlines"] = list(headlines)
        captured["profile"] = profile
        thumbs = []
        for i in range(3):
            t = tmp_path / f"variant_{i}.png"
            t.write_bytes(b"png")
            thumbs.append(SimpleNamespace(
                path=str(t), layout_id=profile.default_layout_id,
                headline=headlines[0], legibility=None, warnings=[]))
        return SimpleNamespace(variants=thumbs)

    monkeypatch.setattr(assemble_stage, "probe_encoder",
                        lambda: SimpleNamespace(encoder="fake"))
    monkeypatch.setattr(assemble_stage, "SegmentRenderer", _FakeRenderer)
    monkeypatch.setattr(
        assemble_stage, "render_draft",
        lambda segment_paths, out, audio=None, encoder_choice=None:
            Path(out).write_bytes(b"mp4"))
    monkeypatch.setattr(assemble_stage, "probe_duration", lambda p: 4.0)
    monkeypatch.setattr(assemble_stage, "run_technical_qc", _fake_qc)
    monkeypatch.setattr(assemble_stage, "generate_variants", fake_generate)
    return captured


def _assemble_inputs(tmp_path, *, spec=None, thumbnail_text="Ocean"):
    audio = tmp_path / "master.wav"
    audio.write_bytes(b"fake-audio")
    timings = [WordTiming("Hello", 0.0, 0.5), WordTiming("world.", 0.5, 1.0)]
    voice = SimpleNamespace(
        mixed_master_wav=str(audio), duration_s=4.0, word_timings=timings)
    bundle = SimpleNamespace(
        script_text="Hello world.", title="Ocean Test",
        thumbnail_text=thumbnail_text)
    ctx_data = dict(pack_dir=str(tmp_path / "pack"),
                    work_dir=str(tmp_path / "work"))
    if spec is not None:
        ctx_data["spec"] = spec
    return bundle, voice, [], _niche_profile(), SimpleNamespace(**ctx_data)


def test_assemble_style_profile_wins_over_niche_default(monkeypatch, tmp_path):
    captured = _patch_heavy_stages(monkeypatch, tmp_path)
    style = StyleProfile(
        channel_id="chan-1",
        thumbnail=ThumbnailStyle(
            layout_class="text_bottom_band", text_words_min=3,
            text_words_max=4, face_expected=True,
            palette_class="high_contrast_warm"),
    )
    monkeypatch.setattr(
        assemble_stage, "resolve_style_profile", lambda spec, base: style)
    bundle, voice, visuals, profile, ctx = _assemble_inputs(
        tmp_path,
        spec=SimpleNamespace(style_profile_path="style.json",
                             channel_id="chan-1"),
        thumbnail_text="One Two Three Four Five Six Seven")

    result = assemble_stage.assemble_project(bundle, voice, visuals, profile, ctx)

    # Precedence: style layout beats the niche default ("big-number"),
    # the style palette preset beats the niche palette, niche fonts stay.
    used = captured["profile"]
    assert used.default_layout_id == "text-bottom-band"
    assert used.palette == palette_preset_for("high_contrast_warm")[0]
    assert used.font_path_latin == "/fonts/niche.ttf"
    # Headline band: 7 words -> truncated to the band max (title and
    # short-title fallbacks are 2 words, below the band minimum).
    assert captured["headlines"][0] == "One Two Three Four..."
    # style_applied record, on the result AND mirrored into notes.
    applied = result.style_applied["thumbnail"]
    assert applied["layout_class"] == "text_bottom_band"
    assert applied["text_words_band"] == [3, 4]
    assert applied["headline_words_before"] == 7
    assert applied["headline_words_after"] == 4
    assert applied["headline_action"] == "truncated"
    assert applied["face_expected"] is True
    assert applied["palette_class"] == "high_contrast_warm"
    assert applied["unmapped"] == ["face_expected"]
    assert result.notes["style_applied"] == result.style_applied
    assert len(result.thumbnails) == 3


def test_assemble_no_profile_is_identical_behaviour(monkeypatch, tmp_path):
    captured = _patch_heavy_stages(monkeypatch, tmp_path)
    monkeypatch.delenv("YTA_STYLE_PROFILE_PATH", raising=False)
    # REAL resolver: spec carries no explicit path, the derived base
    # dir holds no channel default, env is unset -> None.
    bundle, voice, visuals, profile, ctx = _assemble_inputs(
        tmp_path,
        spec=SimpleNamespace(style_profile_path=None, channel_id="chan-none"),
        thumbnail_text="One Two Three Four Five Six Seven")

    result = assemble_stage.assemble_project(bundle, voice, visuals, profile, ctx)

    used = captured["profile"]
    assert used.default_layout_id == "big-number"  # niche default kept
    assert used.palette == profile.palette
    assert captured["headlines"][0] == "One Two Three Four Five Six Seven"
    assert result.style_applied == {}
    assert "style_applied" not in result.notes


def test_thumbnail_profile_caller_logic_regression():
    """The pre-existing _thumbnail_profile behaviour is unchanged."""
    direct = _niche_profile()
    assert assemble_stage._thumbnail_profile(direct) is direct
    from_mapping = assemble_stage._thumbnail_profile(
        {"name": "mapped", "palette": ["#111111", "#222222", "#333333"]})
    assert from_mapping.name == "mapped"
    assert from_mapping.palette == ["#111111", "#222222", "#333333"]
    fallback = assemble_stage._thumbnail_profile(SimpleNamespace(language="en"))
    assert fallback.name == default_profile().name
