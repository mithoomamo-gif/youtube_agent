# Round 2 — E2E Performance Record (Acceptance Test E)

Measured 2026-10-22 (VM clock, UTC) by Worker 4 from the artefacts the
E2E integration team (`backend/app/integration/`) produced in
`outputs/round2/`. Validation test:
`tests/integration/test_e2e_outputs.py` — **8 passed**.

## Videos produced

| Niche | Project | Final MP4 | Duration | Size | Video | Audio |
|---|---|---|---|---|---|---|
| facts | `facts_ocean/samundar-ki-gehrai-ke-raaz-1952544f` | `pack/final.mp4` (copy at `facts_ocean/final.mp4`) | 41.933 s | 3,171,172 B | H.264 High, 1280×720, 30 fps | AAC LC, 48 kHz stereo |
| cricket (Other) | `other_cricket/1992-world-cup-final-ki-kahani-f6d05d45` | `pack/final.mp4` (copy at `other_cricket/final.mp4`) | 56.900 s | 4,619,447 B | H.264 High, 1280×720, 30 fps | AAC LC, 48 kHz stereo |

ffprobe facts above were measured directly by Worker 4
(`ffprobe -show_format -show_streams -of json`, FFmpeg 8.1.2) and agree
with each project's `pack/publish_manifest.json` / `pack/report.md`.

## Total wall time (recorded by the producing team)

| Video | Total elapsed | Source |
|---|---|---|
| facts (`…-1952544f`) | **104.583 s** | `Elapsed` field, `facts_ocean/…/pack/report.md` (team's `ProductionResult.elapsed_s`) |
| cricket (`…-f6d05d45`) | **136.556 s** | `Elapsed` field, `other_cricket/…/pack/report.md` |

Both totals reconcile with filesystem mtimes (project-dir creation →
`report.md` write): facts ≈ 104.6 s, cricket ≈ 136.5 s.

## Per-stage timings

**Not recorded by the producing team.** `ProductionResult`, the pack
`report.md` and `publish_manifest.json` carry only the single total
`elapsed_s`; no per-stage timing field exists in any artefact the team
wrote (no `stage_seconds` or equivalent in the output tree). The table
below is therefore **derived by Worker 4 from file mtimes** — each span
is bounded by the first/last artefact a stage wrote on disk. Treat
these as close estimates (± a few seconds of inter-stage gap), not as
instrumented measurements.

### facts — `samundar-ki-gehrai-ke-raaz-1952544f` (total 104.583 s)

| Stage | Span (s) | mtime evidence (2026-10-22 UTC) |
|---|---|---|
| Startup + content (research/script/Gate A — in-memory, writes no files) + TTS start | ~5.3 | project dir created 20:52:36.25 → first TTS segment `audio/.segments/ch01_seg00.mp3` 20:52:41.60 |
| Voice (edge-tts, 4 chapters) + music bed + mix | ~9.6 | first segment 20:52:41.60 → `audio/mixed_master.wav` 20:52:51.23 |
| Visuals (local plates, 11 scenes) | ~4.7 | `mixed_master.wav` 20:52:51.23 → `visuals/scene_manifest.json` 20:52:55.93 |
| Assemble / render (per-scene FFmpeg segments + concat + thumbnails, in `pack/_work/`) | ~79.3 | first segment graph 20:52:56.46 → `pack/final.mp4` 20:54:15.80 |
| QC + publish-pack freeze (manifest, ledger, report) | ~5.0 | `final.mp4` 20:54:15.80 → `pack/report.md` 20:54:20.83 |

### cricket (Other) — `1992-world-cup-final-ki-kahani-f6d05d45` (total 136.556 s)

| Stage | Span (s) | mtime evidence (2026-10-22 UTC) |
|---|---|---|
| Startup + content + TTS start | ~4.1 | project dir created 20:59:59.70 → first TTS segment 21:00:03.76 |
| Voice (edge-tts, 5 chapters) + music bed + mix | ~18.7 | first segment 21:00:03.76 → `audio/mixed_master.wav` 21:00:22.47 |
| Visuals (local plates) | ~5.4 | `mixed_master.wav` 21:00:22.47 → `visuals/scene_manifest.json` 21:00:27.88 |
| Assemble / render (`pack/_work/`) | ~101.3 | first segment graph 21:00:28.50 → `pack/final.mp4` 21:02:09.81 |
| QC + publish-pack freeze | ~6.4 | `final.mp4` 21:02:09.81 → `pack/report.md` 21:02:16.23 |

**Takeaway:** rendering dominates (~76 % of both runs); voice is the
second-largest stage; content/visuals/pack together are < 15 s.

## Notes from the producing team's own reports (not Worker 4 verdicts)

- facts run: Gate A PASS, Gate B PASS, QC PASS, publish state
  `fake_provider_upload_simulated`, loudness −14.05 LUFS.
- cricket run (final, `…-f6d05d45`): Gate A PASS, Gate B PASS, QC PASS —
  loudness PASS (true peak −1.08 dBTP at −14.05 LUFS integrated);
  publish state `fake_provider_upload_simulated`.
- Run history: the team iterated before settling — two earlier facts
  attempts (`…-e25c6f42`, stalled after visuals; `…-d65d30e8`, completed
  in 177.342 s with Gate B FAIL) were deleted by the team and replaced
  by `…-1952544f` above. The first cricket attempt (`…-c846190b`,
  135.907 s, 57.367 s, 4,313,860 B) FAILED its own Gate B / QC
  (loudness BLOCK `TRUE_PEAK_TOO_HIGH`, true peak −0.97 dBTP) and was
  likewise deleted and replaced by `…-f6d05d45` above. Only the
  surviving runs are recorded in the tables.
- Paths used (both runs, per `report.md`): LLM = deterministic-stub,
  TTS = edge-tts (real), visuals = local-plates, subtitles =
  alignment-stub, publish = fake-provider. No real upload was attempted.
