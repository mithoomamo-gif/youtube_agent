"""Delivered-WPM calibration for edge-tts voices (Round 4, Worker 1).

WHY: Round 3 proved real Urdu edge-tts delivers ~199-225 WPM while niche
profiles plan at 120-150 WPM, so durations missed targets. This module
MEASURES delivered WPM honestly so planning can consume real numbers
(``calibration_data.json``, the canonical team contract) instead of
guesses.

Method
------
For a (voice, rate, text) triple: synthesize with REAL edge-tts via
:class:`~app.providers.tts.adapters.EdgeTTSAdapter`, write the returned
MP3 to disk, measure its duration with ``ffprobe -show_entries
format=duration`` (the same probe the pipeline timing spine / QC use),
and compute::

    delivered_wpm = word_count / (duration_s / 60)

If ffprobe is unavailable for a file, the adapter's WordBoundary-derived
duration is used and the row is marked ``duration_source="word_boundary"``
(never silently mixed: every row records its source).

Word counting rule
------------------
``word_count = len(text.split())`` — whitespace-delimited tokens.
Punctuation attached to a token (``.`` ``,`` Urdu ``۔``) stays part of
that token and does not create a word. This rule applies identically to
Latin-script (Roman Urdu / English) and Urdu-script text: Urdu script is
also whitespace-delimited in normal prose, ZWJ/ZWNJ joiners live *inside*
tokens, and no tokenisation/normalisation is applied — the count is of
exactly the tokens handed to the synthesizer, so WPM is comparable
across scripts for the matched sample texts in ``sample_texts/``.

No API key is needed (edge-tts is keyless) and nothing here logs
environment variables or proxy URLs.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from ...ffmpeg_resolution import ffprobe_executable

#: Rates (percent) measured for every voice, per the Round 4 contract.
DEFAULT_RATES_PCT: tuple[int, ...] = (-25, -10, 0, 10)

#: Voices measured by default: both configured ur-PK defaults and both
#: configured en-US defaults (adapters.DEFAULT_VOICES: male default is
#: what ``default_voice()`` returns; the female is the configured alternate).
DEFAULT_VOICE_TEXTS: dict[str, str] = {
    "ur-PK-UzmaNeural": "roman_urdu_facts",
    "ur-PK-AsadNeural": "roman_urdu_facts",
    "en-US-GuyNeural": "english_facts",
    "en-US-JennyNeural": "english_facts",
}

SAMPLE_TEXTS_DIR = Path(__file__).parent / "sample_texts"
CANONICAL_PATH = Path(__file__).parent / "calibration_data.json"

CALIBRATION_VERSION = 1
METHOD = (
    "Real edge-tts synthesis via EdgeTTSAdapter; audio written to MP3, "
    "duration measured with ffprobe format=duration (fallback: edge-tts "
    "WordBoundary end time, recorded per row); "
    "wpm = whitespace_token_count / (duration_s/60)."
)

#: A synthesizer maps (voice_id, rate_percent, text) -> (audio_bytes,
#: boundary_duration_s_or_None). Injectable so unit tests need no network.
Synthesizer = Callable[[str, float, str], Awaitable[tuple[bytes, Optional[float]]]]


def count_words(text: str) -> int:
    """Whitespace-token word count (see module docstring for the rule)."""
    return len(text.split())


def compute_wpm(word_count: int, duration_s: float) -> float:
    """Delivered words per minute for ``word_count`` in ``duration_s``."""
    if word_count <= 0:
        raise ValueError("word_count must be positive")
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    return round(word_count / (duration_s / 60.0), 2)


def rate_str(rate_pct: float) -> str:
    """edge-tts rate form, e.g. -25 -> '-25%', +10 -> '+10%'."""
    return f"{rate_pct:+.0f}%"


def load_sample_text(text_id: str) -> str:
    path = SAMPLE_TEXTS_DIR / f"{text_id}.txt"
    return path.read_text(encoding="utf-8").strip()


def probe_duration_s(path: Path) -> float:
    """ffprobe format duration (mirrors pipeline/scenes/timing.py)."""
    out = subprocess.run(
        [ffprobe_executable(), "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True, timeout=60)
    duration = float(json.loads(out.stdout)["format"]["duration"])
    if duration <= 0:
        raise ValueError(f"ffprobe reported non-positive duration for {path}")
    return duration


async def _edge_synthesize(voice_id: str, rate_pct: float,
                           text: str) -> tuple[bytes, Optional[float]]:
    """Default real synthesizer: the providers-layer edge-tts adapter."""
    from .adapters import EdgeTTSAdapter
    from .base import TTSRequest

    language = "ur" if voice_id.startswith("ur-") else "en"
    result = await EdgeTTSAdapter().synthesize(TTSRequest(
        text=text, voice_id=voice_id, language=language,
        rate_percent=float(rate_pct)))
    if not result.audio_bytes:
        raise RuntimeError(f"edge-tts returned empty audio for {voice_id}")
    boundary = None if result.duration_estimated else result.duration_s
    return result.audio_bytes, boundary


@dataclass
class Measurement:
    voice: str
    rate_pct: int
    rate: str
    text_id: str
    language: str
    word_count: int
    duration_s: float
    wpm: float
    duration_source: str  # "ffprobe" | "word_boundary"
    timestamp: str
    error: Optional[str] = None


@dataclass
class MeasurementRun:
    measured_at: str
    method: str = METHOD
    measurements: list[Measurement] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)

    def to_raw_dict(self) -> dict[str, Any]:
        return {
            "version": CALIBRATION_VERSION,
            "measured_at": self.measured_at,
            "method": self.method,
            "measurements": [asdict(m) for m in self.measurements],
            "failures": self.failures,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def measure_one(voice: str, rate_pct: int, text: str, text_id: str,
                      *, workdir: Path,
                      synthesizer: Optional[Synthesizer] = None,
                      timestamp: Optional[str] = None) -> Measurement:
    """Synthesize one (voice, rate, text) and measure delivered WPM."""
    synth = synthesizer or _edge_synthesize
    audio, boundary_duration = await synth(voice, float(rate_pct), text)
    workdir.mkdir(parents=True, exist_ok=True)
    safe_voice = voice.replace("/", "_")
    audio_path = workdir / f"{safe_voice}_{rate_pct:+d}.mp3"
    audio_path.write_bytes(audio)
    try:
        duration = probe_duration_s(audio_path)
        source = "ffprobe"
    except Exception:
        if boundary_duration is None or boundary_duration <= 0:
            raise
        duration = float(boundary_duration)
        source = "word_boundary"
    words = count_words(text)
    return Measurement(
        voice=voice, rate_pct=int(rate_pct), rate=rate_str(rate_pct),
        text_id=text_id,
        language="ur" if voice.startswith("ur-") else "en",
        word_count=words, duration_s=round(float(duration), 3),
        wpm=compute_wpm(words, float(duration)), duration_source=source,
        timestamp=timestamp or _now_iso())


async def run_calibration(
    voice_texts: Optional[dict[str, str]] = None,
    rates_pct: tuple[int, ...] = DEFAULT_RATES_PCT,
    *, workdir: Path, synthesizer: Optional[Synthesizer] = None,
    retries: int = 2,
) -> MeasurementRun:
    """Measure every (voice, rate) pair, retrying transient failures.

    A pair that still fails after retries is recorded in ``failures``
    (never faked) and measurement continues with the next pair.
    """
    run = MeasurementRun(measured_at=_now_iso())
    for voice, text_id in (voice_texts or DEFAULT_VOICE_TEXTS).items():
        text = load_sample_text(text_id)
        for rate_pct in rates_pct:
            last_exc: Optional[Exception] = None
            for attempt in range(retries + 1):
                try:
                    run.measurements.append(await measure_one(
                        voice, rate_pct, text, text_id, workdir=workdir,
                        synthesizer=synthesizer, timestamp=run.measured_at))
                    last_exc = None
                    break
                except Exception as exc:  # transient network etc.
                    last_exc = exc
                    if attempt < retries:
                        await asyncio.sleep(2.0 * (attempt + 1))
            if last_exc is not None:
                run.failures.append({
                    "voice": voice, "rate_pct": int(rate_pct),
                    "rate": rate_str(rate_pct), "text_id": text_id,
                    "error": f"{type(last_exc).__name__}: {last_exc}",
                    "timestamp": run.measured_at})
    return run


#: Sample text id -> TTS input script it is written in (R4-002).
_TEXT_ID_SCRIPTS = {
    "roman_urdu_facts": "roman",
    "english_facts": "roman",
    "urdu_script_facts": "urdu_script",
}


def _input_script_for_text(text_id: str) -> str:
    return _TEXT_ID_SCRIPTS.get(text_id, "roman")


def build_calibration_data(run: MeasurementRun,
                           voice_texts: Optional[dict[str, str]] = None
                           ) -> dict[str, Any]:
    """Canonical ``calibration_data.json`` payload (team contract schema).

    {"version": 1, "measured_at": iso, "method": str,
     "voices": {voice: {"rates_pct": {"-25": wpm, ...},
                        "sample_text_id": id, "language": "ur"|"en",
                        "default_input_script": "roman"|"urdu_script",
                        "scripts": {script: {"rates_pct": {...},
                                              "sample_text_id": id}}}}}

    Script-aware (R4-002): EVERY measured (voice, input-script) row is
    kept under ``scripts`` — delivered WPM differs by ~108 WPM between
    Roman-Urdu and Urdu-script input on the ur-PK voices, so a single
    script-blind row cannot plan both.  The top-level
    ``rates_pct``/``sample_text_id`` mirror the voice's ADOPTED
    default script (``urdu_script`` for Urdu voices when that row was
    measured, otherwise the voice's designated sample in
    ``voice_texts``), so script-blind readers get the adopted
    default.  Only successful measurements appear; a missing rate key
    means that rate genuinely failed (see the raw run's ``failures``).
    """
    mapping = voice_texts or DEFAULT_VOICE_TEXTS
    voices: dict[str, Any] = {}
    for m in run.measurements:
        entry = voices.setdefault(m.voice, {
            "rates_pct": {},
            "sample_text_id": mapping.get(m.voice, m.text_id),
            "language": m.language,
            "scripts": {},
        })
        script = _input_script_for_text(m.text_id)
        row = entry["scripts"].setdefault(script, {
            "rates_pct": {},
            "sample_text_id": m.text_id,
        })
        row["rates_pct"][str(m.rate_pct)] = m.wpm
    for voice, entry in voices.items():
        scripts = entry["scripts"]
        if entry["language"] == "ur" and "urdu_script" in scripts:
            default_script = "urdu_script"
        else:
            default_script = _input_script_for_text(
                mapping.get(voice, entry["sample_text_id"]))
            if default_script not in scripts:
                default_script = next(iter(scripts))
        entry["default_input_script"] = default_script
        entry["rates_pct"] = dict(scripts[default_script]["rates_pct"])
        entry["sample_text_id"] = scripts[default_script]["sample_text_id"]
    return {
        "version": CALIBRATION_VERSION,
        "measured_at": run.measured_at,
        "method": run.method,
        "voices": voices,
    }


def write_json(payload: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def write_outputs(run: MeasurementRun, raw_path: Path,
                  canonical_path: Path = CANONICAL_PATH,
                  voice_texts: Optional[dict[str, str]] = None
                  ) -> tuple[Path, Path]:
    """Write BOTH deliverables: the full raw run + canonical data file."""
    write_json(run.to_raw_dict(), raw_path)
    write_json(build_calibration_data(run, voice_texts), canonical_path)
    return raw_path, canonical_path
