"""Per-niche voice presets (data only — roadmap section 30).

Rates are relative to the ur-PK reference voices (~167 wpm at rate 0).
``chapter_pause_ms`` overrides the stitching default for the niche.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NichePreset:
    niche: str
    wpm_min: int
    wpm_max: int
    rate_percent: float          # representative rate inside the spec range
    pitch_hz: float = 0.0
    chapter_pause_ms: int = 650
    chunk_gap_ms: int = 300      # 250-350ms between chunks within a chapter
    elevenlabs_stability: float = 0.55
    elevenlabs_style: float = 0.30
    force_base_for_disclaimer: bool = False

    @property
    def wpm_target(self) -> float:
        return (self.wpm_min + self.wpm_max) / 2.0


PRESETS: dict[str, NichePreset] = {
    "facts": NichePreset(
        "facts", 155, 165, rate_percent=-1.5, pitch_hz=0,
        chapter_pause_ms=500, elevenlabs_stability=0.45, elevenlabs_style=0.35),
    "islamic_stories": NichePreset(
        "islamic_stories", 118, 128, rate_percent=-22.0, pitch_hz=-8,
        chapter_pause_ms=900, elevenlabs_stability=0.65, elevenlabs_style=0.20),
    "horror": NichePreset(
        "horror", 112, 122, rate_percent=-26.0, pitch_hz=-12,
        chapter_pause_ms=1000, elevenlabs_stability=0.55, elevenlabs_style=0.45),
    "history": NichePreset(
        "history", 132, 142, rate_percent=-15.0, pitch_hz=-4,
        chapter_pause_ms=700, elevenlabs_stability=0.60, elevenlabs_style=0.25),
    "motivation": NichePreset(
        "motivation", 138, 148, rate_percent=-10.0, pitch_hz=4,
        chapter_pause_ms=600, elevenlabs_stability=0.40, elevenlabs_style=0.55),
    "tech": NichePreset(
        "tech", 148, 158, rate_percent=-5.0, pitch_hz=0,
        chapter_pause_ms=500, elevenlabs_stability=0.50, elevenlabs_style=0.30),
    "crime": NichePreset(
        "crime", 128, 138, rate_percent=-18.0, pitch_hz=-8,
        chapter_pause_ms=800, elevenlabs_stability=0.60, elevenlabs_style=0.35),
    "health": NichePreset(
        "health", 132, 142, rate_percent=-15.0, pitch_hz=-2,
        chapter_pause_ms=700, elevenlabs_stability=0.62, elevenlabs_style=0.25,
        force_base_for_disclaimer=True),
    "other": NichePreset(
        "other", 135, 145, rate_percent=-12.0, pitch_hz=0,
        chapter_pause_ms=650, elevenlabs_stability=0.55, elevenlabs_style=0.30),
}

#: Aliases users / other modules commonly use.
_ALIASES = {
    "islamic": "islamic_stories",
    "islamic stories": "islamic_stories",
    "custom": "other",
}


def get_preset(niche: str | None, *, segment_type: str | None = None,
               channel_override: NichePreset | None = None) -> NichePreset:
    """Resolve a niche preset, honouring a channel-tuned override if given.

    Health disclaimer segments are FORCED to the canonical (base) health
    preset regardless of channel overrides (roadmap section 30).
    """
    key = (niche or "other").strip().lower()
    key = _ALIASES.get(key, key)
    preset = PRESETS.get(key, PRESETS["other"])
    if segment_type == "disclaimer" and preset.force_base_for_disclaimer:
        return preset
    return channel_override or preset


#: The canonical base preset a health disclaimer is forced to.
BASE_PRESET = PRESETS["health"]
