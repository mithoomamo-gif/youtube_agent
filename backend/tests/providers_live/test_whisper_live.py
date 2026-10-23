"""faster-whisper LIVE test — real model, real transcription, WER gate.

Marked ``live`` and SKIPPED by default, following this directory's
established pattern (see ``test_pixabay_live.py`` / ``README.md``):
the opt-in environment variable ``YTA_LIVE_WHISPER`` plays the role
the API-key variables play for the other harnesses — whisper is a
local model, so there is no key; the "credential" being gated is the
~75 MB model download + CPU transcription time.  Without the env var
the test skips cleanly and makes no network call.

What it proves when opted in
----------------------------
1. The REAL faster-whisper path works end to end through the shipping
   wiring: ``app.providers.transcription.create_transcriber("tiny")``
   builds the pipeline's ``WhisperTranscriber`` with a provider-side
   ``model_loader`` (the wrapper's established injection seam — no
   pipeline file is modified), the model weights come from the
   workspace disk (``<repo>/models/whisper/``, never ``/tmp``), and
   the result's provenance is ``"whisper"`` — NOT the
   ``"alignment_stub"`` fallback.
2. Transcription quality: WER of the raw transcript against the known
   source text of the fixture WAV is below ``WER_THRESHOLD``.

Fixture audio
-------------
``fixtures/whisper_en.wav`` (16-bit WAV, 22.05 kHz mono, 8.5 s) +
``fixtures/whisper_en.txt`` (its exact source text, 21 words).  The
WAV was synthesised in Round 2 with Piper ``en_US-amy-medium``
neural TTS (the same file also lives at
``outputs/round2/voice/whisper_test_en.wav``).  Clean synthetic
speech with a known transcript is exactly what a WER gate needs.

WER threshold justification
-----------------------------
``WER_THRESHOLD = 0.20``.  The ``tiny`` model (39M params) scores
roughly 12–15% WER on *human* LibriSpeech speech; clean TTS speech
is substantially easier, and the Round-2 integration run measured
WER = 0.000 on this exact fixture.  0.20 therefore leaves generous
headroom for CPU/int8 variation across machines while still failing
on any real regression — and a silent fall back to the stub path is
caught independently by the provenance assertions.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

#: Opt-in env var (this directory's "key" for a key-less local model).
ENV_VAR = "YTA_LIVE_WHISPER"

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
AUDIO_PATH = FIXTURES_DIR / "whisper_en.wav"
REFERENCE_PATH = FIXTURES_DIR / "whisper_en.txt"

#: See module docstring for justification.
WER_THRESHOLD = 0.20

_TRUTHY = {"1", "true", "yes", "on"}


@pytest.fixture(scope="module")
def live_opt_in() -> None:
    if os.environ.get(ENV_VAR, "").strip().lower() not in _TRUTHY:
        pytest.skip(
            f"{ENV_VAR} is not set — skipping faster-whisper live test "
            "(it downloads/loads the real tiny model; see "
            "tests/providers_live/README.md)"
        )
    pytest.importorskip(
        "faster_whisper",
        reason="faster-whisper is not installed in backend/.venv "
        "(pip install faster-whisper, or the `transcription` extra)",
    )
    if not AUDIO_PATH.exists() or not REFERENCE_PATH.exists():
        pytest.skip(
            f"whisper fixture audio/text missing under {FIXTURES_DIR}"
        )


def test_whisper_live_transcribes_fixture_with_low_wer(live_opt_in):
    from app.pipeline.subtitles.models import SOURCE_WHISPER
    from app.providers.transcription import create_transcriber, word_error_rate

    reference = REFERENCE_PATH.read_text(encoding="utf-8")

    transcriber = create_transcriber("tiny")
    segments = transcriber.transcribe(AUDIO_PATH, language="en")

    # The REAL path must have produced these — not the alignment stub.
    assert segments, "real faster-whisper returned no segments"
    assert transcriber.last_provenance == SOURCE_WHISPER
    assert transcriber.last_error is None
    assert all(s.source == SOURCE_WHISPER for s in segments)
    assert all(s.words for s in segments), "word timestamps expected"

    transcript = " ".join(s.text.strip() for s in segments).strip()
    assert transcript, "transcript is empty"

    wer = word_error_rate(reference, transcript)
    assert wer <= WER_THRESHOLD, (
        f"WER {wer:.3f} exceeds threshold {WER_THRESHOLD:.2f}\n"
        f"reference:  {reference!r}\ntranscript: {transcript!r}"
    )
