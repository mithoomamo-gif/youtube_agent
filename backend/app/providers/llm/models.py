"""Core data models for the LLM layer: request/result, usage, secrets.

Token buckets on :class:`UsageBuckets` are MUTUALLY EXCLUSIVE. Providers
report them differently — OpenAI counts cached tokens *inclusively* inside
``input_tokens``, Anthropic *exclusively* — adapters normalise at ingestion
so ``input_uncached + input_cached`` is always the true input total and
``output + reasoning`` the true output total. Reasoning tokens are priced
at the output rate.
"""

from __future__ import annotations

import hashlib
import math
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Hard rule (Part 10 / Section 23): no single call may exceed 100K input
# tokens — keeps every call on the cheapest price band of every provider.
MAX_INPUT_TOKENS = 100_000

CACHE_MIN_REUSE = 3  # provider prompt caching only pays off at >=3 reuses


class Tier(str, Enum):
    T0 = "T0"
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


@runtime_checkable
class SecretProvider(Protocol):
    """The ONLY way provider keys reach adapters.

    Implementations are backed by the OS credential store / Secure Vault.
    Adapters receive a SecretProvider plus a ``key_ref`` string; they never
    read env vars or files for keys and never log key values.
    """

    async def get_secret(self, ref: str) -> str | None: ...


class Message(BaseModel):
    role: str = "user"  # "system" | "user" | "assistant"
    content: str


class CachePrefix(BaseModel):
    """A stable prompt prefix (system block + niche profile + channel rules).

    Provider prompt caching is engaged only when ``reuse_count >= 3``.
    """

    id: str
    text: str | None = None
    reuse_count: int = 0

    @property
    def should_cache(self) -> bool:
        return self.reuse_count >= CACHE_MIN_REUSE and bool(self.text or self.id)


class LLMRequest(BaseModel):
    """What agents send. Agents address TIERS only — never model IDs."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tier: Tier
    prompt: str | None = None
    messages: list[Message] | None = None
    output_schema: type[BaseModel] | None = None
    max_output_tokens: int = 1024
    temperature: float = 0.7
    # metadata / attribution (every call belongs to an agent + a video)
    agent_id: str = "unknown"
    project_id: str | None = None  # == video_id for video work
    video_id: str | None = None
    channel_id: str | None = None
    task: str | None = None  # e.g. "script.final_pass"
    prompt_name: str | None = None
    prompt_version: str | None = None
    # reliability / caching hints
    timeout_s: float | None = None  # per-attempt budget; None = tier default
    cache_prefix: CachePrefix | None = None
    cacheable: bool = False  # deterministic T0 task -> local exact-result cache
    input_token_estimate: int | None = None  # caller-supplied, else estimated
    allow_degrade_from_t3: bool = False  # final-pass T3->T2 degrade exception
    # explicit model demand (Round 4 hardening): normally None — agents
    # address tiers only. When a caller DOES name a physical model, the
    # router validates it against BOTH the model registry (models.yaml)
    # and the price catalog (prices.yaml) and refuses with
    # ProviderConfigError BEFORE any provider call when the model is
    # absent from either — no silent tier substitution, no analogy
    # pricing (Round 3: unregistered 'gemini-3.6-flash' ran in prod).
    model_id: str | None = None
    model_provider: str | None = None  # disambiguates model_id if needed
    # logical-call identity for the agent-loop cap (Round 4): retries of
    # one logical call (same step/chapter) must not consume cap budget.
    # Callers that retry a whole generate() at the orchestration level
    # pass a stable id here (e.g. "<project>:script.chapter:4"); when
    # omitted, the router assigns one id per generate() invocation, so
    # in-call retries/fallbacks/repairs still count as ONE call. See
    # ledger.agent_loop_stats.
    logical_call_id: str | None = None

    @model_validator(mode="after")
    def _need_content(self) -> "LLMRequest":
        if not self.prompt and not self.messages:
            raise ValueError("LLMRequest needs `prompt` or `messages`")
        return self

    @property
    def effective_project_id(self) -> str | None:
        return self.project_id or self.video_id

    @property
    def is_script_class(self) -> bool:
        return bool(self.task and self.task.startswith("script"))

    def normalised_messages(self) -> list[Message]:
        msgs = list(self.messages or [])
        if self.prompt:
            msgs.append(Message(role="user", content=self.prompt))
        return msgs

    def system_text(self) -> str | None:
        parts = [m.content for m in self.normalised_messages() if m.role == "system"]
        if self.cache_prefix and self.cache_prefix.text:
            parts.insert(0, self.cache_prefix.text)
        return "\n\n".join(parts) if parts else None

    def estimated_input_tokens(self) -> int:
        if self.input_token_estimate is not None:
            return self.input_token_estimate
        chars = sum(len(m.content) for m in self.normalised_messages())
        if self.cache_prefix and self.cache_prefix.text:
            chars += len(self.cache_prefix.text)
        return max(1, math.ceil(chars / 4))  # ~4 chars/token planning figure

    def prompt_hash(self) -> str:
        body = "\n".join(
            f"{m.role}:{m.content}" for m in self.normalised_messages()
        )
        return hashlib.sha256(
            f"{self.prompt_version or ''}|{body}".encode()
        ).hexdigest()


class UsageBuckets(BaseModel):
    """Mutually exclusive token buckets (see module docstring)."""

    input_uncached: int = 0
    input_cached: int = 0
    input_cache_write: int = 0
    output: int = 0
    reasoning: int = 0

    @property
    def total_input(self) -> int:
        return self.input_uncached + self.input_cached + self.input_cache_write

    @property
    def total_output(self) -> int:
        return self.output + self.reasoning


class LLMResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    text: str = ""
    parsed: Any = None  # validated BaseModel instance when a schema was given
    provider: str
    model: str  # physical model actually used, after any fallback
    tier: Tier
    usage: UsageBuckets = Field(default_factory=UsageBuckets)
    cost_usd: float = 0.0
    fell_back_from: str | None = None  # "provider/model" first attempted
    status: str = "ok"  # ok | cached | failed | refused | aborted_partial
    attempts: int = 1
    latency_ms: float = 0.0
    degraded_from_t3: bool = False  # manifest flag for the T3->T2 exception
    prompt_version: str | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """The ONLY surface agents see."""

    async def generate(self, req: LLMRequest) -> LLMResult: ...
