"""Agent framework package: base class + the 35-agent registry."""

from app.agents.base import (
    AgentContext,
    AgentResult,
    BaseAgent,
    BudgetCheckResult,
    DecisionLogEntry,
    ModelTier,
)
from app.agents.registry import (
    AGENT_REGISTRY,
    AgentDefinition,
    agents_by_phase,
    get_agent,
    validate_registry,
)

__all__ = [
    "AGENT_REGISTRY",
    "AgentContext",
    "AgentDefinition",
    "AgentResult",
    "BaseAgent",
    "BudgetCheckResult",
    "DecisionLogEntry",
    "ModelTier",
    "agents_by_phase",
    "get_agent",
    "validate_registry",
]
