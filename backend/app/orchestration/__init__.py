"""LangGraph orchestration for the YouTube Agent (Worker 4).

The Supervisor graph, the shared Video Project state, the deterministic
Gates A/B/C and the retry/correction/escalation policies.

Canonical imports::

    from app.orchestration import (
        build_supervisor_graph,
        VideoProjectState,
        GateStatus,
        PassToken,
    )
"""

from __future__ import annotations

from .gates import (
    can_pass_gate_a,
    can_pass_gate_b,
    can_pass_gate_c,
    issue_pass_token,
)
from .graph import PHASE_NODE_AGENTS, build_supervisor_graph
from .policies import (
    MAX_CORRECTION_ROUNDS,
    RetryPolicy,
    escalation_reason,
    next_correction_action,
)
from .state import GateStatus, PassToken, VideoProjectState

__all__ = [
    "build_supervisor_graph",
    "VideoProjectState",
    "GateStatus",
    "PassToken",
    "can_pass_gate_a",
    "can_pass_gate_b",
    "can_pass_gate_c",
    "issue_pass_token",
    "MAX_CORRECTION_ROUNDS",
    "RetryPolicy",
    "next_correction_action",
    "escalation_reason",
    "PHASE_NODE_AGENTS",
]
