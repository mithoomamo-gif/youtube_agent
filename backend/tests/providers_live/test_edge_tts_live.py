"""LIVE edge-tts tests — real network calls to Microsoft's speech
endpoint (speech.platform.bing.com). No mocks.

Opt-in only: marked ``live`` and skipped by default (see conftest.py in
this folder). Run explicitly with:

    YTA_LIVE_EDGE_TTS=1 python -m pytest tests/providers_live/ -m live

edge-tts needs no API key, so — following the faster-whisper harness
pattern in this folder — the opt-in environment variable
``YTA_LIVE_EDGE_TTS`` plays the gatekeeping role a key variable plays
for the other harnesses: without it the tests skip cleanly and make NO
network call, so a bare ``-m live`` run without any keys/opt-ins is
all-skipped. Requires the ``edge-tts`` package and working egress to
Microsoft's speech endpoints (via the standard *_proxy env vars when
present).
"""
import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.providers.tts import EdgeTTSAdapter
from app.providers.tts.base import TTSRequest

pytestmark = pytest.mark.live

#: Opt-in env var (this folder's "key" for a key-less service).
ENV_VAR = "YTA_LIVE_EDGE_TTS"

_TRUTHY = {"1", "true", "yes", "on"}


@pytest.fixture(autouse=True)
def _live_edge_opt_in():
    if os.environ.get(ENV_VAR, "").strip().lower() not in _TRUTHY:
        pytest.skip(
            f"{ENV_VAR} is not set — skipping edge-tts live tests "
            "(no API key is needed, but live network synthesis is "
            "opt-in only; see tests/providers_live/README.md)"
        )


def run(coro):
    return asyncio.run(coro)


def _ffprobe_duration_s(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True)
    return float(json.loads(out.stdout)["format"]["duration"])


def test_edge_tts_canary_live():
    """The adapter canary performs a REAL one-line synthesis."""
    adapter = EdgeTTSAdapter()
    assert run(adapter.canary("en-US", "en-US-GuyNeural")) is True


def test_edge_tts_synthesizes_real_audio_file_live(tmp_path):
    """Real synthesis -> real audio file on disk with duration > 0."""
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe not available")
    adapter = EdgeTTSAdapter()
    result = run(adapter.synthesize(TTSRequest(
        text="Hello from the live edge TTS test.",
        voice_id="en-US-GuyNeural", language="en")))
    assert result.audio_bytes, "expected real audio bytes from edge-tts"
    assert result.duration_s > 0

    mp3 = tmp_path / "live_edge.mp3"
    mp3.write_bytes(result.audio_bytes)
    wav = tmp_path / "live_edge.wav"
    subprocess.run(["ffmpeg", "-y", "-i", str(mp3), str(wav)],
                   check=True, capture_output=True)
    assert wav.exists() and wav.stat().st_size > 0
    assert _ffprobe_duration_s(wav) > 0


def test_edge_tts_urdu_voice_live():
    """The Urdu voice used by the pipeline (ur-PK-AsadNeural) is real."""
    adapter = EdgeTTSAdapter()
    voices = run(adapter.list_voices("ur-PK"))
    assert any(v.voice_id == "ur-PK-AsadNeural" for v in voices)
    result = run(adapter.synthesize(TTSRequest(
        text="یہ ایک مختصر آزمائشی جملہ ہے۔",
        voice_id="ur-PK-AsadNeural", language="ur")))
    assert result.audio_bytes
    assert result.duration_s > 0
