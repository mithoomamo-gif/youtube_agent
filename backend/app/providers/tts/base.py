"""Core contracts for the TTS provider layer.

Spec: roadmap section 30 (TTS / Voice Provider Layer).

Design rules baked in here:

* Providers sit behind :class:`TTSProvider`; no agent ever names a provider.
* API keys are obtained ONLY through the shared ``SecretProvider`` protocol
  (``async def get_secret(ref) -> str | None``). Adapters never read the
  environment or files for keys, and never log keys.
* Costing unit: a 10-minute video ~= 9,000 synthesized characters
  (~1,000 chars ~= 1 minute of speech). That ratio drives the
  silent-corruption check.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: ~1,000 synthesized characters ~= 1 minute of speech (roadmap costing unit).
CHARS_PER_MINUTE = 1000.0

#: A result whose measured duration is below this fraction of the expected
#: duration is silent corruption — a failure even on HTTP 200.
SILENT_CORRUPTION_FRACTION = 0.40


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class TTSError(Exception):
    """Base class for TTS-layer errors."""


class ProviderUnavailable(TTSError):
    """Provider cannot serve this request at all (stub, missing key/lib,
    auth/quota/voice-not-found). Escalate to the fallback chain immediately —
    retrying is pointless and, on quota errors, harmful."""


class ProviderError(TTSError):
    """Transient provider failure (timeout / 5xx / 429): retryable."""

    def __init__(self, message: str, *, retry_after_s: float | None = None,
                 status_code: int | None = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s
        self.status_code = status_code


class SilentCorruptionError(ProviderError):
    """Audio came back empty or far shorter than the text justifies."""


class BudgetPaused(TTSError):
    """Projected narration cost would breach the per-video budget cap.

    Raised instead of silently downgrading provider quality (roadmap §30).
    """


class CircuitOpen(ProviderUnavailable):
    """Circuit breaker is open for this provider."""


# --------------------------------------------------------------------------
# Shared secret contract
# --------------------------------------------------------------------------
@runtime_checkable
class SecretProvider(Protocol):
    """Shared contract: resolves a secret reference to a value."""

    async def get_secret(self, ref: str) -> str | None:  # pragma: no cover
        ...


# --------------------------------------------------------------------------
# Request / result
# --------------------------------------------------------------------------
@dataclass
class TTSRequest:
    text: str
    voice_id: str
    language: str = "en"
    rate_percent: float = 0.0          # e.g. -22.0 means 22% slower
    pitch_hz: float = 0.0
    preset: str | None = None          # niche preset name, if any
    model: str | None = None           # provider model override
    # Pronunciation compilation artefacts (see dictionary.py). Optional;
    # adapters use whichever mechanism they support.
    ssml_text: str | None = None
    custom_pronunciations: list[dict[str, Any]] = field(default_factory=list)
    pronunciation_dictionary: dict[str, Any] | None = None
    instructions: str | None = None    # steerable providers (OpenAI-style)


@dataclass
class VoiceInfo:
    voice_id: str
    locale: str
    gender: str | None = None
    name: str | None = None
    provider: str = ""


@dataclass
class TTSResult:
    provider: str
    model: str
    voice_id: str
    billed_chars: int
    duration_s: float
    audio_bytes: bytes = b""
    audio_path: str | None = None
    cost_usd: float = 0.0
    duration_estimated: bool = False
    word_timestamps: list[dict[str, Any]] = field(default_factory=list)
    cache_key: str | None = None
    from_cache: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.audio_bytes and not self.audio_path


def expected_duration_s(text: str, rate_percent: float = 0.0) -> float:
    """Expected narration duration for ``text`` at ``rate_percent``."""
    base = len(text) / CHARS_PER_MINUTE * 60.0
    factor = 1.0 + (rate_percent / 100.0)
    if factor <= 0:
        factor = 0.1
    return base / factor


def check_silent_corruption(result: TTSResult, req: TTSRequest) -> None:
    """Raise :class:`SilentCorruptionError` for empty / truncated audio."""
    if result.is_empty:
        raise SilentCorruptionError(
            f"{result.provider}: empty audio for {len(req.text)} chars")
    expected = expected_duration_s(req.text, req.rate_percent)
    if expected > 0 and result.duration_s < SILENT_CORRUPTION_FRACTION * expected:
        raise SilentCorruptionError(
            f"{result.provider}: duration {result.duration_s:.2f}s is "
            f"<40% of expected {expected:.2f}s for {len(req.text)} chars")


class TTSProvider(abc.ABC):
    """Provider interface every adapter implements."""

    name: str = "base"
    model: str = ""
    #: USD per 1,000 billed characters (API PAYG reference rates).
    cost_per_1k_chars: float = 0.0
    #: Token-bucket parallelism default for this provider.
    max_concurrency: int = 4
    implemented: bool = True

    @abc.abstractmethod
    async def synthesize(self, req: TTSRequest) -> TTSResult:
        """Synthesize one chunk of text."""

    @abc.abstractmethod
    async def list_voices(self, locale: str | None = None) -> list[VoiceInfo]:
        """List voices, optionally filtered to a locale like ``ur-PK``."""

    async def canary(self, locale: str = "en-US", voice_id: str | None = None) -> bool:
        """One-line pre-batch health check. True = healthy for this batch."""
        voice = voice_id or self.default_voice(locale)
        if not voice:
            return False
        try:
            result = await self.synthesize(TTSRequest(
                text="Hello.", voice_id=voice,
                language=(locale or "en").split("-")[0]))
            check_silent_corruption(
                result, TTSRequest(text="Hello.", voice_id=voice))
            return not result.is_empty
        except Exception:
            return False

    def default_voice(self, locale: str | None) -> str | None:
        return None

    def estimate_cost(self, chars: int) -> float:
        return chars / 1000.0 * self.cost_per_1k_chars
