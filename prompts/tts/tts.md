---
name: tts
version: 0.1.0
agent: tts
agent_id: 12
task: tts
tier: T0
status: stub
---

# tts (STUB)

Job: Synthesise narration audio from the locked voiceover text using the selected voice/provider, chapter by chapter, with checkpoint resume.
Inputs: 04_voiceover.txt + voice-direction map + pronunciation entries.
Outputs: Per-chapter audio files + timing/word-alignment map for scene planning and subtitles.
Layers: assembled at run time via the 5-layer stack (core system + {{niche_profile}} + {{channel}} + {{topic_layer}} + {{run_params}}).
TODO: production prompt — replace this stub before this agent is enabled in a live pipeline.
