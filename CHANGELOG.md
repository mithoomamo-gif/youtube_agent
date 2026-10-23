# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Release preflight checks (`packaging/preflight.ps1` and
  `packaging/preflight.sh`): version consistency, icon set, updater
  configuration, migration-chain, changelog, and NSIS configuration
  checks that must pass before a release is built.

### Known release blockers (not yet resolved)

- The Tauri icon set has not been generated yet: `src-tauri/icons/`
  contains only `README.txt`, so `icons/icon.png` and `icons/icon.ico`
  referenced by `bundle.icon` in `src-tauri/tauri.conf.json` are missing.
- The Tauri updater is not configured for a real release yet:
  `plugins.updater.pubkey` in `src-tauri/tauri.conf.json` is empty and
  the updater endpoint is still the placeholder
  `https://example.invalid/releases/latest.json`. No signing key has
  been generated or supplied, so no key value is recorded here.

## [0.1.0] — pending (not yet released)

First release of the YouTube Agent Windows desktop app (Tauri 2 shell,
React/TypeScript UI, Python FastAPI sidecar). This version has **not**
been released yet; no release date is claimed. The entries below are
seeded from the project's git history (build rounds 1–5 and the Style
Lab mini-round, October 2026) and describe what the codebase contains
heading into the first release.

### Added

- Tauri 2 + React/TypeScript desktop shell and UI (10 screens), with the
  Rust shell acting only as a supervisor for the Python sidecar
  (Round 1 / Phase 0–1).
- Python FastAPI sidecar backend: SQLite schema, job queue, WebSocket
  hub, launch-token security, and REST routers (Round 1).
- Multi-agent production system: agent registry, LangGraph supervisor,
  approval gates (Gates A/B/C), 8 built-in niches (facts, Islamic
  stories, horror, history, motivation, tech, crime, health) plus an
  "Other" custom-niche builder (Round 1).
- Provider layer: LLM tier routing, TTS with chunking, Pixabay-first
  stock media, image generation allow-list, YouTube OAuth (PKCE) groundwork,
  and a budget guard (Round 1).
- Production pipeline: FFmpeg segment rendering, loudness normalisation
  (−14 LUFS), subtitles, Pillow thumbnails, QC checks, and a publishing
  state machine (Round 1).
- Integration (Round 2): sidecar job execution runner, persisted
  approvals, scheduler, live WebSocket events, UI bound to the live
  backend, real edge-tts voices, and real faster-whisper transcription;
  first end-to-end videos produced.
- Real providers (Round 3): Gemini-generated scripts, Pixabay stock
  visuals, real Whisper subtitles, channel connect via OAuth/PKCE, and
  quota/budget exposure; three Phase 0 gate test videos produced.
- Full automation (Round 4): unattended scheduled production across
  channels, Urdu-script TTS default with measured speech-rate
  calibration, Gate B conditional-pass policy, full Budget Guard
  enforcement, guardrail taboo screening, and pipeline hardening
  (smaller stock downloads, LLM pacing queue, router integrity).
- Style Lab (mini-round after Round 4): competitor channel/video intake
  scoped per channel, a per-scene video analysis engine (stock / AI /
  raw classification with confidences), style-profile synthesis with an
  enforced no-copy gate, and generation wiring so script, visuals,
  thumbnail, and SEO agents consume an active style profile.
- Growth and learning loop (Round 5): YouTube Analytics ingestion with
  honest "not connected" states, comments intelligence (review-first —
  no automatic posting), a per-channel/per-niche Learning Store with
  evidence-counted rules and calibration reporting, per-video reporting
  receipts, an Analytics/Growth UI, an optional 1080p master quality
  tier, and a fair subtitle QC metric.

### Notes

- Publishing still uses a fake/simulated provider in this codebase; no
  real YouTube upload happens until a channel is connected
  interactively by the user.
- Real analytics and comment data likewise only arrive after a channel
  is connected; until then the app reports "not connected" rather than
  showing invented numbers.
