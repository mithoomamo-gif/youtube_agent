"""Tests for the Gate B conditional-pass policy (minor QC flags).

Covers the full contract matrix for
``app.pipeline.gates.conditional``:

* PASS unchanged -> classification "pass", decision "pass" / allowed.
* WARN minor-only -> "conditional"; review_first needs a recorded
  approval, auto blocks even WITH approval.
* WARN minor + non-minor -> "block".
* WARN with no flags -> "block" (fail closed).
* BLOCK / FAIL -> "block".
* Policy version constant present; MINOR_QC_FLAGS is exactly the two
  starting flags.

Per roadmap section 17: "a gate never seen to fail is assumed broken" --
both the allowing and the blocking paths are exercised here.
"""

import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from app.pipeline.gates.conditional import (  # noqa: E402
    MINOR_FLAG_POLICY_VERSION,
    MINOR_QC_FLAGS,
    GateBDecision,
    QcClassification,
    classify_qc,
    evaluate_conditional,
)
from app.pipeline.gates.tokens import Verdict  # noqa: E402


# ---------------------------------------------------------------------------
# Policy constants
# ---------------------------------------------------------------------------


def test_policy_version_constant_present():
    assert MINOR_FLAG_POLICY_VERSION == "1.0"
    assert isinstance(MINOR_FLAG_POLICY_VERSION, str)
    assert MINOR_FLAG_POLICY_VERSION  # non-empty


def test_minor_qc_flags_contains_exactly_the_two_starting_flags():
    assert isinstance(MINOR_QC_FLAGS, frozenset)
    assert MINOR_QC_FLAGS == frozenset(
        {"BLACK_SEGMENT_BRIEF", "FREEZE_DECLARED_STILL"}
    )
    assert len(MINOR_QC_FLAGS) == 2
    assert "BLACK_SEGMENT_BRIEF" in MINOR_QC_FLAGS
    assert "FREEZE_DECLARED_STILL" in MINOR_QC_FLAGS
    # Block-level counterparts must NOT be in the minor class.
    assert "BLACK_SEGMENT_TOO_LONG" not in MINOR_QC_FLAGS


# ---------------------------------------------------------------------------
# classify_qc: PASS
# ---------------------------------------------------------------------------


def test_classify_pass_enum_no_flags():
    result = classify_qc(Verdict.PASS, ())
    assert isinstance(result, QcClassification)
    assert result.outcome == "pass"
    assert result.minor_flags == ()
    assert result.non_minor_flags == ()
    assert result.policy_version == MINOR_FLAG_POLICY_VERSION
    assert result.reasons  # human-readable reasons present


def test_classify_pass_string_case_insensitive():
    for raw in ("PASS", "pass", "Pass", " pAsS "):
        result = classify_qc(raw, [])
        assert result.outcome == "pass", raw


def test_classify_pass_unchanged_even_with_flags():
    """PASS stays PASS; flags are still partitioned for diagnostics."""
    result = classify_qc(Verdict.PASS, ["BLACK_SEGMENT_BRIEF"])
    assert result.outcome == "pass"
    assert result.minor_flags == ("BLACK_SEGMENT_BRIEF",)
    assert result.non_minor_flags == ()


# ---------------------------------------------------------------------------
# classify_qc: WARN minor-only -> conditional
# ---------------------------------------------------------------------------


def test_classify_warn_black_segment_brief_only_is_conditional():
    """The Round 3 cricket case: 0.6s black passage, sole flag."""
    result = classify_qc(Verdict.WARN, ["BLACK_SEGMENT_BRIEF"])
    assert result.outcome == "conditional"
    assert result.minor_flags == ("BLACK_SEGMENT_BRIEF",)
    assert result.non_minor_flags == ()
    assert result.policy_version == "1.0"
    assert result.reasons


def test_classify_warn_freeze_declared_still_only_is_conditional():
    result = classify_qc(Verdict.WARN, ["FREEZE_DECLARED_STILL"])
    assert result.outcome == "conditional"
    assert result.minor_flags == ("FREEZE_DECLARED_STILL",)
    assert result.non_minor_flags == ()


def test_classify_warn_both_minor_flags_is_conditional():
    result = classify_qc(
        Verdict.WARN, ["BLACK_SEGMENT_BRIEF", "FREEZE_DECLARED_STILL"]
    )
    assert result.outcome == "conditional"
    assert set(result.minor_flags) == {
        "BLACK_SEGMENT_BRIEF",
        "FREEZE_DECLARED_STILL",
    }
    assert result.non_minor_flags == ()


def test_classify_warn_minor_string_input_case_insensitive():
    for raw_verdict in ("WARN", "warn", "Warn"):
        result = classify_qc(raw_verdict, ["black_segment_brief"])
        assert result.outcome == "conditional", raw_verdict
        assert result.minor_flags == ("BLACK_SEGMENT_BRIEF",)


def test_classify_warn_minor_single_string_flag():
    """A bare string flag is one flag, not an iterable of chars."""
    result = classify_qc("WARN", "BLACK_SEGMENT_BRIEF")
    assert result.outcome == "conditional"
    assert result.minor_flags == ("BLACK_SEGMENT_BRIEF",)


def test_classify_warn_duplicate_minor_flags_still_conditional():
    result = classify_qc(
        Verdict.WARN, ["BLACK_SEGMENT_BRIEF", "BLACK_SEGMENT_BRIEF"]
    )
    assert result.outcome == "conditional"
    assert result.minor_flags == ("BLACK_SEGMENT_BRIEF",)


# ---------------------------------------------------------------------------
# classify_qc: WARN that must block
# ---------------------------------------------------------------------------


def test_classify_warn_minor_plus_non_minor_blocks():
    result = classify_qc(
        Verdict.WARN, ["BLACK_SEGMENT_BRIEF", "AUDIO_CLIPPING"]
    )
    assert result.outcome == "block"
    assert result.minor_flags == ("BLACK_SEGMENT_BRIEF",)
    assert result.non_minor_flags == ("AUDIO_CLIPPING",)
    assert result.reasons


def test_classify_warn_block_level_flag_blocks():
    """BLACK_SEGMENT_TOO_LONG is the block counterpart, never minor."""
    result = classify_qc(Verdict.WARN, ["BLACK_SEGMENT_TOO_LONG"])
    assert result.outcome == "block"
    assert result.non_minor_flags == ("BLACK_SEGMENT_TOO_LONG",)


def test_classify_warn_non_minor_only_blocks():
    result = classify_qc(Verdict.WARN, ["LOUDNESS_OUT_OF_RANGE"])
    assert result.outcome == "block"
    assert result.minor_flags == ()
    assert result.non_minor_flags == ("LOUDNESS_OUT_OF_RANGE",)


def test_classify_warn_no_flags_blocks_fail_closed():
    for flags in ((), [], None, ""):
        result = classify_qc(Verdict.WARN, flags)
        assert result.outcome == "block", flags
        assert result.minor_flags == ()
        assert result.non_minor_flags == ()


def test_classify_warn_default_flags_argument_blocks():
    result = classify_qc(Verdict.WARN)
    assert result.outcome == "block"


# ---------------------------------------------------------------------------
# classify_qc: BLOCK / FAIL / unknown
# ---------------------------------------------------------------------------


def test_classify_block_enum_blocks():
    result = classify_qc(Verdict.BLOCK, ["BLACK_SEGMENT_TOO_LONG"])
    assert result.outcome == "block"
    assert result.policy_version == MINOR_FLAG_POLICY_VERSION
    assert result.reasons


def test_classify_block_string_blocks_even_with_minor_flags():
    """A BLOCK verdict never becomes conditional via minor flags."""
    result = classify_qc("BLOCK", ["BLACK_SEGMENT_BRIEF"])
    assert result.outcome == "block"


def test_classify_fail_string_blocks():
    for raw in ("FAIL", "fail", "Fail"):
        result = classify_qc(raw, ["BLACK_SEGMENT_BRIEF"])
        assert result.outcome == "block", raw


def test_classify_unknown_verdict_blocks_fail_closed():
    for raw in ("UNKNOWN", "", "MAYBE"):
        result = classify_qc(raw, ["BLACK_SEGMENT_BRIEF"])
        assert result.outcome == "block", raw


def test_classification_is_frozen():
    result = classify_qc(Verdict.PASS, ())
    with pytest.raises(FrozenInstanceError):
        result.outcome = "block"  # type: ignore[misc]


def test_classification_fields_and_types():
    result = classify_qc(Verdict.WARN, ["BLACK_SEGMENT_BRIEF", "SOMETHING_ELSE"])
    assert isinstance(result.minor_flags, tuple)
    assert isinstance(result.non_minor_flags, tuple)
    assert isinstance(result.reasons, tuple)
    assert all(isinstance(r, str) and r for r in result.reasons)
    assert result.policy_version == MINOR_FLAG_POLICY_VERSION


# ---------------------------------------------------------------------------
# evaluate_conditional: PASS classification
# ---------------------------------------------------------------------------


def _pass_classification():
    return classify_qc(Verdict.PASS, ())


def _conditional_classification():
    return classify_qc(Verdict.WARN, ["BLACK_SEGMENT_BRIEF"])


def _block_classification():
    return classify_qc(Verdict.BLOCK, ["BLACK_SEGMENT_TOO_LONG"])


def test_evaluate_pass_allowed_in_review_first():
    decision = evaluate_conditional(_pass_classification(), "review_first", False)
    assert isinstance(decision, GateBDecision)
    assert decision.outcome == "pass"
    assert decision.allowed is True
    assert decision.reasons


def test_evaluate_pass_allowed_in_auto():
    decision = evaluate_conditional(_pass_classification(), "auto", False)
    assert decision.outcome == "pass"
    assert decision.allowed is True


def test_evaluate_pass_allowed_regardless_of_approval():
    for approved in (True, False):
        decision = evaluate_conditional(
            _pass_classification(), "review_first", approved
        )
        assert decision.outcome == "pass"
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# evaluate_conditional: conditional + review_first
# ---------------------------------------------------------------------------


def test_evaluate_conditional_review_first_without_approval_pending():
    decision = evaluate_conditional(
        _conditional_classification(), "review_first", False
    )
    assert decision.outcome == "conditional_pending_approval"
    assert decision.allowed is False
    assert decision.reasons


def test_evaluate_conditional_review_first_with_approval_allowed():
    decision = evaluate_conditional(
        _conditional_classification(), "review_first", True
    )
    assert decision.outcome == "conditional_approved"
    assert decision.allowed is True
    assert decision.reasons


def test_evaluate_conditional_review_first_default_no_approval():
    """has_recorded_approval defaults to False -> pending, not allowed."""
    decision = evaluate_conditional(_conditional_classification(), "review_first")
    assert decision.outcome == "conditional_pending_approval"
    assert decision.allowed is False


# ---------------------------------------------------------------------------
# evaluate_conditional: conditional + auto (full-auto)
# ---------------------------------------------------------------------------


def test_evaluate_conditional_auto_blocks_without_approval():
    decision = evaluate_conditional(_conditional_classification(), "auto", False)
    assert decision.outcome == "blocked_full_auto"
    assert decision.allowed is False


def test_evaluate_conditional_auto_blocks_even_with_approval():
    """Full-auto NEVER accepts a conditional -- approval is irrelevant."""
    decision = evaluate_conditional(_conditional_classification(), "auto", True)
    assert decision.outcome == "blocked_full_auto"
    assert decision.allowed is False
    assert decision.reasons


# ---------------------------------------------------------------------------
# evaluate_conditional: block classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["review_first", "auto"])
@pytest.mark.parametrize("approved", [True, False])
def test_evaluate_block_never_allowed(mode, approved):
    decision = evaluate_conditional(_block_classification(), mode, approved)
    assert decision.outcome == "block"
    assert decision.allowed is False


def test_evaluate_warn_no_flags_classification_never_allowed():
    """End-to-end: flagless WARN -> block classification -> blocked."""
    classification = classify_qc(Verdict.WARN, [])
    assert classification.outcome == "block"
    for mode in ("review_first", "auto"):
        decision = evaluate_conditional(classification, mode, True)
        assert decision.outcome == "block"
        assert decision.allowed is False


def test_evaluate_warn_mixed_flags_classification_never_allowed():
    classification = classify_qc(
        Verdict.WARN, ["BLACK_SEGMENT_BRIEF", "AUDIO_CLIPPING"]
    )
    assert classification.outcome == "block"
    decision = evaluate_conditional(classification, "review_first", True)
    assert decision.outcome == "block"
    assert decision.allowed is False


# ---------------------------------------------------------------------------
# evaluate_conditional: end-to-end Round 3 cricket scenario + robustness
# ---------------------------------------------------------------------------


def test_round3_cricket_scenario_end_to_end():
    """WARN + only BLACK_SEGMENT_BRIEF: conditional, approval-gated."""
    classification = classify_qc(Verdict.WARN, ["BLACK_SEGMENT_BRIEF"])
    assert classification.outcome == "conditional"

    pending = evaluate_conditional(classification, "review_first", False)
    assert (pending.outcome, pending.allowed) == (
        "conditional_pending_approval",
        False,
    )

    approved = evaluate_conditional(classification, "review_first", True)
    assert (approved.outcome, approved.allowed) == (
        "conditional_approved",
        True,
    )

    auto = evaluate_conditional(classification, "auto", True)
    assert (auto.outcome, auto.allowed) == ("blocked_full_auto", False)


def test_evaluate_decision_is_frozen():
    decision = evaluate_conditional(_pass_classification(), "auto", False)
    with pytest.raises(FrozenInstanceError):
        decision.allowed = False  # type: ignore[misc]


def test_evaluate_unknown_mode_fails_closed_for_conditional():
    decision = evaluate_conditional(
        _conditional_classification(), "sometimes_review", True
    )
    assert decision.outcome == "block"
    assert decision.allowed is False


def test_evaluate_mode_is_case_insensitive():
    decision = evaluate_conditional(
        _conditional_classification(), "Review_First", True
    )
    assert decision.outcome == "conditional_approved"
    assert decision.allowed is True

    decision = evaluate_conditional(_conditional_classification(), "AUTO", True)
    assert decision.outcome == "blocked_full_auto"
    assert decision.allowed is False
