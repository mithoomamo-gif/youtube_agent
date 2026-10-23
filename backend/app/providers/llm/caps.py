"""Hard local spend caps enforced by the router.

The Budget Guard worker builds the full guard (daily caps, reservations,
warnings); these two local caps are enforced here, pre-call, from ledger
sums via an injectable checker callable.

Agent-loop CALL cap semantics (Round 4): the cap counts UNIQUE logical
calls, not ledger rows — ``ledger.agent_loop_stats`` dedupes attempt
rows by logical-call identity, so retries/fallbacks/repairs of one call
never consume cap budget (Round 3: a production died at chapter 4
because retries were counted as calls). Distinct logical calls still
hit the cap exactly as before. The agent-loop COST cap is unchanged:
it sums every attempt row, because retries really spend money.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from .errors import BudgetExceeded
from .ledger import LLMCallLedger
from .models import LLMRequest

PER_VIDEO_CAP_USD = 0.50
AGENT_LOOP_MAX_CALLS = 25
AGENT_LOOP_CAP_USD = 0.15

CapChecker = Callable[[LLMRequest, LLMCallLedger], Awaitable[None]]


async def default_cap_checker(
    req: LLMRequest,
    ledger: LLMCallLedger,
    *,
    per_video_cap_usd: float = PER_VIDEO_CAP_USD,
    agent_loop_max_calls: int = AGENT_LOOP_MAX_CALLS,
    agent_loop_cap_usd: float = AGENT_LOOP_CAP_USD,
) -> None:
    """Raise BudgetExceeded when a cap is already reached (ledger sums)."""
    project_id = req.effective_project_id
    if project_id is not None:
        spent = await ledger.video_cost_usd(project_id)
        if spent >= per_video_cap_usd:
            raise BudgetExceeded(
                f"per-video LLM cap reached: ${spent:.4f} >= "
                f"${per_video_cap_usd:.2f} for video {project_id}"
            )
    calls, agent_spent = await ledger.agent_loop_stats(req.agent_id, project_id)
    if calls >= agent_loop_max_calls:
        raise BudgetExceeded(
            f"agent-loop call cap reached: {calls} >= {agent_loop_max_calls} "
            f"calls by {req.agent_id} on {project_id}"
        )
    if agent_spent >= agent_loop_cap_usd:
        raise BudgetExceeded(
            f"agent-loop cost cap reached: ${agent_spent:.4f} >= "
            f"${agent_loop_cap_usd:.2f} for {req.agent_id} on {project_id}"
        )
