"""Internal error taxonomy for the LLM layer.

Every provider dialect (429/5xx/400, safety blocks, timeouts) is mapped to
exactly one of these classes at the adapter boundary; agents and the router
only ever see these.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .models import UsageBuckets


class LLMError(Exception):
    """Base class for all LLM-layer errors."""


class InputTooLargeError(LLMError):
    """A single call would exceed the hard 100K input-token ceiling."""


class BudgetExceeded(LLMError):
    """A hard local spend cap (per-video or per-agent-loop) was hit."""


class PolicyRefusal(LLMError):
    """The provider refused on content/policy grounds. Not an outage."""

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class ProviderConfigError(LLMError):
    """400/401/403 — bad request or bad key. NEVER retried, NEVER falls back."""

    def __init__(
        self, message: str, *, status_code: int, provider: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.provider = provider


class RateLimitError(LLMError):
    """429 — honour Retry-After, back off, fall back after 2 attempts."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.retry_after_s = retry_after_s


class TransientProviderError(LLMError):
    """5xx / connection failure / per-attempt timeout. Retryable, breaker-fed."""

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class ModelRegistryError(LLMError):
    """404 / unknown model — the registry must be re-pointed, code is fine."""


class SchemaValidationError(LLMError):
    """Structured output failed Pydantic validation after the repair retry."""


class CircuitOpenError(LLMError):
    """Every model in the tier chain is circuit-broken right now."""


class AbortedPartialError(LLMError):
    """Generation started streaming, then failed.

    Per the no-duplicate-spend rule this is NEVER silently restarted on
    another provider: the partial cost is ledgered as ``aborted_partial``
    and the error surfaces to the caller.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        usage: "UsageBuckets | None" = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.usage = usage
