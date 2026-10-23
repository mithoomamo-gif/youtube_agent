---
name: analytics
version: 0.1.0
agent: analytics
agent_id: 29
task: analytics
tier: T1
status: stub
---

# analytics (STUB)

Job: Pull per-video Analytics API data (CTR, retention curve, VPH) and map retention drop-off timestamps back to script sections and hook/cadence choices.
Inputs: Published video IDs + Analytics API reports + locked script sidecar.
Outputs: Performance report per video (CTR, retention-by-section, band-vs-actual comparison at 7d/28d).
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
