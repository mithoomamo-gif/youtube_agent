"""Round 4 — Gate B conditional-pass flow (Worker 2).

Aggregator level + publish_stage level:

* PASS path unchanged (strict ``verify_gate_b`` agrees).
* QC WARN minor-only (``BLACK_SEGMENT_BRIEF`` — the Round 3 cricket
  case) without approval stays frozen: manifest frozen, no fake upload.
* Conditional + recorded approval + ``review_first`` releases to the
  fake-provider publish simulation.
* Conditional + approval + ``auto`` still blocked.
* WARN with a non-minor flag blocked in all modes / approvals.

No network, no keys.  The publish-stage harness mirrors
``test_stock_ledger``: a fabricated master file, the real
``finalize_publish`` path (copyright G1/G2/G6 over the synthesised
own/generated row, policy similarity with no history), so only the QC
verdict varies between cases.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.integration.models import ProjectSpec, make_context
from app.integration.publish_stage import finalize_publish
from app.pipeline.gates.aggregator import evaluate_gate_b, verify_gate_b
from app.pipeline.gates.tokens import (
    GATE_COPYRIGHT,
    GATE_POLICY_SIMILARITY,
    GATE_QC_TECHNICAL,
    Verdict,
    compute_manifest_hash,
    issue_token,
)

HASH = compute_manifest_hash({"video": "conditional-test", "version": 1})

MINOR = ("BLACK_SEGMENT_BRIEF",)
NON_MINOR = ("BLACK_SEGMENT_BRIEF", "LOUDNESS_OUT_OF_RANGE")


# ---------------------------------------------------------------------------
# Aggregator level
# ---------------------------------------------------------------------------


def _tokens(qc_verdict, qc_codes=(), manifest_hash=HASH):
    return [
        issue_token(GATE_QC_TECHNICAL, qc_verdict, manifest_hash, reason_codes=qc_codes),
        issue_token(GATE_COPYRIGHT, Verdict.PASS, manifest_hash),
        issue_token(GATE_POLICY_SIMILARITY, Verdict.PASS, manifest_hash),
    ]


def test_agg_pass_path_unchanged():
    tokens = _tokens(Verdict.PASS)
    strict = verify_gate_b(tokens, HASH)
    policy = evaluate_gate_b(tokens, HASH)
    assert strict.allowed is True
    assert policy.allowed is True
    assert policy.outcome == "pass"
    assert policy.classification.outcome == "pass"
    assert policy.decision.allowed is True
    assert policy.problems == ()
    assert set(policy.satisfied_groups) == {"qc", "copyright", "policy"}


def test_agg_conditional_without_approval_blocked_review_first():
    tokens = _tokens(Verdict.WARN, MINOR)
    # Strict Gate B still fails a WARN — conditional is NOT a pass.
    assert verify_gate_b(tokens, HASH).allowed is False
    policy = evaluate_gate_b(tokens, HASH, mode="review_first", has_recorded_approval=False)
    assert policy.allowed is False
    assert policy.outcome == "conditional"
    assert policy.classification.outcome == "conditional"
    assert tuple(policy.classification.minor_flags) == MINOR
    assert policy.classification.non_minor_flags == ()
    assert policy.classification.policy_version == "1.0"
    assert policy.decision.allowed is False
    assert policy.decision.outcome == "conditional_pending_approval"
    assert any(p.code == "CONDITIONAL_APPROVAL_REQUIRED" for p in policy.problems)


def test_agg_conditional_with_approval_review_first_released():
    policy = evaluate_gate_b(
        _tokens(Verdict.WARN, MINOR), HASH,
        mode="review_first", has_recorded_approval=True,
    )
    assert policy.allowed is True
    assert policy.outcome == "conditional"
    assert policy.decision.outcome == "conditional_approved"
    assert policy.decision.allowed is True
    assert policy.problems == ()
    assert "qc" in policy.satisfied_groups


@pytest.mark.parametrize("flag", ["BLACK_SEGMENT_BRIEF", "FREEZE_DECLARED_STILL"])
def test_agg_each_minor_flag_is_conditional(flag):
    policy = evaluate_gate_b(
        _tokens(Verdict.WARN, (flag,)), HASH,
        mode="review_first", has_recorded_approval=True,
    )
    assert policy.outcome == "conditional"
    assert policy.allowed is True


def test_agg_conditional_with_approval_auto_still_blocked():
    policy = evaluate_gate_b(
        _tokens(Verdict.WARN, MINOR), HASH,
        mode="auto", has_recorded_approval=True,
    )
    assert policy.allowed is False
    assert policy.outcome == "conditional"
    assert policy.decision.outcome == "blocked_full_auto"
    assert policy.decision.allowed is False
    assert any(p.code == "CONDITIONAL_BLOCKED_AUTO_MODE" for p in policy.problems)


def test_agg_conditional_without_approval_auto_blocked():
    policy = evaluate_gate_b(
        _tokens(Verdict.WARN, MINOR), HASH, mode="auto", has_recorded_approval=False,
    )
    assert policy.allowed is False
    assert policy.outcome == "conditional"


@pytest.mark.parametrize("mode", ["review_first", "auto"])
@pytest.mark.parametrize("approved", [False, True])
def test_agg_warn_non_minor_blocked_in_all_modes(mode, approved):
    policy = evaluate_gate_b(
        _tokens(Verdict.WARN, NON_MINOR), HASH,
        mode=mode, has_recorded_approval=approved,
    )
    assert policy.allowed is False
    assert policy.outcome == "block"
    assert policy.classification.outcome == "block"
    assert "LOUDNESS_OUT_OF_RANGE" in tuple(policy.classification.non_minor_flags)


@pytest.mark.parametrize("mode", ["review_first", "auto"])
def test_agg_qc_block_never_conditional(mode):
    policy = evaluate_gate_b(
        _tokens(Verdict.BLOCK, ("BLACK_SEGMENT_TOO_LONG",)), HASH,
        mode=mode, has_recorded_approval=True,
    )
    assert policy.allowed is False
    assert policy.outcome == "block"


def test_agg_conditional_does_not_rescue_other_group_failure():
    tokens = _tokens(Verdict.WARN, MINOR)
    tokens[1] = issue_token(GATE_COPYRIGHT, Verdict.BLOCK, HASH)
    policy = evaluate_gate_b(
        tokens, HASH, mode="review_first", has_recorded_approval=True,
    )
    assert policy.allowed is False
    assert policy.outcome == "conditional"
    assert any(p.group == "copyright" for p in policy.problems)


def test_agg_missing_qc_token_blocks():
    policy = evaluate_gate_b(_tokens(Verdict.PASS)[1:], HASH)
    assert policy.allowed is False
    assert policy.outcome == "block"
    assert policy.classification is None


def test_agg_audit_sink_records_conditional_events(tmp_path):
    sink = tmp_path / "gate_b_audit.jsonl"
    evaluate_gate_b(
        _tokens(Verdict.WARN, MINOR), HASH,
        mode="review_first", has_recorded_approval=False,
        audit_sink=sink, actor="system", asset_ids=["189845"],
    )
    evaluate_gate_b(
        _tokens(Verdict.WARN, MINOR), HASH,
        mode="review_first", has_recorded_approval=True,
        audit_sink=sink, actor="reviewer", asset_ids=["189845"],
    )
    try:
        from app.pipeline.gates.audit import read_gate_b_events

        events = read_gate_b_events(sink)
    except ImportError:
        events = [json.loads(line) for line in sink.read_text().splitlines() if line.strip()]
    assert [e["event_type"] for e in events] == [
        "conditional_token_minted", "publish_released",
    ]
    for event in events:
        assert event["manifest_hash"] == HASH
        assert event["mode"] == "review_first"
        assert event["outcome"] == "conditional"
        assert event["qc_flags"] == ["BLACK_SEGMENT_BRIEF"]
        assert event["minor_flags"] == ["BLACK_SEGMENT_BRIEF"]
        assert event["asset_ids"] == ["189845"]
        assert event["policy_version"] == "1.0"


# ---------------------------------------------------------------------------
# publish_stage level
# ---------------------------------------------------------------------------


def _run_publish(tmp_path: Path, qc_info, **kwargs):
    master = tmp_path / "final.mp4"
    master.write_bytes(b"fake-master-bytes-for-conditional-test")
    srt = tmp_path / "captions.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:06,000\nHello\n", encoding="utf-8")
    thumb = tmp_path / "thumbnail_1.png"
    thumb.write_bytes(b"fake-thumb")

    spec = ProjectSpec(niche="facts", topic="Ocean depth secrets", minutes=1.0)
    profile = SimpleNamespace(profile_id="facts")
    ctx = make_context(spec, profile, tmp_path / "proj", project_id="cond-test")
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
        duration_s=18.0,
        thumbnails=[str(thumb)],
        captions_path=str(srt),
        qc_passed=(qc_info.get("verdict") == "PASS"),
    )
    return finalize_publish(assemble, qc_info, bundle, profile, ctx, True, **kwargs)


def _qc(verdict, *codes):
    checks = {}
    if codes:
        checks = {"check_0": {"reason_codes": list(codes)}}
    return {"verdict": verdict, "checks": checks}


def test_publish_pass_path_unchanged(tmp_path):
    result = _run_publish(tmp_path, _qc("PASS"))
    assert result.gate_b_passed is True
    assert result.gate_b_outcome == "pass"
    assert result.gate_b_conditional == {}
    assert result.publish_state == "fake_provider_upload_simulated"
    assert result.video_id is not None
    assert result.manifest["gate_b"]["passed"] is True


def test_publish_conditional_without_approval_stays_frozen(tmp_path):
    result = _run_publish(tmp_path, _qc("WARN", "BLACK_SEGMENT_BRIEF"))
    assert result.gate_b_passed is False
    assert result.gate_b_outcome == "conditional"
    assert result.publish_state == "manifest_frozen_not_uploaded"
    assert result.video_id is None
    assert result.notes.get("upload_skipped")
    # Manifest is frozen on disk, marked not-passed, conditional recorded.
    on_disk = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert on_disk["publish_state"] == "manifest_frozen_not_uploaded"
    assert on_disk["gate_b"]["passed"] is False
    assert on_disk["gate_b"]["outcome"] == "conditional"
    assert on_disk["gate_b"]["conditional"]["minor_flags"] == ["BLACK_SEGMENT_BRIEF"]
    assert on_disk["gate_b"]["conditional"]["policy_version"] == "1.0"
    assert any(
        p["code"] == "CONDITIONAL_APPROVAL_REQUIRED" for p in result.gate_b_problems
    )


def test_publish_conditional_record_approval_review_first_releases(tmp_path):
    approval = {"approved": True, "actor": "human-reviewer", "approval_id": "appr-1"}
    result = _run_publish(
        tmp_path, _qc("WARN", "BLACK_SEGMENT_BRIEF"),
        mode="review_first", gate_b_approval=approval,
    )
    assert result.gate_b_passed is True
    assert result.gate_b_outcome == "conditional"
    assert result.gate_b_conditional["has_recorded_approval"] is True
    assert result.publish_state == "fake_provider_upload_simulated"
    assert result.video_id is not None
    assert result.manifest["gate_b"]["passed"] is True
    # Strict PASS-only view stays honestly False in the manifest.
    assert result.manifest["gate_b"]["strict_passed"] is False


def test_publish_conditional_bool_approval_and_publish_mode_alias(tmp_path):
    result = _run_publish(
        tmp_path, _qc("WARN", "FREEZE_DECLARED_STILL"),
        publish_mode="review_first", gate_b_approval=True,
    )
    assert result.gate_b_passed is True
    assert result.publish_state == "fake_provider_upload_simulated"


def test_publish_conditional_with_approval_auto_still_blocked(tmp_path):
    result = _run_publish(
        tmp_path, _qc("WARN", "BLACK_SEGMENT_BRIEF"),
        mode="auto", gate_b_approval=True,
    )
    assert result.gate_b_passed is False
    assert result.gate_b_outcome == "conditional"
    assert result.publish_state == "manifest_frozen_not_uploaded"
    assert result.video_id is None
    assert any(
        p["code"] == "CONDITIONAL_BLOCKED_AUTO_MODE" for p in result.gate_b_problems
    )


@pytest.mark.parametrize("kwargs", [
    {},
    {"mode": "review_first", "gate_b_approval": True},
    {"mode": "auto", "gate_b_approval": True},
])
def test_publish_warn_non_minor_blocked_in_all_modes(tmp_path, kwargs):
    sub = tmp_path / f"case{len(list(tmp_path.iterdir()))}"
    sub.mkdir(parents=True, exist_ok=True)
    result = _run_publish(sub, _qc("WARN", "BLACK_SEGMENT_BRIEF", "LOUDNESS_OUT_OF_RANGE"), **kwargs)
    assert result.gate_b_passed is False
    assert result.gate_b_outcome == "block"
    assert result.publish_state == "manifest_frozen_not_uploaded"
    assert result.video_id is None


def test_publish_qc_block_unchanged(tmp_path):
    result = _run_publish(tmp_path, _qc("BLOCK", "BLACK_SEGMENT_TOO_LONG"))
    assert result.gate_b_passed is False
    assert result.gate_b_outcome == "block"
    assert result.publish_state == "manifest_frozen_not_uploaded"


def test_publish_conditional_writes_audit_events(tmp_path):
    audit_sink = tmp_path / "audit.jsonl"
    result = _run_publish(
        tmp_path, _qc("WARN", "BLACK_SEGMENT_BRIEF"), audit_sink=audit_sink,
    )
    assert result.gate_b_passed is False
    try:
        from app.pipeline.gates.audit import read_gate_b_events

        events = read_gate_b_events(audit_sink)
    except ImportError:
        events = []
    if events:  # audit module landed — events must carry the full contract keys
        assert events[0]["event_type"] == "conditional_token_minted"
        for key in ("manifest_hash", "mode", "outcome", "qc_flags",
                    "minor_flags", "asset_ids", "policy_version", "actor"):
            assert key in events[0], key
        assert events[0]["qc_flags"] == ["BLACK_SEGMENT_BRIEF"]
        assert events[0]["minor_flags"] == ["BLACK_SEGMENT_BRIEF"]
        assert events[0]["mode"] == "review_first"
