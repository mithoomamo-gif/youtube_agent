---
name: voice_direction
version: 0.1.0
agent: voice_direction
agent_id: 11
task: voice_direction
tier: T1
status: stub
---

# voice_direction (STUB)

Job: Direct the voice performance for a locked script: pace, emotion curve, emphasis and pause placement per chapter, following the niche profile's voice hints.
Inputs: Locked script + sidecar + niche/channel layers.
Outputs: Voice-direction map (per-chapter delivery notes + SSML-style pause/emphasis marks) for the TTS agent.
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
