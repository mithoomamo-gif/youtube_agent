"""Unit tests for app.integration.stock_query.build_query_ladder.

Pure-function tests only: no network, no keys, no I/O.  Scenes are fed
as real ``SceneRecord`` objects, dicts and plain namespaces; profiles
as real built-in ``NicheProfile`` objects, dicts and namespaces.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.integration.stock_query import URDU_VISUAL_MAP, build_query_ladder
from app.niches.builtins import get_profile
from app.pipeline.scenes.models import SceneClass, SceneRecord, SourceKind


def _minimal_profile(**overrides):
    """A profile with a name but NO visual vocabulary / signature."""
    data = dict(name="Test Niche", profile_id="test_niche", visual_style="")
    data.update(overrides)
    return SimpleNamespace(**data)


def _scene(text: str) -> SimpleNamespace:
    return SimpleNamespace(source_sentence=text)


# ---------------------------------------------------------------------------
# Step 1 -- Roman Urdu -> English
# ---------------------------------------------------------------------------

def test_roadmap_example_samundar_gehrai():
    """Section 24 example narration -> English exact visual query."""
    profile = get_profile("facts")
    scene = _scene("samundar ki gehrai mein roshni khatm ho jati hai")
    ladder = build_query_ladder(scene, profile)
    assert ladder[0] == "ocean depth light"
    assert "ocean" in ladder[0] and "depth" in ladder[0]
    # Step 2 broadens to subject + next subject noun (facts has no mood).
    assert ladder == ["ocean depth light", "ocean light"]


@pytest.mark.parametrize(
    "urdu,english",
    [
        ("samundar", "ocean"),
        ("gehrai", "depth"),
        ("machli", "fish"),
        ("roshni", "light"),
        ("andheraa", "darkness"),
        ("pahar", "mountain"),
        ("barish", "rain"),
        ("jungle", "forest"),
        ("sheher", "city"),
        ("gaon", "village"),
        ("khana", "food"),
        ("kitaab", "book"),
        ("sitare", "stars"),
        ("chaand", "moon"),
        ("sooraj", "sun"),
    ],
)
def test_roman_urdu_dictionary_words(urdu, english):
    assert URDU_VISUAL_MAP[urdu] == english
    ladder = build_query_ladder(_scene(urdu), _minimal_profile())
    assert ladder == [english]


def test_roman_urdu_phrase_maps_and_drops_filler():
    ladder = build_query_ladder(
        _scene("pahar par barish ho rahi hai"), _minimal_profile()
    )
    assert ladder[0] == "mountain rain"


def test_roman_urdu_and_english_mixed():
    ladder = build_query_ladder(
        _scene("samundar mein big waves"), _minimal_profile()
    )
    assert ladder[0] == "ocean big waves"


# ---------------------------------------------------------------------------
# Step 1 -- English passthrough
# ---------------------------------------------------------------------------

def test_english_description_passthrough():
    ladder = build_query_ladder(
        _scene("Deep ocean waves crashing at sunset"), _minimal_profile()
    )
    assert ladder[0] == "deep ocean waves crashing sunset"


def test_english_stopwords_removed():
    ladder = build_query_ladder(
        _scene("the light in the darkness"), _minimal_profile()
    )
    assert ladder[0] == "light darkness"


def test_queries_are_lowercase():
    ladder = build_query_ladder(
        _scene("MOUNTAIN Rain FOREST"), _minimal_profile()
    )
    assert ladder
    assert all(query == query.lower() for query in ladder)


# ---------------------------------------------------------------------------
# Empty / garbage input -> never-empty fallback
# ---------------------------------------------------------------------------

def test_empty_description_falls_back_to_niche_background():
    profile = _minimal_profile(name="Ocean Stories", profile_id="ocean_stories")
    assert build_query_ladder({"source_sentence": "!!! ???"}, profile) == [
        "ocean stories background"
    ]
    assert build_query_ladder(_scene(""), profile) == [
        "ocean stories background"
    ]


def test_filler_only_description_is_garbage_and_falls_back():
    profile = _minimal_profile(name="Ocean Stories", profile_id="ocean_stories")
    ladder = build_query_ladder(_scene("hai mein ki ka ke"), profile)
    assert ladder == ["ocean stories background"]


def test_urdu_script_description_falls_back():
    # Urdu *script* has no Roman tokens to map; nothing extractable.
    profile = _minimal_profile(name="Ocean Stories", profile_id="ocean_stories")
    ladder = build_query_ladder({"source_sentence": "سمندر کی گہرائی"}, profile)
    assert ladder == ["ocean stories background"]


def test_none_scene_and_profile_still_returns_a_query():
    assert build_query_ladder(None, None) == ["nature background"]


def test_fallback_uses_other_niche_name():
    profile = _minimal_profile(
        name="cricket analysis", profile_id="other_cricket_analysis"
    )
    ladder = build_query_ladder(_scene("hai mein ki"), profile)
    assert ladder == ["cricket analysis background"]


def test_builtin_facts_empty_scene_fallback():
    # The facts visual_style carries no signature phrase, so an empty
    # scene lands on the name fallback ("Facts / Curiosity" -> head).
    ladder = build_query_ladder(_scene(""), get_profile("facts"))
    assert ladder == ["facts background"]


# ---------------------------------------------------------------------------
# Brand stripping + people's names
# ---------------------------------------------------------------------------

def test_brand_names_are_stripped():
    ladder = build_query_ladder(
        _scene("coca cola bottle and nike shoes on table"), _minimal_profile()
    )
    assert ladder[0] == "bottle shoes table"
    joined = " ".join(ladder)
    for brand in ("coca", "cola", "nike", "pepsi"):
        assert brand not in joined.split()


def test_single_token_brand_stripped():
    ladder = build_query_ladder(_scene("pepsi can on a beach"), _minimal_profile())
    assert ladder[0] == "beach"
    assert "pepsi" not in " ".join(ladder)


def test_peoples_names_are_stripped():
    ladder = build_query_ladder(
        _scene("Ahmed Khan went to the city market"), _minimal_profile()
    )
    assert ladder[0] == "city market"
    joined = " ".join(ladder).split()
    assert "ahmed" not in joined and "khan" not in joined


def test_capitalised_name_pair_is_stripped():
    ladder = build_query_ladder(
        _scene("the story of John Smith in a small village"),
        _minimal_profile(),
    )
    joined = " ".join(ladder).split()
    assert "john" not in joined and "smith" not in joined
    assert "village" in joined


# ---------------------------------------------------------------------------
# Dedup + shape rules
# ---------------------------------------------------------------------------

def test_repeated_words_deduped_within_and_across_queries():
    ladder = build_query_ladder(
        _scene("ocean ocean ocean waves waves"), _minimal_profile()
    )
    assert ladder == ["ocean waves"]


def test_single_subject_scene_dedupes_step2():
    ladder = build_query_ladder(_scene("ocean"), _minimal_profile())
    assert ladder == ["ocean"]


def test_queries_capped_at_six_words_and_three_queries():
    text = (
        "ocean mountain forest river desert city village garden "
        "valley island"
    )
    ladder = build_query_ladder(_scene(text), get_profile("islamic_stories"))
    assert 1 <= len(ladder) <= 3
    assert all(1 <= len(query.split()) <= 6 for query in ladder)
    assert ladder[0] == "ocean mountain forest river desert city"
    assert len(ladder) == len(set(ladder))


# ---------------------------------------------------------------------------
# Step 2 / step 3 -- niche profile vocabulary
# ---------------------------------------------------------------------------

def test_step2_uses_profile_mood_word():
    profile = get_profile("horror_stories")
    ladder = build_query_ladder(_scene("jungle mein andhera tha"), profile)
    # Step 1 exact, step 2 subject + horror mood ("dark"), step 3 the
    # horror signature phrase (scene was very short: 2 content words).
    assert ladder == ["forest darkness", "forest dark", "dark scenes"]


def test_signature_only_when_scene_is_short():
    profile = get_profile("islamic_stories")
    ladder = build_query_ladder(_scene("chaand"), profile)
    assert ladder == ["moon", "moon soft", "islamic architecture"]


def test_signature_absent_for_full_scene():
    profile = get_profile("islamic_stories")
    ladder = build_query_ladder(
        _scene("masjid mein roshni aur chaand"), profile
    )
    assert ladder == ["mosque light moon", "mosque soft"]
    assert "islamic architecture" not in ladder


def test_empty_scene_with_signature_profile_uses_signature():
    ladder = build_query_ladder(_scene(""), get_profile("islamic_stories"))
    assert ladder == ["islamic architecture"]


def test_dict_scene_and_dict_profile():
    ladder = build_query_ladder(
        {"narration_text": "barish jungle mein"},
        {"name": "Nature", "visual_style": "green forest"},
    )
    assert ladder[0] == "rain forest"
    assert ladder == ["rain forest", "green forest"]


# ---------------------------------------------------------------------------
# Real contract objects
# ---------------------------------------------------------------------------

def test_real_scene_record_and_builtin_profile():
    scene = SceneRecord(
        scene_id="scene_0001",
        source_sentence="samundar ki gehrai mein roshni hai",
        start_s=0.0,
        end_s=5.0,
        scene_class=SceneClass.REAL_WORLD,
        chosen_source=SourceKind.STOCK,
    )
    ladder = build_query_ladder(scene, get_profile("facts"))
    assert ladder[0] == "ocean depth light"


def test_scene_record_visual_fields_win_over_missing_ones():
    # A record whose stock_query was pre-set still ladders from its
    # narration when no dedicated visual description exists.
    scene = SceneRecord(
        scene_id="scene_0002",
        source_sentence="barish aur jungle",
        start_s=0.0,
        end_s=4.0,
        scene_class=SceneClass.REAL_WORLD,
        chosen_source=SourceKind.STOCK,
    )
    ladder = build_query_ladder(scene, _minimal_profile())
    assert ladder[0] == "rain forest"


def test_other_profile_from_builder_smoke():
    from app.niches.builder import build_other_profile

    profile = build_other_profile(
        "cricket analysis", "matches ki kahani aur stats urdu mein"
    )
    ladder = build_query_ladder(_scene(""), profile)
    assert 1 <= len(ladder) <= 3
    assert all(query and query == query.lower() for query in ladder)


# ---------------------------------------------------------------------------
# Invariants across a batch of inputs
# ---------------------------------------------------------------------------

_BATCH_CASES = [
    ("samundar ki gehrai mein roshni khatm ho jati hai", "facts"),
    ("jungle mein andhera tha", "horror_stories"),
    ("chaand", "islamic_stories"),
    ("", "history"),
    ("hai mein ki", "tech"),
    ("deep ocean waves crashing at sunset", "motivation"),
    ("coca cola bottle and nike shoes", "health"),
    ("Ahmed Khan went to the city market", "crime_stories"),
]


def test_invariants_across_batch():
    for text, profile_id in _BATCH_CASES:
        ladder = build_query_ladder(_scene(text), get_profile(profile_id))
        assert 1 <= len(ladder) <= 3, (text, ladder)
        assert len(ladder) == len(set(ladder)), (text, ladder)
        for query in ladder:
            assert query and query == query.lower()
            assert len(query.split()) <= 6


def test_deterministic_repeat_calls():
    scene = _scene("jungle mein andhera tha")
    profile = get_profile("horror_stories")
    assert build_query_ladder(scene, profile) == build_query_ladder(scene, profile)
