"""Base agent framework for the YouTube Agent app.

Framework-only: this module defines the shared contract every one of
the 35 agents (see ``registry.py``) is built on — context, results,
decision logging and budget checks.

It deliberately contains:

* NO provider SDK imports, and
* NO LLM calls.

Agents in this codebase never call a model directly. They request a
*capability tier* (``T0`` Utility / ``T1`` Workhorse / ``T2`` Quality /
``T3`` Flagship — see roadmap section 23, the LLM layer) through the
LLM layer owned by another team; the physical model behind a tier is
data (``config/models.yaml``), never code.

Master Supervisor (Layer 0) is *not* an agent and is not built on this
class for publishing authority — it owns the workflow and is the only
component that can authorise publishing (via Agent 26 / Gate C).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AgentContext",
    "AgentResult",
    "BaseAgent",
    "BudgetCheckResult",
    "DecisionLogEntry",
    "ModelTier",
]

# Agents request TIERS, never models (roadmap section 23).
#   T0 Utility   — routing/classification, dedup, triage (short, high volume)
#   T1 Workhorse — bulk drafting / summarisation where a quality pass follows
#   T2 Quality   — judgement tasks (hooks, fact-check, QC judging)
#   T3 Flagship  — final script pass only; spent where the viewer feels it
ModelTier = Literal["T0", "T1", "T2", "T3"]


class DecisionLogEntry(BaseModel):
    """One recorded decision by an agent.

    Decision logs are read by Reporting (Agent 31), the Watchdog
    (Agent 35) and the UI, so entries stay short and factual.
    """

    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    agent_id: int
    decision: str
    reason: str
    artefacts: list[str] = Field(default_factory=list)


class BudgetCheckResult(BaseModel):
    """Outcome of a pre-flight budget check (Budget Guard)."""

    allowed: bool
    reason: str
    remaining_usd: float | None = None


class AgentContext(BaseModel):
    """Per-project context handed to every agent for a run.

    ``budget_check`` is a callable injected at runtime by the
    Supervisor / Budget Guard, e.g.::

        budget_check(estimated_usd) -> BudgetCheckResult

    (a plain ``bool`` or a ``dict`` with ``allowed``/``reason``/
    ``remaining_usd`` keys are also accepted by
    :meth:`BaseAgent.check_budget`). It is excluded from
    serialisation because callables are runtime wiring, not state —
    checkpoints/state must never carry it (nor any credential).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    project_id: str
    channel_id: str
    niche_profile_id: str
    publish_mode: str
    budget_check: Any = Field(default=None, exclude=True)
    extras: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    """Typed result every agent returns to the Supervisor.

    ``summary`` is a plain, short string (<= ~300 tokens worth) — a
    one-paragraph account of what happened, not a dump of the output.
    Full output lives in the artefact store and is referenced by
    ``output_refs``.
    """

    agent_id: int
    status: Literal["ok", "blocked", "failed", "needs_escalation"]
    output_refs: list[str] = Field(default_factory=list)
    summary: str
    decision_log: list[DecisionLogEntry] = Field(default_factory=list)
    cost_usd: float = 0.0


class BaseAgent(ABC):
    """Base class for all 35 agents.

    Subclasses set the class attributes (mirroring their
    :class:`~app.agents.registry.AgentDefinition`) and implement
    :meth:`run`. Agents receive tasks only from the Master Supervisor,
    only when their dependencies are DONE in shared state; they never
    assign work directly to each other.
    """

    agent_id: int
    slug: str
    default_tier: ModelTier

    def __init__(self, context: AgentContext) -> None:
        self.context: AgentContext = context
        self.decision_log: list[DecisionLogEntry] = []

    # -- decision logging -------------------------------------------------

    def log_decision(
        self,
        decision: str,
        reason: str,
        artefacts: list[str] | None = None,
    ) -> DecisionLogEntry:
        """Record a decision and return the entry that was logged."""
        entry = DecisionLogEntry(
            agent_id=self.agent_id,
            decision=decision,
            reason=reason,
            artefacts=list(artefacts) if artefacts else [],
        )
        self.decision_log.append(entry)
        return entry

    # -- budget -----------------------------------------------------------

    def check_budget(self, estimated_usd: float) -> BudgetCheckResult:
        """Ask the injected Budget Guard whether ``estimated_usd`` may be spent.

        With no callable injected, the check allows the spend (local /
        test wiring) and says so explicitly — it never silently
        invents a remaining balance.
        """
        checker = self.context.budget_check
        if checker is None or not callable(checker):
            return BudgetCheckResult(
                allowed=True,
                reason="no budget guard configured; spend allowed",
                remaining_usd=None,
            )

        try:
            raw: Any = checker(estimated_usd)
        except TypeError:
            # Tolerate guards wired as (agent_id, estimated_usd).
            raw = checker(self.agent_id, estimated_usd)  # type: ignore[operator]

        if isinstance(raw, BudgetCheckResult):
            return raw
        if isinstance(raw, bool):
            return BudgetCheckResult(
                allowed=raw,
                reason="budget guard allowed" if raw else "budget guard denied",
                remaining_usd=None,
            )
        if isinstance(raw, dict):
            return BudgetCheckResult(
                allowed=bool(raw.get("allowed", False)),
                reason=str(raw.get("reason", "")),
                remaining_usd=raw.get("remaining_usd"),
            )
        # Unknown return type: fail closed rather than guess.
        return BudgetCheckResult(
            allowed=False,
            reason=f"unrecognised budget_check result type: {type(raw).__name__}",
            remaining_usd=None,
        )

    # -- checkpointing ------------------------------------------------------

    def on_checkpoint(self, state: dict) -> None:
        """Hook called when the Supervisor checkpoints this agent's work.

        Default is a no-op. Subclasses override it to persist resumable
        progress (e.g. TTS per chapter, resumable upload) so a restart
        resumes from the checkpoint — never from zero.
        """
        return None

    # -- helpers ------------------------------------------------------------

    def build_result(
        self,
        status: Literal["ok", "blocked", "failed", "needs_escalation"],
        summary: str,
        output_refs: list[str] | None = None,
        cost_usd: float = 0.0,
    ) -> AgentResult:
        """Build an :class:`AgentResult` carrying this agent's decision log."""
        return AgentResult(
            agent_id=self.agent_id,
            status=status,
            output_refs=list(output_refs) if output_refs else [],
            summary=summary,
            decision_log=list(self.decision_log),
            cost_usd=cost_usd,
        )

    # -- main entry point ---------------------------------------------------

    @abstractmethod
    def run(self, inputs: dict) -> AgentResult:
        """Run the agent on ``inputs`` (artefact refs / params) and report back.

        Implementations request an LLM tier via the LLM layer when they
        need a model — they never import a provider SDK or name a
        physical model.
        """
        raise NotImplementedError
