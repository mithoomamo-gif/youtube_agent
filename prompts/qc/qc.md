---
name: qc
version: 0.1.0
agent: qc
agent_id: 32
task: qc
tier: T2
status: stub
---

# qc (STUB)

Job: Judge finished artefacts against rubrics (script, thumbnail, render, audio) as an independent quality gate before upload; block and route failures to the owning agent.
Inputs: Final render + locked script + thumbnails + audio report + rubric set.
Outputs: QC verdict (pass/block) with per-rubric scores, failing artefact IDs and reroute instructions.
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
