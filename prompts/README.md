# Prompt Library — YouTube Agent

Prompts are **product assets, not code**. Every prompt in this directory is a
versioned Markdown file with YAML front-matter, resolved at run time by
`backend/app/agents/prompt_assembly.py`. **No prompt string is ever inlined in
agent code** — "which prompt is in production" is always one file read, never
archaeology (roadmap §23, *Prompt versioning*; design source: §32 Research
Workflow, §33 Scripting Workflow + Prompt Strategy).

Layout: `prompts/<agent_slug>/<task>.md` — one directory per agent (35 agents;
the Supervisor is an orchestrator, not an agent, and has no prompt here).

## Front-matter (required on every file)

```yaml
---
name: script_writing          # prompt name (matches the task by convention)
version: 1.0.0                # SemVer — see versioning rules below
agent: script_writing         # agent slug (directory name)
agent_id: 7                   # numeric id from the agent registry (1–35)
task: script_writing          # task name (file name without .md)
tier: T3                      # logical model tier: T0 utility · T1 workhorse · T2 quality · T3 flagship
status: production            # production | stub
output_schema: ScriptChapter  # optional: structured-output contract name
---
```

Agents address **tiers only**, never physical model IDs — the tier → model
mapping lives in the versioned model registry (`config/models.yaml`), so a
model swap or price change is a config update, not a code release (§23).

## Versioning rules (SemVer per prompt)

- **MAJOR** — output structure/schema or task-contract change. Requires
  agent-code review; cannot auto-apply mid-video (a running video finishes on
  the prompt versions snapshotted into its manifest at job creation).
- **MINOR** — tone, behaviour or rubric change.
- **PATCH** — typos and edge-case fixes; may ship in a registry update.

Lifecycle: `draft → staging → production`. A new version is promoted only
after passing the golden regression set (fixed inputs per task, including ≥ 2
Other/custom-niche cases and Urdu + Roman-Urdu cases) scored on schema
validity, a T2 judge rubric and cost delta — a quality drop or a > 20% cost
rise blocks promotion. Every LLM call logs `prompt_version` + content hash, so
any output is traceable to the exact prompt that produced it. Rollback is one
action (re-point the production label), and the registry auto-reverts a version
whose live judge scores or refusal rate degrade past threshold.
**Niche differences are data, never forked prompt copies**: one
`script_writing` prompt serves facts, Islamic stories, horror and any user
"Other" niche — the niche arrives as template variables (Layer 2 below).

## The 5-layer prompt stack (§33)

Every writing/research call assembles its prompt from five layers, **in this
order**. The template file itself is Layer 1; layers 2–5 are placeholders the
assembly step substitutes:

| # | Layer | Placeholder | Source |
|---|-------|-------------|--------|
| 1 | **Core System Layer** — fixed, versioned, never per-niche: role, hard rules (narration only, no fabricated statistics/quotes, …), output contract | the file body (may embed `{{core_system}}` for a shared core block) | this repo |
| 2 | **Niche Profile Layer** — language, tone, pace/WPM, sentence length, structure template, hook families, voice hints, sensitive-topic rules, CTA style, forbidden patterns | `{{niche_profile}}` | Niche Profile Object (Agent 3 / profile library) |
| 3 | **Channel Layer** — channel name/identity, audience age/region, series continuity, recurring sign-off, pronunciation-dictionary excerpts | `{{channel}}` | channel record |
| 4 | **Topic Layer** — topic, keyword pack, research brief, unique-angle statement, competitor gaps to exploit | `{{topic_layer}}` (alias `{{topic}}`) | locked Research Brief v1 |
| 5 | **Run Layer** — target minutes, computed target words, chapter word budgets, interrupt cadence, publish mode (review-first / auto) | `{{run_params}}` | job/run record |

A niche is **data, not code**: built-in niches and user-created Other niches run
through the identical pipeline because only Layer 2 changes.

### Assembly API (`backend/app/agents/prompt_assembly.py`)

```python
from app.agents.prompt_assembly import PromptLayers, load_prompt, assemble_prompt

meta, body = load_prompt("script_writing", "script_writing")
# meta == {"name": ..., "version": "1.0.0", "agent_id": 7, "tier": "T3", ...}

final = assemble_prompt(
    "script_writing", "script_writing",
    PromptLayers(
        core_system="…", niche_profile="…", channel="…",
        topic="…", run_params="…",
    ),
)
```

`load_prompt` parses front-matter with PyYAML when available and falls back to
a built-in minimal parser otherwise (no hard dependency). `assemble_prompt`
substitutes the placeholders in stack order and raises `FileNotFoundError` for
an unknown agent/task.

## Index

**Production prompts** (full bodies encoding the §32/§33 rules):

| agent_id | agent | task | tier | Encodes |
|---|---|---|---|---|
| 1 | trend_research | trend_research | T1 | Evidence-before-generation, seed expansion + autocomplete clustering, Trends momentum/kill rules, quota discipline (search = last resort) |
| 2 | competitor_analysis | competitor_analysis | T1 | Playlist-based sweep, median baselines, outlier ≥3×/≥10×, VPH (24h–30d), gap + contested lists, competitor firewall |
| 3 | niche_profiling | niche_profiling | T3 | The "Other" 5-step auto-build: parse → archetype inherit → per-field confidence inference → clamp/validate → provisional v1 |
| 4 | topic_ideation | topic_ideation | T1 | TOS formula + penalties, two-signal rule, auto-select thresholds, honest Expected Performance Band |
| 5 | keyword_research | keyword_research | T1 | Search budget discipline, Competition Index, winnability check, Keyword Pack contract |
| 6 | outline | outline | T1 | `target_words = minutes × WPM`, section weights, climax at 65–75%, mandatory original layer per chapter |
| 7 | script_writing | script_writing | T3 | Chapter-by-chapter generation, narration-only output + sidecar JSON contract, no fabricated statistics/quotes |
| 8 | hook_retention | hook_retention | T2 | Hook Bank H1–H8, 5 scored candidates, 30-second anatomy (0–5s Proof, no greeting ever), 6 retention-lint rules |
| 9 | fact_check | fact_check | T2 | Atomic claim extraction, tiered sources, two-source rule, GREEN/YELLOW/RED verdicts, Gate A block on RED |
| 10 | editor | editor | T2 | Gate A lock criteria, immutable versioning, language/script-consistency lint, `04_voiceover.txt` contract |

**Stub prompts** (`status: stub`, v0.1.0 — job/inputs/outputs + `TODO:
production prompt`; replace before the agent is enabled live): voice_direction
(11, T1), tts (12, T0), pronunciation (13, T0), scene_planning (14, T1),
stock_sourcing (15, T0), ai_image (16, T1), music_sfx (17, T0),
assembly_editing (18, T0), subtitles (19, T1), audio_mixing (20, T0),
retention_render (21, T0), thumbnail_design (22, T1), thumbnail_text_ctr
(23, T2), title (24, T2), metadata_seo (25, T1), upload (26, T0),
scheduling_community (27, T0), comments (28, T0), analytics (29, T1),
feedback_loop (30, T1), reporting (31, T1), qc (32, T2), copyright (33, T1),
policy (34, T2), watchdog (35, T0).
