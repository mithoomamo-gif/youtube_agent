"""Calibration error math tests (Round 5 — Learning Loop).

Every expected aggregate below is computed BY HAND in the comments
from the fixture rows, independently of the implementation's
formulas — the test fails if the code's arithmetic disagrees with
the hand arithmetic.

Hand-computed fixture (scope channel-1 / facts / views, D+7):

===  ==================  ======  ========  ========  ==========
 #   band (low/mid/high) actual  pct err   verdict   signed err
===  ==================  ======  ========  ========  ==========
 1   800 / 1000 / 1200    1100    +10%      within      +100
 2   1500 / 2000 / 2500   1000    -50%      below      -1000
 3   400 / 500 / 600       600    +20%      within*     +100
 4   900 / 1000 / 1100     1300    +30%      above       +300
===  ==================  ======  ========  ========  ==========

(* row 3 lands exactly on the band's upper bound — bounds are
inclusive, so it counts as within.)

signed bias  = (10 - 50 + 20 + 30) / 4          = +2.5 pct points
MAPE         = (10 + 50 + 20 + 30) / 4          = 27.5 %
coverage     = 2 within / 4 rows                = 0.5
mean signed  = (100 - 1000 + 100 + 300) / 4     = -125.0 views
counts       = within 2, below 1, above 1
"""
from __future__ import annotations

import pytest

from app.calibration.calibration_math import (
    MIN_EVIDENCE,
    enrich_row,
    summarise_point,
    summarise_rows,
)

NOW_MS = 1_700_000_000_000


def _row(video: str, point: str, low: float, mid: float, high: float,
         actual: float, **overrides) -> dict:
    row = {
        "id": f"row-{video}-{point}",
        "project_id": f"proj-{video}",
        "youtube_video_id": video,
        "channel_id": "channel-1",
        "niche": "facts",
        "metric": "views",
        "snapshot_point": point,
        "predicted_low": low,
        "predicted_mid": mid,
        "predicted_high": high,
        "actual_value": actual,
        "snapshot_ref": None,
        "prediction_issued_at": NOW_MS,
        "published_at": NOW_MS,
        "created_at": NOW_MS,
    }
    row.update(overrides)
    return row


D7_FIXTURE = [
    _row("v1", "D+7", 800, 1000, 1200, 1100),
    _row("v2", "D+7", 1500, 2000, 2500, 1000),
    _row("v3", "D+7", 400, 500, 600, 600),
    _row("v4", "D+7", 900, 1000, 1100, 1300),
]


# ---------------------------------------------------------------------------
# Per-row enrichment
# ---------------------------------------------------------------------------


def test_enrich_row_verdicts_and_errors() -> None:
    enriched = [enrich_row(r) for r in D7_FIXTURE]
    assert [r["verdict"] for r in enriched] == [
        "within_band",
        "below_band",
        "within_band",  # upper bound is inclusive
        "above_band",
    ]
    assert [r["signed_error"] for r in enriched] == [100, -1000, 100, 300]
    assert [r["pct_error"] for r in enriched] == [10.0, -50.0, 20.0, 30.0]
    assert [r["abs_pct_error"] for r in enriched] == [10.0, 50.0, 20.0, 30.0]
    assert [r["within_band"] for r in enriched] == [True, False, True, False]


def test_enrich_row_does_not_mutate_input() -> None:
    row = _row("v1", "D+7", 800, 1000, 1200, 1100)
    enrich_row(row)
    assert "pct_error" not in row


# ---------------------------------------------------------------------------
# Hand-computed aggregates
# ---------------------------------------------------------------------------


def test_summarise_point_matches_hand_computation() -> None:
    stats = summarise_point([enrich_row(r) for r in D7_FIXTURE], "D+7")
    assert stats["n"] == 4
    assert stats["n_pct"] == 4
    assert stats["evidence"] == "sufficient"
    assert stats["label"] is None
    # (10 - 50 + 20 + 30) / 4 = 2.5 — positive: actuals ran ABOVE the
    # prediction on average => the system UNDER-predicted.
    assert stats["signed_bias_pct"] == pytest.approx(2.5)
    assert stats["bias_direction"] == "under_predicting"
    # (10 + 50 + 20 + 30) / 4 = 27.5
    assert stats["mape_pct"] == pytest.approx(27.5)
    # 2 of 4 actuals inside the issued band — the honest headline.
    assert stats["band_coverage"] == pytest.approx(0.5)
    assert stats["band_coverage_pct"] == pytest.approx(50.0)
    # (100 - 1000 + 100 + 300) / 4 = -125.0
    assert stats["mean_signed_error"] == pytest.approx(-125.0)
    assert stats["within_band_count"] == 2
    assert stats["below_band_count"] == 1
    assert stats["above_band_count"] == 1


def test_summarise_point_over_predicting_sign() -> None:
    # All actuals below midpoint => negative bias => over-predicting.
    rows = [
        enrich_row(_row("a", "D+28", 900, 1000, 1100, 900)),   # -10%
        enrich_row(_row("b", "D+28", 900, 1000, 1100, 800)),   # -20%
        enrich_row(_row("c", "D+28", 900, 1000, 1100, 700)),   # -30%
    ]
    stats = summarise_point(rows, "D+28")
    assert stats["signed_bias_pct"] == pytest.approx(-20.0)
    assert stats["bias_direction"] == "over_predicting"
    assert stats["mape_pct"] == pytest.approx(20.0)
    assert stats["band_coverage"] == pytest.approx(1 / 3)


def test_summarise_rows_separates_d7_and_d28() -> None:
    rows = D7_FIXTURE + [
        _row("v1", "D+28", 1500, 2000, 2500, 2100),
        _row("v2", "D+28", 1500, 2000, 2500, 2600),
    ]
    summary = summarise_rows(rows)
    assert summary["points"]["D+7"]["n"] == 4
    assert summary["points"]["D+7"]["mape_pct"] == pytest.approx(27.5)
    # D+28 has only 2 rows: no aggregates leak in from the D+7 pool.
    d28 = summary["points"]["D+28"]
    assert d28["n"] == 2
    assert d28["evidence"] == "insufficient_evidence"
    assert summary["rows"] and len(summary["rows"]) == 6


# ---------------------------------------------------------------------------
# Small-n: insufficient evidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 2])
def test_insufficient_evidence_below_min(count: int) -> None:
    assert MIN_EVIDENCE == 3
    stats = summarise_point(
        [enrich_row(r) for r in D7_FIXTURE[:count]], "D+7"
    )
    assert stats["n"] == count
    assert stats["evidence"] == "insufficient_evidence"
    assert stats["label"] == "insufficient evidence"
    assert stats["signed_bias_pct"] is None
    assert stats["mape_pct"] is None
    assert stats["band_coverage"] is None
    assert stats["mean_signed_error"] is None
    # …but the plain tallies of the raw rows are still reported.
    assert stats["within_band_count"] == sum(
        1 for r in D7_FIXTURE[:count] if r["predicted_low"] <= r["actual_value"] <= r["predicted_high"]
    )


def test_exactly_min_evidence_gets_aggregates() -> None:
    stats = summarise_point(
        [enrich_row(r) for r in D7_FIXTURE[:3]], "D+7"
    )
    assert stats["evidence"] == "sufficient"
    # (10 - 50 + 20) / 3 = -6.666...
    assert stats["signed_bias_pct"] == pytest.approx(-20.0 / 3.0)
    assert stats["mape_pct"] == pytest.approx(80.0 / 3.0)
    assert stats["band_coverage"] == pytest.approx(2 / 3)


def test_summarise_rows_empty_scope_still_reports_both_points() -> None:
    summary = summarise_rows([])
    assert summary["rows"] == []
    for point in ("D+7", "D+28"):
        assert summary["points"][point]["n"] == 0
        assert summary["points"][point]["evidence"] == "insufficient_evidence"


# ---------------------------------------------------------------------------
# Zero-midpoint rows: excluded from % stats, counted in coverage
# ---------------------------------------------------------------------------


def test_zero_mid_rows_counted_in_coverage_not_pct_stats() -> None:
    rows = [
        # mid 0: pct error undefined; actual 5 is inside [0, 10].
        enrich_row(_row("z", "D+7", 0, 0, 10, 5)),
        enrich_row(_row("p", "D+7", 50, 100, 150, 150)),  # +50%
        enrich_row(_row("q", "D+7", 50, 100, 150, 50)),   # -50%
    ]
    assert rows[0]["pct_error"] is None
    stats = summarise_point(rows, "D+7")
    assert stats["n"] == 3
    assert stats["n_pct"] == 2
    # bias over pct rows only: (50 - 50) / 2 = 0 => unbiased.
    assert stats["signed_bias_pct"] == pytest.approx(0.0)
    assert stats["bias_direction"] == "unbiased"
    assert stats["mape_pct"] == pytest.approx(50.0)
    # coverage counts all 3 rows (all within band).
    assert stats["band_coverage"] == pytest.approx(1.0)
    # raw signed errors: 5 + 50 - 50 = 5, over all 3 rows.
    assert stats["mean_signed_error"] == pytest.approx(5.0 / 3.0)
