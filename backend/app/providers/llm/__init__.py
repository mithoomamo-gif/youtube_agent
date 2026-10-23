"""Unified LLM provider layer: tier routing, adapters, ledger, caps.

Agents import from here and address TIERS only — never model IDs, never
provider SDKs. Keys reach adapters only via an injected SecretProvider.
"""

from .cache import LocalResultCache
from .caps import CapChecker, default_cap_checker
from .errors import (
    AbortedPartialError,
    BudgetExceeded,
    CircuitOpenError,
    InputTooLargeError,
    LLMError,
    ModelRegistryError,
    PolicyRefusal,
    ProviderConfigError,
    RateLimitError,
    SchemaValidationError,
    TransientProviderError,
)
from .ledger import (
    InMemoryLLMCallLedger,
    LLMCallEntry,
    LLMCallLedger,
    PriceSnapshot,
)
from .model_validation import (
    audit_registry_against_catalog,
    catalog_has_llm_price,
    ensure_model_registered_and_priced,
)
from .models import (
    MAX_INPUT_TOKENS,
    CachePrefix,
    LLMProvider,
    LLMRequest,
    LLMResult,
    Message,
    SecretProvider,
    Tier,
    UsageBuckets,
)
from .pacing import (
    PacingConfig,
    ProviderPacing,
    QueueTimeoutError,
    SingleFlightPacer,
    get_default_pacer,
)
from .registry import ModelEntry, ModelRegistry, RegistryHandle, TierConfig
from .router import LLMRouter
from .anthropic import AnthropicAdapter
from .gemini import GeminiAdapter
from .openai import OpenAIAdapter

__all__ = [
    "MAX_INPUT_TOKENS",
    "AbortedPartialError",
    "AnthropicAdapter",
    "BudgetExceeded",
    "CachePrefix",
    "CapChecker",
    "CircuitOpenError",
    "GeminiAdapter",
    "InMemoryLLMCallLedger",
    "InputTooLargeError",
    "LLMCallEntry",
    "LLMCallLedger",
    "LLMError",
    "LLMProvider",
    "LLMRequest",
    "LLMResult",
    "LLMRouter",
    "LocalResultCache",
    "Message",
    "ModelEntry",
    "ModelRegistry",
    "ModelRegistryError",
    "OpenAIAdapter",
    "PacingConfig",
    "PolicyRefusal",
    "PriceSnapshot",
    "ProviderConfigError",
    "ProviderPacing",
    "QueueTimeoutError",
    "RateLimitError",
    "RegistryHandle",
    "SchemaValidationError",
    "SecretProvider",
    "SingleFlightPacer",
    "Tier",
    "TierConfig",
    "TransientProviderError",
    "UsageBuckets",
    "audit_registry_against_catalog",
    "catalog_has_llm_price",
    "default_cap_checker",
    "ensure_model_registered_and_priced",
    "get_default_pacer",
]
