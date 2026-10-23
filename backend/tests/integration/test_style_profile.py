"""Style Lab W1: style profile schema + synthesis engine tests.

Covers synthesis from 1 and from 3 fixture analyses, title/description
pattern extraction, the HARD no-copy enforcement, the directives shape,
resolver precedence, JSON round-trip, and graceful handling of
analyses whose SEO texts are missing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.integration.style_profile import (
    CompetitorVideoAnalysis,
    HookStats,
    NoCopyViolationError,
    StyleProfile,
    assert_no_copy,
    load_profile,
    resolve_style_profile,
    save_profile,
    style_directives,
    synthesize_style_profile,
)

TITLE_1 = "Top 10 Facts | You Won't Believe"
DESC_1 = (
    "Welcome to the channel where we explore amazing facts every "
    "single week.\n"
    "00:00 Intro\n"
    "01:30 First fact\n"
    "03:00 Second fact\n"
    "Check out https://example.com/more for extra reading material.\n"
    "Subscribe for more videos every week and never miss an episode.\n"
    "#facts #top10 #amazing\n"
)

TITLE_2 = "Amazing Ocean Facts You Never Knew"
DESC_2 = (
    "Dive into the deep ocean and discover creatures nobody has seen "
    "before today.\n"
    "#ocean #deepsea\n"
)

TITLE_3 = "Top 5 Ocean Facts [SHOCKING]"
DESC_3 = (
    "Everything about the ocean in one video for curious minds "
    "everywhere.\n"
    "00:00 Start\n"
    "02:00 Middle part\n"
    "04:00 Final part\n"
    "#ocean\n"
)


def _shots(spec: list[tuple[str, int, float]]) -> list[dict]:
    """Build shot dicts from (visual_class, count, seconds_each)."""
    shots = []
    cursor = 0.0
    for visual_class, count, each in spec:
        for _ in range(count):
            shots.append({
                "start_s": cursor, "end_s": cursor + each,
                "visual_class": visual_class, "confidence": 0.9,
            })
            cursor += each
    return shots


def _chapters(count: int, each: float) -> list[dict]:
    return [{"start_s": i * each, "end_s": (i + 1) * each}
            for i in range(count)]


def make_analysis(**overrides) -> CompetitorVideoAnalysis:
    data = dict(
        source={"kind": "attached_file", "asset_id": "comp-1",
                "sha256": "a" * 64},
        duration_s=600.0,
        speech={"wpm": 150.0, "language": "en", "word_count": 1500},
        hook={"window_s": 8.0, "type": "question"},
        chapters=_chapters(6, 100.0),
        shots=_shots([("stock", 40, 10.0), ("ai", 20, 10.0)]),
        captions={"present": True, "burned_in": True},
        music={"present": True},
        thumbnail={"layout_class": "face_right_text_left",
                   "text_word_count": 4, "face_present": True,
                   "palette_class": "high_contrast_warm"},
        seo={"title_text": TITLE_1, "description_text": DESC_1},
    )
    data.update(overrides)
    return CompetitorVideoAnalysis(**data)


def three_analyses() -> list[CompetitorVideoAnalysis]:
    second = make_analysis(
        source={"kind": "attached_file", "asset_id": "comp-2",
                "sha256": "b" * 64},
        duration_s=660.0,
        speech={"wpm": 160.0, "language": "en", "word_count": 1760},
        hook={"window_s": 10.0, "type": "bold_claim"},
        chapters=_chapters(6, 110.0),
        shots=_shots([("stock", 66, 10.0)]),
        captions={"present": True, "burned_in": True},
        music={"present": False},
        thumbnail={"layout_class": "face_right_text_left",
                   "text_word_count": 3, "face_present": True,
                   "palette_class": "high_contrast_warm"},
        seo={"title_text": TITLE_2, "description_text": DESC_2},
    )
    third = make_analysis(
        source={"kind": "youtube_url", "asset_id": "comp-3",
                "sha256": "c" * 64},
        duration_s=540.0,
        speech={"wpm": 140.0, "language": "en", "word_count": 1260},
        hook={"window_s": 6.0, "type": "question"},
        chapters=_chapters(3, 180.0),
        shots=_shots([("ai", 54, 10.0)]),
        captions={"present": False, "burned_in": False},
        music={"present": True},
        thumbnail={"layout_class": "center_text",
                   "text_word_count": 5, "face_present": False,
                   "palette_class": "dark_moody"},
        seo={"title_text": TITLE_3, "description_text": DESC_3},
    )
    return [make_analysis(), second, third]


# ---------------------------------------------------------------------------
# Schema contracts
# ---------------------------------------------------------------------------


def test_analysis_schema_id_and_extra_forbid():
    analysis = make_analysis()
    assert analysis.schema_version == "competitor_analysis.v1"
    with pytest.raises(ValidationError):
        make_analysis(bogus_field=1)
    with pytest.raises(ValidationError):
        CompetitorVideoAnalysis(
            source={"kind": "attached_file", "asset_id": "x",
                    "sha256": "d" * 64},
            duration_s=10.0,
            shots=[{"start_s": 0.0, "end_s": 5.0,
                    "visual_class": "stolen", "confidence": 0.5}],
        )


def test_profile_schema_id_and_no_copy_manifest_is_literal_false():
    profile = synthesize_style_profile([make_analysis()], "chan-1")
    assert profile.schema_version == "style_profile.v1"
    assert profile.no_copy_manifest.contains_competitor_assets is False
    assert profile.no_copy_manifest.never_reuse_asset_ids == ["comp-1"]
    assert profile.no_copy_manifest.never_reuse_sha256 == ["a" * 64]
    with pytest.raises(ValidationError):
        StyleProfile(channel_id="chan-1",
                     no_copy_manifest={"contains_competitor_assets": True})


# ---------------------------------------------------------------------------
# Synthesis — single analysis
# ---------------------------------------------------------------------------


def test_synthesize_single_analysis():
    profile = synthesize_style_profile([make_analysis()], "chan-1")
    assert profile.channel_id == "chan-1"
    assert profile.video_count == 1
    assert profile.duration.median_s == 600.0
    assert profile.duration.p25_s == 600.0
    assert profile.duration.p75_s == 600.0
    assert profile.duration.target_s == 600.0
    assert profile.speech.wpm_median == 150.0
    assert profile.speech.language == "en"
    assert profile.hook.window_s == 8.0
    assert profile.hook.types == ["question"]
    assert profile.chapters.target_count == 6
    assert profile.chapters.avg_chapter_s == 100.0
    # 400s stock + 200s AI of 600s total shot time.
    assert profile.visuals.stock_ratio == pytest.approx(0.667, abs=1e-3)
    assert profile.visuals.ai_ratio == pytest.approx(0.333, abs=1e-3)
    assert profile.visuals.avg_shot_len_s == 10.0
    assert profile.visuals.cuts_per_min == 6.0
    assert profile.captions.burned_in is True
    assert profile.music.expected is True
    assert profile.thumbnail.layout_class == "face_right_text_left"
    assert profile.thumbnail.text_words_min == 4
    assert profile.thumbnail.text_words_max == 4
    assert profile.thumbnail.face_expected is True
    assert profile.provenance.source_asset_ids == ["comp-1"]


def test_synthesize_requires_at_least_one_analysis():
    with pytest.raises(ValueError):
        synthesize_style_profile([], "chan-1")


# ---------------------------------------------------------------------------
# Synthesis — three analyses
# ---------------------------------------------------------------------------


def test_synthesize_three_analyses_aggregates():
    profile = synthesize_style_profile(three_analyses(), "chan-9")
    assert profile.video_count == 3
    assert profile.duration.median_s == 600.0
    assert profile.duration.p25_s == 570.0
    assert profile.duration.p75_s == 630.0
    assert profile.duration.target_s == 600.0
    assert profile.speech.wpm_median == 150.0
    assert profile.hook.window_s == 8.0
    assert profile.hook.types == ["question", "bold_claim"]
    assert profile.chapters.target_count == 6
    # Stock 1060s / AI 740s of 1800s total; 180 shots over 30 minutes.
    assert profile.visuals.stock_ratio == pytest.approx(0.589, abs=1e-3)
    assert profile.visuals.ai_ratio == pytest.approx(0.411, abs=1e-3)
    assert profile.visuals.cuts_per_min == 6.0
    # 2 of 3 burn captions in; 2 of 3 carry music.
    assert profile.captions.burned_in is True
    assert profile.music.expected is True
    assert profile.thumbnail.layout_class == "face_right_text_left"
    assert profile.thumbnail.text_words_min == 3
    assert profile.thumbnail.text_words_max == 5
    assert profile.thumbnail.face_expected is True
    assert profile.thumbnail.palette_class == "high_contrast_warm"
    assert profile.provenance.source_asset_ids == [
        "comp-1", "comp-2", "comp-3"]


# ---------------------------------------------------------------------------
# Pattern extraction: titles + descriptions
# ---------------------------------------------------------------------------


def test_title_formula_features_from_three_titles():
    formula = synthesize_style_profile(
        three_analyses(), "chan-9").title_formula
    assert formula.len_min == len(TITLE_3)
    assert formula.len_max == len(TITLE_2)
    # Titles 1 and 3 carry a digit; titles 1 and 3 carry a bracket/pipe.
    assert formula.number_rate == pytest.approx(2 / 3, abs=1e-3)
    assert formula.bracket_rate == pytest.approx(2 / 3, abs=1e-3)
    # Only "SHOCKING" is an ALL-CAPS word: (0 + 0 + 1/5) / 3.
    assert formula.caps_rate == pytest.approx(0.2 / 3, abs=1e-3)
    # Shared topic word "Facts" sits mid-title in the aggregate vote.
    assert formula.keyword_position == "middle"


def test_title_formula_single_title_example():
    formula = synthesize_style_profile(
        [make_analysis()], "chan-1").title_formula
    assert formula.len_min == formula.len_max == len(TITLE_1)
    assert formula.number_rate == 1.0   # "Top 10 ..."
    assert formula.bracket_rate == 1.0  # the "|" separator
    assert formula.caps_rate == 0.0
    assert formula.keyword_position in ("start", "middle", "end")


def test_description_structure_single():
    structure = synthesize_style_profile(
        [make_analysis()], "chan-1").description_structure
    assert structure.sections == [
        "intro", "chapters", "links", "cta", "hashtags"]
    assert structure.hashtag_min == 3
    assert structure.hashtag_max == 3
    assert structure.link_expected is True


def test_description_structure_three():
    structure = synthesize_style_profile(
        three_analyses(), "chan-9").description_structure
    assert structure.sections[0] == "intro"
    assert set(structure.sections) == {
        "intro", "chapters", "links", "cta", "hashtags"}
    assert structure.hashtag_min == 1
    assert structure.hashtag_max == 3
    # Only 1 of 3 descriptions carries a link.
    assert structure.link_expected is False


# ---------------------------------------------------------------------------
# Graceful missing SEO
# ---------------------------------------------------------------------------


def test_missing_seo_texts_yield_null_patterns_not_crash():
    bare = make_analysis(seo={"title_text": None,
                              "description_text": None})
    profile = synthesize_style_profile([bare], "chan-1")
    assert profile.title_formula.len_min is None
    assert profile.title_formula.number_rate is None
    assert profile.title_formula.keyword_position is None
    assert profile.description_structure.sections == []
    assert profile.description_structure.hashtag_min is None
    assert profile.description_structure.link_expected is False
    # Non-SEO aggregation is unaffected.
    assert profile.duration.target_s == 600.0


# ---------------------------------------------------------------------------
# No-copy enforcement
# ---------------------------------------------------------------------------


def test_synthesized_profile_contains_no_title_or_description_text():
    profile = synthesize_style_profile(three_analyses(), "chan-9")
    blob = profile.model_dump_json()
    for title in (TITLE_1, TITLE_2, TITLE_3):
        assert title not in blob
    # The shared topic keyword itself is never stored either.
    assert "Facts" not in blob
    assert_no_copy(profile, three_analyses())  # must not raise


def test_assert_no_copy_catches_poisoned_profile_shingle():
    analyses = three_analyses()
    clean = synthesize_style_profile(analyses, "chan-9")
    # A 4-word shingle of TITLE_1 smuggled into a pattern field.
    poisoned = clean.model_copy(update={
        "hook": HookStats(window_s=8.0, types=["top 10 facts you"]),
    })
    with pytest.raises(NoCopyViolationError):
        assert_no_copy(poisoned, analyses)


def test_assert_no_copy_catches_verbatim_description():
    analyses = [make_analysis()]
    clean = synthesize_style_profile(analyses, "chan-1")
    poisoned = clean.model_copy(update={
        "hook": HookStats(window_s=8.0, types=[DESC_2]),
    })
    other = [make_analysis(seo={"title_text": TITLE_2,
                                 "description_text": DESC_2})]
    with pytest.raises(NoCopyViolationError):
        assert_no_copy(poisoned, other)


# ---------------------------------------------------------------------------
# Directives
# ---------------------------------------------------------------------------


def test_style_directives_shape_and_values():
    profile = synthesize_style_profile(three_analyses(), "chan-9")
    directives = style_directives(profile)
    assert set(directives) == {
        "target_duration_s", "speech_wpm", "hook_window_s", "hook_types",
        "chapters_target", "avg_chapter_s", "stock_ratio", "ai_ratio",
        "avg_shot_len_s", "cuts_per_min", "captions_burned_in",
        "music_expected", "thumbnail", "title_formula",
        "description_structure",
    }
    assert set(directives["thumbnail"]) == {
        "layout_class", "text_words_min", "text_words_max",
        "face_expected", "palette_class"}
    assert set(directives["title_formula"]) == {
        "len_min", "len_max", "number_rate", "bracket_rate", "caps_rate",
        "keyword_position"}
    assert set(directives["description_structure"]) == {
        "sections", "hashtag_min", "hashtag_max", "link_expected"}
    assert directives["target_duration_s"] == 600.0
    assert directives["speech_wpm"] == 150.0
    assert directives["hook_types"] == ["question", "bold_claim"]
    assert directives["chapters_target"] == 6
    assert directives["captions_burned_in"] is True
    assert directives["thumbnail"]["layout_class"] == (
        "face_right_text_left")
    assert directives["title_formula"]["keyword_position"] == "middle"


def test_style_directives_none_profile_is_empty():
    assert style_directives(None) == {}


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------


def test_save_load_round_trip(tmp_path):
    profile = synthesize_style_profile(three_analyses(), "chan-9")
    path = save_profile(profile, tmp_path / "nested" / "profile.json")
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert '"style_profile.v1"' in text
    loaded = load_profile(path)
    assert loaded == profile
    assert style_directives(loaded) == style_directives(profile)


# ---------------------------------------------------------------------------
# Resolver precedence
# ---------------------------------------------------------------------------


def _spec(channel_id="chan-1", style_profile_path=None):
    return SimpleNamespace(channel_id=channel_id,
                           style_profile_path=style_profile_path)


def _write_profile(tmp_path, name, channel_id):
    profile = synthesize_style_profile([make_analysis()], channel_id)
    return save_profile(profile, tmp_path / name)


def test_resolver_explicit_path_wins(tmp_path, monkeypatch):
    explicit = _write_profile(tmp_path, "explicit.json", "from-explicit")
    channel_file = tmp_path / "style_profiles" / "chan-1.json"
    save_profile(synthesize_style_profile([make_analysis()],
                                           "from-channel"), channel_file)
    env_file = _write_profile(tmp_path, "env.json", "from-env")
    monkeypatch.setenv("YTA_STYLE_PROFILE_PATH", str(env_file))
    resolved = resolve_style_profile(
        _spec(style_profile_path=str(explicit)), tmp_path)
    assert resolved is not None
    assert resolved.channel_id == "from-explicit"


def test_resolver_channel_file_beats_env(tmp_path, monkeypatch):
    channel_file = tmp_path / "style_profiles" / "chan-1.json"
    save_profile(synthesize_style_profile([make_analysis()],
                                           "from-channel"), channel_file)
    env_file = _write_profile(tmp_path, "env.json", "from-env")
    monkeypatch.setenv("YTA_STYLE_PROFILE_PATH", str(env_file))
    resolved = resolve_style_profile(_spec(), tmp_path)
    assert resolved is not None
    assert resolved.channel_id == "from-channel"


def test_resolver_env_fallback(tmp_path, monkeypatch):
    env_file = _write_profile(tmp_path, "env.json", "from-env")
    monkeypatch.setenv("YTA_STYLE_PROFILE_PATH", str(env_file))
    resolved = resolve_style_profile(_spec(channel_id="chan-x"), tmp_path)
    assert resolved is not None
    assert resolved.channel_id == "from-env"


def test_resolver_nothing_configured_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("YTA_STYLE_PROFILE_PATH", raising=False)
    assert resolve_style_profile(_spec(), tmp_path) is None


def test_resolver_explicit_missing_path_fails_loud(tmp_path, monkeypatch):
    monkeypatch.delenv("YTA_STYLE_PROFILE_PATH", raising=False)
    with pytest.raises(FileNotFoundError):
        resolve_style_profile(
            _spec(style_profile_path=str(tmp_path / "nope.json")),
            tmp_path)
