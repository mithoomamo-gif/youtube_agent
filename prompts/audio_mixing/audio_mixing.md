---
name: audio_mixing
version: 0.1.0
agent: audio_mixing
agent_id: 20
task: audio_mixing
tier: T0
status: stub
---

# audio_mixing (STUB)

Job: Mix and master the final audio: loudness normalisation to YouTube targets, music ducking under narration, silence-beat execution, de-essing/noise cleanup.
Inputs: Assembled render + cue sheet + channel audio targets.
Outputs: Mixed master audio track + loudness report (integrated LUFS, true peak).
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
