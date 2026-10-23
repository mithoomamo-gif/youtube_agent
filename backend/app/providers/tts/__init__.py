"""TTS / voice provider layer (roadmap section 30).

Public surface: provider interface + adapters, chunking engine, engine
orchestrator, pronunciation dictionary, normalization, presets, fallback
chains, reliability primitives, cache and voice manifest.
"""
from .adapters import (DEFAULT_VOICES, AzureSpeechAdapter, EdgeTTSAdapter,
                       ElevenLabsAdapter, GoogleTTSAdapter, OpenAITTSAdapter)
from .base import (BudgetPaused, CircuitOpen, ProviderError,
                   ProviderUnavailable, SecretProvider, SilentCorruptionError,
                   TTSError, TTSProvider, TTSRequest, TTSResult, VoiceInfo,
                   check_silent_corruption, expected_duration_s)
from .cache import DictCache, FileCache, compute_cache_key
from .chunking import (CHUNK_HARD_CAP, CHUNK_TARGET_MAX, CHUNK_TARGET_MIN,
                       Chapter, Chunk, chunk_chapter_text, parse_script,
                       split_sentences)
from .dictionary import (CompiledText, PronunciationDictionary,
                         PronunciationEntry, load_yaml_entries)
from .engine import DEFAULT_VIDEO_BUDGET_USD, JobResult, TTSEngine
from .fallback import FALLBACK_CHAINS, VoiceProfile, resolve_chain
from .manifest import (ManifestChunk, StitchItem, StitchPlan, VoiceManifest,
                       build_stitch_plan)
from .normalization import (NormalizedText, normalize_text,
                            number_to_words_en, number_to_words_hi,
                            number_to_words_ur)
from .presets import PRESETS, NichePreset, get_preset
from .reliability import (CircuitBreaker, TokenBucketLimiter,
                          call_with_retries)
from .transliteration import (TransliterationResult, to_urdu_script,
                              transliterate, transliterate_with_stats)

__all__ = [
    "DEFAULT_VOICES", "AzureSpeechAdapter", "EdgeTTSAdapter",
    "ElevenLabsAdapter", "GoogleTTSAdapter", "OpenAITTSAdapter",
    "BudgetPaused", "CircuitOpen", "ProviderError", "ProviderUnavailable",
    "SecretProvider", "SilentCorruptionError", "TTSError", "TTSProvider",
    "TTSRequest", "TTSResult", "VoiceInfo", "check_silent_corruption",
    "expected_duration_s", "DictCache", "FileCache", "compute_cache_key",
    "CHUNK_HARD_CAP", "CHUNK_TARGET_MAX", "CHUNK_TARGET_MIN", "Chapter",
    "Chunk", "chunk_chapter_text", "parse_script", "split_sentences",
    "CompiledText", "PronunciationDictionary", "PronunciationEntry",
    "load_yaml_entries", "DEFAULT_VIDEO_BUDGET_USD", "JobResult", "TTSEngine",
    "FALLBACK_CHAINS", "VoiceProfile", "resolve_chain", "ManifestChunk",
    "StitchItem", "StitchPlan", "VoiceManifest", "build_stitch_plan",
    "NormalizedText", "normalize_text", "number_to_words_en",
    "number_to_words_hi", "number_to_words_ur", "PRESETS", "NichePreset",
    "get_preset", "CircuitBreaker", "TokenBucketLimiter", "call_with_retries",
    "TransliterationResult", "to_urdu_script", "transliterate",
    "transliterate_with_stats",
]
