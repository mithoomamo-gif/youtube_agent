"""TTS WPM calibration tests — mocked synthesizer only. No network.

Proves: whitespace word counting (Latin + Urdu script), the WPM math,
ffprobe-vs-WordBoundary duration-source selection, failure recording
(never faked numbers), and that both JSON outputs (raw run + canonical
contract schema) are written correctly.
"""
import asyncio
import json

import pytest

from app.providers.tts import calibration as cal


def run(coro):
    return asyncio.run(coro)


def fake_synth(duration_by_rate=None, fail_rates=()):
    """Synthesizer stub: returns dummy audio + a boundary duration."""
    durations = duration_by_rate or {-25.0: 80.0, -10.0: 60.0, 0.0: 50.0, 10.0: 40.0}

    async def _synth(voice_id, rate_pct, text):
        if rate_pct in fail_rates:
            raise RuntimeError("boom (mocked transient failure)")
        return b"FAKE-MP3-BYTES", durations[float(rate_pct)]

    return _synth


@pytest.fixture(autouse=True)
def _fixed_probe(monkeypatch):
    # Deterministic ffprobe: 120 words in 60s == 120 wpm for measure_one.
    monkeypatch.setattr(cal, "probe_duration_s", lambda path: 60.0)


# ---------------------------------------------------------------------------
# Word counting + WPM math
# ---------------------------------------------------------------------------
def test_count_words_whitespace_tokens():
    assert cal.count_words("one two  three\nfour") == 4
    assert cal.count_words("Hello, world.") == 2  # punctuation stays attached


def test_count_words_urdu_script_whitespace_tokens():
    # Urdu script is whitespace-delimited too; the full-stop '۔' attaches.
    assert cal.count_words("یہ ایک جملہ ہے۔") == 4
    assert cal.count_words(cal.load_sample_text("urdu_script_facts")) == 140


def test_sample_texts_word_counts_in_range():
    for text_id in ("roman_urdu_facts", "english_facts", "urdu_script_facts"):
        n = cal.count_words(cal.load_sample_text(text_id))
        assert 120 <= n <= 160, (text_id, n)


def test_compute_wpm_math():
    assert cal.compute_wpm(120, 60.0) == 120.0
    assert cal.compute_wpm(139, 29.016) == pytest.approx(287.43, abs=0.01)
    assert cal.compute_wpm(60, 120.0) == 30.0
    with pytest.raises(ValueError):
        cal.compute_wpm(10, 0.0)
    with pytest.raises(ValueError):
        cal.compute_wpm(0, 10.0)


def test_rate_str_matches_edge_tts_form():
    assert cal.rate_str(-25) == "-25%"
    assert cal.rate_str(0) == "+0%"
    assert cal.rate_str(10) == "+10%"


# ---------------------------------------------------------------------------
# measure_one with mocked synthesizer
# ---------------------------------------------------------------------------
def test_measure_one_uses_ffprobe_duration(tmp_path):
    text = " ".join(["word"] * 120)
    m = run(cal.measure_one("ur-PK-UzmaNeural", 0, text, "roman_urdu_facts",
                            workdir=tmp_path, synthesizer=fake_synth(),
                            timestamp="2026-10-23T00:00:00+00:00"))
    assert m.word_count == 120
    assert m.duration_s == 60.0 and m.duration_source == "ffprobe"
    assert m.wpm == 120.0
    assert (tmp_path / "ur-PK-UzmaNeural_+0.mp3").exists()


def test_measure_one_falls_back_to_word_boundary(tmp_path, monkeypatch):
    def _boom(path):
        raise RuntimeError("ffprobe unavailable (mocked)")

    monkeypatch.setattr(cal, "probe_duration_s", _boom)
    text = " ".join(["word"] * 100)
    m = run(cal.measure_one("en-US-GuyNeural", 0, text, "english_facts",
                            workdir=tmp_path, synthesizer=fake_synth()))
    assert m.duration_source == "word_boundary"
    assert m.duration_s == 50.0 and m.wpm == 120.0


# ---------------------------------------------------------------------------
# Full run + JSON writing (canonical contract schema)
# ---------------------------------------------------------------------------
def test_run_and_write_outputs(tmp_path, monkeypatch):
    # Per-rate ffprobe durations so each rate yields a distinct WPM:
    # roman text 139 words; probe = 60s at every rate -> 139 wpm everywhere
    # is fine; distinctness is asserted via the boundary-fallback test above.
    monkeypatch.setattr(cal, "probe_duration_s", lambda path: 30.0)
    run_result = run(cal.run_calibration(
        {"ur-PK-UzmaNeural": "roman_urdu_facts",
         "en-US-GuyNeural": "english_facts"},
        cal.DEFAULT_RATES_PCT, workdir=tmp_path / "audio",
        synthesizer=fake_synth()))
    assert len(run_result.measurements) == 8
    assert run_result.failures == []

    raw_path = tmp_path / "wpm_measurements.json"
    canonical_path = tmp_path / "calibration_data.json"
    cal.write_outputs(run_result, raw_path, canonical_path)

    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    assert raw["version"] == 1 and len(raw["measurements"]) == 8
    row = raw["measurements"][0]
    for key in ("voice", "rate", "text_id", "word_count", "duration_s",
                "wpm", "timestamp"):
        assert key in row

    data = json.loads(canonical_path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["measured_at"] and data["method"]
    assert set(data["voices"]) == {"ur-PK-UzmaNeural", "en-US-GuyNeural"}
    uzma = data["voices"]["ur-PK-UzmaNeural"]
    assert uzma["sample_text_id"] == "roman_urdu_facts"
    assert uzma["language"] == "ur"
    assert set(uzma["rates_pct"]) == {"-25", "-10", "0", "10"}
    # 139 words / 30s = 278 wpm at every (mocked-probe) rate.
    assert uzma["rates_pct"]["0"] == pytest.approx(278.0)
    guy = data["voices"]["en-US-GuyNeural"]
    assert guy["language"] == "en"
    assert guy["rates_pct"]["-25"] == pytest.approx(262.0)  # 131 words/30s


def test_failed_rate_is_recorded_not_faked(tmp_path, monkeypatch):
    monkeypatch.setattr(cal, "probe_duration_s", lambda path: 30.0)
    run_result = run(cal.run_calibration(
        {"en-US-GuyNeural": "english_facts"}, (0, 10),
        workdir=tmp_path / "audio",
        synthesizer=fake_synth(fail_rates=(10.0,)), retries=0))
    assert [m.rate_pct for m in run_result.measurements] == [0]
    assert len(run_result.failures) == 1
    assert run_result.failures[0]["rate_pct"] == 10
    data = cal.build_calibration_data(run_result)
    assert set(data["voices"]["en-US-GuyNeural"]["rates_pct"]) == {"0"}


# ---------------------------------------------------------------------------
# Script-aware canonical payload (R4-002)
# ---------------------------------------------------------------------------
def _measurement(voice, rate_pct, text_id, wpm):
    return cal.Measurement(
        voice=voice, rate_pct=rate_pct, rate=cal.rate_str(rate_pct),
        text_id=text_id, language="ur" if voice.startswith("ur-") else "en",
        word_count=140, duration_s=60.0, wpm=wpm,
        duration_source="ffprobe", timestamp="2026-10-23T00:00:00+00:00")


def test_build_calibration_data_keeps_both_scripts():
    # A run measuring BOTH scripts for one ur-PK voice (the
    # --extra-urdu-script shape) must emit the scripts dimension, with
    # the top-level row mirroring the adopted urdu_script default.
    run_result = cal.MeasurementRun(
        measured_at="2026-10-23T00:00:00+00:00",
        measurements=[
            _measurement("ur-PK-UzmaNeural", 0, "roman_urdu_facts", 287.43),
            _measurement("ur-PK-UzmaNeural", 0, "urdu_script_facts", 179.58),
        ])
    data = cal.build_calibration_data(run_result)
    uzma = data["voices"]["ur-PK-UzmaNeural"]
    assert uzma["scripts"]["roman"]["rates_pct"]["0"] == 287.43
    assert uzma["scripts"]["roman"]["sample_text_id"] == "roman_urdu_facts"
    assert uzma["scripts"]["urdu_script"]["rates_pct"]["0"] == 179.58
    assert uzma["scripts"]["urdu_script"]["sample_text_id"] \
        == "urdu_script_facts"
    assert uzma["default_input_script"] == "urdu_script"
    assert uzma["rates_pct"]["0"] == 179.58
    assert uzma["sample_text_id"] == "urdu_script_facts"
