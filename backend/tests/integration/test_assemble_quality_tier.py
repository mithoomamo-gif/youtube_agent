"""Round-5 tests: assemble-stage quality tiers (master encode).

``assemble_project`` gains a ``quality_tier`` parameter (default
``"draft"`` — historical 720p behaviour unchanged).  The master tiers
route through ``render_master`` with the right upscale flag, the QC
expected-resolution contract follows the tier actually rendered, and
the render record (quality tier, render wall time, final output file
size) is populated on the ``AssembleResult`` / ``qc_summary`` — which
is what the production report is rendered from.

Heavy stages are faked exactly like test_assemble_subtitles does;
word timings are supplied so the whisper path never runs.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.integration import assemble_stage
from app.pipeline.gates.tokens import Verdict
from app.pipeline.subtitles.models import WordTiming

SCRIPT = "Hello world. This is a test."


class _FakeRenderer:
    def __init__(self, out_dir, encoder_choice=None):
        self.out_dir = Path(out_dir)

    def render_all(self, specs):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / "seg_0001.ts"
        path.write_bytes(b"ts")
        return [path]


def _patch_heavy_stages(monkeypatch, tmp_path):
    calls: dict = {"draft": [], "master": [], "qc_contracts": []}

    def fake_render_draft(segment_paths, out, audio=None, encoder_choice=None):
        calls["draft"].append({"out": Path(out), "audio": audio})
        Path(out).write_bytes(b"draft-mp4")

    def fake_render_master(segment_paths, out, audio=None, upscale=True,
                           encoder_choice=None):
        calls["master"].append(
            {"out": Path(out), "audio": audio, "upscale": upscale})
        Path(out).write_bytes(b"master-mp4-bytes")

    def fake_qc(path, contract=None, declared_still_ranges=None):
        calls["qc_contracts"].append(contract)
        return SimpleNamespace(
            verdict=Verdict.PASS,
            loudness=SimpleNamespace(
                integrated_lufs=-14.0, true_peak_dbtp=-1.0,
                lra_lu=2.0, source="fake"),
            checks=[],
            detections={},
        )

    monkeypatch.setattr(assemble_stage, "probe_encoder",
                        lambda: SimpleNamespace(encoder="fake"))
    monkeypatch.setattr(assemble_stage, "SegmentRenderer", _FakeRenderer)
    monkeypatch.setattr(assemble_stage, "render_draft", fake_render_draft)
    monkeypatch.setattr(assemble_stage, "render_master", fake_render_master)
    monkeypatch.setattr(assemble_stage, "probe_duration", lambda p: 4.0)
    monkeypatch.setattr(assemble_stage, "run_technical_qc", fake_qc)
    thumbs = []
    for i in range(3):
        t = tmp_path / f"variant_{i}.png"
        t.write_bytes(b"png")
        thumbs.append(SimpleNamespace(
            path=str(t), layout_id="fake", headline="Ocean",
            legibility=None, warnings=[]))
    monkeypatch.setattr(
        assemble_stage, "generate_variants",
        lambda *a, **k: SimpleNamespace(variants=thumbs))
    return calls


def _assemble_inputs(tmp_path, *, spec=None):
    audio = tmp_path / "master.wav"
    audio.write_bytes(b"fake-audio")
    timings = [
        WordTiming("Hello", 0.0, 0.5), WordTiming("world.", 0.5, 1.0),
        WordTiming("This", 1.2, 1.6), WordTiming("is", 1.6, 1.8),
        WordTiming("a", 1.8, 1.9), WordTiming("test.", 1.9, 2.5),
    ]
    voice = SimpleNamespace(
        mixed_master_wav=str(audio), duration_s=4.0, word_timings=timings)
    bundle = SimpleNamespace(
        script_text=SCRIPT, title="Ocean Test", thumbnail_text="Ocean")
    ctx_data = dict(pack_dir=str(tmp_path / "pack"),
                    work_dir=str(tmp_path / "work"))
    if spec is not None:
        ctx_data["spec"] = spec
    profile = SimpleNamespace(language="english")
    return bundle, voice, [], profile, SimpleNamespace(**ctx_data)


def test_default_tier_is_draft_and_behaviour_unchanged(monkeypatch, tmp_path):
    calls = _patch_heavy_stages(monkeypatch, tmp_path)
    bundle, voice, visuals, profile, ctx = _assemble_inputs(tmp_path)

    result = assemble_stage.assemble_project(bundle, voice, visuals, profile, ctx)

    assert len(calls["draft"]) == 1 and calls["master"] == []
    assert result.quality_tier == "draft"
    assert result.render_path_used == "draft"
    contract = calls["qc_contracts"][0]
    assert (contract.expected_width, contract.expected_height) == (1280, 720)
    # Report fields populated, and they describe the real final file.
    final = Path(result.final_mp4)
    assert result.output_file_size_bytes == final.stat().st_size == len(b"draft-mp4")
    assert isinstance(result.render_wall_time_s, float)
    assert result.render_wall_time_s >= 0
    assert result.qc_summary["quality_tier"] == "draft"
    assert result.qc_summary["output_file_size_bytes"] == result.output_file_size_bytes
    assert result.qc_summary["render_wall_time_s"] == result.render_wall_time_s
    assert result.notes["render"]["quality_tier"] == "draft"


def test_master_1080p_tier(monkeypatch, tmp_path):
    calls = _patch_heavy_stages(monkeypatch, tmp_path)
    bundle, voice, visuals, profile, ctx = _assemble_inputs(tmp_path)

    result = assemble_stage.assemble_project(
        bundle, voice, visuals, profile, ctx, quality_tier="master_1080p")

    assert calls["draft"] == []
    assert len(calls["master"]) == 1
    assert calls["master"][0]["upscale"] is False
    assert result.quality_tier == "master_1080p"
    assert result.render_path_used == "master_1080p"
    contract = calls["qc_contracts"][0]
    assert (contract.expected_width, contract.expected_height) == (1920, 1080)
    assert result.output_file_size_bytes == len(b"master-mp4-bytes")
    assert result.qc_summary["quality_tier"] == "master_1080p"
    assert result.qc_summary["output_file_size_bytes"] == len(b"master-mp4-bytes")
    # Subtitle sidecar + thumbnails behave exactly as in the draft tier.
    assert Path(result.pack_dir, "captions.srt").is_file()
    assert "Hello world" in result.captions_srt
    assert len(result.thumbnails) == 3


def test_master_1440p_upscale_tier(monkeypatch, tmp_path):
    calls = _patch_heavy_stages(monkeypatch, tmp_path)
    bundle, voice, visuals, profile, ctx = _assemble_inputs(tmp_path)

    result = assemble_stage.assemble_project(
        bundle, voice, visuals, profile, ctx,
        quality_tier="master_1440p_upscale")

    assert calls["draft"] == []
    assert len(calls["master"]) == 1
    assert calls["master"][0]["upscale"] is True
    assert result.quality_tier == "master_1440p_upscale"
    contract = calls["qc_contracts"][0]
    assert (contract.expected_width, contract.expected_height) == (2560, 1440)
    assert result.output_file_size_bytes == len(b"master-mp4-bytes")


def test_unknown_tier_fails_loud(monkeypatch, tmp_path):
    _patch_heavy_stages(monkeypatch, tmp_path)
    bundle, voice, visuals, profile, ctx = _assemble_inputs(tmp_path)
    with pytest.raises(ValueError):
        assemble_stage.assemble_project(
            bundle, voice, visuals, profile, ctx, quality_tier="master_4k")


def test_none_tier_resolves_from_ctx_spec(monkeypatch, tmp_path):
    """The per-channel plumbing seam: quality_tier=None reads the
    tier off ctx.spec (where a channel setting would land)."""
    calls = _patch_heavy_stages(monkeypatch, tmp_path)
    spec = SimpleNamespace(style_profile_path=None, channel_id="chan-x",
                           quality_tier="master_1080p")
    bundle, voice, visuals, profile, ctx = _assemble_inputs(tmp_path, spec=spec)

    result = assemble_stage.assemble_project(
        bundle, voice, visuals, profile, ctx, quality_tier=None)

    assert len(calls["master"]) == 1
    assert result.quality_tier == "master_1080p"


def test_default_call_resolves_tier_from_ctx_spec(monkeypatch, tmp_path):
    """The produce.py path calls assemble with five positional args;
    a tier set on the spec (the per-channel setting) must still win
    over the draft default."""
    calls = _patch_heavy_stages(monkeypatch, tmp_path)
    spec = SimpleNamespace(style_profile_path=None, channel_id="chan-x",
                           quality_tier="master_1440p_upscale")
    bundle, voice, visuals, profile, ctx = _assemble_inputs(tmp_path, spec=spec)

    result = assemble_stage.assemble_project(bundle, voice, visuals, profile, ctx)

    assert len(calls["master"]) == 1
    assert calls["master"][0]["upscale"] is True
    assert result.quality_tier == "master_1440p_upscale"


def test_explicit_arg_wins_over_ctx_spec(monkeypatch, tmp_path):
    calls = _patch_heavy_stages(monkeypatch, tmp_path)
    spec = SimpleNamespace(style_profile_path=None, channel_id="chan-x",
                           quality_tier="master_1080p")
    bundle, voice, visuals, profile, ctx = _assemble_inputs(tmp_path, spec=spec)

    result = assemble_stage.assemble_project(
        bundle, voice, visuals, profile, ctx, quality_tier="draft")

    assert len(calls["draft"]) == 1 and calls["master"] == []
    assert result.quality_tier == "draft"


def test_report_md_carries_render_record(monkeypatch, tmp_path):
    """The pack report writer renders the render record from
    qc_summary — no produce.py change needed for report.md to show
    tier / wall time / file size."""
    from app.integration.pack import write_report_md

    _patch_heavy_stages(monkeypatch, tmp_path)
    bundle, voice, visuals, profile, ctx = _assemble_inputs(tmp_path)
    result = assemble_stage.assemble_project(
        bundle, voice, visuals, profile, ctx, quality_tier="master_1080p")

    report = tmp_path / "report.md"
    write_report_md(
        {
            "title": "Ocean Test",
            "project_id": "p1",
            "niche_id": "facts",
            "topic": "Ocean",
            "duration_s": result.duration_s,
            "loudness_lufs": result.loudness_lufs,
            "qc_passed": result.qc_passed,
            "qc_summary": result.qc_summary,
            "final_mp4": result.final_mp4,
            "pack_dir": result.pack_dir,
        },
        report,
    )
    text = report.read_text(encoding="utf-8")
    assert "master_1080p" in text
    assert "output_file_size_bytes" in text
    assert "render_wall_time_s" in text
