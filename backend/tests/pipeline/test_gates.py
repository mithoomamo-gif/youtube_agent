"""Tests for gate tokens, copyright gates G1/G2/G6, the Gate B
aggregator, the policy similarity gate and the synthesis check.

Per roadmap section 17: "a gate never seen to fail is assumed broken" --
every gate here is tested on both a passing fixture and a planted-failure
fixture.
"""

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from app.pipeline.gates import (  # noqa: E402
    GATE_COPYRIGHT,
    GATE_POLICY_SIMILARITY,
    GATE_QC_TECHNICAL,
    AssetProvenance,
    GateToken,
    LicenceRecord,
    UsageSegment,
    Verdict,
    VideoFingerprint,
    check_g1_licence_completeness,
    check_g2_provenance,
    check_g6_attribution,
    check_similarity,
    check_source_synthesis,
    compute_manifest_hash,
    issue_token,
    run_copyright_gates,
    verify_gate_b,
    verify_token_binding,
)

HASH_A = compute_manifest_hash({"video": "a", "version": 1})
HASH_B = compute_manifest_hash({"video": "a", "version": 2})


# ---------------------------------------------------------------------------
# Token model + binding
# ---------------------------------------------------------------------------


def test_token_binding_accepts_issuing_hash():
    token = issue_token(GATE_QC_TECHNICAL, Verdict.PASS, HASH_A)
    assert verify_token_binding(token, HASH_A)


def test_token_binding_rejects_different_manifest_hash():
    """A token issued for manifest v1 must not validate against v2."""
    token = issue_token(GATE_QC_TECHNICAL, Verdict.PASS, HASH_A)
    assert not verify_token_binding(token, HASH_B)


def test_token_binding_rejects_tampered_verdict():
    token = issue_token(GATE_QC_TECHNICAL, Verdict.BLOCK, HASH_A)
    forged = replace(token, verdict=Verdict.PASS)
    assert not verify_token_binding(forged, HASH_A)


def test_token_issuance_is_deterministic():
    kwargs = dict(
        reason_codes=("X",), evidence={"a": 1}, issued_at="2026-10-23T00:00:00Z"
    )
    t1 = issue_token(GATE_QC_TECHNICAL, Verdict.WARN, HASH_A, **kwargs)
    t2 = issue_token(GATE_QC_TECHNICAL, Verdict.WARN, HASH_A, **kwargs)
    assert t1.digest == t2.digest and t1.token_id == t2.token_id


# ---------------------------------------------------------------------------
# G1 -- licence completeness
# ---------------------------------------------------------------------------

LICENCES_OK = {
    "clip1": LicenceRecord("clip1", "LIC-1", commercial_use_ok=True),
    "clip2": LicenceRecord("clip2", "LIC-2", commercial_use_ok=True),
    "music1": LicenceRecord("music1", "LIC-3", commercial_use_ok=True),
}
SEGMENTS_OK = [
    UsageSegment("clip1", 0.0, 30.0, role="b_roll"),
    UsageSegment("clip2", 30.0, 60.0, role="b_roll"),
    UsageSegment("music1", 0.0, 60.0, role="background_music"),
]


def test_g1_passes_complete_manifest():
    report = check_g1_licence_completeness(SEGMENTS_OK, LICENCES_OK, 60.0)
    assert report.verdict is Verdict.PASS
    assert report.uncovered_intervals == ()
    assert report.incomplete_assets == ()


def test_g1_blocks_missing_licence_and_lists_offender():
    licences = {k: v for k, v in LICENCES_OK.items() if k != "clip2"}
    video_segments = [s for s in SEGMENTS_OK if s.role == "b_roll"]
    report = check_g1_licence_completeness(video_segments, licences, 60.0)
    assert report.verdict is Verdict.BLOCK
    offenders = {p.asset_id: p.reasons for p in report.incomplete_assets}
    assert offenders["clip2"] == ("NO_LICENCE_RECORD",)
    # clip2's seconds are uncovered too.
    assert (30.0, 60.0) in report.uncovered_intervals
    assert report.offending_seconds == 30.0


def test_g1_blocks_timeline_gap():
    segments = [UsageSegment("clip1", 0.0, 30.0), UsageSegment("clip2", 40.0, 60.0)]
    report = check_g1_licence_completeness(segments, LICENCES_OK, 60.0)
    assert report.verdict is Verdict.BLOCK
    assert report.uncovered_intervals == ((30.0, 40.0),)
    assert "G1_TIMELINE_COVERAGE_GAP" in report.reason_codes


def test_g1_blocks_non_commercial_licence():
    licences = dict(LICENCES_OK)
    licences["clip1"] = LicenceRecord("clip1", "LIC-1", commercial_use_ok=False)
    report = check_g1_licence_completeness(SEGMENTS_OK, licences, 60.0)
    assert report.verdict is Verdict.BLOCK
    offenders = {p.asset_id: p.reasons for p in report.incomplete_assets}
    assert "COMMERCIAL_USE_NOT_OK" in offenders["clip1"]


# ---------------------------------------------------------------------------
# G2 -- provenance
# ---------------------------------------------------------------------------


def test_g2_passes_tier1_provenance():
    prov = {"clip1": AssetProvenance("clip1", "pexels", 1, acquisition_hash="ab12")}
    report = check_g2_provenance(["clip1"], prov)
    assert report.verdict is Verdict.PASS


def test_g2_blocks_tier3_source():
    prov = {
        "clip1": AssetProvenance(
            "clip1", "google_images", 3, acquisition_hash="ab12"
        )
    }
    report = check_g2_provenance(["clip1"], prov)
    assert report.verdict is Verdict.BLOCK
    assert "TIER3_SOURCE_PROHIBITED" in report.problems[0].reasons


def test_g2_blocks_unknown_provenance():
    report = check_g2_provenance(["clipX"], {})
    assert report.verdict is Verdict.BLOCK
    assert report.problems[0].reasons == ("UNKNOWN_PROVENANCE",)


def test_g2_blocks_missing_acquisition_record_and_watermark():
    prov = {"clip1": AssetProvenance("clip1", "pexels", 1, watermark_detected=True)}
    report = check_g2_provenance(["clip1"], prov)
    reasons = report.problems[0].reasons
    assert "MISSING_ACQUISITION_RECORD" in reasons
    assert "WATERMARK_DETECTED" in reasons


# ---------------------------------------------------------------------------
# G6 -- attribution diff
# ---------------------------------------------------------------------------

ATTR_TEXT = "Music: 'Sunny Day' by Test Artist (YouTube Audio Library)"
LICENCES_ATTR = [
    LicenceRecord(
        "music1",
        "LIC-3",
        commercial_use_ok=True,
        attribution_required=True,
        attribution_text=ATTR_TEXT,
    ),
    LicenceRecord("clip1", "LIC-1", commercial_use_ok=True),
]


def test_g6_passes_when_attribution_verbatim():
    description = f"Welcome to the video.\n\nCredits:\n{ATTR_TEXT}\nThanks!"
    report = check_g6_attribution(LICENCES_ATTR, description)
    assert report.verdict is Verdict.PASS
    assert report.missing == ()


def test_g6_blocks_missing_attribution():
    report = check_g6_attribution(LICENCES_ATTR, "A description with no credits.")
    assert report.verdict is Verdict.BLOCK
    assert report.missing[0].asset_id == "music1"
    assert report.missing[0].required_text == ATTR_TEXT


def test_g6_blocks_altered_attribution():
    altered = ATTR_TEXT.replace("Sunny Day", "sunny day")  # case changed
    report = check_g6_attribution(LICENCES_ATTR, f"Credits: {altered}")
    assert report.verdict is Verdict.BLOCK


# ---------------------------------------------------------------------------
# Copyright aggregate + Gate B aggregator
# ---------------------------------------------------------------------------


def _copyright_token(manifest_hash, prov_provider="pexels", prov_tier=1):
    provenances = {
        aid: AssetProvenance(aid, prov_provider, prov_tier, acquisition_hash="h")
        for aid in LICENCES_OK
    }
    report = run_copyright_gates(
        segments=SEGMENTS_OK,
        licences=LICENCES_OK,
        provenances=provenances,
        description="no attributions required here",
        total_duration_s=60.0,
    )
    return report.to_token(manifest_hash)


def _pass_tokens(manifest_hash):
    return [
        issue_token(GATE_QC_TECHNICAL, Verdict.PASS, manifest_hash),
        _copyright_token(manifest_hash),
        issue_token(GATE_POLICY_SIMILARITY, Verdict.PASS, manifest_hash),
    ]


def test_copyright_aggregate_token_pass_and_block():
    assert _copyright_token(HASH_A).verdict is Verdict.PASS
    blocked = _copyright_token(HASH_A, prov_provider="pinterest", prov_tier=3)
    assert blocked.verdict is Verdict.BLOCK
    assert blocked.gate_id == GATE_COPYRIGHT


def test_gate_b_allows_three_pass_tokens_same_hash():
    result = verify_gate_b(_pass_tokens(HASH_A), HASH_A)
    assert result.allowed
    assert set(result.satisfied_groups) == {"qc", "copyright", "policy"}


def test_gate_b_rejects_token_bound_to_other_hash():
    tokens = _pass_tokens(HASH_A)
    tokens[0] = issue_token(GATE_QC_TECHNICAL, Verdict.PASS, HASH_B)
    result = verify_gate_b(tokens, HASH_A)
    assert not result.allowed
    assert any(p.code == "MANIFEST_HASH_MISMATCH" for p in result.problems)


def test_gate_b_rejects_warn_and_missing_tokens():
    tokens = _pass_tokens(HASH_A)
    tokens[2] = issue_token(GATE_POLICY_SIMILARITY, Verdict.WARN, HASH_A)
    result = verify_gate_b(tokens, HASH_A)
    assert not result.allowed
    assert any(p.code == "VERDICT_NOT_PASS" for p in result.problems)

    result = verify_gate_b(tokens[:2], HASH_A)
    assert not result.allowed
    assert any(p.code == "MISSING_TOKEN" for p in result.problems)


# ---------------------------------------------------------------------------
# Policy similarity
# ---------------------------------------------------------------------------

SCRIPT_A = (
    "the ocean hides many secrets beneath its waves and sailors have "
    "feared the deep trenches for centuries because strange lights appear "
    "near the surface at night and nobody can explain them fully today"
)
SCRIPT_B_DISTINCT = (
    "cooking rice properly requires patience and the right amount of "
    "water plus a tight lid so the steam does the work while you prepare "
    "the vegetables and spices for the rest of the family dinner tonight"
)


def _prior(video_id, script, structure="countdown", hook="question",
           thumb="big_text", title="howto"):
    return VideoFingerprint(
        video_id=video_id,
        script_text=script,
        structure_template_id=structure,
        hook_formula_id=hook,
        thumbnail_template_id=thumb,
        title_formula_id=title,
    )


def test_similarity_blocks_near_duplicate_script():
    current = _prior("new", SCRIPT_A, structure="story")
    previous = [_prior("old1", SCRIPT_A, structure="countdown")]
    report = check_similarity(current, previous)
    assert report.verdict is Verdict.BLOCK
    assert "SCRIPT_NEAR_DUPLICATE" in report.reason_codes
    assert report.max_script_score > 0.9


def test_similarity_passes_distinct_video():
    current = _prior("new", SCRIPT_B_DISTINCT, structure="story",
                     hook="story_open", thumb="face_closeup", title="story")
    previous = [
        _prior(f"old{i}", SCRIPT_A, structure="countdown")
        for i in range(5)
    ]
    report = check_similarity(current, previous)
    assert report.verdict is Verdict.PASS
    assert report.reason_codes == ()


def test_similarity_structure_three_of_last_five_blocks():
    """Identical structure in 3+ of the last 5 forces BLOCK / rotation."""
    current = _prior("new", SCRIPT_B_DISTINCT, structure="countdown",
                     hook="h_new", thumb="t_new", title="f_new")
    previous = [
        _prior("v1", SCRIPT_A, structure="countdown", hook="h1", thumb="x1", title="y1"),
        _prior("v2", SCRIPT_A, structure="myth_fact", hook="h2", thumb="x2", title="y2"),
        _prior("v3", SCRIPT_A, structure="countdown", hook="h3", thumb="x3", title="y3"),
        _prior("v4", SCRIPT_A, structure="timeline", hook="h4", thumb="x4", title="y4"),
        _prior("v5", SCRIPT_A, structure="countdown", hook="h5", thumb="x5", title="y5"),
        _prior("v6", "gardening tips for small balconies with herbs and "
                     "tomatoes grown in pots through the summer months",
               structure="story", hook="h6", thumb="x6", title="y6"),
    ]
    report = check_similarity(current, previous)
    assert report.recent_counts["structure"] == 3
    assert report.verdict is Verdict.BLOCK
    assert "STRUCTURE_ROTATION_REQUIRED" in report.reason_codes


def test_similarity_warn_is_auto_fixable_once():
    """Same hook formula in 3 of last 5 (one packaging axis) -> WARN."""
    current = _prior("new", SCRIPT_B_DISTINCT, structure="story",
                     hook="question", thumb="t_new", title="f_new")
    previous = [
        _prior(f"v{i}", SCRIPT_A, structure=f"s{i}", hook="question",
               thumb=f"x{i}", title=f"y{i}")
        for i in range(3)
    ] + [
        _prior(f"w{i}", SCRIPT_A, structure=f"z{i}", hook=f"hh{i}",
               thumb=f"xx{i}", title=f"yy{i}")
        for i in range(2)
    ]
    report = check_similarity(current, previous)
    assert report.verdict is Verdict.WARN
    assert report.auto_fixable is True
    assert "PACKAGING_AXIS_REPEATED" in report.reason_codes


# ---------------------------------------------------------------------------
# Source synthesis (>= 3 sources, no source > 40% of claims)
# ---------------------------------------------------------------------------


def test_synthesis_passes_balanced_sources():
    report = check_source_synthesis({"srcA": 10, "srcB": 9, "srcC": 8, "srcD": 3})
    assert report.verdict is Verdict.PASS
    assert report.dominant_share <= 0.40


def test_synthesis_blocks_single_dominant_source():
    report = check_source_synthesis({"srcA": 50, "srcB": 20, "srcC": 20})
    assert report.verdict is Verdict.BLOCK
    assert "SINGLE_SOURCE_DOMINANT" in report.reason_codes
    assert report.dominant_source == "srcA"


def test_synthesis_blocks_too_few_sources_and_empty_ledger():
    report = check_source_synthesis({"srcA": 10, "srcB": 10})
    assert report.verdict is Verdict.BLOCK
    assert "INSUFFICIENT_INDEPENDENT_SOURCES" in report.reason_codes

    empty = check_source_synthesis({})
    assert empty.verdict is Verdict.BLOCK
    assert "EMPTY_FACT_LEDGER" in empty.reason_codes
