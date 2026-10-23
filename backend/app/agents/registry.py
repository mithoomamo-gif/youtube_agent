"""The 35-agent registry — authoritative roster for the YouTube Agent app.

Source: roadmap section 03 (35-Agent Architecture) / MASTER_ROADMAP
Part 5. The Master Supervisor is Layer 0 and is NOT in this registry.

Phases:
    1 Research · 2 Scripting · 3 Production · 4 Editing ·
    5 Packaging · 6 Publishing · 7 Growth · 8 Safety/QA

Gates:
    * Gate A — Script Lock (after Agents 9 + 10): agents 6-10 list
      ``gates=["A"]``; Fact-Check (9) and Editor (10) produce the
      Gate A evidence.
    * Gate B — Pre-Publish: agents 22-25 list ``gates=["B"]`` (their
      outputs are checked at Gate B); QC (32), Copyright (33) and
      Policy (34) list ``gates=["B"]`` and are the only agents with
      ``produces_pass_token=True`` — all three must PASS.
    * Gate C — Publish: Upload (26) is the ONLY agent with
      ``gates=["C"]`` and the only agent allowed to publish.

Tiers are capability tiers only (T0 Utility / T1 Workhorse /
T2 Quality / T3 Flagship, roadmap section 23) — never model names.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.agents.base import ModelTier

__all__ = [
    "AGENT_REGISTRY",
    "AgentDefinition",
    "agents_by_phase",
    "get_agent",
    "validate_registry",
]

GateLetter = Literal["A", "B", "C"]
_VALID_GATES: frozenset[str] = frozenset({"A", "B", "C"})


class AgentDefinition(BaseModel):
    """Static definition of one agent (a logical role, not a process)."""

    agent_id: int
    slug: str
    name: str
    phase: int = Field(ge=1, le=8)
    phase_name: str
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    default_tier: ModelTier
    gates: list[str] = Field(default_factory=list)
    produces_pass_token: bool = False
    description: str

    @field_validator("gates")
    @classmethod
    def _gates_must_be_subset_of_abc(cls, value: list[str]) -> list[str]:
        invalid = [g for g in value if g not in _VALID_GATES]
        if invalid:
            raise ValueError(f"gates must be a subset of 'A', 'B', 'C'; got {invalid!r}")
        return value


# ---------------------------------------------------------------------------
# Registry — exactly 35 entries, ids 1..35
# ---------------------------------------------------------------------------

AGENT_REGISTRY: list[AgentDefinition] = [
    # -- Phase 1: Research --------------------------------------------------
    AgentDefinition(
        agent_id=1,
        slug="trend_research",
        name="Trend Research",
        phase=1,
        phase_name="Research",
        inputs=["niche_profile", "region", "language", "date_range"],
        outputs=["ranked_trend_list", "freshness_scores"],
        default_tier="T1",
        gates=[],
        produces_pass_token=False,
        description="Finds rising topics in the niche; outputs a ranked trend list with freshness evidence.",
    ),
    AgentDefinition(
        agent_id=2,
        slug="competitor_analysis",
        name="Competitor Analysis",
        phase=1,
        phase_name="Research",
        inputs=["niche_profile", "ranked_trend_list"],
        outputs=["competitor_report", "gap_list"],
        default_tier="T1",
        gates=[],
        produces_pass_token=False,
        description="Analyses top channels/videos — formats, hooks, length, cadence — and finds content gaps.",
    ),
    AgentDefinition(
        agent_id=3,
        slug="niche_profiling",
        name="Niche Profiling",
        phase=1,
        phase_name="Research",
        inputs=["niche_idea", "channel_config"],
        outputs=["niche_profile"],
        default_tier="T2",
        gates=[],
        produces_pass_token=False,
        description="Builds/updates the Niche Profile Object (tone, language, WPM, structure, voice, visual style), including custom Other niches.",
    ),
    AgentDefinition(
        agent_id=4,
        slug="topic_ideation",
        name="Topic Ideation & Selection",
        phase=1,
        phase_name="Research",
        inputs=["ranked_trend_list", "competitor_report", "niche_profile", "performance_history", "used_topic_history"],
        outputs=["selected_topic", "backup_topics", "topic_scores"],
        default_tier="T1",
        gates=[],
        produces_pass_token=False,
        description="Generates 10-20 scored topic candidates and selects 1 primary topic plus 2 backups.",
    ),
    AgentDefinition(
        agent_id=5,
        slug="keyword_research",
        name="Keyword & Search Demand",
        phase=1,
        phase_name="Research",
        inputs=["selected_topic", "niche_profile"],
        outputs=["keyword_pack", "title_phrases"],
        default_tier="T0",
        gates=[],
        produces_pass_token=False,
        description="Validates searchability; outputs the keyword pack (primary/secondary/long-tail), intent and title phrases.",
    ),
    # -- Phase 2: Scripting -------------------------------------------------
    AgentDefinition(
        agent_id=6,
        slug="outline",
        name="Outline",
        phase=2,
        phase_name="Scripting",
        inputs=["selected_topic", "keyword_pack", "niche_profile"],
        outputs=["outline"],
        default_tier="T1",
        gates=["A"],
        produces_pass_token=False,
        description="Builds the retention-shaped chapter outline with per-section word budgets. Participates in Gate A (Script Lock).",
    ),
    AgentDefinition(
        agent_id=7,
        slug="script_writing",
        name="Script Writing",
        phase=2,
        phase_name="Scripting",
        inputs=["outline", "niche_profile"],
        outputs=["script_draft"],
        default_tier="T2",
        gates=["A"],
        produces_pass_token=False,
        description="Writes the full narration-only script chapter by chapter (words = minutes x niche WPM). Default tier T2; the final pass escalates to T3. Participates in Gate A.",
    ),
    AgentDefinition(
        agent_id=8,
        slug="hook_retention",
        name="Hook & Retention",
        phase=2,
        phase_name="Scripting",
        inputs=["script_draft", "outline"],
        outputs=["hooked_script", "retention_map"],
        default_tier="T1",
        gates=["A"],
        produces_pass_token=False,
        description="Writes the first-30-seconds hook, open loops every 60-90s and climax teases; runs the retention-lint pass. Participates in Gate A.",
    ),
    AgentDefinition(
        agent_id=9,
        slug="fact_check",
        name="Fact-Check",
        phase=2,
        phase_name="Scripting",
        inputs=["script_draft"],
        outputs=["verified_script", "fact_log"],
        default_tier="T1",
        gates=["A"],
        produces_pass_token=False,
        description="Verifies claims, names, dates and religious/historical references; unverifiable claims are flagged or cut. Produces Gate A evidence.",
    ),
    AgentDefinition(
        agent_id=10,
        slug="editor",
        name="Editor / Localisation",
        phase=2,
        phase_name="Scripting",
        inputs=["verified_script", "niche_profile"],
        outputs=["locked_final_script", "voiceover_text"],
        default_tier="T1",
        gates=["A"],
        produces_pass_token=False,
        description="Polishes flow and language mix; outputs the versioned Locked Final Script plus clean voiceover text. Produces Gate A evidence.",
    ),
    # -- Phase 3: Production ------------------------------------------------
    AgentDefinition(
        agent_id=11,
        slug="voice_direction",
        name="Voice Direction",
        phase=3,
        phase_name="Production",
        inputs=["locked_final_script", "niche_profile"],
        outputs=["voice_brief"],
        default_tier="T1",
        gates=[],
        produces_pass_token=False,
        description="Sets voice ID, rate per niche WPM, emotion and the pause/emphasis map; outputs the Voice Brief.",
    ),
    AgentDefinition(
        agent_id=12,
        slug="tts",
        name="TTS Synthesis",
        phase=3,
        phase_name="Production",
        inputs=["voice_brief", "voiceover_text"],
        outputs=["narration_audio", "tts_log"],
        default_tier="T0",
        gates=[],
        produces_pass_token=False,
        description="Synthesises narration audio chunked by chapter, with measurements and a voice manifest.",
    ),
    AgentDefinition(
        agent_id=13,
        slug="pronunciation",
        name="Pronunciation & Prosody",
        phase=3,
        phase_name="Production",
        inputs=["narration_audio", "locked_final_script"],
        outputs=["final_narration", "pronunciation_dictionary"],
        default_tier="T0",
        gates=[],
        produces_pass_token=False,
        description="Fixes names and pacing; outputs final levelled narration, real timings and updates the shared pronunciation dictionary.",
    ),
    AgentDefinition(
        agent_id=14,
        slug="scene_planning",
        name="Scene Planning",
        phase=3,
        phase_name="Production",
        inputs=["final_narration", "locked_final_script"],
        outputs=["scene_list"],
        default_tier="T1",
        gates=[],
        produces_pass_token=False,
        description="Builds the timed scene list (scene number, lines, timestamps, visual type, on-screen text) from measured narration timings.",
    ),
    AgentDefinition(
        agent_id=15,
        slug="stock_sourcing",
        name="B-Roll / Stock Sourcing",
        phase=3,
        phase_name="Production",
        inputs=["scene_list"],
        outputs=["stock_assets", "licence_records"],
        default_tier="T0",
        gates=[],
        produces_pass_token=False,
        description="Sources licensed stock per scene and records a licence record for every asset.",
    ),
    AgentDefinition(
        agent_id=16,
        slug="ai_image",
        name="AI Image Generation",
        phase=3,
        phase_name="Production",
        inputs=["scene_list", "stock_assets"],
        outputs=["ai_visuals"],
        default_tier="T1",
        gates=[],
        produces_pass_token=False,
        description="Generates custom visuals where stock is missing, in a consistent 16:9 faceless-safe style.",
    ),
    AgentDefinition(
        agent_id=17,
        slug="music_sfx",
        name="Music & SFX",
        phase=3,
        phase_name="Production",
        inputs=["scene_list", "niche_profile"],
        outputs=["music_plan", "sfx_cues", "licence_records"],
        default_tier="T0",
        gates=[],
        produces_pass_token=False,
        description="Plans per-chapter music and SFX cues with a volume map and licence records.",
    ),
    # -- Phase 4: Editing ---------------------------------------------------
    AgentDefinition(
        agent_id=18,
        slug="assembly_editing",
        name="Assembly Editing",
        phase=4,
        phase_name="Editing",
        inputs=["final_narration", "stock_assets", "ai_visuals", "music_plan"],
        outputs=["rough_cut"],
        default_tier="T0",
        gates=[],
        produces_pass_token=False,
        description="Assembles narration, visuals and music into the rough cut.",
    ),
    AgentDefinition(
        agent_id=19,
        slug="subtitles",
        name="Subtitles",
        phase=4,
        phase_name="Editing",
        inputs=["final_narration", "locked_final_script"],
        outputs=["subtitle_file"],
        default_tier="T0",
        gates=[],
        produces_pass_token=False,
        description="Produces timed, styled subtitles/captions reconciled to the locked script.",
    ),
    AgentDefinition(
        agent_id=20,
        slug="audio_mixing",
        name="Audio Mixing & Mastering",
        phase=4,
        phase_name="Editing",
        inputs=["rough_cut", "final_narration", "music_plan"],
        outputs=["mixed_audio"],
        default_tier="T0",
        gates=[],
        produces_pass_token=False,
        description="Ducks music under narration and loudness-normalises the master audio with no clipping or silence.",
    ),
    AgentDefinition(
        agent_id=21,
        slug="retention_render",
        name="Retention Edit & Render",
        phase=4,
        phase_name="Editing",
        inputs=["rough_cut", "mixed_audio", "subtitle_file"],
        outputs=["final_master"],
        default_tier="T0",
        gates=[],
        produces_pass_token=False,
        description="Applies pacing, zooms, overlays and transitions and renders the Final Master (1080p+, correct codec/bitrate); verifies a visual change every 5-8s.",
    ),
    # -- Phase 5: Packaging -------------------------------------------------
    AgentDefinition(
        agent_id=22,
        slug="thumbnail_design",
        name="Thumbnail Design",
        phase=5,
        phase_name="Packaging",
        inputs=["selected_topic", "final_master"],
        outputs=["thumbnail_variants"],
        default_tier="T1",
        gates=["B"],
        produces_pass_token=False,
        description="Designs 3 thumbnail variants, mobile-legibility checked. Its outputs are checked at Gate B (Pre-Publish).",
    ),
    AgentDefinition(
        agent_id=23,
        slug="thumbnail_text_ctr",
        name="Thumbnail Text & CTR",
        phase=5,
        phase_name="Packaging",
        inputs=["thumbnail_variants", "selected_title"],
        outputs=["selected_thumbnail", "thumbnail_text"],
        default_tier="T1",
        gates=["B"],
        produces_pass_token=False,
        description="Writes 3-4 word thumbnail text complementing (never repeating) the title and picks the CTR variant, with an optional A/B pair. Checked at Gate B.",
    ),
    AgentDefinition(
        agent_id=24,
        slug="title",
        name="Title",
        phase=5,
        phase_name="Packaging",
        inputs=["keyword_pack", "selected_topic"],
        outputs=["title_candidates", "selected_title"],
        default_tier="T1",
        gates=["B"],
        produces_pass_token=False,
        description="Generates 5 scored titles (CTR + search), selects 1 and length/promise-checks it. Checked at Gate B.",
    ),
    AgentDefinition(
        agent_id=25,
        slug="metadata_seo",
        name="Description, Tags & Metadata",
        phase=5,
        phase_name="Packaging",
        inputs=["selected_title", "keyword_pack", "locked_final_script"],
        outputs=["metadata_pack"],
        default_tier="T1",
        gates=["B"],
        produces_pass_token=False,
        description="Builds description with chapters, tags, hashtags, pinned comment, end-screen/card suggestions, category and language. Checked at Gate B.",
    ),
    # -- Phase 6: Publishing ------------------------------------------------
    AgentDefinition(
        agent_id=26,
        slug="upload",
        name="Upload",
        phase=6,
        phase_name="Publishing",
        inputs=["final_master", "metadata_pack", "selected_thumbnail", "pass_tokens"],
        outputs=["video_id", "video_url", "publish_receipt"],
        default_tier="T0",
        gates=["C"],
        produces_pass_token=False,
        description="Uploads via the YouTube Data API with metadata. Gate C: the ONLY agent allowed to publish, and only with PASS tokens from Agents 32-34.",
    ),
    AgentDefinition(
        agent_id=27,
        slug="scheduling_community",
        name="Scheduling, Playlist & Community",
        phase=6,
        phase_name="Publishing",
        inputs=["video_id", "metadata_pack"],
        outputs=["schedule", "playlist_assignment", "community_post_pack"],
        default_tier="T0",
        gates=[],
        produces_pass_token=False,
        description="Handles best-time scheduling, playlist assignment, end screens and an optional community post.",
    ),
    # -- Phase 7: Growth ----------------------------------------------------
    AgentDefinition(
        agent_id=28,
        slug="comments",
        name="Comment Engagement",
        phase=7,
        phase_name="Growth",
        inputs=["video_id"],
        outputs=["comment_replies", "moderation_flags", "audience_requests"],
        default_tier="T1",
        gates=[],
        produces_pass_token=False,
        description="Replies in the channel voice, flags moderation issues and logs audience requests.",
    ),
    AgentDefinition(
        agent_id=29,
        slug="analytics",
        name="Analytics & Retention",
        phase=7,
        phase_name="Growth",
        inputs=["video_id"],
        outputs=["analytics_report", "retention_report", "hook_score"],
        default_tier="T1",
        gates=[],
        produces_pass_token=False,
        description="Produces the CTR/retention report mapped to script sections, including a hook score.",
    ),
    AgentDefinition(
        agent_id=30,
        slug="feedback_loop",
        name="Next-Topic Feedback Loop",
        phase=7,
        phase_name="Growth",
        inputs=["analytics_report", "retention_report"],
        outputs=["learning_rules", "topic_feedback"],
        default_tier="T1",
        gates=[],
        produces_pass_token=False,
        description="Converts analytics into bounded learning rules for Agents 1, 4 and 8 — winning/losing topics and hooks.",
    ),
    AgentDefinition(
        agent_id=31,
        slug="reporting",
        name="Reporting",
        phase=7,
        phase_name="Growth",
        inputs=["project_state", "analytics_report", "cost_ledger"],
        outputs=["user_report"],
        default_tier="T1",
        gates=[],
        produces_pass_token=False,
        description="Produces the daily/per-video user report: made, published, results, costs, errors and next planned.",
    ),
    # -- Phase 8: Safety / QA -----------------------------------------------
    AgentDefinition(
        agent_id=32,
        slug="qc",
        name="Quality Control",
        phase=8,
        phase_name="Safety/QA",
        inputs=["final_master", "subtitle_file"],
        outputs=["qc_report", "pass_token"],
        default_tier="T1",
        gates=["B"],
        produces_pass_token=True,
        description="Final video QC PASS/FAIL (black frames, freezes, silence/clipping, subtitle sync, resolution). Produces a Gate B PASS token; FAIL blocks upload.",
    ),
    AgentDefinition(
        agent_id=33,
        slug="copyright",
        name="Copyright",
        phase=8,
        phase_name="Safety/QA",
        inputs=["stock_assets", "ai_visuals", "music_plan", "licence_records"],
        outputs=["copyright_report", "pass_token"],
        default_tier="T1",
        gates=["B"],
        produces_pass_token=True,
        description="PASS/FAIL on all asset licences and Content-ID risk; sends replacement orders to Agents 15-17. Produces a Gate B PASS token.",
    ),
    AgentDefinition(
        agent_id=34,
        slug="policy",
        name="Policy & Monetisation",
        phase=8,
        phase_name="Safety/QA",
        inputs=["final_master", "metadata_pack", "locked_final_script"],
        outputs=["policy_report", "pass_token"],
        default_tier="T1",
        gates=["B"],
        produces_pass_token=True,
        description="PASS/FAIL on reused/low-effort risk, misleading metadata, sensitive topics, disclaimers and advertiser suitability. Produces a Gate B PASS token.",
    ),
    AgentDefinition(
        agent_id=35,
        slug="watchdog",
        name="Error Recovery / Watchdog",
        phase=8,
        phase_name="Safety/QA",
        inputs=["project_state", "event_log"],
        outputs=["recovery_actions", "escalations"],
        default_tier="T0",
        gates=[],
        produces_pass_token=False,
        description="Heartbeats, retries, fallback switches, checkpoint-resume and escalations; monitors the pipeline throughout.",
    ),
]


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

_BY_ID: dict[int, AgentDefinition] = {a.agent_id: a for a in AGENT_REGISTRY}
_BY_SLUG: dict[str, AgentDefinition] = {a.slug: a for a in AGENT_REGISTRY}


def get_agent(agent_id_or_slug: int | str) -> AgentDefinition:
    """Return the :class:`AgentDefinition` for an id (int) or slug (str).

    A digit string (e.g. ``"26"``) is treated as an id. Raises
    :class:`KeyError` when no agent matches.
    """
    if isinstance(agent_id_or_slug, int):
        try:
            return _BY_ID[agent_id_or_slug]
        except KeyError:
            raise KeyError(f"no agent with id {agent_id_or_slug!r}") from None
    key = str(agent_id_or_slug).strip()
    if key.isdigit():
        agent_id = int(key)
        try:
            return _BY_ID[agent_id]
        except KeyError:
            raise KeyError(f"no agent with id {agent_id!r}") from None
    try:
        return _BY_SLUG[key]
    except KeyError:
        raise KeyError(f"no agent with slug {key!r}") from None


def agents_by_phase(phase: int) -> list[AgentDefinition]:
    """All agents in ``phase`` (1..8), in registry (id) order."""
    return [a for a in AGENT_REGISTRY if a.phase == phase]


def validate_registry(
    registry: list[AgentDefinition] | None = None,
) -> list[str]:
    """Return a list of problems with ``registry`` (empty when valid).

    Assertion-free by design: callers (and tests) inspect the returned
    list. Checks count == 35, duplicate ids/slugs, ids exactly 1..35,
    phase range, gate letters, tier values, the Gate C rule (only
    Agent 26) and the Gate B PASS-token rule (Agents 32, 33, 34).
    """
    agents = AGENT_REGISTRY if registry is None else registry
    problems: list[str] = []

    if len(agents) != 35:
        problems.append(f"registry has {len(agents)} entries, expected 35")

    ids = [a.agent_id for a in agents]
    seen_ids: set[int] = set()
    for agent_id in ids:
        if agent_id in seen_ids:
            problems.append(f"duplicate agent_id: {agent_id}")
        seen_ids.add(agent_id)
    if set(ids) != set(range(1, 36)):
        problems.append(f"agent ids must be exactly 1..35, got {sorted(set(ids))!r}")

    slugs = [a.slug for a in agents]
    seen_slugs: set[str] = set()
    for slug in slugs:
        if slug in seen_slugs:
            problems.append(f"duplicate slug: {slug!r}")
        seen_slugs.add(slug)

    for agent in agents:
        if not (1 <= agent.phase <= 8):
            problems.append(f"agent {agent.agent_id} phase {agent.phase} outside 1..8")
        for gate in agent.gates:
            if gate not in _VALID_GATES:
                problems.append(f"agent {agent.agent_id} has invalid gate {gate!r}")
        if agent.default_tier not in ("T0", "T1", "T2", "T3"):
            problems.append(
                f"agent {agent.agent_id} has invalid default_tier {agent.default_tier!r}"
            )
        if not agent.slug:
            problems.append(f"agent {agent.agent_id} has an empty slug")
        if not agent.name:
            problems.append(f"agent {agent.agent_id} has an empty name")

    gate_c_ids = [a.agent_id for a in agents if "C" in a.gates]
    if gate_c_ids != [26]:
        problems.append(f"only agent 26 may hold Gate C, got {gate_c_ids!r}")

    pass_token_ids = sorted(a.agent_id for a in agents if a.produces_pass_token)
    if pass_token_ids != [32, 33, 34]:
        problems.append(
            f"only agents 32, 33, 34 may produce PASS tokens, got {pass_token_ids!r}"
        )

    return problems
