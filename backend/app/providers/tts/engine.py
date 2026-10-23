"""TTSEngine — orchestrates normalization, dictionary compilation, chunking,
caching, retries, circuit breaking, fallback chains, budget caps and the
voice manifest.

Chapter continuity (hard rule): ONE chapter = ONE provider + voice + preset.
If a provider fails mid-chapter, the WHOLE chapter is re-rendered on the
next provider in the chain — mixed voices inside a chapter are a defect.
Fallback therefore happens at chapter granularity, never per chunk.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .base import (BudgetPaused, ProviderUnavailable, TTSProvider,
                   TTSRequest, TTSResult, check_silent_corruption)
from .cache import DictCache, compute_cache_key
from .chunking import Chapter, Chunk, chunk_chapter_text, parse_script
from .dictionary import PronunciationDictionary
from .fallback import VoiceProfile, resolve_chain
from .manifest import ManifestChunk, VoiceManifest, build_stitch_plan
from .normalization import normalize_text
from .presets import NichePreset, get_preset
from .reliability import (DEFAULT_RETRY_DELAYS, CircuitBreaker,
                          TokenBucketLimiter, call_with_retries)

DEFAULT_VIDEO_BUDGET_USD = 1.00


@dataclass
class JobResult:
    video_id: str
    chunks: list[Chunk] = field(default_factory=list)
    results: list[TTSResult] = field(default_factory=list)
    manifest: VoiceManifest | None = None
    manifest_path: str | None = None
    total_cost_usd: float = 0.0
    flagged_tokens: list[str] = field(default_factory=list)
    providers_by_chapter: dict[int, str] = field(default_factory=dict)
    #: Round 4 provenance: "roman" | "urdu_script" + unknown-token count.
    #: Placeholder default only (R4-002 adopted default): synthesize_job
    #: always overwrites it from prepare()'s actual per-profile
    #: resolution (ur-PK profile -> "urdu_script", non-Urdu or explicit
    #: "roman" -> "roman").
    tts_input_script: str = "urdu_script"
    transliteration_unknown_token_count: int = 0


@dataclass
class PreparedChapter:
    chapter: Chapter
    chunks: list[Chunk]


class TTSEngine:
    #: Provenance of the most recent prepare(): which TTS input script
    #: was used + transliteration stats (Round 4).  The class-level
    #: default is a pre-prepare placeholder naming the adopted R4-002
    #: default; prepare() overwrites it on every call with the actual
    #: per-profile resolution.
    last_transliteration: dict = {"tts_input_script": "urdu_script",
                                  "unknown_tokens": [],
                                  "unknown_token_count": 0,
                                  "dictionary_hits": []}

    def __init__(self, providers: dict[str, TTSProvider], *,
                 cache=None, dictionary: PronunciationDictionary | None = None,
                 budget_cap_usd: float = DEFAULT_VIDEO_BUDGET_USD,
                 breaker: CircuitBreaker | None = None,
                 limiter: TokenBucketLimiter | None = None,
                 retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
                 retry_attempts: int = 3,
                 run_canary: bool = True,
                 sleep=None):
        self.providers = dict(providers)
        self.cache = cache if cache is not None else DictCache()
        self.dictionary = dictionary or PronunciationDictionary()
        self.budget_cap_usd = budget_cap_usd
        self.breaker = breaker or CircuitBreaker()
        self.limiter = limiter or TokenBucketLimiter(
            {name: p.max_concurrency for name, p in self.providers.items()})
        self.retry_delays = retry_delays
        self.retry_attempts = retry_attempts
        self.run_canary = run_canary
        self._sleep = sleep or asyncio.sleep
        self.spent_usd = 0.0

    # -- preparation -------------------------------------------------------
    def prepare(self, script, *, video_id: str, profile: VoiceProfile,
                preset: NichePreset) -> tuple[list[PreparedChapter], list[str]]:
        """Normalize + chunk the locked script. Dictionary compilation is per
        provider and happens at render time (cache keys include provider)."""
        chapters = parse_script(script)
        dict_version = self.dictionary.version
        prepared: list[PreparedChapter] = []
        flagged_all: list[str] = []
        # Round 4 pre-TTS transliteration: when the channel's
        # VoiceProfile resolves to tts_input_script="urdu_script" for
        # an Urdu profile.  A real VoiceProfile resolves that field
        # voice-aware in __post_init__ (R4-002: ur-PK -> urdu_script
        # by default, explicit "roman" honoured); a duck-typed profile
        # without the field gets the same voice-aware default here.
        # Chunk boundaries + Chunk.original_text always stay on the
        # ORIGINAL (normalized) script — subtitles are built from
        # original_text; only Chunk.text (what is actually
        # synthesised) is transliterated, dictionary-first.
        _profile_script = getattr(profile, "tts_input_script", None)
        if not _profile_script:
            _profile_script = (
                "urdu_script"
                if profile.lang == "ur"
                or str(getattr(profile, "voice_id", "") or "")
                .strip().lower().startswith("ur-")
                else "roman")
        want_urdu_script = (
            str(_profile_script)
            .strip().lower().replace("-", "_") == "urdu_script"
            and profile.lang == "ur")
        translit_unknown: list[str] = []
        translit_hits: list[str] = []
        global_no = 0
        for chapter in chapters:
            normalized = normalize_text(chapter.text, profile.lang)
            for tok in normalized.flagged_tokens:
                if tok not in flagged_all:
                    flagged_all.append(tok)
            pieces = chunk_chapter_text(normalized.text)
            chunks: list[Chunk] = []
            for i, piece in enumerate(pieces):
                global_no += 1
                tts_text = piece
                if want_urdu_script:
                    from .transliteration import transliterate

                    res = transliterate(piece, dictionary=self.dictionary,
                                        niche=preset.niche)
                    tts_text = res.text
                    translit_unknown.extend(res.unknown_tokens)
                    translit_hits.extend(res.dictionary_hits)
                chunks.append(Chunk(
                    video_id=video_id, chapter_no=chapter.chapter_no,
                    chunk_no=global_no, chunk_no_in_chapter=i + 1,
                    original_text=piece, text=tts_text,
                    voice_id=profile.voice_id, preset=preset.niche,
                    dict_version=dict_version,
                    flagged_tokens=list(normalized.flagged_tokens)))
            prepared.append(PreparedChapter(chapter=chapter, chunks=chunks))
        self.last_transliteration = {
            "tts_input_script": "urdu_script" if want_urdu_script else "roman",
            "unknown_tokens": translit_unknown,
            "unknown_token_count": len(translit_unknown),
            "dictionary_hits": translit_hits,
        }
        return prepared, flagged_all

    # -- one chunk on one provider ----------------------------------------
    async def _render_chunk(self, chunk: Chunk, provider: TTSProvider,
                            profile: VoiceProfile,
                            preset: NichePreset) -> TTSResult:
        voice = (profile.voice_for(provider.name)
                 or provider.default_voice(profile.lang) or "default")
        compiled = self.dictionary.compile(chunk.text, provider.name,
                                           profile.lang)
        model = provider.model
        if provider.name == "elevenlabs" and profile.elevenlabs_model:
            model = profile.elevenlabs_model
        key = compute_cache_key(compiled.text, voice, preset.rate_percent,
                                preset.pitch_hz, chunk.dict_version,
                                provider.name, model)
        cached = self.cache.get(key)
        if cached is not None:
            cached.cache_key = key
            return cached

        req = TTSRequest(
            text=compiled.text, voice_id=voice, language=profile.lang,
            rate_percent=preset.rate_percent, pitch_hz=preset.pitch_hz,
            preset=preset.niche, model=model,
            ssml_text=compiled.ssml,
            custom_pronunciations=compiled.custom_pronunciations,
            pronunciation_dictionary=compiled.elevenlabs_dictionary)

        async def attempt() -> TTSResult:
            result = await self.limiter.run(
                provider.name, lambda: provider.synthesize(req))
            check_silent_corruption(result, req)
            return result

        result = await call_with_retries(
            attempt, attempts=self.retry_attempts, delays=self.retry_delays,
            sleep=self._sleep)
        result.cache_key = key
        if result.cost_usd == 0.0:
            result.cost_usd = provider.estimate_cost(result.billed_chars)
        self.cache.put(key, result)
        return result

    # -- one chapter, with whole-chapter fallback --------------------------
    async def _render_chapter(self, prepared: PreparedChapter,
                              chain: list[str], profile: VoiceProfile,
                              preset: NichePreset,
                              canary_ok: dict[str, bool]) -> tuple[list[TTSResult], str]:
        last_exc: Exception | None = None
        for name in chain:
            provider = self.providers.get(name)
            if provider is None or not canary_ok.get(name, False):
                continue
            try:
                self.breaker.check(name)
            except ProviderUnavailable:
                continue
            # Budget projection for re-rendering this whole chapter here.
            projected = self.spent_usd + sum(
                provider.estimate_cost(len(c.text)) for c in prepared.chunks)
            if projected > self.budget_cap_usd and provider.cost_per_1k_chars > 0:
                raise BudgetPaused(
                    f"projected narration cost ${projected:.2f} would breach "
                    f"per-video cap ${self.budget_cap_usd:.2f} on provider "
                    f"{name}; pausing instead of silently downgrading")
            try:
                results: list[TTSResult] = []
                for chunk in prepared.chunks:
                    results.append(await self._render_chunk(
                        chunk, provider, profile, preset))
                self.breaker.record_success(name)
                self.spent_usd += sum(r.cost_usd for r in results
                                      if not r.from_cache)
                return results, name
            except BudgetPaused:
                raise
            except Exception as exc:  # chapter fails -> whole chapter moves on
                last_exc = exc
                self.breaker.record_failure(name, str(exc))
                continue
        raise ProviderUnavailable(
            f"all providers in chain {chain} failed for chapter "
            f"{prepared.chapter.chapter_no}: {last_exc}")

    # -- full job ------------------------------------------------------------
    async def synthesize_job(self, script, *, video_id: str = "video",
                               profile: VoiceProfile | None = None,
                               niche: str | None = None,
                               preset: NichePreset | None = None,
                               output_dir: str | None = None) -> JobResult:
        profile = profile or VoiceProfile()
        preset = preset or get_preset(niche or "other")
        self.spent_usd = 0.0
        prepared, flagged = self.prepare(
            script, video_id=video_id, profile=profile, preset=preset)

        # Pre-flight budget projection on the cheapest chain entry that is
        # actually configured (normally edge-tts at $0).
        chain = resolve_chain(profile.lang, profile)
        configured = [n for n in chain if n in self.providers]
        if not configured:
            raise ProviderUnavailable(
                f"no configured providers for chain {chain}")
        projected_total = min(
            sum(self.providers[n].estimate_cost(len(c.text))
                for p in prepared for c in p.chunks)
            for n in configured)
        if projected_total > self.budget_cap_usd:
            raise BudgetPaused(
                f"projected narration cost ${projected_total:.2f} exceeds "
                f"per-video cap ${self.budget_cap_usd:.2f}")

        # Canary per configured provider, once per batch.
        canary_ok: dict[str, bool] = {}
        for name in configured:
            if self.run_canary:
                locale = profile.lang
                canary_ok[name] = await self.providers[name].canary(
                    locale, profile.voice_for(name))
            else:
                canary_ok[name] = True

        job = JobResult(
            video_id=video_id, flagged_tokens=flagged,
            tts_input_script=self.last_transliteration["tts_input_script"],
            transliteration_unknown_token_count=(
                self.last_transliteration["unknown_token_count"]))
        manifest_chunks: list[ManifestChunk] = []
        for prep in prepared:
            results, used = await self._render_chapter(
                prep, configured, profile, preset, canary_ok)
            job.providers_by_chapter[prep.chapter.chapter_no] = used
            for chunk, result in zip(prep.chunks, results):
                job.chunks.append(chunk)
                job.results.append(result)
                manifest_chunks.append(ManifestChunk(
                    chunk_no=chunk.chunk_no, chapter_no=chunk.chapter_no,
                    provider=result.provider, voice=result.voice_id,
                    preset=preset.niche, chars=result.billed_chars,
                    billed_cost=round(result.cost_usd, 6),
                    duration_s=round(result.duration_s, 3),
                    cache_key=result.cache_key or "",
                    status="cache_hit" if result.from_cache else "ok",
                    audio_path=result.audio_path,
                    gap_after_ms=preset.chunk_gap_ms))
        job.total_cost_usd = round(sum(r.cost_usd for r in job.results
                                       if not r.from_cache), 6)
        manifest = VoiceManifest(
            video_id=video_id, dict_version=self.dictionary.version,
            language=profile.lang, preset=preset.niche,
            total_cost_usd=job.total_cost_usd,
            providers_by_chapter=dict(job.providers_by_chapter),
            flagged_tokens=flagged, chunks=manifest_chunks)
        manifest.stitch_plan = build_stitch_plan(
            manifest_chunks, preset.chapter_pause_ms)
        job.manifest = manifest
        if output_dir:
            job.manifest_path = manifest.write(output_dir)
        return job
