"""Round 4 (Worker 2) — calibration store + calibrated planner tests.

Covers:
* the versioned calibration store loader
  (:mod:`app.providers.tts.calibration_store`): valid file, missing
  file -> EXPLICIT ``unmeasured-default`` fallback provenance, bad
  schema version -> clear error, malformed payloads -> error, and a
  file that appears after the store was created being picked up;
* the planner integration (:mod:`app.integration.planning` +
  :mod:`app.integration.content`): word target = calibrated WPM x
  target minutes (fixture 210 WPM, 3 min -> 630 words within ±2%),
  planned at rate 0 by default with NO compensating rate override,
  an explicit user rate override planning at its own calibrated
  bucket, and the real produce path consuming the calibrated plan
  (mocked at the produce stage boundary);
* the profile layer (:meth:`NicheProfile.calibrated_target_words`);
* the voice stage honouring — and by default NOT applying — the
  spec's rate override.

Fixture calibration data follows the team contract schema exactly,
with the Urdu voices in the Round 3 evidence range (~200-225 WPM at
rate 0; the -25% bucket slower, the +10% bucket faster).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.integration import content as content_mod
from app.integration import planning, produce, voice_stage
from app.integration.content import build_content, run_gate_a
from app.integration.models import ProjectSpec
from app.niches.builtins import get_profile
from app.providers.tts.calibration_store import (
    PROVENANCE_MEASURED,
    PROVENANCE_UNMEASURED_DEFAULT,
    CalibrationError,
    CalibrationStore,
)

FIXTURE_MEASURED_AT = "2026-10-23T00:00:00+00:00"
FIXTURE_METHOD = "fixture: real edge-tts + ffprobe (test data)"


def fixture_payload() -> dict:
    """Team-contract schema; Urdu rate-0 WPM inside the Round 3
    evidence range (~199-225 WPM)."""
    return {
        "version": 1,
        "measured_at": FIXTURE_MEASURED_AT,
        "method": FIXTURE_METHOD,
        "voices": {
            "ur-PK-AsadNeural": {
                "rates_pct": {"-25": 160.0, "-10": 190.0, "0": 210.0, "10": 232.0},
                "sample_text_id": "roman_urdu_facts",
                "language": "ur",
            },
            "ur-PK-UzmaNeural": {
                "rates_pct": {"-25": 168.0, "-10": 198.0, "0": 220.0, "10": 243.0},
                "sample_text_id": "roman_urdu_facts",
                "language": "ur",
            },
            "en-US-GuyNeural": {
                "rates_pct": {"-25": 140.0, "-10": 168.0, "0": 185.0, "10": 205.0},
                "sample_text_id": "english_facts",
                "language": "en",
            },
        },
    }


def write_store(tmp_path: Path, payload: dict | None = None) -> CalibrationStore:
    path = tmp_path / "calibration_data.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload or fixture_payload()), encoding="utf-8")
    return CalibrationStore(path)


def _spec(**overrides) -> ProjectSpec:
    data = dict(niche="facts", topic="Samundar ki gehrai ke raaz", minutes=3.0)
    data.update(overrides)
    return ProjectSpec(**data)


# ---------------------------------------------------------------------------
# Loader: valid file
# ---------------------------------------------------------------------------

def test_loader_valid_file_returns_measured_wpm_with_provenance(tmp_path):
    store = write_store(tmp_path)
    hit = store.lookup("ur-PK-AsadNeural", 0)
    assert hit is not None
    assert hit.wpm == 210.0
    assert hit.rate_pct == 0.0
    assert hit.sample_text_id == "roman_urdu_facts"
    assert hit.language == "ur"
    prov = hit.provenance
    assert prov.source == PROVENANCE_MEASURED
    assert prov.version == 1
    assert prov.measured_at == FIXTURE_MEASURED_AT
    assert prov.method == FIXTURE_METHOD
    assert prov.path == str(tmp_path / "calibration_data.json")
    assert store.available_voices() == [
        "en-US-GuyNeural", "ur-PK-AsadNeural", "ur-PK-UzmaNeural",
    ]


def test_loader_resolves_nearest_measured_bucket(tmp_path):
    store = write_store(tmp_path)
    # Facts preset rate is -1.5% -> bucket 0.  History preset -15% ->
    # nearest bucket is -10 (not -25).  Rates beyond the table clamp
    # to the edge bucket.
    assert store.lookup("ur-PK-AsadNeural", -1.5).rate_pct == 0.0
    assert store.lookup("ur-PK-AsadNeural", -15).rate_pct == -10.0
    assert store.lookup("ur-PK-AsadNeural", -15).wpm == 190.0
    assert store.lookup("ur-PK-AsadNeural", 8).rate_pct == 10.0
    assert store.lookup("ur-PK-AsadNeural", -40).rate_pct == -25.0


def test_loader_picks_up_file_that_appears_later(tmp_path):
    path = tmp_path / "calibration_data.json"
    store = CalibrationStore(path)
    assert store.lookup("ur-PK-AsadNeural", 0) is None  # not there yet
    path.write_text(json.dumps(fixture_payload()), encoding="utf-8")
    hit = store.lookup("ur-PK-AsadNeural", 0)
    assert hit is not None and hit.wpm == 210.0
    assert hit.provenance.source == PROVENANCE_MEASURED


# ---------------------------------------------------------------------------
# Loader: missing file -> explicit fallback (never fake "measured")
# ---------------------------------------------------------------------------

def test_loader_missing_file_falls_back_with_explicit_provenance(tmp_path):
    store = CalibrationStore(tmp_path / "does_not_exist.json")
    assert store.lookup("ur-PK-AsadNeural", 0) is None
    assert store.available_voices() == []
    prov = store.provenance()
    assert prov.source == PROVENANCE_UNMEASURED_DEFAULT
    assert prov.version is None and prov.measured_at is None
    fallback = store.wpm_for("ur-PK-AsadNeural", 0, fallback_wpm=150.0)
    assert fallback.wpm == 150.0
    assert fallback.is_measured is False
    assert fallback.provenance.source == PROVENANCE_UNMEASURED_DEFAULT
    assert "unmeasured-default" in fallback.provenance.label()


def test_loader_unknown_voice_falls_back_labelled(tmp_path):
    store = write_store(tmp_path)
    assert store.lookup("ur-PK-NoSuchNeural", 0) is None
    fallback = store.wpm_for("ur-PK-NoSuchNeural", 0, fallback_wpm=135.0)
    assert fallback.wpm == 135.0
    assert fallback.provenance.source == PROVENANCE_UNMEASURED_DEFAULT


# ---------------------------------------------------------------------------
# Loader: bad version / malformed -> clear error
# ---------------------------------------------------------------------------

def test_loader_bad_version_raises_clear_error(tmp_path):
    payload = fixture_payload()
    payload["version"] = 2
    store = write_store(tmp_path, payload)
    with pytest.raises(CalibrationError, match="unsupported schema version 2"):
        store.lookup("ur-PK-AsadNeural", 0)
    with pytest.raises(CalibrationError):
        store.wpm_for("ur-PK-AsadNeural", 0, fallback_wpm=150.0)


def test_loader_missing_version_raises(tmp_path):
    payload = fixture_payload()
    del payload["version"]
    store = write_store(tmp_path, payload)
    with pytest.raises(CalibrationError, match="unsupported schema version"):
        store.lookup("ur-PK-AsadNeural", 0)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(voices=[]),                       # voices not object
        lambda p: p.update(measured_at=""),                  # no provenance stamp
        lambda p: p.update(method=None),                     # no method
        lambda p: p["voices"]["ur-PK-AsadNeural"].update(rates_pct={}),
        lambda p: p["voices"]["ur-PK-AsadNeural"]["rates_pct"].update({"0": -5}),
        lambda p: p["voices"]["ur-PK-AsadNeural"]["rates_pct"].update({"fast": 10}),
    ],
)
def test_loader_malformed_payloads_raise(tmp_path, mutate):
    payload = fixture_payload()
    mutate(payload)
    store = write_store(tmp_path, payload)
    with pytest.raises(CalibrationError):
        store.lookup("ur-PK-AsadNeural", 0)


def test_loader_invalid_json_raises(tmp_path):
    path = tmp_path / "calibration_data.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CalibrationError, match="not valid JSON"):
        CalibrationStore(path).lookup("ur-PK-AsadNeural", 0)


# ---------------------------------------------------------------------------
# Profile layer: calibrated helpers carry provenance
# ---------------------------------------------------------------------------

def test_profile_calibrated_helpers(tmp_path):
    store = write_store(tmp_path)
    profile = get_profile("facts")  # unmeasured planning constant: 150
    words, calib = profile.calibrated_target_words(
        3.0, "ur-PK-AsadNeural", 0, store=store
    )
    assert words == 630
    assert calib.wpm == 210.0 and calib.is_measured
    # No voice context -> the old constant, honestly labelled.
    unmeasured = profile.planning_wpm(store=store)
    assert unmeasured.wpm == 150.0
    assert unmeasured.provenance.source == PROVENANCE_UNMEASURED_DEFAULT
    # The legacy helper is untouched.
    assert profile.target_words(3.0) == 450


# ---------------------------------------------------------------------------
# Planner: calibrated math
# ---------------------------------------------------------------------------

def test_plan_210wpm_3min_yields_630_words(tmp_path):
    store = write_store(tmp_path)
    plan = planning.plan_word_target(
        _spec(voice_id="ur-PK-AsadNeural"), get_profile("facts"), store=store
    )
    # Voice pinned by the spec; rate is the facts preset (-1.5%) whose
    # nearest calibrated bucket is 0 -> 210 WPM.  210 * 3 = 630.
    assert plan.voice_id == "ur-PK-AsadNeural"
    assert plan.rate_source == "niche-preset"
    assert plan.calibration.rate_pct == 0.0
    assert plan.wpm == 210.0
    assert plan.target_words == pytest.approx(630, rel=0.02)
    assert plan.target_words == 630
    assert plan.provenance.source == PROVENANCE_MEASURED
    assert plan.provenance.measured_at == FIXTURE_MEASURED_AT


def test_plan_default_voice_needs_no_rate_override(tmp_path):
    """The Round 3 failure mode: previously the plan used 150 WPM and
    a -25% rate override compensated.  Now the default plan (preset
    rate untouched, no override anywhere) already uses the measured
    WPM of the voice the voice stage will pick."""
    store = write_store(tmp_path)
    spec = _spec()  # no voice_id, no tts_rate_percent
    assert spec.tts_rate_percent is None
    plan = planning.plan_word_target(spec, get_profile("facts"), store=store)
    assert plan.voice_id == "ur-PK-UzmaNeural"  # facts voice-style hint
    assert plan.rate_pct == pytest.approx(-1.5)  # preset, not an override
    assert plan.wpm == 220.0  # Uzma bucket 0
    assert plan.target_words == 660


def test_plan_missing_calibration_uses_labelled_profile_constant(tmp_path):
    store = CalibrationStore(tmp_path / "absent.json")
    plan = planning.plan_word_target(_spec(minutes=2.0), get_profile("facts"), store=store)
    assert plan.wpm == 150.0  # facts profile.target_wpm, as before Round 4
    assert plan.target_words == 300
    assert plan.provenance.source == PROVENANCE_UNMEASURED_DEFAULT


def test_plan_explicit_override_plans_at_its_own_bucket(tmp_path, caplog):
    store = write_store(tmp_path)
    spec = _spec(minutes=2.0, voice_id="ur-PK-AsadNeural", tts_rate_percent=-25.0)
    with caplog.at_level(logging.WARNING, logger="app.integration.planning"):
        plan = planning.plan_word_target(spec, get_profile("facts"), store=store)
    assert plan.rate_source == "spec-override"
    assert plan.calibration.rate_pct == -25.0
    assert plan.wpm == 160.0
    assert plan.target_words == 320
    assert any("override" in r.message.lower() for r in caplog.records)


def test_plan_english_spec_uses_english_voice(tmp_path):
    store = write_store(tmp_path)
    plan = planning.plan_word_target(
        _spec(language="en", voice_id="en-US-GuyNeural"),
        get_profile("facts"),
        store=store,
    )
    assert plan.wpm == 185.0
    assert plan.target_words == 555


# ---------------------------------------------------------------------------
# Content stage: the builders consume the calibrated plan
# ---------------------------------------------------------------------------

def _inject_store(monkeypatch, store):
    real_plan = planning.plan_word_target
    monkeypatch.setattr(
        content_mod,
        "plan_word_target",
        lambda spec, profile, **kw: real_plan(spec, profile, store=store, **kw),
    )


def test_build_content_uses_calibrated_target(tmp_path, monkeypatch):
    _inject_store(monkeypatch, write_store(tmp_path))
    bundle = build_content(_spec(voice_id="ur-PK-AsadNeural"), get_profile("facts"))
    assert bundle.target_words == 630
    assert bundle.planning_wpm == 210.0
    assert bundle.planning_voice_id == "ur-PK-AsadNeural"
    assert bundle.wpm_provenance["source"] == PROVENANCE_MEASURED
    assert bundle.wpm_provenance["version"] == 1
    assert bundle.wpm_provenance["measured_at"] == FIXTURE_MEASURED_AT
    assert bundle.wpm_provenance["method"] == FIXTURE_METHOD
    # The composed narration honours the plan (Gate A tolerance ±25%).
    words = len(bundle.narration_text.split())
    assert 630 * 0.75 <= words <= 630 * 1.25
    passed, reasons = run_gate_a(bundle, get_profile("facts"))
    assert passed, reasons


def test_build_content_without_calibration_keeps_old_numbers_labelled(
    tmp_path, monkeypatch
):
    _inject_store(monkeypatch, CalibrationStore(tmp_path / "absent.json"))
    bundle = build_content(_spec(minutes=2.0), get_profile("facts"))
    assert bundle.target_words == 300  # 150 WPM x 2 min, pre-Round-4 plan
    assert bundle.planning_wpm == 150.0
    assert bundle.wpm_provenance["source"] == PROVENANCE_UNMEASURED_DEFAULT


def test_build_content_records_planning_provenance_by_default():
    """Whatever the ambient calibration state, a bundle always carries
    a planning WPM consistent with its word target + provenance."""
    spec = _spec(minutes=1.0)
    bundle = build_content(spec, get_profile("facts"))
    assert bundle.planning_wpm > 0
    assert bundle.target_words == max(30, round(bundle.planning_wpm * 1.0))
    assert bundle.wpm_provenance["source"] in (
        PROVENANCE_MEASURED,
        PROVENANCE_UNMEASURED_DEFAULT,
    )
    if bundle.wpm_provenance["source"] == PROVENANCE_MEASURED:
        assert bundle.wpm_provenance["version"] == 1
        assert bundle.wpm_provenance["measured_at"]


# ---------------------------------------------------------------------------
# Produce path: produce_video consumes the calibrated plan
# (mocked at the produce stage boundary, like test_produce_glue)
# ---------------------------------------------------------------------------

def _stage_fakes(tmp_path: Path) -> SimpleNamespace:
    """Contract-shaped stage fakes (mirrors tests/integration/conftest,
    which is not visible from tests/ root where this file lives)."""
    final = tmp_path / "final.mp4"
    final.write_bytes(b"fake-mp4")
    srt = tmp_path / "captions.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    thumb = tmp_path / "thumb.png"
    thumb.write_bytes(b"fake-png")
    return SimpleNamespace(
        voice=SimpleNamespace(
            narration_wav="/nonexistent/narration.wav", duration_s=55.0,
            path_used="stub", voice_id="stub-voice", chapter_files=[],
            music_wav=None, mixed_master_wav="/nonexistent/master.wav",
            ledger_rows=[],
        ),
        visuals=SimpleNamespace(
            scene_manifest=[], asset_files=[], ledger_rows=[],
            total_duration_s=55.0,
        ),
        assemble=SimpleNamespace(
            final_mp4=str(final), duration_s=55.0, captions_srt=str(srt),
            subtitle_path_used="whisper-stub", thumbnails=[str(thumb)],
            qc_passed=True, qc_summary="all good", loudness_lufs=-14.2,
            render_path_used="ffmpeg-local", ledger_rows=[],
            licence_segments=[],
        ),
        publish=SimpleNamespace(
            gate_b_passed=True, manifest={"video_id": "fake-1"},
            manifest_path=None, publish_state="ready_for_review", tokens=[],
        ),
    )


def test_produce_path_planning_uses_calibration(monkeypatch, tmp_path):
    _inject_store(monkeypatch, write_store(tmp_path / "cal"))
    stage_fakes = _stage_fakes(tmp_path)
    captured: dict = {}

    def real_content(spec, profile):
        bundle = build_content(spec, profile)
        captured["bundle"] = bundle
        return bundle

    def rec(name, ret):
        def _fn(*args):
            return ret
        return _fn

    monkeypatch.setattr(produce, "build_content", real_content)
    monkeypatch.setattr(
        produce, "run_gate_a",
        lambda bundle, profile: run_gate_a(bundle, profile),
    )
    monkeypatch.setattr(produce, "synthesize_voice", rec("voice", stage_fakes.voice))
    monkeypatch.setattr(
        produce, "plan_and_generate_visuals", rec("visuals", stage_fakes.visuals)
    )
    monkeypatch.setattr(
        produce, "assemble_project", rec("assemble", stage_fakes.assemble)
    )
    monkeypatch.setattr(
        produce, "finalize_publish", rec("publish", stage_fakes.publish)
    )

    result = produce.produce_video(
        _spec(voice_id="ur-PK-AsadNeural"), tmp_path / "out"
    )
    bundle = captured["bundle"]
    assert bundle.target_words == 630  # 210 WPM x 3 min, calibrated
    assert bundle.wpm_provenance["source"] == PROVENANCE_MEASURED
    assert result.gate_a_passed is True
    assert result.title == bundle.title


# ---------------------------------------------------------------------------
# Voice stage: override available, unused by default, logged when set
# ---------------------------------------------------------------------------

def _run_voice_stage(monkeypatch, tmp_path, spec) -> list[float]:
    rates: list[float] = []
    wav = tmp_path / "chapter.wav"
    wav.write_bytes(b"fake")

    def fake_render(index, text, **kw):
        rates.append(kw["rate_percent"])
        return wav, "stub"

    monkeypatch.setattr(voice_stage, "_render_chapter", fake_render)
    monkeypatch.setattr(voice_stage, "_probe_duration", lambda p: 12.0)
    monkeypatch.setattr(
        voice_stage, "_generate_music_bed_raw", lambda d, out: Path(out)
    )
    import app.pipeline.audio.mix as mix_mod
    import app.pipeline.audio.music as music_mod
    import app.pipeline.audio.voice as voice_mod

    monkeypatch.setattr(
        voice_mod, "assemble_voice_stem", lambda files, gaps_ms, out: Path(out)
    )
    monkeypatch.setattr(music_mod, "prepare_music", lambda *a, **k: None)
    monkeypatch.setattr(mix_mod, "mix_and_master", lambda *a, **k: None)

    bundle = SimpleNamespace(
        chapters=[SimpleNamespace(index=1, text="Ek chapter ka text hai.")],
        narration_text="Ek chapter ka text hai.",
    )
    ctx = SimpleNamespace(audio_dir=str(tmp_path / "audio"), spec=spec)
    voice_stage.synthesize_voice(bundle, get_profile("facts"), ctx)
    return rates


def test_voice_stage_default_rate_is_preset_no_override(monkeypatch, tmp_path):
    spec = SimpleNamespace(voice_id=None, language="ur")  # no override attr
    rates = _run_voice_stage(monkeypatch, tmp_path, spec)
    assert rates == [pytest.approx(-1.5)]  # facts preset, untouched


def test_voice_stage_explicit_override_applied_and_logged(
    monkeypatch, tmp_path, caplog
):
    spec = SimpleNamespace(voice_id=None, language="ur", tts_rate_percent=-25.0)
    with caplog.at_level(logging.WARNING, logger="app.integration.voice_stage"):
        rates = _run_voice_stage(monkeypatch, tmp_path, spec)
    assert rates == [pytest.approx(-25.0)]
    assert any("override" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# R4-002: script-aware calibration (voice x input-script) + planner
#
# The Round 4 run-1 miss shape: an Urdu-script production planned from
# the ROMAN WPM row (862 words @287.43 WPM for 3 min) delivered
# 223.7 s against a 180 s target (+24%).  These tests pin that a
# default ur-PK production plans from the Urdu-script row, an explicit
# roman production still plans from the Roman row, and non-Urdu
# voices are unchanged.
# ---------------------------------------------------------------------------

def scripted_payload() -> dict:
    """Script-aware fixture: distinct Roman vs Urdu-script rows.

    Urdu-script rate-0 values are the real Round 4 measurements
    (Uzma 179.58 / Asad 174.91 WPM, urdu_script_facts); the Roman rows
    keep this file's earlier fixture values so the two scripts are
    unmistakably different.  Top-level rates mirror the adopted
    default script (urdu_script for ur-PK), per the canonical schema.
    The English voice mirrors the canonical shape too: a single
    roman row under ``scripts``.
    """
    payload = fixture_payload()
    payload["voices"]["en-US-GuyNeural"].update({
        "default_input_script": "roman",
        "scripts": {
            "roman": {
                "rates_pct": {"-25": 140.0, "-10": 168.0, "0": 185.0, "10": 205.0},
                "sample_text_id": "english_facts",
            },
        },
    })
    payload["voices"]["ur-PK-AsadNeural"].update({
        "rates_pct": {"-25": 131.23, "-10": 157.44, "0": 174.91, "10": 192.41},
        "sample_text_id": "urdu_script_facts",
        "default_input_script": "urdu_script",
        "scripts": {
            "roman": {
                "rates_pct": {"-25": 160.0, "-10": 190.0, "0": 210.0, "10": 232.0},
                "sample_text_id": "roman_urdu_facts",
            },
            "urdu_script": {
                "rates_pct": {"-25": 131.23, "-10": 157.44, "0": 174.91, "10": 192.41},
                "sample_text_id": "urdu_script_facts",
            },
        },
    })
    payload["voices"]["ur-PK-UzmaNeural"].update({
        "rates_pct": {"-25": 134.67, "-10": 161.59, "0": 179.58, "10": 197.52},
        "sample_text_id": "urdu_script_facts",
        "default_input_script": "urdu_script",
        "scripts": {
            "roman": {
                "rates_pct": {"-25": 168.0, "-10": 198.0, "0": 220.0, "10": 243.0},
                "sample_text_id": "roman_urdu_facts",
            },
            "urdu_script": {
                "rates_pct": {"-25": 134.67, "-10": 161.59, "0": 179.58, "10": 197.52},
                "sample_text_id": "urdu_script_facts",
            },
        },
    })
    return payload


def test_store_lookup_is_script_aware(tmp_path):
    store = write_store(tmp_path, scripted_payload())
    urdu = store.lookup("ur-PK-AsadNeural", 0, input_script="urdu_script")
    assert urdu is not None and urdu.wpm == 174.91
    assert urdu.input_script == "urdu_script"
    assert urdu.sample_text_id == "urdu_script_facts"
    roman = store.lookup("ur-PK-AsadNeural", 0, input_script="roman")
    assert roman is not None and roman.wpm == 210.0
    assert roman.input_script == "roman"
    assert roman.sample_text_id == "roman_urdu_facts"
    # No script requested -> the entry's adopted default (urdu_script).
    default = store.lookup("ur-PK-AsadNeural", 0)
    assert default is not None and default.wpm == 174.91
    assert default.input_script == "urdu_script"
    # A script with no measured row is never filled from another
    # script's row: English voices have no Urdu-script measurement.
    assert store.lookup("en-US-GuyNeural", 0, input_script="urdu_script") is None
    guy = store.lookup("en-US-GuyNeural", 0, input_script="roman")
    assert guy is not None and guy.wpm == 185.0


def test_canonical_file_carries_both_scripts_exact_measured_wpm():
    """The shipped canonical calibration_data.json: exact Round 4
    measured rows for BOTH scripts (raw rows:
    outputs/round4/tts_proof/wpm_measurements.json)."""
    from app.providers.tts.calibration_store import DEFAULT_CALIBRATION_PATH

    store = CalibrationStore(DEFAULT_CALIBRATION_PATH)
    uzma_urdu = store.lookup("ur-PK-UzmaNeural", 0, input_script="urdu_script")
    assert uzma_urdu is not None and uzma_urdu.wpm == 179.58
    uzma_roman = store.lookup("ur-PK-UzmaNeural", 0, input_script="roman")
    assert uzma_roman is not None and uzma_roman.wpm == 287.43
    asad_urdu = store.lookup("ur-PK-AsadNeural", 0, input_script="urdu_script")
    assert asad_urdu is not None and asad_urdu.wpm == 174.91
    asad_roman = store.lookup("ur-PK-AsadNeural", 0, input_script="roman")
    assert asad_roman is not None and asad_roman.wpm == 278.22
    # Adopted default for ur-PK voices is the Urdu-script row.
    assert store.lookup("ur-PK-UzmaNeural", 0).wpm == 179.58
    # Non-Urdu voices unchanged (single roman/English row).
    guy = store.lookup("en-US-GuyNeural", 0)
    assert guy is not None and guy.wpm == 183.99 and guy.input_script == "roman"


def test_plan_default_urpk_production_uses_urdu_script_wpm(tmp_path):
    """The run-1 regression: a DEFAULT ur-PK spec (no explicit
    tts_input_script) must plan from the Urdu-script row — Uzma
    179.58 WPM x 3 min = 539 words, NOT the Roman 287.43 row."""
    store = write_store(tmp_path, scripted_payload())
    spec = _spec(minutes=3.0)  # facts default voice = Uzma, urdu default
    assert spec.tts_input_script == "urdu_script"
    plan = planning.plan_word_target(spec, get_profile("facts"), store=store)
    assert plan.voice_id == "ur-PK-UzmaNeural"
    assert plan.input_script == "urdu_script"
    assert plan.calibration.input_script == "urdu_script"
    assert plan.calibration.sample_text_id == "urdu_script_facts"
    assert plan.wpm == 179.58
    assert plan.target_words == 539


def test_plan_default_urpk_production_canonical_store_no_run1_miss():
    """Same regression against the REAL canonical store: the facts
    default voice at 3 min plans 539 words (Urdu row), not the
    run-1 Roman plan of 862 words that landed +24% long."""
    from app.providers.tts.calibration_store import get_calibration_store

    plan = planning.plan_word_target(
        _spec(minutes=3.0), get_profile("facts"),
        store=get_calibration_store())
    assert plan.input_script == "urdu_script"
    assert plan.wpm == 179.58
    assert plan.target_words == 539


def test_plan_explicit_roman_production_uses_roman_wpm(tmp_path):
    store = write_store(tmp_path, scripted_payload())
    spec = _spec(minutes=3.0, tts_input_script="roman")
    plan = planning.plan_word_target(spec, get_profile("facts"), store=store)
    assert plan.input_script == "roman"
    assert plan.calibration.input_script == "roman"
    assert plan.calibration.sample_text_id == "roman_urdu_facts"
    assert plan.wpm == 220.0  # scripted fixture Uzma roman row
    assert plan.target_words == 660


def test_plan_english_production_unchanged_by_script_dimension(tmp_path):
    store = write_store(tmp_path, scripted_payload())
    plan = planning.plan_word_target(
        _spec(language="en", voice_id="en-US-GuyNeural"),
        get_profile("facts"),
        store=store,
    )
    assert plan.input_script == "roman"
    assert plan.wpm == 185.0
    assert plan.target_words == 555
