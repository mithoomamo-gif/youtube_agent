# MEDIA_PLACEHOLDER

This fixture is hermetic: it contains **no real media** and nothing is
downloaded. In a real (non-hermetic) run, this directory would hold the
generated/downloaded assets for this project:

- `voiceover.wav` — synthesised narration audio for the script
- `scenes/scene-XXX.<ext>` — one stock clip/image or AI-generated image per scene
- `final.mp4` — the rendered video (checked by the golden-video suite, Section 17 L5)
- `thumbnail.jpg` — the rendered thumbnail (1280x720 or larger)

Tests must treat this placeholder as the media stand-in, use fake providers,
and assert on manifests, ledgers and call counts — never on real media bytes.
