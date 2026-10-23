"""faster-whisper integration wrapper with an alignment STUB fallback.

The import of ``faster_whisper`` is **lazy** (inside :meth:`_load_model`), so
this module imports cleanly on machines where the package — or the model
weights — are not installed yet.

STUB MODE
---------
Stub mode activates when *any* of the following is true:

1. the caller passes ``stub_mode=True`` (or ``force_stub=True``) to
   :class:`WhisperTranscriber`, or calls :meth:`transcribe` after setting it;
2. the environment variable :data:`STUB_ENV_VAR` (``YTA_SUBTITLES_STUB``) is
   set to a truthy value (``1`` / ``true`` / ``yes`` / ``on``);
3. at call time the real path fails for any reason — package missing, model
   download/load error, transcription error, or an empty result.

In stub mode the wrapper **never crashes for a missing model**: it derives
segments from alignment data instead, in priority order:

a. explicit ``alignment_words`` (TTS word boundaries / forced alignment) —
   the actual happy-path timing source for this product; else
b. the original script text distributed over the measured audio duration
   (probed from a WAV header or ``ffprobe``, or a caller-supplied
   ``duration_s``), proportionally to token length.

Every segment produced by the fallback is marked
``source="alignment_stub"`` (real whisper output is ``source="whisper"``),
and the transcriber records ``last_provenance`` / ``last_error`` so callers
and QC can see exactly which path produced the timings.  If stub mode has
*no* timing source at all (no alignment words and no script+duration), it
raises :class:`SubtitlesStubError` with that explanation rather than
fabricating timings.
"""

from __future__ import annotations

import json
import os
import subprocess
import wave
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from .models import (
    SOURCE_ALIGNMENT_STUB,
    SOURCE_WHISPER,
    TranscriptSegment,
    WordTiming,
    coerce_word_timings,
)
from ...ffmpeg_resolution import ffprobe_executable

#: Environment variable that forces stub mode when truthy.
STUB_ENV_VAR = "YTA_SUBTITLES_STUB"

#: Roadmap defaults: large-v3-turbo on GPU, medium int8 on CPU.  The wrapper
#: itself stays device-agnostic; the caller picks the model size.
DEFAULT_MODEL_SIZE = "large-v3-turbo"
CPU_FALLBACK_MODEL_SIZE = "medium"

_TRUTHY = {"1", "true", "yes", "on"}


class SubtitlesStubError(RuntimeError):
    """Stub mode was required but no alignment/script timing source exists."""


def stub_requested_by_env(env: Optional[dict[str, str]] = None) -> bool:
    value = (env if env is not None else os.environ).get(STUB_ENV_VAR, "")
    return value.strip().lower() in _TRUTHY


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def probe_duration_seconds(audio_path: str | Path) -> Optional[float]:
    """Best-effort audio duration (WAV header, else ffprobe). None if unknown."""
    path = Path(audio_path)
    if not path.exists():
        return None
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as wav:
                frames = wav.getnframes()
                rate = wav.getframerate()
                if rate:
                    return frames / float(rate)
        except (wave.Error, OSError):
            pass
    try:
        proc = subprocess.run(
            [
                ffprobe_executable(),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode == 0:
            data = json.loads(proc.stdout or "{}")
            duration = data.get("format", {}).get("duration")
            if duration is not None:
                return float(duration)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


class WhisperTranscriber:
    """Transcribe audio to word-timestamped segments via faster-whisper.

    Parameters
    ----------
    model_size:
        faster-whisper model name (default ``large-v3-turbo``).
    device / compute_type:
        Passed through to ``WhisperModel``; ``None`` lets faster-whisper
        choose (``auto`` / ``default``).
    stub_mode:
        ``True`` forces stub mode, ``False`` forces attempting the real
        model first (still falling back to stub on failure), ``None``
        (default) defers to the ``YTA_SUBTITLES_STUB`` env var.
    model_loader:
        Injectable ``() -> model`` callable — the seam tests use to mock
        faster-whisper without downloading weights.
    alignment_words / script_text:
        Default fallback timing sources for stub mode; both can also be
        supplied per-call to :meth:`transcribe`.
    """

    def __init__(
        self,
        model_size: str = DEFAULT_MODEL_SIZE,
        device: Optional[str] = None,
        compute_type: Optional[str] = None,
        *,
        stub_mode: Optional[bool] = None,
        force_stub: bool = False,
        model_loader: Optional[Callable[[], Any]] = None,
        alignment_words: Optional[Iterable[Any]] = None,
        script_text: Optional[str] = None,
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.stub_mode: Optional[bool] = True if force_stub else stub_mode
        self._model_loader = model_loader
        self._model: Any = None
        self.alignment_words = coerce_word_timings(alignment_words)
        self.script_text = script_text
        #: Provenance of the most recent :meth:`transcribe` result.
        self.last_provenance: Optional[str] = None
        #: Why the real whisper path was not used, when it wasn't.
        self.last_error: Optional[str] = None

    # -- model loading (lazy) ------------------------------------------------

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        if self._model_loader is not None:
            self._model = self._model_loader()
            return self._model
        # LAZY import: module must import fine without faster-whisper present.
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        self._model = WhisperModel(
            self.model_size,
            device=self.device or "auto",
            compute_type=self.compute_type or "default",
        )
        return self._model

    def _stub_requested(self) -> bool:
        if self.stub_mode is not None:
            return self.stub_mode
        return stub_requested_by_env()

    # -- public API ----------------------------------------------------------

    def transcribe(
        self,
        audio_path: str | Path,
        language: Optional[str] = None,
        *,
        alignment_words: Optional[Iterable[Any]] = None,
        script_text: Optional[str] = None,
        duration_s: Optional[float] = None,
    ) -> list[TranscriptSegment]:
        """Return word-timestamped segments for ``audio_path``.

        Real faster-whisper output is marked ``source="whisper"``; anything
        produced by the stub fallback is marked ``source="alignment_stub"``.
        """
        fallback_words = (
            coerce_word_timings(alignment_words)
            if alignment_words is not None
            else list(self.alignment_words)
        )
        script = script_text if script_text is not None else self.script_text

        if self._stub_requested():
            self.last_error = "stub mode forced (flag or YTA_SUBTITLES_STUB env)"
            return self._stub_transcribe(
                audio_path,
                language=language,
                alignment_words=fallback_words,
                script_text=script,
                duration_s=duration_s,
            )

        try:
            model = self._load_model()
            raw_segments, info = model.transcribe(
                str(audio_path),
                language=language,
                word_timestamps=True,
                vad_filter=True,
            )
            segments = [
                self._convert_segment(raw) for raw in raw_segments if raw is not None
            ]
            segments = [s for s in segments if s.text.strip() or s.words]
            if not segments:
                raise ValueError("faster-whisper returned no segments")
            detected = _get(info, "language", None) if info is not None else None
            if language is None and detected:
                segments = [
                    TranscriptSegment(
                        text=s.text,
                        start=s.start,
                        end=s.end,
                        words=s.words,
                        language=s.language or str(detected),
                        source=s.source,
                    )
                    for s in segments
                ]
            self.last_provenance = SOURCE_WHISPER
            self.last_error = None
            return segments
        except Exception as exc:  # package missing, download failed, bad audio…
            self.last_error = f"{type(exc).__name__}: {exc}"
            return self._stub_transcribe(
                audio_path,
                language=language,
                alignment_words=fallback_words,
                script_text=script,
                duration_s=duration_s,
            )

    # -- conversion ----------------------------------------------------------

    @staticmethod
    def _convert_segment(raw: Any) -> TranscriptSegment:
        words: list[WordTiming] = []
        for raw_word in _get(raw, "words", None) or []:
            text = _get(raw_word, "word", None) or _get(raw_word, "text", "")
            start = float(_get(raw_word, "start", 0.0))
            end = float(_get(raw_word, "end", start))
            words.append(
                WordTiming(
                    word=str(text),
                    start=start,
                    end=max(start, end),
                    source=SOURCE_WHISPER,
                    probability=_get(raw_word, "probability", None),
                )
            )
        text = str(_get(raw, "text", "") or "").strip()
        if not text and words:
            text = "".join(w.word for w in words).strip()
        start = float(_get(raw, "start", words[0].start if words else 0.0))
        end = float(_get(raw, "end", words[-1].end if words else start))
        return TranscriptSegment(
            text=text,
            start=start,
            end=max(start, end),
            words=tuple(words),
            language=_get(raw, "language", None),
            source=SOURCE_WHISPER,
        )

    # -- stub fallback ---------------------------------------------------------

    def _stub_transcribe(
        self,
        audio_path: str | Path,
        *,
        language: Optional[str],
        alignment_words: Sequence[WordTiming],
        script_text: Optional[str],
        duration_s: Optional[float],
    ) -> list[TranscriptSegment]:
        if alignment_words:
            segments = segments_from_alignment_words(alignment_words, script_text)
        elif script_text and script_text.strip():
            duration = duration_s
            if duration is None:
                duration = probe_duration_seconds(audio_path)
            if duration is None or duration <= 0:
                raise SubtitlesStubError(
                    "Stub mode cannot derive timings: no alignment words were "
                    "provided and the script text has no known audio duration "
                    "(pass duration_s= or an audio file whose duration can be "
                    f"probed). Underlying cause: {self.last_error}"
                )
            segments = segments_from_script_duration(script_text, duration)
        else:
            raise SubtitlesStubError(
                "Stub mode cannot derive timings: provide alignment_words "
                "(TTS word boundaries) or script_text plus a duration. "
                f"Underlying cause: {self.last_error}"
            )
        if language:
            segments = [
                TranscriptSegment(
                    text=s.text,
                    start=s.start,
                    end=s.end,
                    words=s.words,
                    language=language,
                    source=s.source,
                )
                for s in segments
            ]
        self.last_provenance = SOURCE_ALIGNMENT_STUB
        return segments


# ---------------------------------------------------------------------------
# Stub segment builders (also useful standalone)
# ---------------------------------------------------------------------------

_SENTENCE_ENDINGS = (".", "!", "?", "…", "۔", "؟")


def segments_from_alignment_words(
    alignment_words: Sequence[WordTiming],
    script_text: Optional[str] = None,
) -> list[TranscriptSegment]:
    """Group alignment word timings into segments (sentence-sized chunks).

    Word *text* is snapped to the original script when the token counts line
    up — the script is ground truth, timings are what alignment contributes.
    """
    words = coerce_word_timings(alignment_words)
    if not words:
        return []
    if script_text and script_text.strip():
        tokens = script_text.split()
        if len(tokens) == len(words):
            words = [
                WordTiming(
                    word=token,
                    start=w.start,
                    end=w.end,
                    source=SOURCE_ALIGNMENT_STUB,
                    probability=w.probability,
                )
                for token, w in zip(tokens, words)
            ]
    groups: list[list[WordTiming]] = []
    current: list[WordTiming] = []
    for word in words:
        current.append(
            WordTiming(
                word=word.word,
                start=word.start,
                end=word.end,
                source=SOURCE_ALIGNMENT_STUB,
                probability=word.probability,
            )
        )
        ends_sentence = word.word.rstrip().endswith(_SENTENCE_ENDINGS)
        too_long = current[-1].end - current[0].start >= 7.0
        if ends_sentence or too_long or len(current) >= 14:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return [
        TranscriptSegment(
            text=" ".join(w.word for w in group).strip(),
            start=group[0].start,
            end=group[-1].end,
            words=tuple(group),
            source=SOURCE_ALIGNMENT_STUB,
        )
        for group in groups
        if group
    ]


def segments_from_script_duration(
    script_text: str, duration_s: float
) -> list[TranscriptSegment]:
    """Distribute script words over ``duration_s`` proportional to length.

    Last-resort stub timing source: word boundaries are estimates derived
    from the script + total duration, which is why the provenance is always
    ``alignment_stub`` and never ``whisper``.
    """
    tokens = script_text.split()
    if not tokens or duration_s <= 0:
        return []
    weights = [max(len(token.strip(".,!?…")), 1) for token in tokens]
    total_weight = float(sum(weights))
    words: list[WordTiming] = []
    cursor = 0.0
    elapsed_weight = 0.0
    for token, weight in zip(tokens, weights):
        elapsed_weight += weight
        end = duration_s * (elapsed_weight / total_weight)
        words.append(
            WordTiming(
                word=token, start=cursor, end=end, source=SOURCE_ALIGNMENT_STUB
            )
        )
        cursor = end
    return segments_from_alignment_words(words, script_text)


def transcribe(
    audio_path: str | Path,
    language: Optional[str] = None,
    **kwargs: Any,
) -> list[TranscriptSegment]:
    """One-shot convenience wrapper around :class:`WhisperTranscriber`.

    Accepts the transcriber's constructor keywords (``model_size``,
    ``stub_mode``/``force_stub``, ``model_loader``, ``alignment_words``,
    ``script_text``) plus the per-call keywords of
    :meth:`WhisperTranscriber.transcribe` (``duration_s``).
    """
    constructor_keys = {
        "model_size",
        "device",
        "compute_type",
        "stub_mode",
        "force_stub",
        "model_loader",
        "alignment_words",
        "script_text",
    }
    constructor = {k: kwargs.pop(k) for k in list(kwargs) if k in constructor_keys}
    transcriber = WhisperTranscriber(**constructor)
    return transcriber.transcribe(audio_path, language=language, **kwargs)
