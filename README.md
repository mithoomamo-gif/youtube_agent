# YouTube Agent

A fully automatic Windows desktop app for producing and publishing long-form, faceless YouTube videos — in any niche, including a custom **"Other"** niche you define yourself.

> **Status:** Roadmap approved 2026-10-23 (Part 14 recommended defaults) — coding started. **MVP target: end of Week 8** per the master roadmap. Modules are landing in parallel; see the build note below.

## What it does

Pick a channel, a niche (facts, Islamic stories, horror, history, motivation, tech, crime, health, … or **Other — your own**), and a topic — or let it run on a schedule. A 35-agent pipeline researches, scripts, voices, illustrates, edits, quality-checks, and uploads the video, with human review gates you control. Closing the app or crashing mid-job costs at most the current step: every stage is checkpointed and resumable.

## Architecture

| Layer | Technology | Role |
|---|---|---|
| Desktop shell | **Tauri 2** (Rust, thin supervisor only) | Native window, tray, updater, sidecar lifecycle; WebView has no direct filesystem access |
| Frontend UI | **React + TypeScript + Vite** (WebView2) | Dashboard, New Video Wizard, Review queue, agent monitor, costs & quota meters |
| Backend sidecar | **Python 3.12 + FastAPI + Pydantic v2** | Local REST + one WebSocket on `127.0.0.1` only; provider adapters, scheduler, cost/quota ledgers |
| Orchestration | **LangGraph** with SQLite checkpointer | The **35 agents** are logical role definitions / graph nodes in one process — not 35 processes |
| Storage | **SQLite (WAL) + SQLAlchemy 2.0 + Alembic** | Single local file: state, queue, audit, cost ledger, checkpoints — the source of truth |
| Rendering | **Bundled FFmpeg** (local) | Scene-segment rendering on the user's PC; a crash costs one segment, not the video |
| Packaging | **PyInstaller `--onedir` sidecar + Tauri NSIS** per-user installer | No admin/UAC needed for install or updates |

**Key principles**

- **BYOK (Bring Your Own Keys):** the user supplies their own provider API keys. Keys and YouTube refresh tokens stay on the user's PC in Windows Credential Manager (DPAPI fallback) — never in SQLite, logs, or plaintext files, and there is no central vendor vault.
- **Local-first:** rendering, state, and secrets are all local. External traffic is sidecar → provider over TLS, one adapter per provider.
- **Budget-guarded:** every provider call writes the cost/quota ledger; per-video, per-day, and per-provider caps pause paid work before money leaks.

## Repository layout

| Path | Contents |
|---|---|
| `src/` | React + TypeScript frontend (Vite) |
| `src-tauri/` | Tauri 2 / Rust shell — window, tray, sidecar spawn/supervision, IPC |
| `backend/` | Python 3.12 FastAPI sidecar — LangGraph pipeline, provider adapters, SQLite store |
| `tests/` | Test suites (backend, frontend, integration) |
| `docs/` | Developer setup, build, and release documentation |
| `tools/` | Repo / packaging helper scripts |
| `.github/` | CI workflows |

## Documentation

- [Developer setup](docs/DEV_SETUP.md) — toolchain, environment, running the app in dev mode
- [Build guide](docs/BUILD.md) — sidecar packaging (PyInstaller) and the Tauri/NSIS installer
- [Release guide](docs/RELEASE.md) — versioning, signing, and update channel process
- **Master roadmap:** `~/workspace/youtube-windows-agent/MASTER_ROADMAP.md` — the full pre-coding roadmap (15 parts) this repo implements, including the Part 14 decision checklist approved on 2026-10-23

## Cost model (summary)

Designed around a **budget stack of ≈ $0.25–0.75 per video** (roadmap Part 10):

- **Free ≈ $0.00–0.03/video** — edge-tts narration ($0), local FLUX image generation ($0 marginal), stock footage
- **Budget / Standard ≈ $0.25–0.75/video** — the target operating point: tiered LLM routing, paid-TTS fallbacks, a small number of API images, local rendering
- **Premium ≈ $1.50–3.50/video** — flagship LLM pass, premium voices/images, opt-in per channel

Guardrails warn at 70–80% of caps and pause new paid work at the cap unless the user explicitly enables overage. Full model, price registry, and worked examples: roadmap Part 10.

## Build note

> ⚠️ **No code in this repo is warranted to build yet.** Teams are landing modules in `backend/`, `src/`, and `src-tauri/` in parallel, so the tree may be partial or inconsistent at any given moment during the initial build-out. Don't treat a broken build at this stage as a regression — check back once the Week 1 module landings are complete, and follow [docs/DEV_SETUP.md](docs/DEV_SETUP.md) / [docs/BUILD.md](docs/BUILD.md) as they are published.

## Round 2 — Integration (2026-10-23)

Round 2 wired the Round 1 modules into a working end-to-end system and proved it with a dedicated acceptance suite. Bug/verification log: [docs/ROUND2_BUGS.md](docs/ROUND2_BUGS.md) · one-command verification: `./tools/round2_verify.sh`.

**What works end-to-end now**

- **Sidecar ↔ execution loop:** the FastAPI sidecar boots via the env contract (`python -m app.main`; `YTA_PORT/YTA_TOKEN/YTA_BOOT_ID/YTA_DATA_DIR`), serves `/health/live` + `/health/ready`, and — with `YTA_EXECUTION=1` — runs the job runner: creating a project enqueues a 10-job DAG (`stage.research → … → gate.b`), jobs execute, live progress streams over WebSocket (`agent.status_changed`, `pipeline.phase_changed`, `cost.ticker`), and a review-first project stops at Gate A with a pending approval (`projects.status='awaiting_review'`).
- **Approvals persist:** Alembic migration `0002_approvals` landed; gate approve/reject decisions are stored in SQLite and survive a sidecar restart (Round 1's in-memory approvals gap is closed).
- **"Other" niches persist:** an Other niche created via `POST /api/v1/niches` (e.g. Cricket Analysis) is stored in SQLite and survives restarts.
- **First real videos:** the integration pipeline (`backend/app/integration/`) produced two complete videos into `outputs/round2/` (gitignored — artefacts live on disk): a **facts** video (41.9 s) and a **cricket "Other"-niche** video (56.9 s), each 720p H.264 + AAC, both passing Gate A, Gate B and QC (loudness ≈ −14 LUFS), packaged with publish manifest, licence ledger and report. Voice is **real edge-tts**; wall time ≈ 105 s (facts) / ≈ 137 s (cricket) for ~1-minute videos, render-dominated — see [tools/round2_perf.md](tools/round2_perf.md).
- **UI live binding:** the React screens now read live sidecar state via the API/WebSocket client (Round 1 placeholders replaced on the main screens).

**How to run it**

```bash
# Backend + execution (from repo root)
cd backend && YTA_PORT=0 YTA_TOKEN=dev-token YTA_BOOT_ID=dev YTA_DATA_DIR=.tmp_data YTA_EXECUTION=1 .venv/bin/python -m app.main
# UI
npm run dev
# Verify everything (acceptance + backend + root suites + typecheck + build + ffprobe)
./tools/round2_verify.sh
```

Acceptance suite: `backend/.venv/bin/python -m pytest tests/integration -q` — **18 passed** (boot/health, Other-niche persistence, project execution + WS events, approvals persistence, E2E output validation). Full regression at Round 2 close: backend **579 passed / 8 skipped**, root **44 passed / 1 skipped**, typecheck + build green.

**What remains stubbed / key-blocked**

- LLM content is still the **deterministic stub** in E2E runs (real provider keys wire in Round 3); subtitles use the alignment stub (real faster-whisper path landed under `backend/app/providers/transcription/` but is not yet the E2E default); publishing stops at `fake_provider_upload_simulated` — the **OAuth connect endpoint is still a stub**, so no real YouTube upload yet.
- Tauri/Rust shell remains uncompiled on this VM (Windows/CI only); app icons and the updater signing key are still placeholders (Round 6 packaging).
- Known open bugs: `GET /api/v1/projects/{id}` can serve stale in-memory project state (R2-002), and the execution scheduler has an order-dependent rotation flake (R2-004) — details in [docs/ROUND2_BUGS.md](docs/ROUND2_BUGS.md).

## Round 3 — backend gaps closed (2026-10-23)

Four backend gaps that were stubs at Round 2 close are now real. Bug/verification log: [docs/ROUND3_BUGS.md](docs/ROUND3_BUGS.md).

- **YouTube quota is visible:** `GET /api/v1/usage/quota` reports today's YouTube API usage in the three buckets — uploads (limit 100/day), search (limit 100/day) and shared (limit 10,000 units/day) — counted from the app's own usage ledger, with the time the buckets reset (midnight Pacific). `GET /api/v1/queue/snapshot` shows the same quota object, so the dashboard's quota display is live, not blank.
- **Budget caps are settable and enforced:** `GET /api/v1/settings/budget` shows the current caps (per-video, daily, monthly, plus warning thresholds) and `PUT /api/v1/settings/budget` changes them — the values are saved in the database and survive an app restart. If a video's estimated cost goes over the per-video cap, the runner now stops it at Gate A with a `budget_pending` approval and a `budget.warning` event instead of spending silently.
- **Channel connect is a real OAuth flow:** `POST /api/v1/channels/connect` starts a proper Google sign-in flow (PKCE, one-time loopback redirect) and `POST /api/v1/channels/{id}/revalidate` checks the saved sign-in is still valid (a channel whose sign-in expired flips to `needs_reauth`). Until the one-time setup below is done, connect honestly returns `409 youtube_client_not_configured` together with the setup steps — it no longer pretends to connect.
- **Provider "test connection" is real:** `POST /api/v1/providers/{id}/test` (gemini, pixabay, elevenlabs, openai, …) makes the provider's cheapest real authenticated call. With no key saved it reports `status: not_configured`; with a bad key it reports an error category (e.g. `invalid_key`) — the old "stub — real adapter pending" response is gone.

**The one remaining step only you can do — Google Cloud OAuth client setup (one time, ~10 minutes):** the app cannot finish connecting a YouTube channel until you create a free Google Cloud OAuth client and save its `client_id` + `client_secret` in the app. In short: create a project at console.cloud.google.com, enable the **YouTube Data API v3** (and YouTube Analytics API), configure the OAuth consent screen and publish it to **Production** (if it stays in Testing mode, Google expires channel sign-ins after 7 days), create an OAuth client of type **Desktop app**, and paste its client ID and secret into the app. The app shows these same steps, in order, whenever you press Connect before this is done — after that, connecting a channel is just signing in with the channel's Google account.
