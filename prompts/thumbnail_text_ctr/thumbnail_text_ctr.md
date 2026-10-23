---
name: thumbnail_text_ctr
version: 0.1.0
agent: thumbnail_text_ctr
agent_id: 23
task: thumbnail_text_ctr
tier: T2
status: stub
---

# thumbnail_text_ctr (STUB)

Job: Write and judge thumbnail text and pick the thumbnail/title pairing with the strongest predicted CTR without breaking title–hook congruence.
Inputs: Thumbnail variants + title shortlist + hook text + niche CTR history.
Outputs: Selected thumbnail + thumbnail text (≤ 4 words target) + CTR rationale and A/B runner-up.
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
