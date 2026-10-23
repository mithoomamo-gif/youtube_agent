---
name: assembly_editing
version: 0.1.0
agent: assembly_editing
agent_id: 18
task: assembly_editing
tier: T0
status: stub
---

# assembly_editing (STUB)

Job: Assemble scenes, narration, music and SFX into the edit timeline (FFmpeg/render engine), executing every production interrupt marker from the sidecar.
Inputs: Scene plan + assets + narration audio + cue sheet + interrupt marker map.
Outputs: Assembled draft render + edit decision list (EDL) for the retention-render pass.
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
