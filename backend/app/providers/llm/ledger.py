"""Append-only LLM call ledger.

The provider layer is the ONLY writer. This module defines the protocol
plus an in-memory implementation; the SQLite implementation (system of
record) is owned by another team and must satisfy the same protocol.
Every attempt — success, failure, retry, fallback, refusal — is its own
row, so retry/fallback spend is visible per agent.

Logical-call identity (Round 4): rows carry ``logical_call_id`` — one id
per LOGICAL call, shared by every attempt row of that call (retries,
cross-provider fallbacks, schema-repair retries, tier escalations all
belong to the one logical call that spawned them). The router stamps it
(from ``LLMRequest.logical_call_id`` when the caller supplies a stable
step/chapter key, else a fresh id per ``generate()`` invocation).
``agent_loop_stats`` counts DISTINCT logical calls for the call cap, so
retries never consume cap budget (Round 3: the cap killed a production
at chapter 4 purely by counting retries); the COST cap still sums every
row, because retried attempts really did spend money.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class PriceSnapshot(BaseModel):
    registry_version: str
    input_price_per_1m: float
    output_price_per_1m: float
    cached_input_price_per_1m: float


class LLMCallEntry(BaseModel):
    call_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    # Logical-call identity (see module docstring): every attempt row of
    # one logical call shares this id. None on legacy/directly-appended
    # rows — agent_loop_stats falls back for those (see _logical_key).
    logical_call_id: str | None = None
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    agent_id: str = "unknown"
    project_id: str | None = None
    video_id: str | None = None
    channel_id: str | None = None
    task: str | None = None
    provider: str
    model: str
    tier: str
    # mutually exclusive token buckets
    input_uncached: int = 0
    input_cached: int = 0
    input_cache_write: int = 0
    output: int = 0
    reasoning: int = 0
    price_snapshot: PriceSnapshot
    cost_usd: float = 0.0
    prompt_version: str | None = None
    prompt_hash: str | None = None
    fell_back_from: str | None = None
    status: str = "ok"  # ok | cached | failed | refused | aborted_partial
    latency_ms: float = 0.0
    attempts: int = 1
    error_class: str | None = None
    degraded_from_t3: bool = False


@runtime_checkable
class LLMCallLedger(Protocol):
    """Append-only ledger interface (SQLite impl lives elsewhere)."""

    async def append(self, entry: LLMCallEntry) -> None: ...

    async def video_cost_usd(self, project_id: str) -> float: ...

    async def agent_loop_stats(
        self, agent_id: str, project_id: str | None
    ) -> tuple[int, float]:
        """(UNIQUE logical call count, total cost) for one agent/video.

        The count dedupes attempt rows by logical-call identity (see
        module docstring); the cost sums ALL rows, retries included.
        """
        ...


def _logical_key(entry: LLMCallEntry) -> str:
    """Dedup key for one ledger row's logical call.

    Primary: the stamped ``logical_call_id``. Fallback for rows written
    before Round 4 (or appended directly by other writers): attempts of
    one logical call share (agent, project, task, prompt_hash), so that
    tuple identifies the call; a row with no prompt_hash at all can only
    be identified by its own row id, i.e. it counts as its own call.
    """
    if entry.logical_call_id:
        return f"logical:{entry.logical_call_id}"
    if entry.prompt_hash:
        return (
            f"hash:{entry.agent_id}:{(entry.project_id or entry.video_id)}:"
            f"{entry.task}:{entry.prompt_hash}"
        )
    return f"row:{entry.call_id}"


class InMemoryLLMCallLedger:
    """In-memory ledger — tests, dry runs, and the router's cap sums."""

    def __init__(self) -> None:
        self.entries: list[LLMCallEntry] = []

    async def append(self, entry: LLMCallEntry) -> None:
        self.entries.append(entry)

    async def video_cost_usd(self, project_id: str) -> float:
        return sum(
            e.cost_usd
            for e in self.entries
            if (e.project_id or e.video_id) == project_id
        )

    async def agent_loop_stats(
        self, agent_id: str, project_id: str | None
    ) -> tuple[int, float]:
        rows = [
            e
            for e in self.entries
            if e.agent_id == agent_id
            and (project_id is None or (e.project_id or e.video_id) == project_id)
        ]
        unique_calls = len({_logical_key(e) for e in rows})
        return unique_calls, sum(e.cost_usd for e in rows)
