---
name: copyright
version: 0.1.0
agent: copyright
agent_id: 33
task: copyright
tier: T1
status: stub
---

# copyright (STUB)

Job: Check assets and script for copyright risk: stock/AI licence provenance, music rights, reused-content similarity signals and claim-dispute evidence from the provenance chain.
Inputs: Asset licence list + fact ledger/provenance chain + similarity check results.
Outputs: Copyright clearance report (per-asset licence status, risks, required replacements) gating upload.
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
