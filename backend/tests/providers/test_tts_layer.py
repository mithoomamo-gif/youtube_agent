"""TTS layer tests — mocked providers only. No network, no real audio."""
import asyncio
import json

import pytest

from app.providers.tts import (
    PRESETS, BudgetPaused, CircuitBreaker, DictCache, GoogleTTSAdapter,
    OpenAITTSAdapter, PronunciationDictionary, PronunciationEntry,
    ProviderUnavailable, TTSEngine, TTSProvider, TTSRequest, TTSResult,
    VoiceProfile, check_silent_corruption, compute_cache_key, get_preset,
    normalize_text, resolve_chain, split_sentences,
)
from app.providers.tts.base import SilentCorruptionError, expected_duration_s
from app.providers.tts.chunking import chunk_chapter_text


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fake providers
# ---------------------------------------------------------------------------
class FakeProvider(TTSProvider):
    """Returns well-formed fake audio; duration matches the text length."""

    def __init__(self, name="edge", model="fake-1", cost=0.0,
                 fail_on_call=None, corrupt=False, always_fail=False):
        self.name = name
        self.model = model
        self.cost_per_1k_chars = cost
        self.calls = 0
        self.texts: list[str] = []
        self.fail_on_call = fail_on_call      # 1-based call index to fail on
        self.corrupt = corrupt
        self.always_fail = always_fail

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        self.calls += 1
        self.texts.append(req.text)
        if self.always_fail or (self.fail_on_call is not None
                                and self.calls >= self.fail_on_call):
            from app.providers.tts.base import ProviderError
            raise ProviderError(f"{self.name}: boom")
        duration = 0.5 if self.corrupt else expected_duration_s(
            req.text, req.rate_percent)
        return TTSResult(
            provider=self.name, model=self.model, voice_id=req.voice_id,
            billed_chars=len(req.text), duration_s=duration,
            audio_bytes=b"FAKE-AUDIO", cost_usd=self.estimate_cost(len(req.text)))

    async def list_voices(self, locale=None):
        return []


class DictSecrets:
    def __init__(self, values=None):
        self.values = values or {}

    async def get_secret(self, ref):
        return self.values.get(ref)


def make_engine(providers, **kw):
    kw.setdefault("retry_delays", (0.0, 0.0, 0.0))
    kw.setdefault("run_canary", False)
    return TTSEngine(providers, **kw)


def sentences(n, words=12, start=0):
    return " ".join(
        f"Sentence {i} " + " ".join(["story"] * words) + "."
        for i in range(start, start + n))


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
def test_chunking_never_exceeds_hard_cap():
    text = sentences(200)          # ~17k chars
    chunks = chunk_chapter_text(text)
    assert len(chunks) > 5
    assert all(len(c) <= 2800 for c in chunks)


def test_chunking_never_splits_mid_sentence():
    text = sentences(120)
    chunks = chunk_chapter_text(text)
    for chunk in chunks:
        assert chunk.rstrip().endswith(".")
    # Reassembled chunks contain exactly the original sentences.
    rejoined = split_sentences(" ".join(chunks))
    assert rejoined == split_sentences(text)


def test_chunking_targets_respected_on_sample_text():
    text = sentences(100)          # sentences ~85 chars each
    chunks = chunk_chapter_text(text)
    assert all(len(c) <= 1500 for c in chunks)
    # With uniform ~85-char sentences, packing should fill near the target.
    assert sum(len(c) for c in chunks) >= len(text) - len(chunks)
    assert len(chunks) >= 5


def test_single_overlong_sentence_still_capped():
    text = ("word " * 900).strip() + "."
    chunks = chunk_chapter_text(text)
    assert all(len(c) <= 2800 for c in chunks)


# ---------------------------------------------------------------------------
# Cache keys + dictionary version
# ---------------------------------------------------------------------------
def test_cache_key_changes_with_dict_version():
    base = dict(text="hello world", voice_id="v1", rate_percent=-10.0,
                pitch_hz=0.0, provider="edge", model="m1")
    k1 = compute_cache_key(dict_version="dict-1", **base)
    k2 = compute_cache_key(dict_version="dict-2", **base)
    assert k1 != k2
    assert k1 == compute_cache_key(dict_version="dict-1", **base)


def test_dictionary_version_changes_when_entry_changes():
    d1 = PronunciationDictionary([PronunciationEntry(surface="AI", alias="A I")])
    d2 = PronunciationDictionary([PronunciationEntry(surface="AI", alias="ay eye")])
    assert d1.version != d2.version


def test_engine_cache_hit_avoids_resynthesis():
    fake = FakeProvider()
    engine = make_engine({"edge": fake})
    script = sentences(5)
    profile = VoiceProfile(language="en", voice_id="en-US-GuyNeural")
    run(engine.synthesize_job(script, profile=profile, niche="facts"))
    calls_after_first = fake.calls
    assert calls_after_first > 0
    job2 = run(engine.synthesize_job(script, profile=profile, niche="facts"))
    assert fake.calls == calls_after_first          # all cache hits
    assert all(r.from_cache for r in job2.results)


# ---------------------------------------------------------------------------
# Chapter continuity: mid-chapter failure re-renders the WHOLE chapter
# ---------------------------------------------------------------------------
def test_mid_chapter_failure_rerenders_whole_chapter_on_fallback():
    primary = FakeProvider(name="edge", fail_on_call=2)   # 2nd chunk dies
    fallback = FakeProvider(name="azure", model="azure-fake")
    engine = make_engine({"edge": primary, "azure": fallback})
    text = sentences(60)                                 # -> several chunks
    profile = VoiceProfile(language="en", voice_id="en-US-GuyNeural")
    job = run(engine.synthesize_job(text, profile=profile, niche="facts"))

    assert job.providers_by_chapter == {1: "azure"}
    assert job.results, "expected rendered chunks"
    # No mixed voices in the chapter: every result is the fallback provider.
    assert all(r.provider == "azure" for r in job.results)
    # The whole chapter was re-rendered: fallback saw every chunk.
    assert fallback.calls == len(job.results)
    assert len(job.results) >= 3


def test_canary_failure_skips_provider_for_batch():
    bad = FakeProvider(name="edge", always_fail=True)
    good = FakeProvider(name="azure", model="azure-fake")
    engine = make_engine({"edge": bad, "azure": good}, run_canary=True)
    profile = VoiceProfile(language="en", voice_id="en-US-GuyNeural")
    job = run(engine.synthesize_job(sentences(3), profile=profile, niche="facts"))
    assert job.providers_by_chapter == {1: "azure"}


# ---------------------------------------------------------------------------
# Silent corruption
# ---------------------------------------------------------------------------
def test_silent_corruption_detected_on_short_audio():
    req = TTSRequest(text="x " * 450, voice_id="v")       # 900 chars
    result = TTSResult(provider="fake", model="m", voice_id="v",
                       billed_chars=900, duration_s=1.0, audio_bytes=b"a")
    with pytest.raises(SilentCorruptionError):
        check_silent_corruption(result, req)
    empty = TTSResult(provider="fake", model="m", voice_id="v",
                      billed_chars=900, duration_s=60.0, audio_bytes=b"")
    with pytest.raises(SilentCorruptionError):
        check_silent_corruption(empty, req)


def test_silent_corruption_counts_as_failure_and_falls_back():
    corrupt = FakeProvider(name="edge", corrupt=True)
    good = FakeProvider(name="azure", model="azure-fake")
    engine = make_engine({"edge": corrupt, "azure": good})
    profile = VoiceProfile(language="en", voice_id="en-US-GuyNeural")
    job = run(engine.synthesize_job(sentences(10), profile=profile, niche="facts"))
    assert job.providers_by_chapter == {1: "azure"}
    assert all(r.provider == "azure" for r in job.results)


# ---------------------------------------------------------------------------
# Fallback chains + Urdu / ElevenLabs audition gate
# ---------------------------------------------------------------------------
def test_urdu_chain_excludes_elevenlabs_without_audition():
    chain = resolve_chain("ur", VoiceProfile(language="ur",
                                             urdu_audition_passed=False))
    assert chain == ["edge", "azure", "google"]
    assert "elevenlabs" not in chain


def test_urdu_chain_includes_elevenlabs_after_audition():
    chain = resolve_chain("ur-PK", VoiceProfile(language="ur-PK",
                                                urdu_audition_passed=True))
    assert chain[-1] == "elevenlabs"


def test_english_and_hindi_chains():
    assert resolve_chain("en") == ["edge", "openai", "elevenlabs", "azure"]
    assert resolve_chain("hi") == ["edge", "azure", "google", "elevenlabs"]


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
def test_normalization_numbers_to_spoken_words_english():
    out = normalize_text(
        "In 1992 the crowd of 1,000 people paid $500, which was 50% of the goal.",
        "en")
    assert "nineteen ninety-two" in out.text
    assert "one thousand people" in out.text
    assert "five hundred dollars" in out.text
    assert "fifty percent" in out.text
    assert not any(ch.isdigit() for ch in out.text)


def test_normalization_abbreviations_dates_and_markup():
    out = normalize_text("**Dr. Smith** arrived on 2026-10-23 at 10:30. [music]", "en")
    assert "Doctor Smith" in out.text
    assert "October" in out.text and "twenty twenty-six" in out.text
    assert "ten thirty" in out.text
    assert "**" not in out.text and "[music]" not in out.text


def test_normalization_flags_latin_tokens_in_urdu_text():
    out = normalize_text("یہ کہانی WhatsApp پر بہت مشہور ہوئی۔", "ur")
    assert "WhatsApp" in out.flagged_tokens


def test_normalization_urdu_digits_become_words():
    out = normalize_text("سال ۱۹۹۲ میں یہ واقعہ ہوا۔", "ur")
    assert "۱۹۹۲" not in out.text
    assert "نوے" in out.text or "دو" in out.text   # spoken digit words present


# ---------------------------------------------------------------------------
# Pronunciation dictionary compilation
# ---------------------------------------------------------------------------
def _dict():
    return PronunciationDictionary([
        PronunciationEntry(surface="YouTube", alias="You Tube",
                           ipa="ˈjuːtjuːb", lang="en")])


def test_dictionary_alias_for_edge_and_subtitle_restoration():
    compiled = _dict().compile("Watch YouTube daily", "edge", "en")
    assert compiled.text == "Watch You Tube daily"
    assert compiled.subtitle_text() == "Watch YouTube daily"   # original kept


def test_dictionary_phoneme_for_azure_and_rules_for_elevenlabs():
    azure = _dict().compile("Watch YouTube daily", "azure", "en")
    assert "<phoneme" in azure.ssml and "ˈjuːtjuːb" in azure.ssml
    el = _dict().compile("Watch YouTube daily", "elevenlabs", "en")
    assert el.elevenlabs_dictionary["rules"][0]["type"] == "phoneme"
    google = _dict().compile("Watch YouTube daily", "google", "en")
    assert google.custom_pronunciations[0]["phrase"] == "YouTube"


def test_bundled_global_dictionary_loads():
    d = PronunciationDictionary.load_bundled("islamic_stories")
    surfaces = {e.surface for e in d.entries}
    assert "YouTube" in surfaces and "حضرت یوسف" in surfaces


# ---------------------------------------------------------------------------
# Reliability: circuit breaker, budget, stubs, secrets
# ---------------------------------------------------------------------------
def test_circuit_breaker_opens_after_three_failures():
    breaker = CircuitBreaker(threshold=3, open_seconds=900)
    for _ in range(3):
        breaker.record_failure("edge", "boom")
    assert breaker.is_open("edge")
    with pytest.raises(ProviderUnavailable):
        breaker.check("edge")
    breaker.record_success("azure")
    assert not breaker.is_open("azure")


def test_budget_cap_pauses_instead_of_spending():
    pricey = FakeProvider(name="edge", cost=1.0)   # $1 per 1k chars
    engine = make_engine({"edge": pricey}, budget_cap_usd=1.00)
    profile = VoiceProfile(language="en", voice_id="en-US-GuyNeural")
    with pytest.raises(BudgetPaused):
        run(engine.synthesize_job(sentences(80), profile=profile, niche="facts"))


def test_stubs_raise_provider_unavailable_never_succeed():
    for adapter in (GoogleTTSAdapter(), OpenAITTSAdapter()):
        assert adapter.implemented is False
        with pytest.raises(ProviderUnavailable):
            run(adapter.synthesize(TTSRequest(text="hi", voice_id="v")))
        assert run(adapter.canary()) is False


def test_azure_requires_secret_and_never_reads_env():
    from app.providers.tts import AzureSpeechAdapter
    adapter = AzureSpeechAdapter(DictSecrets({}), "azure-key")
    with pytest.raises(ProviderUnavailable):
        run(adapter.synthesize(TTSRequest(text="hi", voice_id="en-US-GuyNeural")))


def test_elevenlabs_cost_models():
    from app.providers.tts import ElevenLabsAdapter
    secrets = DictSecrets({"k": "x"})
    flash = ElevenLabsAdapter(secrets, "k", model="eleven_flash_v2_5")
    multi = ElevenLabsAdapter(secrets, "k", model="eleven_multilingual_v2")
    assert flash.estimate_cost(9000) == pytest.approx(0.36)
    assert multi.estimate_cost(9000) == pytest.approx(0.72)


# ---------------------------------------------------------------------------
# Presets + manifest
# ---------------------------------------------------------------------------
def test_presets_match_spec():
    assert PRESETS["facts"].chapter_pause_ms == 500
    assert PRESETS["islamic_stories"].rate_percent == -22.0
    assert PRESETS["islamic_stories"].pitch_hz == -8
    assert PRESETS["islamic_stories"].chapter_pause_ms == 900
    assert PRESETS["horror"].rate_percent == -26.0
    assert PRESETS["horror"].chapter_pause_ms == 1000
    assert PRESETS["motivation"].pitch_hz == 4
    assert PRESETS["other"].chapter_pause_ms == 650
    health = get_preset("health", segment_type="disclaimer",
                        channel_override=PRESETS["facts"])
    assert health.niche == "health"      # disclaimer forced to base preset


def test_manifest_written_with_stitch_gaps(tmp_path):
    fake = FakeProvider()
    engine = make_engine({"edge": fake})
    script = "# Chapter 1: Start\n" + sentences(30) + \
             "\n\n# Chapter 2: Next\n" + sentences(30, start=100)
    profile = VoiceProfile(language="en", voice_id="en-US-GuyNeural")
    job = run(engine.synthesize_job(script, profile=profile, niche="facts",
                                    output_dir=str(tmp_path)))
    assert job.manifest_path
    data = json.loads(open(job.manifest_path, encoding="utf-8").read())
    assert data["video_id"] == "video"
    assert data["chunks"], "manifest must list chunks"
    first = data["chunks"][0]
    for key in ("provider", "voice", "preset", "chars", "billed_cost",
                "duration_s", "cache_key", "status"):
        assert key in first
    plan = data["stitch_plan"]
    assert plan["sample_rate"] == 44100 and plan["channels"] == 1
    gaps = [i["gap_after_ms"] for i in plan["items"]]
    assert 500 in gaps                    # facts chapter pause between chapters
    assert gaps[-1] == 0
