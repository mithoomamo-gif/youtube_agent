"""Per-language fallback chains, as data (roadmap section 30).

URDU:    edge-tts -> Azure (SAME voice IDs, inaudible switch) -> Google
         ur-IN (accent shift logged) -> ElevenLabs ONLY IF the voice
         profile's ``urdu_audition_passed`` flag is set — Urdu is NOT in
         ElevenLabs Multilingual v2's language list, so an unauditioned
         ElevenLabs Urdu render is never allowed into a chain.
ENGLISH: edge-tts -> OpenAI mini-tts -> ElevenLabs Flash -> Azure
HINDI:   edge-tts -> Azure (same IDs) -> Google -> ElevenLabs
"""
from __future__ import annotations

from dataclasses import dataclass

FALLBACK_CHAINS: dict[str, list[str]] = {
    "ur": ["edge", "azure", "google", "elevenlabs"],
    "en": ["edge", "openai", "elevenlabs", "azure"],
    "hi": ["edge", "azure", "google", "elevenlabs"],
}


@dataclass
class VoiceProfile:
    """Channel voice identity (Voice Bible, roadmap section 10)."""
    voice_profile_id: str = "default"
    language: str = "en"                       # 'ur' | 'hi' | 'en' or locale
    voice_id: str = ""                         # primary voice id
    provider_voices: dict[str, str] | None = None  # per-provider voice override
    urdu_audition_passed: bool = False         # gates ElevenLabs for Urdu
    elevenlabs_model: str | None = None
    #: Narration script handed to TTS (Round 4): "roman" or
    #: "urdu_script" — Roman Urdu is transliterated (dictionary-first)
    #: before synthesis.  Per-channel selectable; see
    #: transliteration.py.  DEFAULT (R4-002, the adopted A/B winner):
    #: ``None`` here means "not set" and :meth:`__post_init__`
    #: resolves it voice-aware — ``"urdu_script"`` for ur-PK voices /
    #: Urdu profiles, ``"roman"`` for non-Urdu voices.  An explicit
    #: value (either way) is always honoured.  After construction the
    #: attribute is always a plain ``str``.
    tts_input_script: str | None = None

    def __post_init__(self) -> None:
        if self.tts_input_script is not None:
            cleaned = str(self.tts_input_script).strip().lower() \
                .replace("-", "_")
            self.tts_input_script = (
                "urdu_script" if cleaned in ("urdu", "urdu_script")
                else "roman" if cleaned == "roman" else cleaned)
            return
        voice = str(self.voice_id or "").strip().lower()
        if voice:
            self.tts_input_script = (
                "urdu_script" if voice.startswith("ur-") else "roman")
        else:
            self.tts_input_script = (
                "urdu_script" if self.lang == "ur" else "roman")

    @property
    def lang(self) -> str:
        return (self.language or "en").split("-")[0].lower()

    def voice_for(self, provider: str, defaults: dict[str, str] | None = None) -> str:
        if self.provider_voices and provider in self.provider_voices:
            return self.provider_voices[provider]
        if provider in ("edge", "azure") and self.voice_id:
            # edge-tts and Azure expose the SAME voice IDs by design.
            return self.voice_id
        if defaults and provider in defaults:
            return defaults[provider]
        return self.voice_id


def resolve_chain(language: str, profile: VoiceProfile | None = None) -> list[str]:
    """Ordered provider names for a language, with the Urdu/ElevenLabs
    audition gate applied."""
    lang = (language or "en").split("-")[0].lower()
    chain = list(FALLBACK_CHAINS.get(lang, FALLBACK_CHAINS["en"]))
    if lang == "ur" and not (profile and profile.urdu_audition_passed):
        chain = [p for p in chain if p != "elevenlabs"]
    return chain
