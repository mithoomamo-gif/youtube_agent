"""Agent catalogue endpoints (roadmap section 31, MASTER_ROADMAP §5.2).

The 35 agents are logical role definitions on one orchestrator — this
router serves the static catalogue merged with (currently always idle)
live state.  Names and phases are taken verbatim from the §5.2 table.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.errors import ApiError

router = APIRouter(prefix="/api/v1", tags=["agents"])

# (phase, [agent names in order]) — MASTER_ROADMAP §5.2, agents 1..35.
_PHASES: list[tuple[str, list[str]]] = [
    (
        "research",
        [
            "Trend Research",
            "Competitor Analysis",
            "Niche Profiling",
            "Topic Ideation & Selection",
            "Keyword & Search Demand",
        ],
    ),
    (
        "scripting",
        [
            "Script Structure / Outline",
            "Script Writing",
            "Hook & Retention",
            "Fact-Check & Accuracy",
            "Script Editor & Localisation",
        ],
    ),
    (
        "production",
        [
            "Voice Direction",
            "TTS / Voice Generation",
            "Pronunciation & Prosody",
            "Scene Planning",
            "B-Roll / Stock Sourcing",
            "AI Image / Visual Generation",
            "Music & SFX",
        ],
    ),
    (
        "editing",
        [
            "Video Assembly",
            "Subtitle & Caption",
            "Audio Mixing & Mastering",
            "Retention Editing & Render",
        ],
    ),
    (
        "packaging",
        [
            "Thumbnail Design",
            "Thumbnail Text & CTR",
            "Title Generation & Testing",
            "Description, Tags & Metadata",
        ],
    ),
    (
        "publishing",
        [
            "Upload",
            "Scheduling, Playlist & Community",
        ],
    ),
    (
        "growth",
        [
            "Comment Engagement",
            "Analytics & Retention Analysis",
            "Next-Topic Feedback Loop",
            "Reporting",
        ],
    ),
    (
        "safety_qa",
        [
            "Quality Check",
            "Copyright & Licensing",
            "YouTube Policy & Monetisation Compliance",
            "Error Recovery & Watchdog",
        ],
    ),
]


def _catalogue() -> list[dict[str, Any]]:
    agents: list[dict[str, Any]] = []
    n = 0
    for phase, names in _PHASES:
        for name in names:
            n += 1
            agents.append(
                {
                    "id": f"agent_{n:02d}",
                    "phase": phase,
                    "name": name,
                    "status": "idle",
                }
            )
    return agents


AGENTS: list[dict[str, Any]] = _catalogue()
assert len(AGENTS) == 35, "agent catalogue must contain exactly 35 roles"


@router.get("/agents")
async def list_agents() -> dict:
    return {"items": [dict(a) for a in AGENTS], "next_cursor": None}


# Defined before /agents/{agent_id} so "runs" is not captured as an id.
@router.get("/agents/runs/{run_id}")
async def get_agent_run(run_id: str) -> dict:
    # Skeleton: run records live in the agent_runs table once the
    # orchestrator lands; every id is unknown until then.
    raise ApiError(
        404, "agent_run_not_found", f"No agent run with id '{run_id}'."
    )


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str) -> dict:
    for agent in AGENTS:
        if agent["id"] == agent_id:
            return {**agent, "current_run": None, "recent_runs": []}
    raise ApiError(404, "agent_not_found", f"No agent with id '{agent_id}'.")
