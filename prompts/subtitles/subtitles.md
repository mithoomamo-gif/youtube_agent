---
name: subtitles
version: 0.1.0
agent: subtitles
agent_id: 19
task: subtitles
tier: T1
status: stub
---

# subtitles (STUB)

Job: Produce accurate, well-timed subtitles in the profile language (and translations where configured) from the narration alignment map.
Inputs: Narration audio + word-alignment map + locked script text.
Outputs: Subtitle files (SRT/VTT) per language + a timing/accuracy QC note.
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
