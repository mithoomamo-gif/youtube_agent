"""Round 4 — stage bridge tests (Worker 1).

Proves the execution DAG's ``run_<stage>`` dispatch now reaches REAL
integration machinery instead of the handler stub:

(a) every non-gate stage is discoverable via
    ``app.execution.integration_client.call_stage``;
(b) a full chain of stage calls for one project produces non-stub
    artefacts end-to-end with provenance fields — content stages run
    the REAL deterministic content builder (the content layer's own
    offline path, provenance ``deterministic-stub`` at the *LLM* level,
    which is honest and distinct from the handler's stage stub);
    expensive provider stages (voice / visuals / assemble / publish)
    are faked with contract-shaped results, following the existing
    pattern in ``tests/integration/conftest.py``;
(c) idempotency: re-running a stage returns the checkpointed result
    without redoing the stage call;
(d) the monolith ``produce_video`` path still works (its own glue
    tests live in test_produce_glue.py; one smoke check is repeated
    here so this file is self-contained).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.execution import integration_client
from app.integration import content as content_mod
from app.integration import stage_bridge

STAGES = [
    "research", "script", "voice", "visuals",
    "render", "thumbnail", "qc", "package",
]


def _project(tmp_path, **overrides):
    project = {
        "id": "proj-bridge-1",
        "channel_id": "chan-bridge-1",
        "niche_profile_id": "facts",
        "topic": "Ocean depth secrets",
        "target_minutes": 1,
        "target_words": None,
        "publish_mode": "review_first",
        "schedule_id": None,
    }
    project.update(overrides)
    return project


def _payload(tmp_path):
    return {"base_dir": str(tmp_path / "work")}


@pytest.fixture()
def cheap_stages(monkeypatch, tmp_path):
    """Real content builder; cheap contract-shaped downstream stages."""
    calls: list[str] = []
    captured: dict = {}

    def fake_build_content(spec, profile):
        calls.append("content")
        captured.setdefault("spec_topics", []).append(spec.topic)
        return content_mod.build_content(spec, profile)

    def fake_voice(bundle, profile, ctx):
        calls.append("voice")
        wav = Path(ctx.audio_dir) / "narration.wav"
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(b"fake-wav")
        return SimpleNamespace(
            narration_wav=str(wav),
            duration_s=42.0,
            path_used="stub-silence",
            voice_id="ur-PK-AsadNeural",
            chapter_files=[],
            music_wav=None,
            mixed_master_wav=None,
            ledger_rows=[{
                "asset_id": "narration_voice", "scene_id": "all",
                "source": "own/generated (edge-tts synthesis)",
                "licence": "own work", "kind": "voice",
                "local_path": str(wav), "notes": "test fake",
            }],
        )

    def fake_visuals(bundle, voice, profile, ctx):
        calls.append("visuals")
        manifest = Path(ctx.visuals_dir) / "scene_manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("{}", encoding="utf-8")
        return SimpleNamespace(
            scene_manifest=[{"scene_id": "scene_0001", "source": "plate:local"}],
            asset_files={"scene_0001": str(Path(ctx.visuals_dir) / "s.png")},
            ledger_rows=[{
                "asset_id": "scene_0001", "scene_id": "scene_0001",
                "source": "own/generated (local Pillow plate)",
                "licence": "own work", "kind": "image",
                "local_path": str(Path(ctx.visuals_dir) / "s.png"),
                "notes": "",
            }],
            total_duration_s=42.0,
            manifest_path=str(manifest),
            scene_sources=["plate:local"],
            stock_share=0.0,
            stock_count=0,
            plate_count=1,
            stock_enabled=False,
        )

    def fake_assemble(bundle, voice, visuals, profile, ctx):
        calls.append("assemble")
        pack = Path(ctx.pack_dir)
        pack.mkdir(parents=True, exist_ok=True)
        final = pack / "final.mp4"
        final.write_bytes(b"fake-mp4")
        (pack / "captions.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        thumbs = []
        for i in range(3):
            thumb = pack / f"thumbnail_{i + 1}.png"
            thumb.write_bytes(b"fake-png")
            thumbs.append(str(thumb))
        return SimpleNamespace(
            final_mp4=str(final),
            duration_s=42.0,
            captions_srt="1\n00:00:00,000 --> 00:00:01,000\nHello\n",
            subtitle_path_used="alignment-stub",
            subtitle_source="alignment_stub",
            thumbnails=thumbs,
            qc_passed=True,
            qc_summary={"verdict": "pass", "checks": {}},
            loudness_lufs=-14.1,
            render_path_used="draft",
            licence_segments=[{
                "asset_id": "scene_0001", "scene_id": "scene_0001",
                "licence_id": "lic-scene_0001", "provider": "local_plate",
                "content_hash": "abc", "start_s": 0.0, "end_s": 42.0,
                "role": "b_roll",
            }],
        )

    def fake_publish(assemble, qc_info, bundle, profile, ctx, gate_a):
        calls.append("publish")
        captured["gate_a_passed"] = gate_a
        captured["qc_info"] = qc_info
        manifest = Path(ctx.pack_dir) / "publish_manifest.json"
        manifest.write_text(json.dumps({"fake": True}), encoding="utf-8")
        return SimpleNamespace(
            gate_b_passed=True,
            manifest={"fake": True},
            manifest_path=str(manifest),
            publish_state="fake_provider_upload_simulated",
            tokens=[],
        )

    monkeypatch.setattr(stage_bridge, "build_content_auto", fake_build_content)
    monkeypatch.setattr(stage_bridge, "synthesize_voice", fake_voice)
    monkeypatch.setattr(stage_bridge, "plan_and_generate_visuals", fake_visuals)
    monkeypatch.setattr(stage_bridge, "assemble_project", fake_assemble)
    monkeypatch.setattr(stage_bridge, "finalize_publish", fake_publish)
    return SimpleNamespace(calls=calls, captured=captured)


# ---------------------------------------------------------------------------
# (a) Discoverability via integration_client
# ---------------------------------------------------------------------------

def test_all_stages_discoverable_via_integration_client(cheap_stages, tmp_path):
    import app.integration as integration_pkg

    for stage in STAGES:
        assert callable(getattr(integration_pkg, f"run_{stage}")), stage

    project = _project(tmp_path)
    payload = _payload(tmp_path)
    for stage in STAGES:
        result = integration_client.call_stage(stage, project, payload)
        assert isinstance(result, dict), stage
        assert result.get("stage") == stage
        assert not result.get("stub"), stage  # never the handler stub shape


# ---------------------------------------------------------------------------
# (b) Full chain: non-stub artefacts + provenance end-to-end
# ---------------------------------------------------------------------------

def test_full_chain_produces_real_artefacts_with_provenance(cheap_stages, tmp_path):
    project = _project(tmp_path)
    payload = _payload(tmp_path)
    results = {}
    for stage in STAGES:
        results[stage] = integration_client.call_stage(stage, project, payload)
        assert results[stage] is not None, stage

    # Research: real brief for the provided topic.
    research = results["research"]
    assert research["artefact"]["resolved_topic"] == "Ocean depth secrets"
    assert research["artefact"]["outline"]
    assert Path(research["file_path"]).is_file()

    # Script: real content bundle (deterministic LLM path, honestly
    # labelled by the content layer itself — not a handler stub).
    script = results["script"]
    assert script["artefact"]["script_text"]
    assert script["artefact"]["narration_text"]
    assert script["artefact"]["title"]
    assert script["provenance"]["llm_path"] == "deterministic-stub"
    assert Path(script["file_path"]).is_file()

    # Voice / visuals provenance.
    assert results["voice"]["provenance"]["tts"] == "stub-silence"
    assert Path(results["voice"]["file_path"]).is_file()
    assert results["visuals"]["provenance"]["visuals"] == "local-plates"
    assert results["visuals"]["provenance"]["scene_sources"] == ["plate:local"]

    # Render / thumbnail / qc all come from the one assemble run.
    assert cheap_stages.calls.count("assemble") == 1
    assert Path(results["render"]["file_path"]).is_file()
    assert results["render"]["provenance"]["subtitles"] == "alignment-stub"
    assert len(results["thumbnail"]["artefact"]["thumbnails"]) == 3
    assert results["qc"]["artefact"]["qc_passed"] is True
    assert results["qc"]["artefact"]["loudness_lufs"] == pytest.approx(-14.1)

    # Package: fake-provider publish, manifest + ledger + report in pack.
    package = results["package"]
    assert package["provenance"]["publish"] == "fake"
    assert package["artefact"]["gate_b_passed"] is True
    pack_dir = Path(package["file_path"]).parent
    assert Path(package["file_path"]).is_file()
    assert (pack_dir / "licence_ledger.csv").is_file()
    assert (pack_dir / "report.md").is_file()
    # The publish stage received produce.py's provider translation.
    rows = cheap_stages.captured["qc_info"]["licence_segments"]
    assert rows and rows[0]["provider"] == "generated"


def test_auto_topic_is_resolved_by_research_before_script(cheap_stages, tmp_path):
    project = _project(tmp_path, topic="auto:facts")
    payload = _payload(tmp_path)

    research = stage_bridge.run_research(project, payload)
    resolved = research["artefact"]["resolved_topic"]
    assert resolved and not resolved.startswith("auto:")
    assert research["artefact"]["topic_source"] == "auto-ideation-deterministic"

    stage_bridge.run_script(project, payload)
    # The script stage's spec carried the resolved topic, not the sentinel.
    assert cheap_stages.captured["spec_topics"]
    assert all(not t.startswith("auto:") for t in cheap_stages.captured["spec_topics"])
    assert cheap_stages.captured["spec_topics"][-1] == resolved


# ---------------------------------------------------------------------------
# (c) Idempotency
# ---------------------------------------------------------------------------

def test_run_voice_twice_does_not_duplicate_work(cheap_stages, tmp_path):
    project = _project(tmp_path)
    payload = _payload(tmp_path)

    first = stage_bridge.run_voice(project, payload)
    second = stage_bridge.run_voice(project, payload)

    assert cheap_stages.calls.count("voice") == 1
    assert cheap_stages.calls.count("content") == 1  # bundle also cached
    assert second.get("cached") is True
    assert second["file_path"] == first["file_path"]
    assert second["artefact"]["narration_wav"] == first["artefact"]["narration_wav"]


def test_render_thumbnail_qc_share_one_assemble_run(cheap_stages, tmp_path):
    project = _project(tmp_path)
    payload = _payload(tmp_path)
    stage_bridge.run_render(project, payload)
    again = stage_bridge.run_render(project, payload)
    stage_bridge.run_thumbnail(project, payload)
    stage_bridge.run_qc(project, payload)
    assert cheap_stages.calls.count("assemble") == 1
    assert again.get("cached") is True


# ---------------------------------------------------------------------------
# (d) Monolith produce_video still works (glue-level, staged fakes)
# ---------------------------------------------------------------------------

def test_produce_video_monolith_still_passes(stage_fakes, monkeypatch, tmp_path):
    from app.integration import produce

    monkeypatch.setattr(produce, "build_content", lambda *a: stage_fakes.bundle)
    monkeypatch.setattr(produce, "run_gate_a", lambda *a: (True, []))
    monkeypatch.setattr(produce, "synthesize_voice", lambda *a: stage_fakes.voice)
    monkeypatch.setattr(
        produce, "plan_and_generate_visuals", lambda *a: stage_fakes.visuals)
    monkeypatch.setattr(produce, "assemble_project", lambda *a: stage_fakes.assemble)
    monkeypatch.setattr(produce, "finalize_publish", lambda *a: stage_fakes.publish)

    from app.integration.models import ProjectSpec

    result = produce.produce_video(
        ProjectSpec(niche="facts", topic="Ocean depth secrets", minutes=1.0),
        tmp_path / "mono",
    )
    assert result.final_mp4 == stage_fakes.assemble.final_mp4
    assert result.gate_b_passed is True
    assert Path(result.licence_ledger_csv).is_file()
    assert Path(result.report_md).is_file()


# ---------------------------------------------------------------------------
# (e) D2/D1 regressions (Round 4 fix): run_script inside an ACTIVE
# event loop (the execution handlers' context) must drive the live
# builder via the run-loop owner; a live failure must propagate
# verbatim — never be masked by build_content_auto's event-loop
# RuntimeError — and a $0 bundle must not emit an explicit zero cost.
# ---------------------------------------------------------------------------


def _patch_live_only(monkeypatch, fake_live):
    monkeypatch.setattr(content_mod, "build_content_live", fake_live)
    # No build_content_auto override: _build_content must use the
    # live + run-loop path (the cheap_stages fixture is NOT used here).
    monkeypatch.setattr(stage_bridge, "build_content_auto", None)


def test_run_script_in_async_context_runs_live_builder(monkeypatch, tmp_path):
    import asyncio

    calls = {"n": 0}

    async def fake_live(spec, profile, **kwargs):
        calls["n"] += 1
        bundle = content_mod.build_content(spec, profile)
        bundle.llm_path = "gemini-live"
        bundle.llm_models = ["fake-model"]
        bundle.llm_cost_usd = 0.018
        return bundle

    _patch_live_only(monkeypatch, fake_live)
    project = _project(tmp_path)
    payload = _payload(tmp_path)

    result = asyncio.run(_run_script_async(project, payload))

    assert calls["n"] == 1
    assert result["provenance"]["llm_path"] == "gemini-live"
    assert result["cost_micros"] == 18_000
    assert result["cost_usd"] == pytest.approx(0.018)


async def _run_script_async(project, payload):
    # run_script is sync bridge code called from inside a running
    # loop by the DAG handlers — reproduce exactly that context.
    return stage_bridge.run_script(project, payload)


def test_run_script_live_failure_propagates_unmasked(monkeypatch, tmp_path):
    import asyncio

    async def failing_live(spec, profile, **kwargs):
        raise RuntimeError("fake gemini 429 quota exhausted")

    _patch_live_only(monkeypatch, failing_live)
    project = _project(tmp_path)
    payload = _payload(tmp_path)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_run_script_async(project, payload))
    # The original live error, verbatim — NOT the loop-guard error
    # the old build_content_auto fallback masked it with.
    assert "fake gemini 429 quota exhausted" in str(excinfo.value)
    assert "cannot run inside an active event loop" not in str(excinfo.value)


def test_run_script_zero_cost_bundle_emits_no_explicit_zero_cost(
    monkeypatch, tmp_path
):
    async def no_key_live(spec, profile, **kwargs):
        # The real no-key path: deterministic bundle, llm_cost_usd=0.
        return content_mod.build_content(spec, profile)

    _patch_live_only(monkeypatch, no_key_live)
    result = stage_bridge.run_script(_project(tmp_path), _payload(tmp_path))

    assert result["provenance"]["llm_path"] == "deterministic-stub"
    # D1: no explicit zero may reach handlers._resolve_cost, so its
    # per-stage cost table (script: 25000) applies downstream.
    assert "cost_micros" not in result
    assert "cost_usd" not in result


def test_result_omits_zero_cost_but_keeps_nonzero():
    project = _project(Path("/tmp"))
    zero = stage_bridge._result("script", project, {}, cost_usd=0.0)
    assert "cost_micros" not in zero and "cost_usd" not in zero
    none = stage_bridge._result("script", project, {}, cost_usd=None)
    assert "cost_micros" not in none and "cost_usd" not in none
    paid = stage_bridge._result("script", project, {}, cost_usd=0.018)
    assert paid["cost_micros"] == 18_000
    assert paid["cost_usd"] == pytest.approx(0.018)
