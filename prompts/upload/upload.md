---
name: upload
version: 0.1.0
agent: upload
agent_id: 26
task: upload
tier: T0
status: stub
---

# upload (STUB)

Job: Upload the final render with its metadata pack via the YouTube Data API (videos.insert bucket), set visibility per publish mode, and record the video ID.
Inputs: Final render + metadata pack + publish mode from run parameters.
Outputs: Upload receipt (video ID, URL, visibility, quota spent) written to the project record.
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
