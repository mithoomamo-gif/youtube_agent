---
name: stock_sourcing
version: 0.1.0
agent: stock_sourcing
agent_id: 15
task: stock_sourcing
tier: T0
status: stub
---

# stock_sourcing (STUB)

Job: Source licensed stock footage/photos for planned scenes, preferring free/licensed libraries, and record licence provenance per asset.
Inputs: Scene plan (stock-type scenes) + niche visual constraints.
Outputs: Downloaded asset list with source, licence and scene mapping; gaps flagged for the AI-image agent.
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
