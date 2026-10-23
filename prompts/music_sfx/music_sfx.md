---
name: music_sfx
version: 0.1.0
agent: music_sfx
agent_id: 17
task: music_sfx
tier: T0
status: stub
---

# music_sfx (STUB)

Job: Select background music and sound effects per chapter mood, respecting licence rules and the script's music-shift interrupt markers.
Inputs: Locked script sidecar (music-shift markers) + niche tone + licensed library catalogue.
Outputs: Music/SFX cue sheet (asset, licence, in/out points, ducking notes) for assembly and mixing.
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
