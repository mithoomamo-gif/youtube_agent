# DEV_SETUP — Developer Setup

> YouTube Agent — Windows desktop app for faceless, long-form YouTube automation.
> Stack (locked, Roadmap Part 2 / §06): **Tauri 2 (thin Rust shell) + React / TypeScript / Vite UI + Python 3.12 FastAPI sidecar + LangGraph (35 logical agents) + SQLite (WAL) + bundled FFmpeg.** BYOK — users bring their own provider keys.
>
> **Honesty note:** This document describes the *intended* interface per the Master Roadmap (`~/workspace/youtube-windows-agent/MASTER_ROADMAP.md`, Parts 4, 11, 12) and roadmap sections §06, §31, §36. Where a piece is not wired yet, it is marked **“lands with the shell team”** / **“not yet wired”** rather than presented as working. Canonical commands below are the contract other teams build against — if your checkout differs, fix the checkout or update this file in the same PR.

---

## 1. Prerequisites

Install these before anything else. Versions are minimums unless marked pinned.

| Tool | Version | Why | Check |
|---|---|---|---|
| **Python** | **3.12** (exact minor line; 3.12.x) | Backend sidecar, FastAPI, LangGraph. Do not use 3.13+ until the dependency set is re-pinned. | `python --version` |
| **Node.js** | **20+ LTS** (Node 24 is present on the current Linux dev VM and works) | React / TypeScript / Vite UI, Tauri CLI | `node --version` |
| **npm** | Ships with Node (10.x with Node 24) | UI dependencies and scripts | `npm --version` |
| **Rust toolchain** | Stable, via **rustup** | Tauri 2 shell only. The Rust layer is a thin supervisor (window/tray, spawn/kill sidecar, updater, autostart) — product logic never lives in Rust. | `rustc --version`, `cargo --version` |
| **FFmpeg + ffprobe** | Pinned bundled build for shipping; any recent build on `PATH` for dev | Render, encode, probe. Production detection tries NVENC → QSV → AMF → `libx264`. | `ffmpeg -version`, `ffprobe -version` |
| **Git** | Any current | Source control | `git --version` |

### Current dev VM status (Linux)

Verified 2026-10-23 on this VM: Python 3.12.3, Node v24.20.0, npm 10.9.4, FFmpeg 8.1.2, Git 2.43.0 — **no `rustc` / `cargo`**.

Consequence, stated plainly:

- ✅ Backend (Python) work: fully doable here — run the sidecar standalone (see §5.1).
- ✅ UI (React/Vite) work: fully doable here in a browser via `npm run dev` (see §5.2).
- ❌ Tauri shell build / `tauri dev` / NSIS packaging: **not possible on this VM** until a Rust toolchain is installed. Shell build is deferred to a Windows machine or Windows CI (`windows-latest`). Do not treat a Linux shell build failure as a code bug.

### Windows-only notes

These only apply on Windows — skip them on Linux/macOS dev machines:

- **WebView2 Runtime** — required by Tauri. Pre-installed on Windows 11; on Windows 10 the installer’s `embedBootstrapper` mode handles it (see `BUILD.md`). For dev, install the Evergreen runtime from Microsoft if `tauri dev` complains it is missing.
- **NSIS** — required only for packaging the per-user installer. Tauri’s bundler drives it; you do not write NSIS by hand except for hooks in `src-tauri/windows/hooks.nsh`. Lands with the shell/packaging team — see `BUILD.md`.
- **Microsoft C++ Build Tools / VC++ Redistributable** — needed by the Rust/Tauri build and checked at install time by the POSTINSTALL hook.
- **Windows SDK `signtool.exe`** — only for signing work and release verification. Normal dev builds are unsigned. See `BUILD.md` / `RELEASE.md`.
- **No Windows Service, ever.** Scheduled runs use Task Scheduler launching the app as the logged-on user (`--headless --run-schedule <id>`). A Session-0 service would break DPAPI-scoped OAuth tokens and GPU/NVENC access (Roadmap §11.9).

Install Rust on Windows via rustup:

```powershell
# https://rustup.rs — then, in a fresh terminal:
rustup default stable
rustup target add x86_64-pc-windows-msvc
cargo --version
```

---

## 2. Repo layout

Repo root: `~/workspace/youtube-agent-app/` (on Windows: wherever you cloned it).

```
youtube-agent-app/
├── backend/                  # Python 3.12 sidecar — ALL product logic lives here
│   ├── app/
│   │   ├── main.py           # FastAPI entry point (python -m app.main) — intended interface
│   │   ├── api/              # FastAPI routes (REST /api/v1 + WebSocket)
│   │   ├── orchestrator/     # LangGraph supervisor graph, DAG / state machine
│   │   ├── agents/           # 35 role definitions: prompt + tools + I/O schema
│   │   ├── workers/          # Capped pools: LLM/API, CPU, render (FFmpeg), transfer
│   │   ├── providers/        # One adapter per external API (YouTube, Gemini, TTS, stock, …)
│   │   └── store/            # SQLite + SQLAlchemy + Alembic migrations
│   ├── tests/                # pytest suite (see §6)
│   ├── requirements.txt      # Pinned Python dependencies (or pyproject equivalent)
│   └── agent-backend.spec    # PyInstaller --onedir spec — lands with the packaging team
├── src/                      # React + TypeScript UI (Vite)
│   └── ...                   # Screens, components (Tailwind + shadcn/ui)
├── src-tauri/                # Tauri 2 shell (Rust, thin) — lands with the shell team
│   ├── tauri.conf.json       # Bundler, externalBin, updater, WebView2 config
│   ├── src/                  # Supervisor: spawn/health-check/kill sidecar, tray, updater
│   └── windows/hooks.nsh     # NSIS installer hooks (PRE/POST INSTALL/UNINSTALL)
├── docs/
│   ├── DEV_SETUP.md          # This file
│   ├── BUILD.md              # Packaging, signing, update channels
│   └── RELEASE.md            # Release-day checklist, rollout rings, rollback
├── package.json              # UI scripts: dev / build / tauri
├── vite.config.ts
└── README.md
```

Rules that keep the layout honest:

- **35 agents are logical LangGraph roles, not 35 processes.** Most are waiting on an API at any moment (a coroutine, not a process). Only LLM/API calls, CPU work, and FFmpeg renders consume real resources, each behind a capped pool.
- **Three transports, each exactly one job** (Roadmap Part 4 / §31): Tauri IPC = native OS actions only; REST over loopback HTTP = commands + CRUD; one WebSocket = live state. The WebView never gets direct filesystem access. The sidecar never binds anything except `127.0.0.1`.
- **State does not live in the repo or the install dir.** On Windows the running app uses `%APPDATA%\YouTubeAgent\` for config, SQLite DB, and logs, and `%USERPROFILE%\Videos\YouTubeAgent\` by default for rendered output. In dev, `YTA_DATA_DIR` (see §5.1) points wherever you tell it.

---

## 3. First-time setup

Do these in order, once per machine.

### 3.1 Clone

```bash
git clone <repo-url> youtube-agent-app
cd youtube-agent-app
```

### 3.2 Backend — Python venv + dependencies

```bash
cd backend
python -m venv .venv

# Linux / macOS:
source .venv/bin/activate
# Windows (PowerShell):
# .\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt
# If the checkout uses pyproject.toml instead:
# pip install -e ".[dev]"
```

> ⚠️ Always work inside `.venv`. A global Python install will drift and produce “works on my machine” backend bugs that CI cannot reproduce.

### 3.3 UI — npm install

From the repo root:

```bash
cd ..            # back to repo root, if you were in backend/
npm install
```

This installs Vite, React, TypeScript, Tailwind / shadcn tooling, and the Tauri CLI / API packages declared in `package.json`.

### 3.4 Shell — Rust (Windows or Rust-equipped machine only)

```bash
# Prerequisite: rustup toolchain from §1.
# Verify Tauri's own view of your machine:
npm run tauri -- info
```

> **On the current Linux dev VM:** skip this step. There is no Rust toolchain installed, by design — the sidecar runs standalone for backend work and the UI runs in a plain browser. Shell verification happens on Windows / CI. **Lands with the shell team.**

### 3.5 Sanity checks

```bash
ffmpeg -version        # must succeed — backend probes this at runtime
ffprobe -version
python --version       # 3.12.x, inside the venv for backend work
node --version         # 20+ (24 on the dev VM)
```

---

## 4. Configuration in dev — no `.env` with secrets

- **Provider API keys (Gemini, OpenAI, ElevenLabs, Pexels/Pixabay, …) are BYOK and are never committed, never put in a repo `.env`, and never stored in SQLite or plaintext JSON.** In the running app they live in Windows Credential Manager via `keyring`, with a DPAPI-encrypted file fallback (`%APPDATA%\YouTubeAgent\secrets.dat`). In dev, set them through the UI’s Settings → Providers screen (once wired) or your OS keyring — not through files in the repo.
- **YouTube OAuth refresh tokens** follow the same rule: Credential Manager / DPAPI only, held only by the backend, never returned to the UI.
- The **only** configuration channel from the shell into the sidecar is the `YTA_*` environment contract in §5.1. Do not invent a second config file for launch parameters.

---

## 5. Running each piece in dev

You rarely run the whole desktop app while developing. Run the piece you are working on.

### 5.1 Backend sidecar standalone (primary dev mode)

The sidecar is a normal FastAPI app. The shell normally spawns it; in dev you spawn it yourself with the same environment contract the shell uses.

#### The `YTA_*` env contract (Roadmap §4.6–4.7 / §31)

This contract is the *only* configuration channel into the sidecar. Every variable matters:

| Variable | Meaning | Dev value |
|---|---|---|
| `YTA_PORT` | Port to bind, on `127.0.0.1` **only** | Pick a free port, e.g. `8765` |
| `YTA_TOKEN` | Per-launch token (32 random bytes, base64url in production). Sent by clients as `X-Local-Token` header on every `/api/v1/*` request. Delivered to the sidecar **via env only** — never command line, never file, never URL. | Any non-empty string for local dev, e.g. `dev-token` |
| `YTA_BOOT_ID` | UUID for this backend process. Stamps every WebSocket event and log line; `(boot_id, seq)` is the resync cursor. A restart **always** mints a new `boot_id` + token. | A UUID, e.g. output of `python -c "import uuid; print(uuid.uuid4())"` |
| `YTA_DATA_DIR` | Data directory: SQLite DB, logs, PID file live here. Production: `%APPDATA%\YouTubeAgent` | A scratch dir outside the repo, e.g. `./.dev-data` or `/tmp/yta-dev` |
| `YTA_PARENT_PID` | Shell PID for the parent watchdog. If the parent disappears (and `YTA_DETACHED` is not set), the sidecar drains and exits within ~5 s so no headless backend is left running invisibly. | Your shell’s PID, or omit in detached dev runs (see below) |
| `YTA_DETACHED` | `1` **only** for Task Scheduler headless runs — disables the parent watchdog. | Set to `1` for a standalone dev sidecar so it does not exit when a parent PID goes away |
| `YTA_LOG_LEVEL` | Log level. Default `info`; `debug` only from Settings → Diagnostics. | `debug` while developing, `info` otherwise |

Exit codes the shell keys restart logic off (know them when debugging): `0` clean shutdown · `2` fatal config / bind-scope error — do not auto-restart · `3` port bind failure — retry with a fresh port · `4` DB schema newer than this binary (downgrade protection) · `5` missing/invalid launch environment · anything else = crash.

#### Start it

```bash
cd backend
source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1

# Linux / macOS:
export YTA_PORT=8765
export YTA_TOKEN=dev-token
export YTA_BOOT_ID="$(python -c 'import uuid; print(uuid.uuid4())')"
export YTA_DATA_DIR="$PWD/.dev-data"
export YTA_DETACHED=1
export YTA_LOG_LEVEL=debug

# Windows (PowerShell):
# $env:YTA_PORT="8765"; $env:YTA_TOKEN="dev-token"
# $env:YTA_BOOT_ID=[guid]::NewGuid().ToString()
# $env:YTA_DATA_DIR="$PWD\.dev-data"; $env:YTA_DETACHED="1"; $env:YTA_LOG_LEVEL="debug"

python -m app.main
```

Notes:

- The bind host is an invariant, not a preference: `python -m app.main` binds `127.0.0.1` only (hardcoded in `app.main`), and the port comes from `YTA_PORT` (exported above). Never bind `0.0.0.0`. The sidecar self-checks its bind scope at startup and exits with code `2` if it is not loopback-only.
- The entry point `python -m app.main` is the intended canonical interface (equivalently, the `youtube-agent-backend` console script, `app.main:run`). `app.main` defines `create_app()` / `run()`, not a module-level `app`, so `uvicorn app.main:app` does not work. If your checkout names the module differently, align the checkout to this or update this doc in the same change — do not let the two drift silently.
- **Readiness ≠ process started.** On startup the sidecar prints one structured line to stdout — `{"event":"listening","port":…,"boot_id":"…"}` — then the shell (or you) polls health:
  - `GET /health/live` — liveness only, returns `{"status":"ok"}` and nothing else. This is the **one** token-exempt endpoint, so the shell can poll before the UI exists. It must never leak version or capability data.
  - `GET /health/ready` — token required. Reports DB open + migrations applied + job-queue recovery done. Providers are **not** checked here (lazy, per-job).
  - Cold-start budget is **30 s**. Heavy assets (Whisper / FLUX models) lazy-load per job so readiness never waits on them.

```bash
curl http://127.0.0.1:8765/health/live
curl -H "X-Local-Token: dev-token" http://127.0.0.1:8765/health/ready
curl -H "X-Local-Token: dev-token" http://127.0.0.1:8765/api/v1/system/info
```

- First call the UI makes after connecting is `GET /api/v1/system/info` — the version/capability handshake (`app_version`, `backend_version`, `api_version: 1`, `ws_schema: 1`, `boot_id`, capability flags). UI ↔ backend compatibility is negotiated there, because the backend package updates independently of the shell (see `BUILD.md`, Channel B).
- WebSocket: browsers cannot set custom headers, so the token never goes in the WS URL. Flow: `POST /api/v1/ws-ticket` over REST (token in header) → single-use 60-second ticket → open `ws://127.0.0.1:{port}/api/v1/ws` and send the ticket as the **first frame** (`client.hello`) within 5 s, or the backend closes with `4401`.
- Graceful shutdown: `POST /api/v1/system/shutdown` (token required) drains jobs, closes sockets (code `4402`), checkpoints SQLite, and exits `0`. Budget: 10 s, after which the shell kills the whole process tree via its Job Object — safe by design, because job leases + LangGraph checkpoints let the next launch resume.

### 5.2 UI in a browser (no Tauri)

For pure UI work — fastest loop, works on the Linux dev VM:

```bash
# Repo root:
npm install        # first time only
npm run dev        # Vite dev server; open the printed http://localhost:… URL
```

- Point the UI at your standalone sidecar from §5.1. Anything that needs the backend connection info (`{base_url, ws_url, token}`) normally receives it from the shell over Tauri IPC (`get_backend_connection()`); in browser-only mode use the dev connection settings / env the UI team provides. If that bridge is not wired in your checkout yet, UI screens that need live data will show the backend-down state — that is expected, not a bug to paper over. **Lands with the shell team.**
- The UI must handle the connection states honestly: connected / reconnecting (backoff 0.5 s → 1 → 2 → 4 → 8 → capped 15 s) / backend-down, shown as a persistent pill, never a blocking modal. REST is the source of truth; the socket is a resyncable notification channel.

### 5.3 Full desktop app — `tauri dev` (Windows / Rust machine only)

```bash
# Repo root, on a machine with rustup + WebView2 + build tools:
npm run tauri dev
# Equivalent direct form:
# npx tauri dev
```

What this does, when wired: builds the Rust shell, starts Vite, spawns the sidecar via `externalBin` with a fresh ephemeral port, a fresh `YTA_TOKEN` / `YTA_BOOT_ID` per launch, polls `/health/live` → `/health/ready`, and only then hands the connection to the WebView and swaps the splash for the dashboard. Closing the window minimises to tray — the sidecar keeps running **by design**; Quit from the tray is what shuts it down.

> **Not available on the current Linux dev VM** (no Rust toolchain). Do backend work via §5.1 and UI work via §5.2 there. Full-shell verification is a Windows/CI step.

---

## 6. Running tests

### 6.1 Backend — pytest

The canonical cross-cutting suite lives at the **repo root** in `tests/` (fixtures, golden
datasets, fault corpus, contract tests) and is what CI runs:

```bash
cd ~/workspace/youtube-agent-app        # repo root
python -m pytest tests/ -q              # default run (live tests excluded by default)
python -m pytest tests/ -m unit         # one marker (see table)
python -m pytest tests/ -m "not live"   # everything except live-provider tests
python -m pytest tests/ -x -q           # stop on first failure, quiet
make test                               # same suite via the Makefile
```

The backend package also carries its own package-local suite under `backend/tests/`
(run from `backend/` with its venv active); CI runs it separately from the root suite.

Markers (register every new marker in `pytest.ini` / `pyproject.toml` — an unregistered marker is a review blocker):

| Marker | Covers | Needs | Runs |
|---|---|---|---|
| `unit` | Pure functions, Pydantic I/O schemas for all 35 agents, quota math, slug/title rules, licence-ledger logic, niche-profile builder (incl. **Other**/custom niches) | Nothing external | Every PR — hard fail |
| `contract` | Provider-adapter contracts against recorded responses (VCR cassettes/fixtures); REST/WS protocol contract; UI↔backend schema agreement | Recorded fixtures only | Every PR (recorded); weekly live smoke separately |
| `integration` | Full pipeline DAG on fixture scripts with **all providers faked** (hermetic pipeline), checkpoint/resume, idempotency, gate ordering | Bundled FFmpeg for render stages | PR (short fixtures), nightly (full) |
| `ffmpeg` | Anything invoking real FFmpeg/ffprobe: graphs, encoder detection (NVENC/QSV/AMF/`libx264`), loudness, subtitle timing, golden-frame tolerances | FFmpeg on `PATH` | PR where render code changed; nightly full |
| `live` | Real calls to real providers / YouTube sandbox with real keys | Real (sandbox) keys, network, quota | **Never on a normal PR.** Weekly / manual only, explicitly opted into |
| `golden` | Golden-dataset agent evals + golden-video regression (SSIM, loudness −14 LUFS ±1, cue timing). Statistical thresholds, never exact string matches | Golden datasets under `evals/`; judge model for rubric-scored cases | PR smoke subset (10 cases/agent); full suite nightly |

Testing rules that are not optional (Roadmap §17):

- **No retry-to-pass.** A flaky case is quarantined with a ticket, never re-run until green. Variance is reported (mean ± spread over 3 runs at production temperature).
- **Prompt / model / provider changes are code changes** — they run the full affected eval suites and can block merge (judge task-success drop >2% relative, cost/run rise >25% without a recorded quality gain, or any deterministic regression).
- **The judge is never the judged.** LLM-as-judge uses a different, stronger model than the agent’s default, calibrated against human ratings (Cohen’s κ ≥ 0.7).
- **Goldens change only via a “bless” PR** — side-by-side diffs + a written reason, reviewed separately from the code change that caused the diff.
- Determinism boundary for hermetic runs: fakes + temperature 0 must give identical scene lists, subtitle timings ±100 ms, and identical QC verdicts across two runs.

### 6.2 UI — Vitest

```bash
npm run test          # Vitest (unit/component) — intended script name
npm run build         # Type-check + production build; must pass before any PR
```

> If `npm run test` is not yet defined in your checkout’s `package.json`, that is a gap to fix in the UI team’s next PR — the script names `dev` / `build` / `test` / `tauri` are the canonical interface this doc assumes.

### 6.3 Shell — cargo test (Windows / Rust machine only)

```bash
cd src-tauri
cargo test
```

**Lands with the shell team.** Not runnable on the current Linux dev VM.

### 6.4 What “green” means before a PR

- [ ] `pytest tests/ -m "not live"` passes
- [ ] `npm run build` passes (type-check included)
- [ ] No secrets, keys, or tokens in the diff, in logs, or in test fixtures (redaction happens at write time, not read time)
- [ ] New/changed agent prompts have golden cases; a fixed bug adds the case that would have caught it

---

## 7. Troubleshooting

| Symptom | Likely cause & fix |
|---|---|
| `cargo: command not found` / `tauri dev` fails immediately on the dev VM | **Expected on the Linux dev VM — no Rust toolchain installed.** Do backend work (§5.1) + browser UI work (§5.2). Shell builds belong on Windows/CI. Do not try to “fix” this by vendoring a toolchain ad hoc; raise it with the shell team if the VM policy changes. |
| Sidecar exits immediately, code `5` | Missing/invalid launch environment — one of the `YTA_*` variables (usually `YTA_TOKEN` or `YTA_PORT`) is unset. Re-export the full contract from §5.1. |
| Sidecar exits with code `3` | Port already in use. Pick a different `YTA_PORT` (production retries up to 5 fresh ports automatically). Check for a leftover sidecar from a previous run first. |
| Sidecar exits with code `2` | Fatal config / bind-scope error — most often something tried to bind non-loopback. The bind host is hardcoded to `127.0.0.1` in `app.main` (port via `YTA_PORT`); do not work around the self-check. |
| Sidecar exits with code `4` | DB schema is newer than this binary (you opened a data dir written by a newer backend). Use a fresh `YTA_DATA_DIR` for dev, or run the matching newer backend. Never downgrade-migrate a real data dir. |
| Sidecar starts then exits after ~5 s | Parent watchdog: `YTA_PARENT_PID` pointed at a process that died, and `YTA_DETACHED` was not set. For standalone dev, set `YTA_DETACHED=1`. |
| `/health/live` OK but `/health/ready` never ready | DB open / migration / queue-recovery is failing. Read the backend log in `YTA_DATA_DIR` (`backend.log`, rotating 10 MB × 5). Check the data dir is writable and not shared with another running sidecar. |
| Every `/api/v1/*` call returns `401` | Token mismatch: the `X-Local-Token` header does not equal this process’s `YTA_TOKEN`. Tokens are per-launch and are **never** persisted — a restarted sidecar needs its new token. Constant-time comparison is deliberate; there is no “dev bypass”. |
| Requests return `403` despite a correct token | Host/Origin validation rejected the call (DNS-rebinding / CSWSH defences). `Host` must be `127.0.0.1:{port}` / `localhost:{port}` / `[::1]:{port}`; `Origin`, if present, must be exactly the Tauri WebView origin. Plain `curl` (no `Origin`) is fine; a browser page from another origin is supposed to fail. |
| WebSocket closes with `4401` | Ticket missing, expired (60 s), replayed, or `client.hello` was not the first frame within 5 s. Fetch a fresh ticket via `POST /api/v1/ws-ticket` and reconnect. |
| WebSocket closes with `4410` / UI told to resync | WS schema version mismatch, sequence gap, or `boot_id` changed (backend restarted). Discard socket state and resync from REST (`GET /projects/{id}/snapshot`, queue snapshot) — REST is the source of truth. |
| UI shows backend-down while the sidecar is running | Connection info mismatch (port/token changed after a restart) or the UI is in browser mode without the IPC bridge. In `tauri dev`, the shell pushes `backend://connection-changed`; in browser mode, re-check your dev connection settings. |
| FFmpeg not found / encoder probe fails | FFmpeg/ffprobe not on `PATH`, or a listed encoder is unusable (driver/session limits — an encoder can be *listed* but fail a real encode). The backend test-encodes a 2-second clip per candidate at first run; if only `libx264` works, expect an amber warning and slower renders, not a crash. |
| Renders freeze the machine / UI stutters | You are exceeding the concurrency budget. Renders are capped at 1–2 concurrent, Below Normal priority; the adaptive governor lowers pools under sustained RAM >85% / CPU >90%. Use **Laptop mode** (renders 1, API pool 12) on constrained hardware instead of raising limits. |
| Tests pass locally, fail in CI | Usually one of: tests run outside `.venv` locally (dependency drift), an unmarked `live` test sneaking real network calls into a PR run, or FFmpeg version differences. Pin the FFmpeg build in CI to the bundled build and keep markers honest. |
| “Orphan” Python process after the app closes | A bug — report it. The shell owns spawning **and** killing the sidecar (Job Object around the whole tree), and the parent watchdog is the backstop. Never fix it by hand-killing in a way that hides the leak from the logs. |
| OAuth refresh token dies after 7 days in dev | The Google OAuth consent screen is still in **Testing** mode. Production verification is a Phase 0 exit criterion (Roadmap Part 12) — until then, expect weekly re-consent in dev. |

---

## 8. Where to read next

- `docs/BUILD.md` — how the dev pieces become the signed installer + the two update channels.
- `docs/RELEASE.md` — what must happen, in order, on release day.
- Master Roadmap Part 4 (§31) — the full REST/WebSocket/launch-token protocol.
- Master Roadmap Part 11 / §36 — packaging, signing, updates, first-run wizard.
- Master Roadmap Part 12 — the ~30-week timeline and its phase gates (MVP end of Week 8; full automation end of Week 18).
