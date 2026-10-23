"""Round 4 Worker 3 — pre-TTS Roman Urdu -> Urdu script transliteration.

Golden set + invariants: dictionary policy first (respell aliases and
naturalised Urdu surfaces exactly as the bundled dictionary specifies),
unknown-English passthrough + counting, idempotence on Urdu input,
Western digits / punctuation preserved, and the pipeline option
(ProjectSpec / VoiceProfile / voice-stage helper / TTSEngine): since
the R4-002 fix the ur-PK defaults resolve to urdu_script (the adopted
A/B winner) while explicit "roman" keeps the legacy path unchanged.
"""
from __future__ import annotations

import pytest

from app.providers.tts.dictionary import (PronunciationDictionary,
                                           PronunciationEntry)
from app.providers.tts.transliteration import (to_urdu_script,
                                               transliterate)

# ---------------------------------------------------------------------------
# Golden sentences
# ---------------------------------------------------------------------------
GOLDEN = [
    # Round 3 Phase-0 facts title line: lexicon + function words.
    ("Samundar ki gehrai ke raaz", "سمندر کی گہرائی کے راز"),
    # Dictionary-covered English term (global.yaml: کرکٹ / "cricket").
    ("Cricket ki kahani bahut dilchasp hai",
     "کرکٹ کی کہانی بہت دلچسپ ہے"),
    # Common narration verbs / adjectives via lexicon + rules.
    ("mushkil waqt mein insan ko sabar karna chahiye",
     "مشکل وقت میں انسان کو صبر کرنا چاہیے"),
    ("kitab mein ek ajeeb raaz chhupa tha",
     "کتاب میں ایک عجیب راز چھپا تھا"),
    # Dictionary phrase (global.yaml: حضرت یوسف / "Hazrat Yousuf").
    ("Hazrat Yousuf ki kahani", "حضرت یوسف کی کہانی"),
    # Mixed Urdu-script + Roman line: Urdu spans pass through unchanged.
    ("یہ kahani meri hai", "یہ کہانی میری ہے"),
]


@pytest.mark.parametrize("roman,urdu", GOLDEN)
def test_golden_sentences(roman, urdu):
    assert to_urdu_script(roman) == urdu


def test_golden_with_cricket_and_score_words():
    # "jeeta" etc via lexicon/rules; World Cup stays Latin (see below).
    assert to_urdu_script("Pakistan ne final jeeta") == \
        "پاکستان نے final جیتا"


# ---------------------------------------------------------------------------
# Idempotence / Urdu passthrough
# ---------------------------------------------------------------------------
def test_idempotent_on_urdu_script_input():
    urdu = "سمندر کی گہرائی کے راز، کرکٹ کی کہانی"
    assert to_urdu_script(urdu) == urdu


@pytest.mark.parametrize("roman,_urdu", GOLDEN)
def test_idempotent_on_own_output(roman, _urdu):
    once = to_urdu_script(roman)
    assert to_urdu_script(once) == once


def test_idempotent_on_dictionary_respell_output():
    # Respelt aliases are finished dictionary text: a second pass must
    # not transliterate "You Tube" / "A I" into Urdu.
    once = to_urdu_script("YouTube par AI ka istemal hai")
    assert once == "You Tube پر A I کا استعمال ہے"
    assert to_urdu_script(once) == once


# ---------------------------------------------------------------------------
# Digits / punctuation preservation
# ---------------------------------------------------------------------------
def test_western_digits_kept_as_is():
    out = to_urdu_script("1992 mein Pakistan ne World Cup jeeta")
    assert out == "1992 میں پاکستان نے World Cup جیتا"
    assert "1992" in out


def test_punctuation_preserved():
    out = to_urdu_script("Kya aap tayyar hain? Zaroor, bilkul!")
    assert out == "کیا آپ تیار ہیں? ضرور, بالکل!"


# ---------------------------------------------------------------------------
# Dictionary policy: respell aliases exactly as specified, never guessed
# ---------------------------------------------------------------------------
def test_dictionary_respelling_alias_global():
    result = transliterate("YouTube par video dekho")
    assert result.text == "You Tube پر video دیکھو"
    assert "YouTube" in result.dictionary_hits


def test_dictionary_respelling_alias_niche():
    # facts.yaml: NASA -> "Nassa"
    result = transliterate("NASA ne kaha", niche="facts")
    assert result.text == "Nassa نے کہا"
    # tech.yaml: GPT -> "G P T"
    assert to_urdu_script("GPT", niche="tech") == "G P T"
    # health.yaml: paracetamol -> "para seat a moll" (multiword alias)
    assert to_urdu_script("paracetamol lena chahiye", niche="health") == \
        "para seat a moll لینا چاہیے"


def test_dictionary_naturalised_urdu_surface_cricket():
    result = transliterate("cricket")
    assert result.text == "کرکٹ"
    assert result.dictionary_hits == ["cricket"]


def test_caller_dictionary_overrides_and_merges():
    custom = PronunciationDictionary(entries=[
        PronunciationEntry(surface="streaming", alias="اسٹریمنگ", lang="en"),
    ])
    # Caller entry wins / merges over the bundled global layer: here the
    # alias itself is Urdu script, used verbatim as the replacement.
    out = transliterate("streaming shuru hui", dictionary=custom)
    assert out.text == "اسٹریمنگ شروع ہوئی"
    assert "streaming" in out.dictionary_hits
    # Bundled global entries still apply alongside the caller dictionary.
    out2 = transliterate("cricket streaming", dictionary=custom)
    assert out2.text == "کرکٹ اسٹریمنگ"


# ---------------------------------------------------------------------------
# Unknown English: passthrough + counting
# ---------------------------------------------------------------------------
def test_unknown_english_passthrough_and_counted():
    result = transliterate("1992 mein Pakistan ne World Cup jeeta")
    assert result.unknown_tokens == ["World", "Cup"]
    assert result.unknown_token_count == 2
    assert "World Cup" in result.text


def test_unknown_english_patterns_counted():
    result = transliterate("yeh information video hai")
    assert "information" in result.unknown_tokens
    assert "video" in result.unknown_tokens
    assert result.text == "یہ information video ہے"


def test_no_unknowns_for_pure_roman_urdu():
    result = transliterate("Samundar ki gehrai ke raaz")
    assert result.unknown_tokens == []
    assert result.transliterated_tokens == 5


# ---------------------------------------------------------------------------
# Pipeline option: config surface + defaults
# ---------------------------------------------------------------------------
def test_project_spec_tts_input_script_default_and_validation():
    from app.integration.models import ProjectSpec

    # R4-002 adopted default: Urdu productions (the spec default
    # language) default to urdu_script; non-Urdu stays roman; an
    # explicit choice either way is honoured.
    spec = ProjectSpec(topic="x")
    assert spec.tts_input_script == "urdu_script"
    assert ProjectSpec(topic="x", language="en").tts_input_script == "roman"
    assert ProjectSpec(
        topic="x", voice_id="en-US-GuyNeural").tts_input_script == "roman"
    assert ProjectSpec(
        topic="x", language="en",
        voice_id="ur-PK-UzmaNeural").tts_input_script == "urdu_script"
    spec2 = ProjectSpec(topic="x", tts_input_script="urdu_script")
    assert spec2.tts_input_script == "urdu_script"
    assert ProjectSpec(
        topic="x", tts_input_script="roman").tts_input_script == "roman"
    with pytest.raises(Exception):
        ProjectSpec(topic="x", tts_input_script="arabic")


def test_voice_profile_tts_input_script_default():
    from app.providers.tts.fallback import VoiceProfile

    # R4-002 adopted default: ur-PK profiles resolve to urdu_script,
    # non-Urdu profiles to roman, explicit values are honoured.
    assert VoiceProfile(language="ur").tts_input_script == "urdu_script"
    assert VoiceProfile(
        language="ur", voice_id="ur-PK-AsadNeural").tts_input_script \
        == "urdu_script"
    assert VoiceProfile(language="en").tts_input_script == "roman"
    assert VoiceProfile(
        language="en", voice_id="en-US-GuyNeural").tts_input_script \
        == "roman"
    assert VoiceProfile(
        language="ur", tts_input_script="urdu_script").tts_input_script \
        == "urdu_script"
    assert VoiceProfile(
        language="ur", tts_input_script="roman").tts_input_script == "roman"


def test_voice_stage_prepare_texts_urdu_opt_in():
    from app.integration.voice_stage import _prepare_tts_texts

    spec = {"tts_input_script": "urdu_script"}
    texts, used, unknown, hits = _prepare_tts_texts(
        ["Cricket ki kahani"], {}, spec, {}, "ur", "ur-PK-AsadNeural")
    assert texts == ["کرکٹ کی کہانی"]
    assert used == "urdu_script"
    assert unknown == []
    assert hits == ["Cricket"]


def test_voice_stage_prepare_texts_default_roman_unchanged():
    from app.integration.voice_stage import _prepare_tts_texts

    texts, used, unknown, hits = _prepare_tts_texts(
        ["Cricket ki kahani"], {}, {}, {}, "ur", "ur-PK-AsadNeural")
    assert texts == ["Cricket ki kahani"]
    assert used == "roman"
    assert unknown == [] and hits == []


def test_voice_stage_prepare_texts_not_applied_for_english_voice():
    from app.integration.voice_stage import _prepare_tts_texts

    spec = {"tts_input_script": "urdu_script"}
    texts, used, _u, _h = _prepare_tts_texts(
        ["This is a story"], {}, spec, {}, "en", "en-US-GuyNeural")
    assert texts == ["This is a story"]
    assert used == "roman"


def test_engine_prepare_transliterates_chunks_keeps_original():
    from app.providers.tts.engine import TTSEngine
    from app.providers.tts.fallback import VoiceProfile
    from app.providers.tts.presets import get_preset

    engine = TTSEngine({})
    profile = VoiceProfile(language="ur", voice_id="ur-PK-AsadNeural",
                           tts_input_script="urdu_script")
    prepared, _flagged = engine.prepare(
        "Samundar ki gehrai ke raaz hai.",
        video_id="v1", profile=profile, preset=get_preset("facts"))
    chunk = prepared[0].chunks[0]
    # Synthesised text is Urdu script; original (subtitle source) is not.
    assert chunk.text == "سمندر کی گہرائی کے راز ہے."
    assert chunk.original_text == "Samundar ki gehrai ke raaz hai."
    assert engine.last_transliteration["tts_input_script"] == "urdu_script"


def test_engine_prepare_explicit_roman_unchanged():
    # An explicit per-channel "roman" choice keeps the legacy path
    # byte-identical (R4-002: the override still works).
    from app.providers.tts.engine import TTSEngine
    from app.providers.tts.fallback import VoiceProfile
    from app.providers.tts.presets import get_preset

    engine = TTSEngine({})
    profile = VoiceProfile(language="ur", voice_id="ur-PK-AsadNeural",
                           tts_input_script="roman")
    prepared, _flagged = engine.prepare(
        "Samundar ki gehrai ke raaz hai.",
        video_id="v1", profile=profile, preset=get_preset("facts"))
    chunk = prepared[0].chunks[0]
    assert chunk.text == chunk.original_text
    assert engine.last_transliteration["tts_input_script"] == "roman"


def test_engine_prepare_default_ur_profile_uses_urdu_script():
    # R4-002 adopted default: a ur-PK VoiceProfile with NO explicit
    # tts_input_script now transliterates by default.
    from app.providers.tts.engine import TTSEngine
    from app.providers.tts.fallback import VoiceProfile
    from app.providers.tts.presets import get_preset

    engine = TTSEngine({})
    profile = VoiceProfile(language="ur", voice_id="ur-PK-AsadNeural")
    prepared, _flagged = engine.prepare(
        "Samundar ki gehrai ke raaz hai.",
        video_id="v1", profile=profile, preset=get_preset("facts"))
    chunk = prepared[0].chunks[0]
    assert chunk.text == "سمندر کی گہرائی کے راز ہے."
    assert chunk.original_text == "Samundar ki gehrai ke raaz hai."
    assert engine.last_transliteration["tts_input_script"] == "urdu_script"


def test_package_level_export():
    from app.providers.tts import to_urdu_script as exported

    assert exported("ki kahani") == "کی کہانی"
