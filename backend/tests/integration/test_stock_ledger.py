"""Tests for app.integration.stock_ledger (Round 3).

Proof obligations (per task):

* ``ledger_row_for_stock_asset`` emits rows in the REAL Round 2
  ``LicenceLedger`` format (``app.integration.ledger.CSV_COLUMNS``)
  carrying provider / provider asset id / Pixabay licence / kind /
  local path, with the page URL + download URL + terms hash + file
  sha256 in ``notes``.
* A fabricated mini-pack (2 stock scenes + 1 Round-2 generated plate
  scene) written through the REAL ``LicenceLedger`` and run through
  the REAL ``publish_stage.finalize_publish`` Gate B path PASSES.
* The same pack with one stock scene's ledger row REMOVED FAILS G1 —
  proving the gate is really checking the rows, not waving them
  through.

No network, no keys: assets are fabricated stand-ins with the exact
``stock_fetch.StockSceneAsset`` field shape; file hashes are real
SHA-256 digests of the dummy files written under ``tmp_path``.
"""

from __future__ import annotations

import csv
import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.integration.ledger import CSV_COLUMNS, LicenceLedger
from app.integration.models import ProjectSpec, make_context
from app.integration.publish_stage import (
    _build_copyright_inputs,
    _collect_licence_rows,
    finalize_publish,
)
from app.integration.stock_ledger import (
    PIXABAY_LICENCE_NAME,
    ledger_row_for_stock_asset,
    summarise_sources,
)
from app.integration.visuals_stage import (
    LEDGER_KIND as PLATE_KIND,
    LEDGER_LICENCE as PLATE_LICENCE,
    LEDGER_SOURCE as PLATE_SOURCE,
)
from app.pipeline.gates.copyright import check_g1_licence_completeness
from app.pipeline.gates.tokens import Verdict

TOTAL_S = 18.0  # 3 scenes x 6 s


# ---------------------------------------------------------------------------
# Fabricated fixtures
# ---------------------------------------------------------------------------


@dataclass
class FakeStockAsset:
    """Stand-in with the exact StockSceneAsset field shape."""

    local_path: str
    provider: str
    provider_asset_id: str
    asset_page_url: str
    download_url: str
    licence_name: str
    terms_hash: str
    file_sha256: str
    width: int
    height: int
    duration_s: float
    kind: str


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_asset(tmp_path: Path, stem: str, asset_id: str, kind: str) -> FakeStockAsset:
    payload = f"fake-{kind}-bytes-{asset_id}".encode()
    local = tmp_path / "visuals" / f"{stem}.{'mp4' if kind == 'video' else 'jpg'}"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(payload)
    return FakeStockAsset(
        local_path=str(local),
        provider="pixabay",
        provider_asset_id=asset_id,
        asset_page_url=f"https://pixabay.com/videos/id-{asset_id}/",
        download_url=f"https://cdn.pixabay.com/video/{asset_id}.mp4",
        licence_name=PIXABAY_LICENCE_NAME,
        terms_hash=_sha(b"Pixabay Content License terms text (captured)"),
        file_sha256=_sha(payload),
        width=1920,
        height=1080,
        duration_s=6.0,
        kind=kind,
    )


SCENES = [
    {"scene_id": "scene_0001", "start_s": 0.0, "end_s": 6.0},
    {"scene_id": "scene_0002", "start_s": 6.0, "end_s": 12.0},
    {"scene_id": "scene_0003", "start_s": 12.0, "end_s": 18.0},
]


@pytest.fixture()
def stock_assets(tmp_path):
    return [
        _make_asset(tmp_path, "scene_0001", "10345", "video"),
        _make_asset(tmp_path, "scene_0002", "20789", "photo"),
    ]


@pytest.fixture()
def stock_rows(stock_assets):
    return [
        ledger_row_for_stock_asset(asset, scene)
        for asset, scene in zip(stock_assets, SCENES[:2])
    ]


@pytest.fixture()
def plate_file(tmp_path):
    path = tmp_path / "visuals" / "scene_0003.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake-plate-png")
    return path


def _plate_gate_row(plate_file: Path) -> dict:
    """The plate scene's row in the shape assemble_stage emits into
    ``licence_segments`` in Round 2 (provider already normalised to
    ``generated`` at the produce boundary, as in the real runs)."""
    return {
        "asset_id": "scene_0003",
        "scene_id": "scene_0003",
        "start_s": 12.0,
        "end_s": 18.0,
        "licence_id": "OWN-scene_0003",
        "provider": "generated",
        "content_hash": _sha(b"fake-plate-png"),
        "source_type": "still",
        "role": "b_roll",
    }


def _add_csv_row(ledger: LicenceLedger, row: dict) -> None:
    """Write one superset row through the real ledger's CSV contract."""
    ledger.add(
        asset_id=row["asset_id"],
        scene_id=row["scene_id"],
        source=row["source"],
        licence_name=row["licence"],
        kind=row["kind"],
        local_path=row["local_path"],
        notes=row["notes"],
    )


# ---------------------------------------------------------------------------
# Row format
# ---------------------------------------------------------------------------


def test_row_carries_exact_csv_columns_and_values(stock_assets, stock_rows):
    row = stock_rows[0]
    # The first seven keys are exactly the LicenceLedger CSV columns.
    assert list(row)[:7] == list(CSV_COLUMNS)
    assert row["asset_id"] == "10345"  # the real provider asset id
    assert row["scene_id"] == "scene_0001"
    assert row["source"] == "pixabay"  # provider as source
    assert row["licence"] == "Pixabay Content License"
    assert row["kind"] == "video"
    assert row["local_path"] == stock_assets[0].local_path
    assert stock_rows[1]["kind"] == "photo"
    assert stock_rows[1]["asset_id"] == "20789"


def test_row_notes_carry_full_appeal_evidence(stock_assets, stock_rows):
    asset, row = stock_assets[0], stock_rows[0]
    for expected in (
        asset.asset_page_url,
        asset.download_url,
        asset.terms_hash,
        asset.file_sha256,
        asset.provider_asset_id,
    ):
        assert expected in row["notes"]
    # Gate-join aliases agree with the CSV fields.
    assert row["provider"] == "pixabay"
    assert row["provider_asset_id"] == "10345"
    assert row["content_hash"] == asset.file_sha256
    assert row["licence_name"] == "Pixabay Content License"
    assert row["start_s"] == 0.0 and row["end_s"] == 6.0


def test_row_is_defensive_about_stand_in_shapes(tmp_path):
    # Dict asset + plain-string scene id (no timeline) still works.
    row = ledger_row_for_stock_asset(
        {
            "local_path": str(tmp_path / "x.mp4"),
            "provider_asset_id": 998877,
            "asset_page_url": "https://pixabay.com/videos/id-998877/",
            "download_url": "https://cdn.pixabay.com/video/998877.mp4",
            "terms_hash": "t" * 64,
            "file_sha256": "f" * 64,
            "kind": "image",  # normalised onto the ledger's photo kind
        },
        "scene_0009",
    )
    assert row["asset_id"] == "998877"
    assert row["scene_id"] == "scene_0009"
    assert row["source"] == "pixabay"  # default provider
    assert row["licence"] == PIXABAY_LICENCE_NAME  # default licence name
    assert row["kind"] == "photo"
    assert row["start_s"] is None and row["end_s"] is None


# ---------------------------------------------------------------------------
# Ledger CSV via the real LicenceLedger
# ---------------------------------------------------------------------------


def test_ledger_csv_roundtrip_with_plate_row(stock_rows, plate_file, tmp_path):
    ledger = LicenceLedger()
    for row in stock_rows:
        _add_csv_row(ledger, row)
    # The plate scene's Round 2 generated row, verbatim values.
    ledger.add(
        asset_id="scene_0003",
        scene_id="scene_0003",
        source=PLATE_SOURCE,
        licence_name=PLATE_LICENCE,
        kind=PLATE_KIND,
        local_path=str(plate_file),
        notes="",
    )
    csv_path = ledger.write_csv(tmp_path / "pack" / "licence_ledger.csv")

    with csv_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
        assert fh.seek(0) or True
    with csv_path.open(newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    assert header == list(CSV_COLUMNS)
    assert len(rows) == 3
    stock_csv = [r for r in rows if r["source"] == "pixabay"]
    assert {r["asset_id"] for r in stock_csv} == {"10345", "20789"}
    assert all(r["licence"] == "Pixabay Content License" for r in stock_csv)
    assert {r["kind"] for r in stock_csv} == {"video", "photo"}
    plate_csv = [r for r in rows if r["scene_id"] == "scene_0003"]
    assert plate_csv[0]["source"] == PLATE_SOURCE
    assert plate_csv[0]["licence"] == PLATE_LICENCE


# ---------------------------------------------------------------------------
# Gate B / G1 through the real publish_stage path
# ---------------------------------------------------------------------------


def _make_master(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    master = tmp_path / "final.mp4"
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=1",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(master),
            ],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0 or not master.is_file():
            raise RuntimeError(proc.stderr[-200:])
    except Exception:
        master.write_bytes(b"fake-master-bytes")
    return master


def _run_finalize(tmp_path: Path, gate_rows: list[dict]):
    """Minimal real finalize_publish run (shapes mirror produce.py)."""
    master = _make_master(tmp_path)
    srt = tmp_path / "captions.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:06,000\nHello\n", encoding="utf-8"
    )
    thumb = tmp_path / "thumbnail_1.png"
    thumb.write_bytes(b"fake-thumb")

    spec = ProjectSpec(niche="facts", topic="Ocean depth secrets", minutes=1.0)
    profile = SimpleNamespace(profile_id="facts")
    ctx = make_context(spec, profile, tmp_path / "proj", project_id="stock-test")
    bundle = SimpleNamespace(
        title="Ocean Depth Secrets",
        description="A test description with no attribution requirements.",
        tags=["ocean", "facts"],
        script_text=(
            "the ocean hides many secrets beneath its waves and sailors "
            "have feared the deep trenches for centuries"
        ),
    )
    assemble = SimpleNamespace(
        final_mp4=str(master),
        duration_s=TOTAL_S,
        thumbnails=[str(thumb)],
        captions_path=str(srt),
        licence_segments=gate_rows,
        qc_passed=True,
    )
    qc_info = {"verdict": "PASS", "checks": {}}
    return finalize_publish(assemble, qc_info, bundle, profile, ctx, True)


def test_finalize_publish_passes_gate_b_with_real_stock_rows(
    stock_rows, plate_file, tmp_path
):
    gate_rows = stock_rows + [_plate_gate_row(plate_file)]
    result = _run_finalize(tmp_path, gate_rows)

    assert result.notes["copyright"]["g1"] == "PASS"
    assert result.notes["copyright"]["g2"] == "PASS"
    assert result.notes["copyright"]["g6"] == "PASS"
    assert result.manifest["licence_ledger_completeness"] == 1.0
    assert result.gate_b_passed is True
    assert result.publish_state == "fake_provider_upload_simulated"


def test_copyright_join_directly_passes(stock_rows, plate_file):
    """The exact translation finalize_publish performs, isolated."""
    rows = _collect_licence_rows(
        SimpleNamespace(licence_segments=stock_rows + [_plate_gate_row(plate_file)]),
        None,
        None,
    )
    segments, licences, provenances = _build_copyright_inputs(rows)
    from app.pipeline.gates.copyright import run_copyright_gates

    report = run_copyright_gates(
        segments=segments,
        licences=licences,
        provenances=provenances,
        description="no attributions required",
        total_duration_s=TOTAL_S,
    )
    assert report.g1.verdict is Verdict.PASS
    assert report.g2.verdict is Verdict.PASS
    assert report.g6.verdict is Verdict.PASS


def test_finalize_publish_fails_g1_when_stock_row_missing(
    stock_rows, plate_file, tmp_path
):
    """Drop scene_0002's stock row: its 6 s become uncovered -> BLOCK."""
    gate_rows = [stock_rows[0], _plate_gate_row(plate_file)]
    result = _run_finalize(tmp_path / "neg", gate_rows)

    assert result.gate_b_passed is False
    assert result.notes["copyright"]["g1"] == "BLOCK"
    assert "G1_TIMELINE_COVERAGE_GAP" in result.notes["copyright"]["reason_codes"]
    assert result.manifest["licence_ledger_completeness"] < 1.0


def test_g1_blocks_segment_whose_licence_record_is_missing(stock_rows, plate_file):
    """Segment present, licence record absent -> NO_LICENCE_RECORD."""
    rows = stock_rows + [_plate_gate_row(plate_file)]
    segments, licences, _provenances = _build_copyright_inputs(rows)
    del licences["20789"]  # scene_0002's stock asset loses its record
    report = check_g1_licence_completeness(segments, licences, TOTAL_S)
    assert report.verdict is Verdict.BLOCK
    offenders = {p.asset_id: p.reasons for p in report.incomplete_assets}
    assert offenders["20789"] == ("NO_LICENCE_RECORD",)
    assert (6.0, 12.0) in report.uncovered_intervals


# ---------------------------------------------------------------------------
# summarise_sources
# ---------------------------------------------------------------------------


def test_summarise_sources_counts_share_and_rows(stock_assets, plate_file):
    summary = summarise_sources(
        [
            (stock_assets[0], SCENES[0]),
            (stock_assets[1], SCENES[1]),
            {
                "scene_id": "scene_0003",
                "source": PLATE_SOURCE,
                "licence": PLATE_LICENCE,
                "kind": PLATE_KIND,
                "asset_path": str(plate_file),
            },
        ]
    )
    assert summary["stock_count"] == 2
    assert summary["plate_count"] == 1
    assert summary["stock_share"] == pytest.approx(2 / 3)
    assert len(summary["ledger_rows"]) == 2
    assert {r["asset_id"] for r in summary["ledger_rows"]} == {"10345", "20789"}
    assert all(r["source"] == "pixabay" for r in summary["ledger_rows"])


def test_summarise_sources_labels_count_without_fabricating_rows():
    summary = summarise_sources(["pixabay", "pixabay", "local_plate"])
    assert summary["stock_count"] == 2
    assert summary["plate_count"] == 1
    assert summary["stock_share"] == pytest.approx(2 / 3)
    # Bare labels carry no downloaded asset: no row may be invented.
    assert summary["ledger_rows"] == []


def test_summarise_sources_empty():
    assert summarise_sources([]) == {
        "stock_count": 0,
        "plate_count": 0,
        "stock_share": 0.0,
        "ledger_rows": [],
    }
