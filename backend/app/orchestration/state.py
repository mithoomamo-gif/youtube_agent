"""Shared state for the Supervisor graph (Worker 4 — LangGraph orchestration).

Implements the Video Project Object from the master roadmap:

* Part 5 / section 03 (35 agents): the Supervisor owns one Video Project per
  video; agents never pass files to each other informally — everything flows
  through this shared, checkpointed state.
* Part 6 intro: a project runs Research -> Scripting -> Production -> Editing
  -> Packaging -> Publishing -> Growth, with hard Gates A/B/C in between.

``VideoProjectState`` is a ``TypedDict`` (``total=False``) so LangGraph can
merge partial node updates into it.  The two Pydantic models (``PassToken``
and ``GateStatus``) are the *serialised* gate bookkeeping: inside the graph
state, ``gate_status`` is stored as a plain ``dict`` produced by
``GateStatus.model_dump(mode="json")`` and re-validated with
``GateStatus.model_validate(...)`` whenever a gate needs to read it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

# Agents allowed to issue Gate B PASS tokens (Part 5.2, Phase 8):
# 32 = Quality Check, 33 = Copyright & Licensing,
# 34 = YouTube Policy & Monetisation Compliance.
GATE_B_AGENT_IDS: tuple[int, ...] = (32, 33, 34)

# The only agent allowed through Gate C (Part 5.3): 26 = Upload.
UPLOAD_AGENT_ID: int = 26


class PassToken(BaseModel):
    """A Gate B PASS token issued by one of the Phase 8 gate agents.

    Tokens are *bound to the frozen publish manifest*: a token only counts
    when its ``manifest_hash`` equals the project's
    ``publish_manifest_hash`` (roadmap Part 5.3 — any edit after freezing
    creates manifest v2 and invalidates earlier tokens).
    """

    gate: Literal["B"] = "B"
    agent_id: Literal[32, 33, 34]
    verdict: Literal["PASS"] = "PASS"
    manifest_hash: str
    issued_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class GateStatus(BaseModel):
    """Serialised gate bookkeeping stored on the Video Project.

    * Gate A — Script Lock: ``gate_a_script_locked`` plus the content hash
      of the locked script that downstream agents pin.
    * Gate B — Pre-Publish: PASS tokens from agents 32, 33 and 34 and the
      aggregate ``gate_b_passed`` flag (set only by the Supervisor graph
      after :func:`app.orchestration.gates.can_pass_gate_b` succeeds).
    * Gate C — Publish: ``gate_c_published_by`` records which agent was
      authorised to publish (must be 26, the Upload agent).
    """

    gate_a_script_locked: bool = False
    gate_a_script_hash: str | None = None
    gate_b_tokens: list[PassToken] = Field(default_factory=list)
    gate_b_passed: bool = False
    gate_c_published_by: int | None = None


PublishMode = Literal["review_first", "auto"]
ProjectStatus = Literal[
    "running",
    "awaiting_gate_a",
    "awaiting_gate_b",
    "awaiting_gate_c",
    "done",
    "blocked",
    "escalated",
]


class VideoProjectState(TypedDict, total=False):
    """LangGraph state for one Video Project (one video end-to-end)."""

    # Identity / job definition (Supervisor creates these up front).
    project_id: str
    channel_id: str
    niche_profile_id: str
    topic: str
    target_minutes: float
    publish_mode: PublishMode

    # Pipeline position (1..7; Phase 8 gates run throughout).
    phase: int

    # Phase 1 — Research.
    research_brief_ref: str

    # Phase 2 — Scripting.
    outline_ref: str
    script_versions: list[str]
    locked_script_ref: str
    locked_script_hash: str
    fact_ledger_ref: str
    red_claim_count: int
    retention_lint_passed: bool

    # Phase 3 — Production (voice + visuals).
    voice_ref: str
    scene_manifest_ref: str

    # Phase 4 — Editing.
    draft_ref: str
    master_ref: str

    # Phase 5 — Packaging / SEO.
    thumbnail_refs: list[str]
    metadata_ref: str

    # Phase 6 — Publishing: hash of the frozen publish manifest that
    # Gate B PASS tokens are bound to.
    publish_manifest_hash: str

    # Gates — serialised GateStatus (see GateStatus above).
    gate_status: dict[str, Any]

    # Reliability (roadmap section 08): per-step correction-round counters
    # (max MAX_CORRECTION_ROUNDS, then escalate) and the current
    # plain-language escalation message, if any.
    correction_rounds: dict[str, int]
    escalation: str | None

    status: ProjectStatus
    decision_log: list[dict[str, Any]]
