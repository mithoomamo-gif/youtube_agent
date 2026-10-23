"""Glue tests for app.integration.produce / run (Worker 6).

All stage functions are monkeypatched with contract-shaped fakes, so
these tests run fast with no network, no TTS and no ffmpeg.  They
verify the ORCHESTRATION only: spec -> result shape, ledger/report
pack files, profile resolution (builtin + OTHER), Gate A flagging,
and the CLI.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from app.integration import produce, run as run_cli
from app.integration.models import ProductionResult, ProjectSpec


def _patch_stages(monkeypatch, fakes, gate=(True, []), calls=None):
    def rec(name, ret):
        def _fn(*args):
            if calls is not None:
                calls.append(name)
            return ret
        return _fn

    monkeypatch.setattr(produce, "build_content", rec("content", fakes.bundle))
    monkeypatch.setattr(produce, "run_gate_a", rec("gate_a", gate))
    monkeypatch.setattr(produce, "synthesize_voice", rec("voice", fakes.voice))
    monkeypatch.setattr(
        produce, "plan_and_generate_visuals", rec("visuals", fakes.visuals)
    )
    monkeypatch.setattr(produce, "assemble_project", rec("assemble", fakes.assemble))
    monkeypatch.setattr(produce, "finalize_publish", rec("publish", fakes.publish))


def _spec(**overrides) -> ProjectSpec:
    data = dict(niche="facts", topic="Ocean depth secrets", minutes=1.0)
    data.update(overrides)
    return ProjectSpec(**data)


# ---------------------------------------------------------------------------
# Happy path: shape, ledger, report
# ---------------------------------------------------------------------------

def test_spec_to_result_shape(monkeypatch, stage_fakes, tmp_path):
    calls: list[str] = []
    _patch_stages(monkeypatch, stage_fakes, calls=calls)
    result = produce.produce_video(_spec(), tmp_path / "out")

    assert isinstance(result, ProductionResult)
    assert result.niche_id == "facts"
    assert result.topic == "Ocean depth secrets"
    assert result.title == stage_fakes.bundle.title
    assert result.final_mp4 == stage_fakes.assemble.final_mp4
    assert result.thumbnails == stage_fakes.assemble.thumbnails
    assert result.captions_srt == stage_fakes.assemble.captions_srt
    assert result.duration_s == pytest.approx(55.0)
    assert result.loudness_lufs == pytest.approx(-14.2)
    assert result.gate_a_passed is True
    assert result.gate_b_passed is True
    assert result.qc_passed is True
    assert result.publish_state == "ready_for_review"
    # Stages ran in contract order.
    assert calls == ["content", "gate_a", "voice", "visuals", "assemble", "publish"]
    # Honest paths_used.
    assert result.paths_used["llm"] == "deterministic-stub"
    assert result.paths_used["tts"] == "stub"  # from the voice fake
    assert result.paths_used["visuals"] == "local-plates"
    assert result.paths_used["subtitles"] == "whisper-stub"
    assert result.paths_used["publish"] == "fake-provider"


def test_ledger_csv_and_report_written(monkeypatch, stage_fakes, tmp_path):
    _patch_stages(monkeypatch, stage_fakes)
    base = tmp_path / "out"
    result = produce.produce_video(_spec(), base)

    ledger = Path(result.licence_ledger_csv)
    assert ledger.exists() and ledger.name == "licence_ledger.csv"
    with ledger.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == [
        "asset_id", "scene_id", "source", "licence", "kind", "local_path", "notes",
    ]
    # One voice row + one visuals row from the fakes.
    assert len(rows) == 3
    asset_ids = {r[0] for r in rows[1:]}
    assert asset_ids == {"voice-1", "plate-1"}

    report = Path(result.report_md)
    assert report.exists() and report.name == "report.md"
    text = report.read_text(encoding="utf-8")
    assert "Ocean" in text

    # Manifest dict from publish fake is frozen to a JSON pack file.
    assert result.publish_manifest_json
    manifest = json.loads(Path(result.publish_manifest_json).read_text())
    assert manifest["video_id"] == "fake-1"

    # Headline artifacts are also flattened into the caller's base dir.
    assert (base / "final.mp4").exists()
    assert (base / "licence_ledger.csv").exists()
    assert (base / "report.md").exists()


def test_assemble_captions_srt_text_is_normalised(monkeypatch, stage_fakes, tmp_path):
    """Regression: assemble_stage returns SRT *text*, not a path.

    produce must map it to the pack captions file (and must not try to
    stat the text as a path -- that raises OSError for long text).
    """
    srt_text = "1\n00:00:00,000 --> 00:00:01,000\nHello world\n"
    stage_fakes.assemble.captions_srt = srt_text
    _patch_stages(monkeypatch, stage_fakes)
    result = produce.produce_video(_spec(), tmp_path / "srt")
    assert result.captions_srt
    path = Path(result.captions_srt)
    assert path.is_file() and path.name == "captions.srt"
    assert path.read_text(encoding="utf-8") == srt_text


def test_qc_info_licence_rows_provider_translated(monkeypatch, stage_fakes, tmp_path):
    """assemble licence_segments say provider='local_plate'; the
    publish stage's tier table only knows 'generated' for such
    own/generated assets, so produce must translate the label in the
    qc_info it hands to finalize_publish."""
    stage_fakes.assemble.licence_segments = [
        {"asset_id": "scene_0001", "scene_id": "scene_0001",
         "licence_id": "lic-scene_0001", "provider": "local_plate",
         "content_hash": "abc", "start_s": 0.0, "end_s": 4.0,
         "role": "b_roll"},
    ]
    captured = {}

    def fake_publish(assemble, qc_info, bundle, profile, ctx, gate_a):
        captured["qc_info"] = qc_info
        return stage_fakes.publish

    _patch_stages(monkeypatch, stage_fakes)
    monkeypatch.setattr(produce, "finalize_publish", fake_publish)
    produce.produce_video(_spec(), tmp_path / "lic")
    rows = captured["qc_info"]["licence_segments"]
    assert rows and rows[0]["provider"] == "generated"
    assert rows[0]["asset_id"] == "scene_0001"


# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------

def test_resolve_profile_builtin():
    profile = produce.resolve_profile(_spec(niche="facts"))
    assert profile.profile_id == "facts"


def test_resolve_profile_other():
    spec = _spec(
        niche="other",
        custom_niche_name="cricket analysis",
        niche_idea="matches ki kahani aur stats Urdu mein",
    )
    profile = produce.resolve_profile(spec)
    assert profile.profile_id.startswith("other_")


def test_other_run_uses_other_profile(monkeypatch, stage_fakes, tmp_path):
    _patch_stages(monkeypatch, stage_fakes)
    spec = _spec(
        niche="other",
        custom_niche_name="cricket analysis",
        niche_idea="matches ki kahani aur stats Urdu mein",
        topic="1992 World Cup final ki kahani",
    )
    result = produce.produce_video(spec, tmp_path / "cricket")
    assert result.niche_id.startswith("other_")


# ---------------------------------------------------------------------------
# Gate A failure flagging
# ---------------------------------------------------------------------------

def test_gate_a_failure_blocks_auto_mode(monkeypatch, stage_fakes, tmp_path):
    calls: list[str] = []
    _patch_stages(
        monkeypatch, stage_fakes, gate=(False, ["RED claim somewhere"]), calls=calls
    )
    result = produce.produce_video(_spec(publish_mode="auto"), tmp_path / "blocked")

    assert result.gate_a_passed is False
    assert result.gate_b_passed is False
    assert result.publish_state == "blocked_gate_a"
    assert result.final_mp4 is None
    assert any("Gate A FAILED" in w for w in result.warnings)
    # Voice / visuals / assemble / publish never ran.
    assert calls == ["content", "gate_a"]
    # Pack files are still written for a blocked run.
    assert Path(result.licence_ledger_csv).exists()
    assert Path(result.report_md).exists()


def test_gate_a_failure_continues_in_review_first(monkeypatch, stage_fakes, tmp_path):
    _patch_stages(monkeypatch, stage_fakes, gate=(False, ["lint issue"]))
    result = produce.produce_video(
        _spec(publish_mode="review_first"), tmp_path / "review"
    )
    assert result.gate_a_passed is False
    assert any("Gate A FAILED" in w for w in result.warnings)
    assert any("review_first" in w for w in result.warnings)
    # ...but production continued and produced a final file.
    assert result.final_mp4 == stage_fakes.assemble.final_mp4


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_arg_parsing_facts():
    args = run_cli.build_parser().parse_args(
        ["--niche", "facts", "--topic", "Ocean secrets", "--minutes", "1"]
    )
    spec = run_cli.spec_from_args(args)
    assert spec.niche == "facts"
    assert spec.topic == "Ocean secrets"
    assert spec.minutes == 1.0


def test_cli_arg_parsing_other():
    args = run_cli.build_parser().parse_args(
        [
            "--niche", "other", "--custom-niche", "cricket analysis",
            "--niche-idea", "matches ki kahani aur stats Urdu mein",
            "--topic", "1992 World Cup final ki kahani", "--minutes", "2",
        ]
    )
    spec = run_cli.spec_from_args(args)
    assert spec.niche == "other"
    assert spec.custom_niche_name == "cricket analysis"
    assert spec.niche_idea == "matches ki kahani aur stats Urdu mein"
    assert spec.minutes == 2.0


def test_cli_mocked_run_prints_json(monkeypatch, tmp_path, capsys):
    fake_result = ProductionResult(
        project_id="cli-test", niche_id="facts", topic="Ocean secrets",
        title="Ocean secrets!", pack_dir=str(tmp_path),
        licence_ledger_csv=str(tmp_path / "licence_ledger.csv"),
        report_md=str(tmp_path / "report.md"),
        gate_a_passed=True, publish_state="ready_for_review",
    )
    monkeypatch.setattr(
        "app.integration.produce.produce_video", lambda spec, base: fake_result
    )
    rc = run_cli.main(
        ["--niche", "facts", "--topic", "Ocean secrets",
         "--base-dir", str(tmp_path)]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["topic"] == "Ocean secrets"
    assert out["gate_a_passed"] is True


# ---------------------------------------------------------------------------
# Slow: the real pipeline (skipped from fast runs via -m "not slow")
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_real_produce_video_short(tmp_path):
    """Real stages at 0.5 min. May need edge-tts/network or ffmpeg.

    Kept slow-marked: run with ``-m slow``.

    Round 3 fixed the Round 2 scene-beat infeasibility this spec used
    to hit: at real edge-tts speed (~250-300 wpm effective, far above
    the profile's 150 wpm) the 0.5-minute chapters measure only ~17 s,
    so most scenes are sub-4 s.  Scene planning now runs in
    measured-narration mode (see
    ``backend/app/pipeline/scenes/beats.py``): measured durations win
    and sub-4 s scenes are legal, instead of beat-minimum clamping
    inflating the timeline past the measured total and collapsing the
    final scene to ``end_s <= start_s``.  The hermetic version of this
    exact case is pinned in ``test_visuals_short_scenes.py``.  This
    test therefore asserts the real behaviour directly -- any
    failure, including a SceneRecord ``end_s`` validation error,
    fails the test (the old skip-on-``end_s`` workaround was retired
    in Round 4).
    """
    pytest.importorskip("app.integration.voice_stage")
    pytest.importorskip("app.integration.assemble_stage")
    spec = ProjectSpec(
        niche="facts", topic="Ocean depth secrets", minutes=0.5, seed=0
    )
    result = produce.produce_video(spec, tmp_path / "real")
    assert result.gate_a_passed is True
    assert result.final_mp4 and Path(result.final_mp4).exists()
    assert result.duration_s and result.duration_s > 0
