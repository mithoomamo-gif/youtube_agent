---
name: policy
version: 0.1.0
agent: policy
agent_id: 34
task: policy
tier: T2
status: stub
---

# policy (STUB)

Job: Screen topics, scripts and metadata against YouTube policy and niche hard-rule packs (health, finance, religion, crime language); route violations back with specific fixes.
Inputs: Topic candidates + draft/final script + metadata pack + policy rule packs.
Outputs: Policy verdict per artefact (clear / fix-required with cited rule / blocked) for the Supervisor.
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
