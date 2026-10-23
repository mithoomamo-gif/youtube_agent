"""Calibration error mathematics — pure functions, no I/O.

Given calibration rows (predicted band vs actual, per published
video), this module computes how wrong the system was.  Everything
here is a pure function of the rows handed in, so the numbers a
report shows are exactly reproducible — and hand-checkable — from
the raw rows, which are always returned alongside the aggregates
(see :mod:`app.calibration.report`).  Nothing is filtered, trimmed
or winsorised: **every row in scope counts, misses included.**

Definitions (per scope, per snapshot point — D+7 and D+28 are always
computed separately, never pooled)
=====================================================================
For each row *i* with issued band midpoint ``mid_i`` and actual
``actual_i``:

* ``signed_error_i = actual_i - mid_i`` (raw metric units).
* ``pct_error_i = (actual_i - mid_i) / mid_i * 100`` — the error
  relative to the band midpoint.  Defined only for ``mid_i > 0``.
* ``within_band_i = predicted_low_i <= actual_i <= predicted_high_i``
  (band bounds are inclusive).

Aggregates over the rows at one snapshot point:

* **Signed bias** = mean of ``pct_error_i`` over rows with
  ``mid_i > 0``, in percentage points.  Sign convention:
  **positive bias means actuals ran ABOVE the prediction — the
  system systematically UNDER-predicted**; negative bias means the
  system over-predicted.  ``bias_direction`` spells this out in
  words (``'under_predicting'`` / ``'over_predicting'`` /
  ``'unbiased'`` when the mean is exactly 0).
* **MAPE** (mean absolute percentage error) = mean of
  ``|pct_error_i|`` over rows with ``mid_i > 0``, in percent.
* **Band coverage rate** = (rows with ``within_band_i``) / (all rows
  at the point) — the honest headline number: how often reality
  landed inside the band the system issued.  Rows with ``mid_i = 0``
  still count here (and in ``mean_signed_error``); they are excluded
  only from the two percentage aggregates, and ``n_pct`` reports how
  many rows those aggregates actually rest on.
* **Mean signed error** = mean of ``signed_error_i`` over *all* rows
  at the point, in raw metric units.

Small-n handling
================
A scope/point with fewer than :data:`MIN_EVIDENCE` (= 3) rows does
not get aggregate statistics: ``signed_bias_pct``, ``mape_pct``,
``band_coverage`` and ``mean_signed_error`` are ``None``,
``evidence`` is ``'insufficient_evidence'`` and ``label`` is the
human string ``'insufficient evidence'``.  The raw rows (and the
plain within/below/above tallies, which are counts of those rows,
not estimates) are still reported — small-n suppresses conclusions,
never data.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "MIN_EVIDENCE",
    "INSUFFICIENT_EVIDENCE",
    "enrich_row",
    "summarise_point",
    "summarise_rows",
]

#: Fewer rows than this at a snapshot point => aggregates are
#: labelled 'insufficient evidence' and reported as None.
MIN_EVIDENCE = 3

#: Machine label used when a scope/point is below MIN_EVIDENCE.
INSUFFICIENT_EVIDENCE = "insufficient_evidence"

#: Human label paired with INSUFFICIENT_EVIDENCE.
INSUFFICIENT_LABEL = "insufficient evidence"


def enrich_row(row: Mapping[str, Any]) -> dict:
    """Return ``row`` as a dict plus its derived per-row error fields.

    Adds ``signed_error``, ``pct_error`` (``None`` when the issued
    midpoint is 0), ``abs_pct_error``, ``within_band`` (bool) and
    ``verdict`` (``'within_band'`` / ``'below_band'`` /
    ``'above_band'``).  The input mapping is not mutated.
    """
    data = dict(row)
    mid = float(data["predicted_mid"])
    low = float(data["predicted_low"])
    high = float(data["predicted_high"])
    actual = float(data["actual_value"])
    signed = actual - mid
    data["signed_error"] = signed
    if mid > 0:
        pct = signed / mid * 100.0
        data["pct_error"] = pct
        data["abs_pct_error"] = abs(pct)
    else:
        data["pct_error"] = None
        data["abs_pct_error"] = None
    data["within_band"] = low <= actual <= high
    if actual < low:
        data["verdict"] = "below_band"
    elif actual > high:
        data["verdict"] = "above_band"
    else:
        data["verdict"] = "within_band"
    return data


def summarise_point(rows: Iterable[Mapping[str, Any]], snapshot_point: str) -> dict:
    """Aggregate stats for already-enriched ``rows`` at one point.

    ``rows`` must all belong to the same scope and the same
    ``snapshot_point`` (the caller groups; this function trusts the
    grouping and reports ``snapshot_point`` back verbatim).
    """
    rows = list(rows)
    n = len(rows)
    pct_errors = [r["pct_error"] for r in rows if r["pct_error"] is not None]
    n_pct = len(pct_errors)
    within = sum(1 for r in rows if r["within_band"])
    below = sum(1 for r in rows if r["verdict"] == "below_band")
    above = sum(1 for r in rows if r["verdict"] == "above_band")

    stats: dict[str, Any] = {
        "snapshot_point": snapshot_point,
        "n": n,
        "n_pct": n_pct,
        "within_band_count": within,
        "below_band_count": below,
        "above_band_count": above,
        "evidence": "sufficient",
        "label": None,
        "signed_bias_pct": None,
        "bias_direction": None,
        "mape_pct": None,
        "band_coverage": None,
        "band_coverage_pct": None,
        "mean_signed_error": None,
    }
    if n < MIN_EVIDENCE:
        stats["evidence"] = INSUFFICIENT_EVIDENCE
        stats["label"] = INSUFFICIENT_LABEL
        return stats

    if n_pct:
        bias = sum(pct_errors) / n_pct
        stats["signed_bias_pct"] = bias
        stats["mape_pct"] = sum(abs(p) for p in pct_errors) / n_pct
        if bias > 0:
            stats["bias_direction"] = "under_predicting"
        elif bias < 0:
            stats["bias_direction"] = "over_predicting"
        else:
            stats["bias_direction"] = "unbiased"
    coverage = within / n if n else None
    stats["band_coverage"] = coverage
    stats["band_coverage_pct"] = coverage * 100.0 if coverage is not None else None
    stats["mean_signed_error"] = sum(r["signed_error"] for r in rows) / n
    return stats


def summarise_rows(rows: Iterable[Mapping[str, Any]]) -> dict:
    """Full per-scope summary: enriched rows + per-point aggregates.

    ``rows`` must all belong to one (channel, niche, metric) scope;
    D+7 and D+28 are summarised separately.  Returns
    ``{"rows": [...enriched...], "points": {"D+7": stats, "D+28":
    stats}}`` — points with no rows get an ``n=0`` insufficient-
    evidence summary rather than being omitted, so a report can
    never silently drop a grading point.
    """
    from app.calibration.schema import SNAPSHOT_POINTS

    enriched = [enrich_row(r) for r in rows]
    points: dict[str, dict] = {}
    for point in SNAPSHOT_POINTS:
        points[point] = summarise_point(
            [r for r in enriched if r["snapshot_point"] == point], point
        )
    return {"rows": enriched, "points": points}
