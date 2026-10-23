---
name: watchdog
version: 0.1.0
agent: watchdog
agent_id: 35
task: watchdog
tier: T0
status: stub
---

# watchdog (STUB)

Job: Monitor pipeline health: stuck jobs, quota burn vs caps, provider circuit-breaker state, retry storms and cost-cap proximity; alert the Supervisor.
Inputs: Job/run state + quota tracker + cost ledger + provider status.
Outputs: Health alerts and run-status digests (no content decisions — escalation only).
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
