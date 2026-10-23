---
name: scheduling_community
version: 0.1.0
agent: scheduling_community
agent_id: 27
task: scheduling_community
tier: T0
status: stub
---

# scheduling_community (STUB)

Job: Schedule publication at the channel's best-performing times and prepare community-tab / pinned-comment posts that reinforce the video's open loops.
Inputs: Upload receipt + channel analytics (best times) + scheduling preferences.
Outputs: Publish schedule entry + community post drafts queued for approval/auto-post per mode.
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
