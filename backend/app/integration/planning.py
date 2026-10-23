"""Calibrated script-length planning (Round 4, Worker 2).

THE problem this module fixes: before Round 4 the content stage planned
script length as ``profile.target_wpm * minutes`` with the niche
profile's *planning constant* (110–160 WPM, clamped in
:mod:`app.niches.models`).  Round 3 measured real Urdu edge-tts
delivering ~199–225 WPM, so videos landed short (history: 133 s vs a
~180 s target) unless a post-hoc TTS rate override (facts: −25%) was
applied to slow the voice down to the plan.

THE fix: plan with the voice's MEASURED WPM.  :func:`plan_word_target`
resolves, for one production,

1. the **voice** that will actually narrate (``spec.voice_id`` wins,
   then an explicit voice on the profile, then the language default —
   the same precedence :mod:`app.integration.voice_stage` uses), and
2. the **rate** that voice will actually run at — an explicit user
   override (``spec.tts_rate_percent``) when set, otherwise the niche
   TTS preset's rate (``app.providers.tts.presets``), otherwise 0 —

and asks the versioned calibration store
(:mod:`app.providers.tts.calibration_store`) for that voice+rate's
measured WPM.  The word target is then simply::

    target_words = calibrated_wpm * target_minutes

so at the default rate the plan is correct with NO compensating rate
override.  The override mechanism stays available (set
``spec.tts_rate_percent``): when one is set it is honoured by both the
planner (which plans at the calibrated WPM *for that rate*) and the
voice stage, and it is logged loudly — it is never applied silently
and never needed by default.

Provenance: every :class:`WordTarget` carries the calibration
provenance (``measured`` with version / measured_at / method, or the
explicit ``unmeasured-default`` label when no calibration file exists
and the niche profile's old planning constant was used as fallback).
The content stage persists it on the bundle and logs it, so a plan can
always be audited back to the measurement that produced it.

Fallback WPM when unmeasured: the niche profile's ``target_wpm`` —
i.e. exactly the pre-Round-4 behaviour, but now labelled as such.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.providers.tts.calibration_store import (
    CalibratedWpm,
    CalibrationStore,
    resolve_planning_wpm,
)

__all__ = [
    "WordTarget",
    "plan_word_target",
    "resolve_planning_input_script",
    "resolve_planning_rate",
    "resolve_planning_voice",
]

logger = logging.getLogger(__name__)

#: Niche profile id -> providers-layer preset key (mirrors voice_stage's
#: map; presets are keyed by short niche names).
_PRESET_KEY_MAP = {
    "horror_stories": "horror",
    "crime_stories": "crime",
    "islamic": "islamic_stories",
    "islamic stories": "islamic_stories",
    "custom": "other",
}

_LANGUAGE_NAMES = {
    "roman-urdu": "ur",
    "roman urdu": "ur",
    "urdu": "ur",
    "english": "en",
    "hindi": "hi",
}

#: Last-resort voice when neither spec, profile nor locale tables know
#: one (the project default: Roman-Urdu narration, male default voice).
_FALLBACK_VOICE = "ur-PK-AsadNeural"


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """Duck-typed attribute/mapping read (same idiom as sibling stages)."""
    for name in names:
        if obj is None:
            break
        if isinstance(obj, dict):
            if name in obj and obj[name] not in (None, ""):
                return obj[name]
        else:
            value = getattr(obj, name, None)
            if value not in (None, ""):
                return value
    return default


def _language_base(profile: Any, spec: Any = None) -> str:
    """Narration language base code ('ur' / 'en' / 'hi' / ...)."""
    spec_lang = str(_get(spec, "language", "lang", "locale") or "").strip().lower()
    if spec_lang in ("en", "english", "en-us", "en-gb"):
        return "en"
    raw = _get(profile, "language", "lang", "locale") or spec_lang
    if raw:
        tag = str(raw).strip().lower().replace("_", "-")
        base = tag.split("+")[0].strip()
        return _LANGUAGE_NAMES.get(base, base.split("-")[0])
    return "ur"


def resolve_planning_voice(profile: Any, spec: Any = None) -> str:
    """The voice id the voice stage will narrate with.

    Precedence mirrors ``voice_stage._resolve_voice`` /
    ``synthesize_voice``: the spec's explicit voice wins, then an
    explicit voice on the profile, then the locale default (male)
    from the providers layer's ``DEFAULT_VOICES`` table, honouring a
    female hint in the profile's voice style.
    """
    spec_voice = _get(spec, "voice_id", "tts_voice_id")
    if spec_voice and "Neural" in str(spec_voice):
        return str(spec_voice)
    explicit = _get(profile, "voice_id", "tts_voice_id", "edge_voice_id", "voice")
    if explicit and "Neural" in str(explicit):
        return str(explicit)
    provider_voices = _get(profile, "provider_voices")
    if isinstance(provider_voices, dict):
        for key in ("edge", "edge-tts", "azure"):
            if provider_voices.get(key):
                return str(provider_voices[key])
    try:
        from app.providers.tts.adapters import DEFAULT_VOICES

        lang = _language_base(profile, spec)
        locale = next(
            (loc for loc in DEFAULT_VOICES if loc.split("-")[0] == lang),
            "en-US",
        )
        voices = DEFAULT_VOICES[locale]
        hint = " ".join(
            str(_get(profile, name) or "")
            for name in ("voice_style", "voice_hint", "voice_gender", "gender")
        ).lower()
        # Same hint precedence as voice_stage._resolve_voice: explicit
        # voice names first, then gender words, else the male default.
        if "uzma" in hint:
            return str(voices["female"])
        if "asad" in hint:
            return str(voices["male"])
        if any(w in hint for w in ("female", "woman", "lady", "aurat")):
            return str(voices["female"])
        return str(voices["male"])
    except Exception:
        return _FALLBACK_VOICE


def resolve_planning_rate(profile: Any, spec: Any = None) -> tuple[float, str]:
    """The TTS rate (percent) the voice stage will run at.

    Returns ``(rate_pct, source)`` where source is ``"spec-override"``
    (the user explicitly set ``spec.tts_rate_percent``),
    ``"niche-preset"`` (the providers-layer preset for the profile's
    niche — what the voice stage applies by default) or ``"default"``
    (0%, no preset resolvable).  An explicit override is logged loudly:
    it is a deliberate user choice, never a silent compensation.
    """
    override = _get(spec, "tts_rate_percent", "rate_percent")
    if override is not None:
        try:
            rate = float(override)
        except (TypeError, ValueError):
            rate = None  # type: ignore[assignment]
        if rate is not None:
            logger.warning(
                "TTS rate override explicitly set by user: %+.1f%% "
                "(planning will use the calibrated WPM at this rate; "
                "no override is needed by default)",
                rate,
            )
            return rate, "spec-override"
    try:
        from app.providers.tts.presets import get_preset

        niche = _get(profile, "profile_id", "niche", "preset", "niche_id")
        key = str(niche or "other").strip().lower()
        preset = get_preset(_PRESET_KEY_MAP.get(key, key))
        return float(preset.rate_percent), "niche-preset"
    except Exception:
        return 0.0, "default"


def resolve_planning_input_script(profile: Any, spec: Any = None,
                                  voice_id: str | None = None) -> str:
    """The TTS input script the production will ACTUALLY use
    (``"roman"`` / ``"urdu_script"``; R4-002).

    Resolution order: the spec's ``tts_input_script`` (a real
    :class:`ProjectSpec` already carries the voice-aware adopted
    default, and an explicit per-channel choice), then the profile's,
    then the voice-aware default — ``"urdu_script"`` for an ``ur-*``
    voice or an Urdu production, ``"roman"`` otherwise.  The planner
    must plan with the calibration row for THIS script: planning an
    Urdu-script production from Roman WPM was the Round 4 run-1
    +24% duration miss.
    """
    for obj in (spec, profile):
        raw = _get(obj, "tts_input_script", "tts_script", "input_script")
        if raw:
            cleaned = str(raw).strip().lower().replace("-", "_")
            if cleaned in ("urdu_script", "urdu"):
                return "urdu_script"
            if cleaned == "roman":
                return "roman"
    voice = str(voice_id or "").strip().lower()
    if voice:
        return "urdu_script" if voice.startswith("ur-") else "roman"
    return "urdu_script" if _language_base(profile, spec) == "ur" else "roman"


@dataclass(frozen=True)
class WordTarget:
    """A planned script-length target with full audit provenance."""

    #: Planned narration word count for the production.
    target_words: int
    #: Target runtime the plan was computed for, in minutes.
    target_minutes: float
    #: Voice the plan assumes (the voice the voice stage will use).
    voice_id: str
    #: Rate the plan assumes (percent; the requested rate — the
    #: calibrated bucket actually used is on ``calibration.rate_pct``).
    rate_pct: float
    #: Where ``rate_pct`` came from: "spec-override" | "niche-preset" |
    #: "default".
    rate_source: str
    #: The calibrated (or explicitly-labelled fallback) WPM used.
    calibration: CalibratedWpm
    #: TTS input script the plan assumes (R4-002): the calibration
    #: row was selected for this script.
    input_script: str = "roman"

    @property
    def wpm(self) -> float:
        return self.calibration.wpm

    @property
    def provenance(self):
        return self.calibration.provenance

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_words": self.target_words,
            "target_minutes": self.target_minutes,
            "voice_id": self.voice_id,
            "rate_pct": self.rate_pct,
            "rate_source": self.rate_source,
            "input_script": self.input_script,
            "wpm": self.wpm,
            "calibration": self.calibration.as_dict(),
        }

    def log_line(self) -> str:
        return (
            f"word target: {self.target_words} words for "
            f"{self.target_minutes:g} min = {self.wpm:g} WPM "
            f"(voice {self.voice_id}, rate {self.rate_pct:+.1f}% "
            f"[{self.rate_source}], bucket {self.calibration.rate_pct:+g}%, "
            f"input script {self.input_script}) "
            f"— WPM provenance: {self.provenance.label()}"
        )


def plan_word_target(
    spec: Any,
    profile: Any,
    *,
    minutes: float | None = None,
    store: CalibrationStore | None = None,
) -> WordTarget:
    """Compute the calibrated word target for one production.

    ``target_words = calibrated_wpm(voice, rate, input_script) *
    target_minutes`` (minimum 30 words, matching the content stage's
    historical floor), where ``input_script`` is the script the
    production will actually hand to TTS (R4-002 — a Roman plan for
    an Urdu-script production misses its duration target by ~+24%).
    When no calibration data exists for the voice, the niche profile's
    ``target_wpm`` planning constant is the fallback and the result's
    provenance says ``unmeasured-default`` — the pre-Round-4 numbers,
    honestly labelled.
    """
    if minutes is None:
        raw = _get(spec, "minutes", default=1.0)
        try:
            minutes = float(raw) if raw else 1.0
        except (TypeError, ValueError):
            minutes = 1.0
    if minutes <= 0:
        minutes = 1.0
    voice_id = resolve_planning_voice(profile, spec)
    rate_pct, rate_source = resolve_planning_rate(profile, spec)
    input_script = resolve_planning_input_script(profile, spec, voice_id)
    fallback_raw = _get(profile, "target_wpm", "wpm", "wpm_target", default=140)
    try:
        fallback_wpm = float(fallback_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        fallback_wpm = 140.0
    calibration = resolve_planning_wpm(
        voice_id, rate_pct, fallback_wpm=fallback_wpm, store=store,
        input_script=input_script,
    )
    target_words = max(30, int(round(calibration.wpm * minutes)))
    plan = WordTarget(
        target_words=target_words,
        target_minutes=float(minutes),
        voice_id=voice_id,
        rate_pct=float(rate_pct),
        rate_source=rate_source,
        calibration=calibration,
        input_script=input_script,
    )
    logger.info("script-length planning — %s", plan.log_line())
    return plan
