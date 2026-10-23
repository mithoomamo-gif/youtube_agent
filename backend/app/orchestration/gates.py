"""Hard gates A, B and C — deterministic, framework-free checks.

These functions are the single source of truth for gate logic (roadmap
Part 5.3).  They are pure: no LangGraph, no LLM, no I/O — the graph nodes
in :mod:`app.orchestration.graph` call them, and tests exercise them
directly.  Gates are absolute (roadmap section 08, invariant 6): a PASS
can only come from these checks succeeding — never from a retry, a
fallback, or a user skip.

* **Gate A — Script Lock** (after Agents 10 + 9): a locked script
  candidate must be present, there must be zero RED (unverifiable/false)
  claims, and the retention lint must have passed.
* **Gate B — Pre-Publish**: Gate A must have passed, and PASS tokens from
  ALL of Agents 32 (QC), 33 (Copyright) and 34 (Policy) must be present,
  each bound to the project's frozen ``publish_manifest_hash``.
* **Gate C — Publish**: Gate B must have passed, and the requesting agent
  must be Agent 26 (Upload) — no other agent may publish, ever.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .state import GATE_B_AGENT_IDS, UPLOAD_AGENT_ID, GateStatus, PassToken

__all__ = [
    "can_pass_gate_a",
    "can_pass_gate_b",
    "can_pass_gate_c",
    "issue_pass_token",
    "parse_gate_status",
    "dump_gate_status",
]


# ---------------------------------------------------------------------------
# State access helpers (accept the graph's plain-dict state, a Mapping, a
# GateStatus, or — for convenience — already-parsed PassToken objects).
# ---------------------------------------------------------------------------


def _get(state: Any, key: str, default: Any = None) -> Any:
    if isinstance(state, Mapping):
        return state.get(key, default)
    return getattr(state, key, default)


def parse_gate_status(state: Any) -> GateStatus:
    """Return the ``GateStatus`` for a project state (never raises).

    ``state`` is the graph state mapping whose ``gate_status`` value is a
    serialised ``GateStatus`` dict.  A bare ``GateStatus`` instance, or a
    mapping that *is* the serialised gate status, are also accepted so
    callers/tests can pass whichever shape they already hold.  Anything
    unparseable yields a fresh (all-failing) ``GateStatus``.
    """
    raw: Any = None
    if isinstance(state, GateStatus):
        return state
    if isinstance(state, Mapping):
        raw = state.get("gate_status")
        if raw is None and (
            "gate_a_script_locked" in state or "gate_b_tokens" in state
        ):
            raw = state
    elif state is not None:
        raw = getattr(state, "gate_status", None)

    if isinstance(raw, GateStatus):
        return raw
    if isinstance(raw, Mapping):
        try:
            return GateStatus.model_validate(dict(raw))
        except Exception:
            return GateStatus()
    return GateStatus()


def dump_gate_status(gate_status: GateStatus) -> dict[str, Any]:
    """Serialise a ``GateStatus`` for storage inside the graph state."""
    return gate_status.model_dump(mode="json")


def _all_tokens(state: Any, gate_status: GateStatus) -> list[PassToken]:
    """Collect Gate B tokens from the gate status (and, defensively, from
    a top-level ``gate_b_tokens`` key if a caller stored them there)."""
    tokens: list[PassToken] = list(gate_status.gate_b_tokens)
    if isinstance(state, Mapping):
        extra = state.get("gate_b_tokens")
        if extra:
            for item in extra:
                try:
                    token = (
                        item
                        if isinstance(item, PassToken)
                        else PassToken.model_validate(item)
                    )
                except Exception:
                    continue
                if token not in tokens:
                    tokens.append(token)
    return tokens


def _gate_a_passed(state: Any, gate_status: GateStatus) -> bool:
    if gate_status.gate_a_script_locked:
        return True
    # Defensive fallback for states that carry the flag at top level.
    return bool(_get(state, "gate_a_script_locked", False))


# ---------------------------------------------------------------------------
# Gate A — Script Lock
# ---------------------------------------------------------------------------


def can_pass_gate_a(state: Any) -> tuple[bool, list[str]]:
    """Check Gate A.  Returns ``(passed, reasons)``; every failed
    requirement contributes one human-readable reason."""
    reasons: list[str] = []

    locked_ref = _get(state, "locked_script_ref")
    locked_hash = _get(state, "locked_script_hash")
    script_versions = _get(state, "script_versions") or []
    has_candidate = bool(locked_ref) or bool(locked_hash) or bool(script_versions)
    if not has_candidate:
        reasons.append("No locked script candidate present")

    red_claim_count = _get(state, "red_claim_count")
    if red_claim_count is None:
        reasons.append("RED claim count has not been recorded")
    elif int(red_claim_count) != 0:
        reasons.append(f"RED claims present: {int(red_claim_count)}")

    if _get(state, "retention_lint_passed") is not True:
        reasons.append("Retention lint has not passed")

    return (not reasons, reasons)


# ---------------------------------------------------------------------------
# Gate B — Pre-Publish
# ---------------------------------------------------------------------------


def issue_pass_token(agent_id: int, manifest_hash: str) -> PassToken:
    """Issue a Gate B PASS token bound to ``manifest_hash``.

    Only the Phase 8 gate agents — 32 (Quality Check), 33 (Copyright &
    Licensing) and 34 (YouTube Policy & Monetisation Compliance) — may
    issue PASS tokens.  Any other ``agent_id`` raises ``ValueError``.
    """
    if agent_id not in GATE_B_AGENT_IDS:
        raise ValueError(
            f"Agent {agent_id} may not issue Gate B PASS tokens; "
            f"only agents {list(GATE_B_AGENT_IDS)} (QC, Copyright, Policy) may."
        )
    return PassToken(
        gate="B",
        agent_id=agent_id,  # type: ignore[arg-type]  # narrowed by the check above
        verdict="PASS",
        manifest_hash=manifest_hash,
        issued_at=datetime.now(timezone.utc),
    )


def can_pass_gate_b(state: Any) -> tuple[bool, list[str]]:
    """Check Gate B.  Returns ``(passed, reasons)``.

    Requires Gate A to have passed AND a PASS token from every one of
    Agents 32, 33 and 34 whose ``manifest_hash`` equals the project's
    frozen ``publish_manifest_hash``.
    """
    reasons: list[str] = []
    gate_status = parse_gate_status(state)

    if not _gate_a_passed(state, gate_status):
        reasons.append("Gate A (script lock) has not passed")

    manifest_hash = _get(state, "publish_manifest_hash")
    if not manifest_hash:
        reasons.append("Publish manifest hash is missing")

    tokens = _all_tokens(state, gate_status)
    for agent_id in GATE_B_AGENT_IDS:
        agent_tokens = [t for t in tokens if t.agent_id == agent_id]
        if not agent_tokens:
            reasons.append(f"Missing PASS token from agent {agent_id}")
            continue
        valid = [
            t
            for t in agent_tokens
            if t.verdict == "PASS"
            and manifest_hash
            and t.manifest_hash == manifest_hash
        ]
        if not valid:
            reasons.append(
                f"PASS token from agent {agent_id} does not match "
                "the publish manifest hash"
            )

    return (not reasons, reasons)


# ---------------------------------------------------------------------------
# Gate C — Publish
# ---------------------------------------------------------------------------


def can_pass_gate_c(
    state: Any, requesting_agent_id: int
) -> tuple[bool, list[str]]:
    """Check Gate C.  Returns ``(passed, reasons)``.

    Only Agent 26 (Upload) may execute publication, and only after Gate B
    has passed (roadmap Part 5.3).
    """
    reasons: list[str] = []
    gate_status = parse_gate_status(state)

    gate_b_ok = gate_status.gate_b_passed or bool(_get(state, "gate_b_passed", False))
    if not gate_b_ok:
        # A state whose tokens already satisfy Gate B counts as passed
        # even if the aggregate flag has not been written yet.
        gate_b_ok, _ = can_pass_gate_b(state)
    if not gate_b_ok:
        reasons.append("Gate B (pre-publish) has not passed")

    if requesting_agent_id != UPLOAD_AGENT_ID:
        reasons.append(
            f"Only agent {UPLOAD_AGENT_ID} (Upload) may publish; "
            f"requesting agent is {requesting_agent_id}"
        )

    return (not reasons, reasons)
