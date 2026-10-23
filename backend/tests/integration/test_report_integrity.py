"""Auditor recomputation tests for report integrity (Round 4, #5).

Round 3 fragility #5: auto-generated ``report.md`` fields contradicted
the packs' own ledgers (``llm=deterministic-stub`` / ``$0.00`` for real
Gemini scripts; an inverted stock/plate summary).  These tests build a
production pack through the mocked produce path, then recompute every
derived figure INDEPENDENTLY from the raw files on disk
(``licence_ledger.csv``, ``llm_usage.json``, ``publish_manifest.json``)
using only stdlib parsing and test-local logic, and assert the report
matches.  No expected value is computed with the report generator's
own helpers (``app.integration.pack`` / ``report_facts`` are the code
under test, never the oracle), so reintroducing a divergent field —
a parallel counter, a copied label, a cost overwrite — fails here.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import pytest

from app.integration import produce
from app.integration.models import ProjectSpec


# ---------------------------------------------------------------------------
# Stage patching (same monkeypatch contract as test_produce_glue)
# ---------------------------------------------------------------------------

def _patch(monkeypatch, fakes, gate=(True, [])):
    monkeypatch.setattr(produce, "build_content", lambda *a: fakes.bundle)
    monkeypatch.setattr(produce, "run_gate_a", lambda *a: gate)
    monkeypatch.setattr(produce, "synthesize_voice", lambda *a: fakes.voice)
    monkeypatch.setattr(produce, "plan_and_generate_visuals", lambda *a: fakes.visuals)
    monkeypatch.setattr(produce, "assemble_project", lambda *a: fakes.assemble)
    monkeypatch.setattr(produce, "finalize_publish", lambda *a: fakes.publish)


def _spec(**overrides) -> ProjectSpec:
    data = dict(niche="facts", topic="Ocean depth secrets", minutes=1.0)
    data.update(overrides)
    return ProjectSpec(**data)


# ---------------------------------------------------------------------------
# Independent auditor helpers (stdlib only — NOT the generator's code)
# ---------------------------------------------------------------------------

def _audit_visuals(ledger_csv: Path) -> dict:
    """Recompute stock/plate facts from the licence CSV on disk."""
    with ledger_csv.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    scenes = [r for r in rows if r["scene_id"].strip().lower() not in ("", "all")]
    stock = plate = 0
    for row in scenes:
        src = row["source"].lower()
        if "pixabay" in src or "pexels" in src or src.startswith("stock"):
            stock += 1
        elif "plate" in src:
            plate += 1
    classified = stock + plate
    return {
        "rows": scenes,
        "stock": stock,
        "plate": plate,
        "share": round(stock / classified, 4) if classified else 0.0,
    }


def _report_field(report: str, label: str) -> str:
    match = re.search(rf"{re.escape(label)}:?\s*(.+)", report)
    assert match, f"report is missing field {label!r}"
    return match.group(1).strip()


def _overview_cost(report: str) -> float:
    match = re.search(r"\| Cost \| \$([0-9.]+) \|", report)
    assert match, "report Overview has no Cost row"
    return float(match.group(1))


def _poisoned_fakes(stage_fakes):
    """Fakes whose in-memory counters DELIBERATELY lie.

    The bundle is real-Gemini-shaped; the visuals stage claims
    stock_count=999 / share=0.99 while its own ledger rows say
    2 stock + 1 plate; the assemble stage carries a poison
    cost_usd=0.0 and a duration that disagrees with the manifest.
    A report built from counters/labels instead of ledgers fails.
    """
    fakes = stage_fakes
    fakes.bundle.llm_path = "gemini-live"
    fakes.bundle.llm_models = ["gemini-3.1-flash-lite", "gemini-3.8-flash"]
    fakes.bundle.llm_usage = {"input_tokens": 2019, "output_tokens": 1823, "calls": 5}
    fakes.bundle.llm_cost_usd = 0.006021

    fakes.visuals.scene_manifest = [
        {"scene_id": "scene_0001", "source": "pixabay"},
        {"scene_id": "scene_0002", "source": "plate:local"},
        {"scene_id": "scene_0003", "source": "pixabay"},
    ]
    fakes.visuals.stock_count = 999
    fakes.visuals.plate_count = 888
    fakes.visuals.stock_share = 0.99
    fakes.visuals.ledger_rows = [
        {"asset_id": "pixabay-111", "scene_id": "scene_0001",
         "source": "Pixabay", "licence": "Pixabay Content License",
         "kind": "video", "local_path": "/x/scene_0001.mp4", "notes": ""},
        {"asset_id": "plate-1", "scene_id": "scene_0002",
         "source": "own/generated: PIL plate", "licence_name": "own work",
         "kind": "image", "local_path": "/x/scene_0002.png", "notes": ""},
        {"asset_id": "pixabay-222", "scene_id": "scene_0003",
         "source": "Pixabay", "licence": "Pixabay Content License",
         "kind": "video", "local_path": "/x/scene_0003.mp4", "notes": ""},
    ]
    fakes.assemble.cost_usd = 0.0  # poison: must NOT overwrite ledger cost
    fakes.assemble.duration_s = 54.0  # divergent: manifest says 55.0
    fakes.publish.manifest = {
        "video_id": "fake-1",
        "duration_seconds": 55.0,
        "gate_a_passed": True,
        "gate_b": {"passed": True},
        "publish_state": "ready_for_review",
    }
    return fakes


# ---------------------------------------------------------------------------
# The auditor test
# ---------------------------------------------------------------------------

def test_report_matches_ledgers_auditor_recompute(monkeypatch, stage_fakes, tmp_path):
    fakes = _poisoned_fakes(stage_fakes)
    _patch(monkeypatch, fakes)
    base = tmp_path / "out"
    result = produce.produce_video(_spec(), base)

    pack = Path(result.pack_dir)
    report = Path(result.report_md).read_text(encoding="utf-8")

    # --- visuals: recomputed from licence_ledger.csv -------------------
    audit = _audit_visuals(Path(result.licence_ledger_csv))
    assert (audit["stock"], audit["plate"]) == (2, 1)  # fixture sanity
    assert _report_field(report, "- Stock scenes") == str(audit["stock"])
    assert _report_field(report, "- Local plate scenes") == str(audit["plate"])
    assert float(_report_field(report, "- Stock share")) == pytest.approx(audit["share"])
    for row in audit["rows"]:
        assert f"- {row['scene_id']}: {row['source']}" in report

    # --- LLM: recomputed from llm_usage.json ---------------------------
    llm_ledger = json.loads((pack / "llm_usage.json").read_text(encoding="utf-8"))
    assert llm_ledger["cost_usd"] == pytest.approx(0.006021)
    assert _overview_cost(report) == pytest.approx(llm_ledger["cost_usd"])
    assert result.cost_usd == pytest.approx(llm_ledger["cost_usd"])
    assert _report_field(report, "- **LLM models:**") == ", ".join(llm_ledger["models"])
    assert _report_field(report, "- **LLM input tokens:**") == str(
        llm_ledger["usage"]["input_tokens"])
    assert _report_field(report, "- **LLM output tokens:**") == str(
        llm_ledger["usage"]["output_tokens"])
    assert _report_field(report, "- **LLM calls:**") == str(llm_ledger["usage"]["calls"])
    assert _report_field(report, "- **LLM cost (USD):**") == "$0.006021"
    assert "| llm | gemini-live |" in report
    assert result.paths_used["llm"] == "gemini-live"
    # The usage ledger is a headline pack file: flattened to base too.
    assert (base / "llm_usage.json").exists()

    # --- gates / state / duration: recomputed from the manifest --------
    manifest = json.loads(Path(result.publish_manifest_json).read_text(encoding="utf-8"))
    assert manifest["duration_seconds"] == 55.0
    assert result.duration_s == pytest.approx(manifest["duration_seconds"])
    assert f"| Duration | {manifest['duration_seconds']} s |" in report
    gate_b = "PASS" if manifest["gate_b"]["passed"] else "FAIL"
    assert f"| Gate B (Pre-Publish) | {gate_b} |" in report
    assert f"| Publish state | {manifest['publish_state']} |" in report
    assert result.publish_state == manifest["publish_state"]


def test_report_unions_ledger_and_scene_manifest(monkeypatch, stage_fakes, tmp_path):
    """A scene with NO licence-ledger row must not vanish from the
    report: the licence ledger carries rows for licensed assets, the
    scene manifest is the per-scene record — the report's visuals
    facts are the union of the two (ledger source wins per scene).
    This is the shape behind test_visuals_stock's
    test_produce_report_shows_visual_sources failure."""
    from types import SimpleNamespace

    stage_fakes.visuals = SimpleNamespace(
        scene_manifest=[
            {"scene_id": "scene_0001", "source": "stock:pixabay:pix-001"},
            {"scene_id": "scene_0002", "source": "plate:local"},
        ],
        asset_files={},
        ledger_rows=[
            {"asset_id": "pix-001", "scene_id": "scene_0001",
             "source": "stock:pixabay:pix-001",
             "licence_name": "Pixabay Content License",
             "kind": "image", "local_path": "/nonexistent/stock.jpg"},
        ],
        total_duration_s=55.0,
        path_used="stock-first",
        scene_sources=["stock:pixabay:pix-001", "plate:local"],
        stock_count=1,
        plate_count=1,
        stock_share=0.5,
    )
    _patch(monkeypatch, stage_fakes)
    result = produce.produce_video(_spec(), tmp_path / "union")
    report = Path(result.report_md).read_text(encoding="utf-8")

    # Independent union: CSV scenes (ledger wins) + manifest-only scenes.
    with Path(result.licence_ledger_csv).open(newline="", encoding="utf-8") as fh:
        csv_scenes = {
            r["scene_id"]: r["source"]
            for r in csv.DictReader(fh)
            if r["scene_id"].strip().lower() not in ("", "all")
        }
    expected = dict(csv_scenes)
    for entry in stage_fakes.visuals.scene_manifest:
        expected.setdefault(entry["scene_id"], entry["source"])
    assert set(expected) == {"scene_0001", "scene_0002"}  # fixture sanity
    exp_stock = sum(1 for s in expected.values() if "pixabay" in s.lower())
    exp_plate = sum(1 for s in expected.values() if "plate" in s.lower())

    assert _report_field(report, "- Stock scenes") == str(exp_stock)
    assert _report_field(report, "- Local plate scenes") == str(exp_plate)
    assert float(_report_field(report, "- Stock share")) == pytest.approx(
        exp_stock / (exp_stock + exp_plate))
    for scene_id, source in expected.items():
        assert f"- {scene_id}: {source}" in report


def test_stub_run_reports_stub_zero_and_ledger_visuals(monkeypatch, stage_fakes, tmp_path):
    """No live evidence anywhere -> stub label and $0.00 are HONEST,
    and the visuals summary still comes from the ledger (the default
    fake's single plate row), not from the empty scene manifest."""
    _patch(monkeypatch, stage_fakes)
    result = produce.produce_video(_spec(), tmp_path / "stub")
    report = Path(result.report_md).read_text(encoding="utf-8")

    audit = _audit_visuals(Path(result.licence_ledger_csv))
    assert (audit["stock"], audit["plate"]) == (0, 1)
    assert _report_field(report, "- Stock scenes") == "0"
    assert _report_field(report, "- Local plate scenes") == "1"
    assert float(_report_field(report, "- Stock share")) == 0.0
    assert _overview_cost(report) == 0.0
    assert "| llm | deterministic-stub |" in report
    llm_ledger = json.loads(
        (Path(result.pack_dir) / "llm_usage.json").read_text(encoding="utf-8"))
    assert llm_ledger["path"] == "deterministic-stub"
    assert llm_ledger["cost_usd"] == 0.0


def test_live_evidence_overrides_stub_label(monkeypatch, stage_fakes, tmp_path):
    """The exact Round 3 contradiction shape: a bundle LABELLED stub
    but carrying real models/tokens/cost in its usage ledger.  The
    derived report must follow the ledger, never the label."""
    stage_fakes.bundle.llm_path = "deterministic-stub"
    stage_fakes.bundle.llm_models = ["gemini-3.6-flash"]
    stage_fakes.bundle.llm_usage = {
        "input_tokens": 7444, "output_tokens": 14491, "calls": 8}
    stage_fakes.bundle.llm_cost_usd = 0.059924
    _patch(monkeypatch, stage_fakes)
    result = produce.produce_video(_spec(), tmp_path / "live")
    report = Path(result.report_md).read_text(encoding="utf-8")

    assert "| llm | gemini-live |" in report
    assert "deterministic-stub" not in report
    assert _overview_cost(report) == pytest.approx(0.059924)
    assert result.cost_usd == pytest.approx(0.059924)
    assert result.paths_used["llm"] == "gemini-live"


def test_llm_facts_recomputed_from_per_call_entries(monkeypatch, stage_fakes, tmp_path):
    """When the bundle carries per-call ledger entries, models/tokens/
    cost are summed from the OK entries only — the bundle's own
    aggregate fields (deliberately wrong here) are not trusted."""
    entries = [
        {"model": "gemini-3.1-flash-lite", "status": "ok",
         "input_uncached": 414, "input_cached": 0, "input_cache_write": 0,
         "output": 211, "reasoning": 0, "cost_usd": 0.00042},
        {"model": "gemini-3.8-flash", "status": "ok",
         "input_uncached": 397, "input_cached": 0, "input_cache_write": 0,
         "output": 0, "reasoning": 1148, "cost_usd": 0.004603},
        {"model": "gemini-3.1-pro-preview", "status": "failed",
         "input_uncached": 500, "input_cached": 0, "input_cache_write": 0,
         "output": 500, "reasoning": 0, "cost_usd": 0.0},
    ]
    stage_fakes.bundle.llm_ledger_entries = entries
    stage_fakes.bundle.llm_path = "gemini-live"
    stage_fakes.bundle.llm_models = ["wrong-model"]
    stage_fakes.bundle.llm_usage = {"input_tokens": 1, "output_tokens": 1, "calls": 1}
    stage_fakes.bundle.llm_cost_usd = 9.99
    _patch(monkeypatch, stage_fakes)
    result = produce.produce_video(_spec(), tmp_path / "entries")
    report = Path(result.report_md).read_text(encoding="utf-8")

    # Independent recomputation from the entry literals above.
    ok = [e for e in entries if e["status"] == "ok"]
    exp_models = list(dict.fromkeys(e["model"] for e in ok))
    exp_in = sum(e["input_uncached"] + e["input_cached"] + e["input_cache_write"] for e in ok)
    exp_out = sum(e["output"] + e["reasoning"] for e in ok)
    exp_cost = round(sum(e["cost_usd"] for e in ok), 6)

    assert _report_field(report, "- **LLM models:**") == ", ".join(exp_models)
    assert _report_field(report, "- **LLM input tokens:**") == str(exp_in)
    assert _report_field(report, "- **LLM output tokens:**") == str(exp_out)
    assert _report_field(report, "- **LLM calls:**") == str(len(ok))
    assert _overview_cost(report) == pytest.approx(exp_cost)
    assert result.cost_usd == pytest.approx(exp_cost)
    # The on-disk usage ledger transcribes the entries for auditors.
    on_disk = json.loads(
        (Path(result.pack_dir) / "llm_usage.json").read_text(encoding="utf-8"))
    assert len(on_disk["entries"]) == len(entries)
    assert on_disk["cost_usd"] == pytest.approx(exp_cost)


def test_visual_records_union_ledger_wins_per_scene():
    """Unit-pin the union rule itself: ledger source wins for scenes
    present in both records; manifest-only scenes are kept; counts
    are recomputed from the merged list, not summed from inputs."""
    from app.integration.report_facts import facts_from_visual_records

    facts = facts_from_visual_records(
        [{"scene_id": "scene_0001", "source": "Pixabay"}],
        [
            {"scene_id": "scene_0001", "source": "plate:local"},
            {"scene_id": "scene_0002", "source": "plate:local"},
            {"scene_id": "all", "source": "own/generated: stub voice"},
        ],
    )
    assert facts["visual_sources"] == [
        {"scene_id": "scene_0001", "source": "Pixabay"},
        {"scene_id": "scene_0002", "source": "plate:local"},
    ]
    assert facts["stock_count"] == 1
    assert facts["plate_count"] == 1
    assert facts["stock_share"] == 0.5


def test_pack_cost_format_preserves_subcent_costs(tmp_path):
    """$0.002559 must not render as $0.00 (the 2-dp formatting hid
    real ledger costs in Round 3 packs)."""
    from app.integration.pack import write_report_md

    path = write_report_md(
        {"title": "T", "cost_usd": 0.002559, "paths_used": {}}, tmp_path / "r.md")
    assert "$0.002559" in path.read_text(encoding="utf-8")
    path = write_report_md(
        {"title": "T", "cost_usd": 0.0, "paths_used": {}}, tmp_path / "r2.md")
    assert "$0.00" in path.read_text(encoding="utf-8")
    path = write_report_md(
        {"title": "T", "cost_usd": 1.5, "paths_used": {}}, tmp_path / "r3.md")
    assert "$1.50" in path.read_text(encoding="utf-8")
