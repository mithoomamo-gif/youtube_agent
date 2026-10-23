"""Supervisor graph — LangGraph orchestration for one Video Project.

Master Supervisor (Layer 0, roadmap Part 5.1): owns the workflow, moves
the project through the seven phases, and enforces the hard gates.  Agents
never assign work to each other; everything flows through this graph.

Topology::

    START
      -> phase_1_research -> phase_2_scripting -> gate_a
      -> phase_3_production -> phase_4_editing -> phase_5_packaging
      -> gate_b -> phase_6_publishing -> gate_c -> phase_7_growth
      -> done -> END

A failed gate routes to END with ``status="blocked"`` and a plain-language
``escalation`` — a pipeline may pause, never publish in an unverified
state (roadmap section 08).

Phase nodes are thin, deterministic stubs: they record a decision-log
entry and advance ``phase``.  The real per-agent work (Agents 1-35, see
``app.agents.registry``) plugs into these nodes later; there are no LLM
calls in this module.

Human review: in ``review_first`` publish mode, ``gate_a`` and ``gate_b``
pause via :func:`langgraph.types.interrupt` and only proceed on an
approving resume value.  In ``auto`` mode the gates are still applied —
they are automated, not removed (roadmap Part 5.3).

Checkpointing (roadmap section 08): one thread per Video Project, state
saved after every node.  With ``db_path`` the graph uses
``SqliteSaver``; if the sqlite checkpoint package is unavailable it falls
back to ``MemorySaver`` so the graph always builds.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import END, START, StateGraph

from .gates import can_pass_gate_a, can_pass_gate_b, can_pass_gate_c, dump_gate_status, parse_gate_status
from .policies import escalation_reason
from .state import UPLOAD_AGENT_ID, VideoProjectState

__all__ = ["build_supervisor_graph", "PHASE_NODE_AGENTS"]

# ---------------------------------------------------------------------------
# Phase -> agent ids (roadmap Part 5.2 / section 03).  Another worker owns
# ``backend/app/agents/registry.py`` (AGENT_REGISTRY, ids 1..35); when it is
# present we try to derive this mapping from it, otherwise the hardcoded
# roadmap mapping below is used — the graph never blocks on the registry.
# ---------------------------------------------------------------------------

_HARDCODED_PHASE_AGENTS: dict[str, tuple[int, ...]] = {
    "phase_1_research": (1, 2, 3, 4, 5),
    "phase_2_scripting": (6, 7, 8, 9, 10),
    "phase_3_production": (11, 12, 13, 14, 15, 16, 17),
    "phase_4_editing": (18, 19, 20, 21),
    "phase_5_packaging": (22, 23, 24, 25),
    "phase_6_publishing": (26, 27),
    "phase_7_growth": (28, 29, 30, 31),
}

_PHASE_NUMBER_BY_NODE: dict[str, int] = {
    "phase_1_research": 1,
    "phase_2_scripting": 2,
    "phase_3_production": 3,
    "phase_4_editing": 4,
    "phase_5_packaging": 5,
    "phase_6_publishing": 6,
    "phase_7_growth": 7,
}


def _phase_agents_from_registry() -> dict[str, tuple[int, ...]] | None:
    """Best-effort phase mapping from AGENT_REGISTRY; None if unusable."""
    try:
        from app.agents.registry import AGENT_REGISTRY  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        by_phase: dict[int, list[int]] = {}
        items = AGENT_REGISTRY.items() if hasattr(AGENT_REGISTRY, "items") else []
        for agent_id, spec in items:
            phase = spec.get("phase") if isinstance(spec, dict) else getattr(spec, "phase", None)
            if isinstance(phase, int) and 1 <= phase <= 7:
                by_phase.setdefault(phase, []).append(int(agent_id))
        if sorted(by_phase) != [1, 2, 3, 4, 5, 6, 7]:
            return None
        return {
            node: tuple(sorted(by_phase[number]))
            for node, number in _PHASE_NUMBER_BY_NODE.items()
        }
    except Exception:
        return None


PHASE_NODE_AGENTS: dict[str, tuple[int, ...]] = (
    _phase_agents_from_registry() or _HARDCODED_PHASE_AGENTS
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_entry(node: str, event: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"node": node, "event": event, "detail": detail, "at": _now_iso(), **extra}


def _append_log(state: VideoProjectState, entry: dict[str, Any]) -> list[dict[str, Any]]:
    return [*(state.get("decision_log") or []), entry]


def _is_approval(value: Any) -> bool:
    """Interpret a human resume value at a review gate.

    Approving: ``True``, ``None`` (a bare continue), strings like
    ``"approve"``/``"yes"``, dicts like ``{"approved": True}`` or
    ``{"decision": "approve"}``.  Anything explicitly negative rejects.
    """
    if value is None or value is True:
        return True
    if value is False:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"approve", "approved", "pass", "yes", "y", "ok"}
    if isinstance(value, dict):
        for key in ("approved", "approve", "accepted"):
            if key in value:
                return bool(value[key])
        for key in ("decision", "action", "verdict"):
            if key in value:
                return str(value[key]).strip().lower() in {
                    "approve",
                    "approved",
                    "pass",
                    "yes",
                    "ok",
                }
        return False
    return bool(value)


def _make_checkpointer(db_path: str | None) -> Any:
    """SqliteSaver for ``db_path`` when possible, else MemorySaver."""
    if db_path:
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver

            entered = SqliteSaver.from_conn_string(db_path)
            # Installed versions expose from_conn_string as a context
            # manager; enter it and keep the manager alive on the saver
            # so the connection survives for the graph's lifetime.
            if hasattr(entered, "__enter__"):
                saver = entered.__enter__()
                saver._supervisor_cm = entered  # type: ignore[attr-defined]
                return saver
            return entered
        except Exception:
            pass  # fall through to the in-memory fallback
    from langgraph.checkpoint.memory import MemorySaver

    return MemorySaver()


# ---------------------------------------------------------------------------
# Phase nodes (thin deterministic stubs — real agent work plugs in later)
# ---------------------------------------------------------------------------


def _phase_node(node_name: str) -> Callable[[VideoProjectState], dict[str, Any]]:
    phase_number = _PHASE_NUMBER_BY_NODE[node_name]
    agent_ids = PHASE_NODE_AGENTS.get(node_name, ())

    def _node(state: VideoProjectState) -> dict[str, Any]:
        return {
            "phase": phase_number,
            "status": "running",
            "decision_log": _append_log(
                state,
                _log_entry(
                    node_name,
                    "phase_completed",
                    f"Phase {phase_number} stub completed "
                    f"(agents {list(agent_ids)} plug in here).",
                    phase=phase_number,
                    agents=list(agent_ids),
                ),
            ),
        }

    _node.__name__ = node_name
    return _node


# ---------------------------------------------------------------------------
# Gate nodes
# ---------------------------------------------------------------------------


def _gate_a_node(state: VideoProjectState) -> dict[str, Any]:
    passed, reasons = can_pass_gate_a(state)
    if not passed:
        message = escalation_reason(reasons, gate="Gate A (script lock)")
        return {
            "status": "blocked",
            "escalation": message,
            "decision_log": _append_log(
                state,
                _log_entry("gate_a", "gate_failed", message, reasons=reasons),
            ),
        }

    if state.get("publish_mode", "auto") == "review_first":
        from langgraph.types import interrupt

        decision = interrupt(
            {
                "gate": "A",
                "status": "awaiting_gate_a",
                "message": "Gate A checks passed. Approve the script lock to continue.",
                "locked_script_ref": state.get("locked_script_ref"),
                "script_versions": state.get("script_versions") or [],
            }
        )
        if not _is_approval(decision):
            message = escalation_reason(
                "The user did not approve the script lock at Gate A.",
                gate="Gate A (script lock)",
            )
            return {
                "status": "blocked",
                "escalation": message,
                "decision_log": _append_log(
                    state, _log_entry("gate_a", "gate_rejected", message)
                ),
            }

    gate_status = parse_gate_status(state)
    locked_ref = state.get("locked_script_ref")
    if not locked_ref and state.get("script_versions"):
        locked_ref = state["script_versions"][-1]
    locked_hash = state.get("locked_script_hash")
    if not locked_hash and locked_ref:
        locked_hash = hashlib.sha256(str(locked_ref).encode("utf-8")).hexdigest()

    gate_status.gate_a_script_locked = True
    gate_status.gate_a_script_hash = locked_hash

    update: dict[str, Any] = {
        "status": "running",
        "gate_status": dump_gate_status(gate_status),
        "decision_log": _append_log(
            state,
            _log_entry(
                "gate_a",
                "gate_passed",
                "Gate A passed: script locked.",
                locked_script_hash=locked_hash,
            ),
        ),
    }
    if locked_ref:
        update["locked_script_ref"] = locked_ref
    if locked_hash:
        update["locked_script_hash"] = locked_hash
    return update


def _gate_b_node(state: VideoProjectState) -> dict[str, Any]:
    passed, reasons = can_pass_gate_b(state)
    if not passed:
        message = escalation_reason(reasons, gate="Gate B (pre-publish)")
        return {
            "status": "blocked",
            "escalation": message,
            "decision_log": _append_log(
                state,
                _log_entry("gate_b", "gate_failed", message, reasons=reasons),
            ),
        }

    if state.get("publish_mode", "auto") == "review_first":
        from langgraph.types import interrupt

        decision = interrupt(
            {
                "gate": "B",
                "status": "awaiting_gate_b",
                "message": (
                    "Gate B checks passed: QC, Copyright and Policy agents "
                    "all returned PASS for this publish manifest. "
                    "Approve to continue to publishing."
                ),
                "publish_manifest_hash": state.get("publish_manifest_hash"),
            }
        )
        if not _is_approval(decision):
            message = escalation_reason(
                "The user did not approve publishing at Gate B.",
                gate="Gate B (pre-publish)",
            )
            return {
                "status": "blocked",
                "escalation": message,
                "decision_log": _append_log(
                    state, _log_entry("gate_b", "gate_rejected", message)
                ),
            }

    gate_status = parse_gate_status(state)
    gate_status.gate_b_passed = True
    return {
        "status": "running",
        "gate_status": dump_gate_status(gate_status),
        "decision_log": _append_log(
            state,
            _log_entry(
                "gate_b",
                "gate_passed",
                "Gate B passed: PASS tokens from agents 32, 33 and 34 "
                "verified against the publish manifest hash.",
            ),
        ),
    }


def _phase_6_publishing_node(state: VideoProjectState) -> dict[str, Any]:
    """Phase 6 stub with the Gate C check inside it.

    Publishing only proceeds when :func:`can_pass_gate_c` succeeds for
    Agent 26 (Upload).  Otherwise the project is blocked and escalated —
    nothing publishes in a failed or unverified state.
    """
    base = _phase_node("phase_6_publishing")(state)
    passed, reasons = can_pass_gate_c(state, requesting_agent_id=UPLOAD_AGENT_ID)
    if not passed:
        message = escalation_reason(reasons, gate="Gate C (publish)")
        base.update(
            {
                "status": "blocked",
                "escalation": message,
                "decision_log": [
                    *base["decision_log"],
                    _log_entry("phase_6_publishing", "gate_c_failed", message, reasons=reasons),
                ],
            }
        )
    return base


def _gate_c_node(state: VideoProjectState) -> dict[str, Any]:
    passed, reasons = can_pass_gate_c(state, requesting_agent_id=UPLOAD_AGENT_ID)
    if not passed:
        message = escalation_reason(reasons, gate="Gate C (publish)")
        return {
            "status": "blocked",
            "escalation": message,
            "decision_log": _append_log(
                state,
                _log_entry("gate_c", "gate_failed", message, reasons=reasons),
            ),
        }

    gate_status = parse_gate_status(state)
    gate_status.gate_c_published_by = UPLOAD_AGENT_ID
    return {
        "status": "running",
        "gate_status": dump_gate_status(gate_status),
        "decision_log": _append_log(
            state,
            _log_entry(
                "gate_c",
                "gate_passed",
                f"Gate C passed: agent {UPLOAD_AGENT_ID} (Upload) is "
                "authorised to publish this exact manifest.",
            ),
        ),
    }


def _done_node(state: VideoProjectState) -> dict[str, Any]:
    return {
        "status": "done",
        "escalation": None,
        "decision_log": _append_log(
            state,
            _log_entry("done", "project_done", "Video project completed all phases and gates."),
        ),
    }


# ---------------------------------------------------------------------------
# Routing (conditional edges out of the gates)
# ---------------------------------------------------------------------------


def _route_after_gate_a(state: VideoProjectState) -> str:
    return "phase_3_production" if parse_gate_status(state).gate_a_script_locked else END


def _route_after_gate_b(state: VideoProjectState) -> str:
    return "phase_6_publishing" if parse_gate_status(state).gate_b_passed else END


def _route_after_phase_6(state: VideoProjectState) -> str:
    if state.get("status") in {"blocked", "escalated"}:
        return END
    passed, _ = can_pass_gate_c(state, requesting_agent_id=UPLOAD_AGENT_ID)
    return "gate_c" if passed else END


def _route_after_gate_c(state: VideoProjectState) -> str:
    if parse_gate_status(state).gate_c_published_by == UPLOAD_AGENT_ID:
        return "phase_7_growth"
    return END


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_supervisor_graph(db_path: str | None = None, checkpointer: Any = None):
    """Build and compile the Supervisor graph.

    Args:
        db_path: Optional SQLite path for the LangGraph checkpointer
            (one thread per Video Project, resumed after crashes —
            roadmap section 08).  Ignored when ``checkpointer`` is given.
        checkpointer: A pre-built LangGraph checkpointer to use as-is.

    Returns:
        The compiled LangGraph graph.  Invoke it with
        ``config={"configurable": {"thread_id": project_id}}``.
    """
    if checkpointer is None:
        checkpointer = _make_checkpointer(db_path)

    builder = StateGraph(VideoProjectState)

    for node_name in (
        "phase_1_research",
        "phase_2_scripting",
        "phase_3_production",
        "phase_4_editing",
        "phase_5_packaging",
        "phase_7_growth",
    ):
        builder.add_node(node_name, _phase_node(node_name))
    builder.add_node("gate_a", _gate_a_node)
    builder.add_node("gate_b", _gate_b_node)
    builder.add_node("phase_6_publishing", _phase_6_publishing_node)
    builder.add_node("gate_c", _gate_c_node)
    builder.add_node("done", _done_node)

    builder.add_edge(START, "phase_1_research")
    builder.add_edge("phase_1_research", "phase_2_scripting")
    builder.add_edge("phase_2_scripting", "gate_a")
    builder.add_conditional_edges("gate_a", _route_after_gate_a, ["phase_3_production", END])
    builder.add_edge("phase_3_production", "phase_4_editing")
    builder.add_edge("phase_4_editing", "phase_5_packaging")
    builder.add_edge("phase_5_packaging", "gate_b")
    builder.add_conditional_edges("gate_b", _route_after_gate_b, ["phase_6_publishing", END])
    builder.add_conditional_edges("phase_6_publishing", _route_after_phase_6, ["gate_c", END])
    builder.add_conditional_edges("gate_c", _route_after_gate_c, ["phase_7_growth", END])
    builder.add_edge("phase_7_growth", "done")
    builder.add_edge("done", END)

    return builder.compile(checkpointer=checkpointer)
