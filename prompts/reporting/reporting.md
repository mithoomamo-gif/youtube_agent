---
name: reporting
version: 0.1.0
agent: reporting
agent_id: 31
task: reporting
tier: T1
status: stub
---

# reporting (STUB)

Job: Compile the human-readable run/period report: videos produced, costs per agent/video, quota use, performance vs expected bands, and calibration error per niche.
Inputs: Project records + LLM cost ledger + analytics reports.
Outputs: Daily/weekly report document with cost, quota and performance summaries.
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
