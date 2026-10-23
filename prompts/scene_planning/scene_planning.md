---
name: scene_planning
version: 0.1.0
agent: scene_planning
agent_id: 14
task: scene_planning
tier: T1
status: stub
---

# scene_planning (STUB)

Job: Break the locked script into timed scenes mapped to narration segments and interrupt markers, assigning each scene a visual type (stock, AI image, text card, zoom).
Inputs: Locked script sidecar (word offsets + interrupt markers) + visual style hint.
Outputs: Scene plan (ordered scenes with time ranges, visual type, search/generation brief per scene).
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
