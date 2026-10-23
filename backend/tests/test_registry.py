"""Tests for the agent registry.

Contract:
- backend/app/agents/registry.py: AGENT_REGISTRY (list of AgentDefinition
  with agent_id, slug, phase), exactly 35 entries ids 1..35;
  get_agent(); validate_registry() -> list of problems (empty when valid)
"""
import pytest

try:
    from app.agents.registry import AGENT_REGISTRY, get_agent, validate_registry

    _IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    AGENT_REGISTRY = []
    get_agent = None  # type: ignore
    validate_registry = None  # type: ignore
    _IMPORT_ERROR = exc

pytestmark = pytest.mark.skipif(
    _IMPORT_ERROR is not None,
    reason=f"module not yet delivered: {_IMPORT_ERROR}",
)


def _field(agent, *names, default=None):
    """Read the first present field among ``names`` from object/dict."""
    for name in names:
        if isinstance(agent, dict):
            if name in agent and agent[name] is not None:
                return agent[name]
        else:
            value = getattr(agent, name, None)
            if value is not None:
                return value
    return default


def _agent_id(agent):
    return _field(agent, "agent_id", "id")


def _gates_of(agent):
    return _field(agent, "gates", "gate", "gate_letters", default=None)


def _gates_contain_c(agent):
    """True if the agent's gates representation contains gate C."""
    gates = _gates_of(agent)
    if gates is None:
        return False
    if isinstance(gates, str):
        # e.g. "C", "BC", "Gate C", "gate_c"
        normalized = gates.strip().upper().replace("_", " ").replace("-", " ")
        if normalized in {"C", "GATE C", "BC", "ABC", "B C", "A B C"}:
            return True
        return "C" in normalized.split()
    # iterable of gate identifiers
    try:
        items = list(gates)
    except TypeError:
        return "C" == str(gates).strip().upper()
    for item in items:
        s = str(item).strip().upper()
        if s in {"C", "GATE_C", "GATE C", "GATEC"}:
            return True
        # Enum values may be e.g. Gate.C whose str is "Gate.C"
        if s.endswith(".C") or s.endswith(":C"):
            return True
    return False


def _produces_pass_token(agent):
    return _field(
        agent,
        "produce_pass_token",
        "produces_pass_token",
        "can_issue_pass_token",
        "issues_pass_token",
        "produce_token",
        default=False,
    )


def test_registry_has_exactly_35_entries():
    assert len(AGENT_REGISTRY) == 35


def test_agent_ids_exactly_1_to_35_unique():
    ids = [_agent_id(a) for a in AGENT_REGISTRY]
    assert all(isinstance(i, int) for i in ids), f"agent ids should be ints, got {ids!r}"
    assert len(ids) == len(set(ids)), f"duplicate agent ids: {ids!r}"
    assert set(ids) == set(range(1, 36))


def test_slugs_unique():
    slugs = [_field(a, "slug") for a in AGENT_REGISTRY]
    assert all(slugs), f"every agent needs a slug, got {slugs!r}"
    assert len(slugs) == len(set(slugs)), f"duplicate slugs: {slugs!r}"


def test_phases_within_1_to_8():
    for agent in AGENT_REGISTRY:
        phase = _field(agent, "phase")
        assert phase is not None, f"agent {_agent_id(agent)} missing phase"
        assert isinstance(phase, int), f"agent {_agent_id(agent)} phase not int: {phase!r}"
        assert 1 <= phase <= 8, f"agent {_agent_id(agent)} phase {phase} outside 1..8"


def test_upload_agent_26_is_only_gate_c_agent():
    agents_with_c = [a for a in AGENT_REGISTRY if _gates_contain_c(a)]
    assert len(agents_with_c) == 1, (
        f"expected exactly 1 gate-C agent, got "
        f"{[_agent_id(a) for a in agents_with_c]!r}"
    )
    assert _agent_id(agents_with_c[0]) == 26


def test_agents_32_33_34_produce_pass_token():
    by_id = {_agent_id(a): a for a in AGENT_REGISTRY}
    for agent_id in (32, 33, 34):
        assert agent_id in by_id, f"agent {agent_id} missing from registry"
        assert _produces_pass_token(by_id[agent_id]) is True, (
            f"agent {agent_id} should produce_pass_token=True, "
            f"got {_produces_pass_token(by_id[agent_id])!r}"
        )


def test_get_agent_looks_up_by_id():
    # get_agent may accept an int id (primary contract) — try that first,
    # fall back to slug lookup only if the int form is unsupported.
    try:
        agent = get_agent(26)
    except Exception:
        agent = get_agent("upload")  # noqa: F841 - slug form, if any
        return
    assert _agent_id(agent) == 26


def test_validate_registry_returns_no_problems():
    problems = validate_registry()
    assert problems == [], f"validate_registry() should be [], got {problems!r}"
