---
name: ai_image
version: 0.1.0
agent: ai_image
agent_id: 16
task: ai_image
tier: T1
status: stub
---

# ai_image (STUB)

Job: Generate AI images for scenes stock cannot cover, in the channel's visual style, with prompts logged for reproducibility.
Inputs: Scene plan (AI-image scenes) + visual style hint + thumbnail style references.
Outputs: Generated image assets mapped to scene IDs + the generation prompt log.
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
