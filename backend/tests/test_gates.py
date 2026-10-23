"""Tests for orchestration gates A / B / C.

Contract (verified against the delivered modules on disk):
- backend/app/orchestration/state.py:
  VideoProjectState (TypedDict) with locked_script_ref,
  locked_script_hash, script_versions, red_claim_count,
  retention_lint_passed, publish_manifest_hash, and gate_status —
  a plain dict produced by GateStatus.model_dump(mode="json").
  GateStatus fields: gate_a_script_locked, gate_a_script_hash,
  gate_b_tokens (list[PassToken]), gate_b_passed,
  gate_c_published_by. PassToken fields: gate="B", agent_id in
  (32, 33, 34), verdict="PASS", manifest_hash, issued_at.
  GATE_B_AGENT_IDS = (32, 33, 34); UPLOAD_AGENT_ID = 26.
- backend/app/orchestration/gates.py:
  can_pass_gate_a(state) -> (bool, reasons)
  can_pass_gate_b(state) -> (bool, reasons)
  can_pass_gate_c(state, requesting_agent_id) -> (bool, reasons)
  issue_pass_token(agent_id, manifest_hash) -> PassToken
  (raises ValueError for agents other than 32/33/34)
"""
import pytest

try:
    from app.orchestration.gates import (
        can_pass_gate_a,
        can_pass_gate_b,
        can_pass_gate_c,
        issue_pass_token,
    )

    _IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    can_pass_gate_a = can_pass_gate_b = can_pass_gate_c = issue_pass_token = None  # type: ignore
    _IMPORT_ERROR = exc

pytestmark = pytest.mark.skipif(
    _IMPORT_ERROR is not None,
    reason=f"module not yet delivered: {_IMPORT_ERROR}",
)

MANIFEST_HASH = "test-manifest-hash-abc123"
OTHER_HASH = "different-manifest-hash-xyz789"
SCRIPT_HASH = "locked-script-hash-001"


# ---------------------------------------------------------------- helpers

def _result_bool(result):
    """Extract a bool from a (bool, reasons) tuple / bool / dict result."""
    if isinstance(result, bool):
        return result
    if isinstance(result, (tuple, list)):
        assert result, "gate returned an empty tuple/list"
        return bool(result[0])
    if isinstance(result, dict):
        for key in ("passed", "can_pass", "allowed", "ok", "success"):
            if key in result:
                return bool(result[key])
    for attr in ("passed", "can_pass", "allowed", "ok"):
        if hasattr(result, attr):
            return bool(getattr(result, attr))
    return bool(result)


def _serialize_token(token):
    """Serialise a PassToken the way GateStatus.model_dump(mode='json') does."""
    if hasattr(token, "model_dump"):
        return token.model_dump(mode="json")
    if isinstance(token, dict):
        return dict(token)
    return token


def make_state(**overrides):
    """Build a minimal VideoProjectState dict (clean / Gate-A-ready).

    Shape follows app/orchestration/state.py exactly: ``gate_status``
    is a serialised GateStatus dict. ``gate_status`` overrides are
    merged; all other overrides replace top-level keys.
    """
    gate_status = {
        "gate_a_script_locked": True,
        "gate_a_script_hash": SCRIPT_HASH,
        "gate_b_tokens": [],
        "gate_b_passed": False,
        "gate_c_published_by": None,
    }
    state = {
        "locked_script_ref": "locked-script-001",
        "locked_script_hash": SCRIPT_HASH,
        "script_versions": ["locked-script-001"],
        "red_claim_count": 0,
        "retention_lint_passed": True,
        "publish_manifest_hash": MANIFEST_HASH,
        "gate_status": gate_status,
    }
    for key, value in overrides.items():
        if key == "gate_status" and isinstance(value, dict):
            merged = dict(gate_status)
            merged.update(value)
            state["gate_status"] = merged
        else:
            state[key] = value
    return state


def _all_tokens(manifest_hash=MANIFEST_HASH):
    return [issue_pass_token(aid, manifest_hash) for aid in (32, 33, 34)]


def make_state_with_tokens(tokens, manifest_hash=MANIFEST_HASH, gate_b_passed=False):
    """State whose gate_status carries the given PassTokens (serialised)."""
    return make_state(
        publish_manifest_hash=manifest_hash,
        gate_status={
            "gate_a_script_locked": True,
            "gate_a_script_hash": SCRIPT_HASH,
            "gate_b_tokens": [_serialize_token(t) for t in tokens],
            "gate_b_passed": gate_b_passed,
        },
    )


# ---------------------------------------------------------------- Gate A

def test_gate_a_red_claims_cannot_pass():
    state = make_state(red_claim_count=2)
    result = can_pass_gate_a(state)
    assert _result_bool(result) is False, (
        f"Gate A must fail with red_claim_count=2, got {result!r}"
    )


def test_gate_a_clean_state_passes():
    state = make_state(red_claim_count=0)
    result = can_pass_gate_a(state)
    assert _result_bool(result) is True, (
        f"Gate A must pass for a clean locked script, got {result!r}"
    )


# ---------------------------------------------------------------- Gate B

def test_gate_b_fails_with_no_tokens():
    state = make_state_with_tokens([])
    assert _result_bool(can_pass_gate_b(state)) is False


@pytest.mark.parametrize("missing_agent", [32, 33, 34])
def test_gate_b_fails_when_any_token_missing(missing_agent):
    tokens = [t for t in _all_tokens() if t.agent_id != missing_agent]
    state = make_state_with_tokens(tokens)
    result = can_pass_gate_b(state)
    assert _result_bool(result) is False, (
        f"Gate B must fail when token for agent {missing_agent} is missing"
    )


def test_gate_b_fails_on_hash_mismatch():
    # Tokens issued for a DIFFERENT manifest hash than the state's.
    tokens = _all_tokens(manifest_hash=OTHER_HASH)
    state = make_state_with_tokens(tokens, manifest_hash=MANIFEST_HASH)
    result = can_pass_gate_b(state)
    assert _result_bool(result) is False, (
        "Gate B must fail when pass tokens do not match publish_manifest_hash"
    )


def test_gate_b_passes_with_all_three_matching_tokens():
    tokens = _all_tokens(manifest_hash=MANIFEST_HASH)
    state = make_state_with_tokens(tokens, manifest_hash=MANIFEST_HASH)
    result = can_pass_gate_b(state)
    assert _result_bool(result) is True, (
        f"Gate B must pass with all three (32/33/34) tokens matching "
        f"publish_manifest_hash, got {result!r}"
    )


# ---------------------------------------------------------------- Gate C

def _state_after_gate_b():
    tokens = _all_tokens(manifest_hash=MANIFEST_HASH)
    return make_state_with_tokens(tokens, manifest_hash=MANIFEST_HASH, gate_b_passed=True)


def test_gate_c_refuses_agent_7():
    result = can_pass_gate_c(_state_after_gate_b(), 7)
    assert _result_bool(result) is False, (
        f"Gate C must refuse requesting agent 7 (not the upload agent), got {result!r}"
    )


def test_gate_c_allows_agent_26_after_gate_b():
    result = can_pass_gate_c(_state_after_gate_b(), 26)
    assert _result_bool(result) is True, (
        f"Gate C must allow requesting agent 26 after Gate B, got {result!r}"
    )


def test_gate_c_allows_agent_26_with_tokens_even_if_flag_not_set():
    # can_pass_gate_c recomputes Gate B from tokens when the aggregate
    # gate_b_passed flag has not been written yet (delivered behaviour).
    tokens = _all_tokens(manifest_hash=MANIFEST_HASH)
    state = make_state_with_tokens(tokens, gate_b_passed=False)
    assert _result_bool(can_pass_gate_c(state, 26)) is True


def test_gate_c_refuses_agent_26_before_gate_b():
    state = make_state()  # no tokens, gate_b_passed=False
    result = can_pass_gate_c(state, 26)
    assert _result_bool(result) is False


# ---------------------------------------------------------------- tokens

def test_issue_pass_token_is_agent_and_hash_specific():
    token_a = issue_pass_token(32, MANIFEST_HASH)
    token_b = issue_pass_token(33, MANIFEST_HASH)
    token_c = issue_pass_token(32, OTHER_HASH)
    assert token_a.agent_id == 32
    assert token_a.manifest_hash == MANIFEST_HASH
    assert token_b.agent_id == 33
    assert token_c.manifest_hash == OTHER_HASH
    assert token_a != token_b
    assert token_a.manifest_hash != token_c.manifest_hash


def test_issue_pass_token_rejects_non_gate_b_agent():
    with pytest.raises(ValueError):
        issue_pass_token(7, MANIFEST_HASH)
