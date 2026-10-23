---
name: metadata_seo
version: 0.1.0
agent: metadata_seo
agent_id: 25
task: metadata_seo
tier: T1
status: stub
---

# metadata_seo (STUB)

Job: Write upload metadata: description (primary keyword in line 1), tags, chapters, pinned-comment draft and end-screen suggestions, per the keyword pack's metadata strategy.
Inputs: Locked script + keyword pack (search-first vs suggested-first) + channel layer.
Outputs: Metadata pack (title final, description, tags, timestamps, hashtags) ready for the Upload agent.
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
