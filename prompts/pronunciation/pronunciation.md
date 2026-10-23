---
name: pronunciation
version: 0.1.0
agent: pronunciation
agent_id: 13
task: pronunciation
tier: T0
status: stub
---

# pronunciation (STUB)

Job: Maintain and apply the channel pronunciation dictionary: recurring names, religious/technical terms and place names, spelled and phonetically annotated consistently.
Inputs: Locked script + channel layer (dictionary excerpts) + niche profile terms.
Outputs: Pronunciation dictionary updates + per-script respelling/phoneme annotations for TTS.
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
