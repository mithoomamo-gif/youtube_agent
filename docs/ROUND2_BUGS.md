# ROUND 2 — Bug Log & Verification Tracker

> **Project:** YouTube Agent (`~/workspace/youtube-agent-app/`)
> **Round:** 2 — Integration (first real end-to-end video, UI live binding, real edge-tts / Whisper)
> **Date opened:** 2026-10-23
> **Status:** ✅ **CLOSED — Round 2 verification complete (2026-10-23).** All five acceptance areas (A–E) pass; full regression green (see Verification runs). Two bugs remain open for owning teams (R2-002, R2-004); none blocks the Round 2 gate.
> **Round 1 baseline:** Green and committed — `342ce65` (backend suite 466 passed; root suite 29 passed / 1 skipped).
> **How to verify:** `./tools/round2_verify.sh` from the repo root — runs the Round 2 acceptance suite (`tests/integration`), the backend suite, the root suite, `npm run typecheck` + `npm run build`, and ffprobe-checks any MP4s in `outputs/round2/`. Per-step logs land in `.tmp/round2-verify/logs/`.
> **How to file a bug here:** one row per bug in the table below. Copy the failing test name and the first meaningful error line verbatim from the verify log — do not paraphrase tracebacks. Set `Status` to `open` on filing; only the coordinator flips it to `fixed` / `verified` / `wontfix` after a re-run.

**Severity legend:** `blocker` = Round 2 cannot be called done while open · `major` = a deliverable works partially or a suite is red · `minor` = works, but degraded / noisy · `trivial` = cosmetic / docs.

**Status legend:** `open` → `investigating` → `fixed` (pending re-run) → `verified` · `blocked` (waiting on another deliverable) · `wontfix`.

---

## Bug table

| ID | Severity | File | Failing test | Error | Suspected owner | Status |
|---|---|---|---|---|---|---|
| R2-001 | minor | `docs/DEV_SETUP.md` §5.1 (+ docstring in `backend/app/main.py`) | — (manual run of the documented command) | `ERROR: Error loading ASGI app. Attribute "app" not found in module "app.main"` — `app.main` defines only `create_app()`/`run()`; the working entry is `python -m app.main` (console script `youtube-agent-backend = app.main:run`) | Backend main / docs team | open |
| R2-002 | major | `backend/app/routers/projects.py` | Found by `tests/integration/test_project_execution_ws.py` (acceptance C) | `GET /api/v1/projects/{id}` serves the router's in-memory dict first and reports `status='draft'`, `current_phase=null` forever, even after execution moved the DB row to `awaiting_review`. No test fails on it (acceptance reads DB-backed approvals/jobs endpoints), but the UI would show stale project state | Projects router / backend core team | open |
| R2-003 | major | `backend/app/integration/` (real produce path) | `backend/tests/integration/test_produce_glue.py::test_real_produce_video_short` | Gate A failure in the real-produce test — deterministic across all of Worker 5's mid-landing snapshots (last ~20:40Z). Root cause not isolated before the E2E team's 21:02 cricket re-run (loudness fix: true peak −0.97 → −1.08 dBTP); the coordinator's final full-suite run (579 passed, 0 failed) does not reproduce it | E2E / integration team | verified (not reproducing; monitor — reopen if it recurs) |
| R2-004 | major | `backend/app/execution/` (scheduler rotation) | `backend/tests/test_execution_scheduler.py::test_fixed_list_rotation` | Failed in two independent suite runs (20:28Z full, ~20:35Z half-suite), passed standalone (20/20) and in the coordinator's final run: `assert topics == ["First topic", "Second topic"]` → `AssertionError: assert ['Second topi...'First topic'] == ['First topic...Second topic']` — rotation starting point off by one / order-dependent under suite conditions | Execution team | open (flake — needs investigation; do not dismiss as landing noise) |
| R2-005 | blocker→resolved | `outputs/round2/` (E2E deliverable) | `tests/integration/test_e2e_outputs.py` (6 root-suite failures while absent) | `Failed: No MP4 files found under .../outputs/round2 ... expected a 'facts' MP4 and a 'cricket' MP4 for acceptance test E.` — resolved when the E2E team landed both renders (~20:59–21:02); acceptance E now 8/8 and root suite green | E2E / integration team | verified |

<!-- Coordinator: add real bugs below the template row as worker reports arrive.
     Keep IDs sequential (R2-001, R2-002, …) and never reuse an ID. -->

---

## Blockers (missing Round 2 deliverables)

Round 2 is not done until every item here has landed and passed `tools/round2_verify.sh`. Items are checked off by the coordinator only — a worker's "done" claim is not a check-off.

| # | Missing deliverable | Expected location | Why it blocks | Status |
|---|---|---|---|---|
| B1 | Round 2 acceptance tests | `tests/integration/` (beyond the Round 1 `test_placeholder_pipeline.py`) | Step 1 of the verify script has nothing real to run — integration is unproven | ✅ resolved — 5 acceptance modules, 18 tests, all passing |
| B2 | First end-to-end Round 2 render output | `outputs/round2/*.mp4` | The ffprobe step of the verify script stays SKIPPED until a real render lands; "first real video" is the headline Round 2 gate | ✅ resolved — facts (41.933 s) + cricket/Other (56.900 s) MP4s landed, ffprobe-validated (see R2-005) |
| B3 | Approvals table | `backend/migrations/versions/0002_*.py` (see Known stubs) | Human-review gates have no persisted approvals store | ✅ resolved — `0002_approvals.py` landed; acceptance D proves decisions persist across restart |

<!-- Coordinator: append blockers discovered during verification (e.g. a suite
     that cannot even collect, a provider binding that was never wired). -->

---

## Known stubs / key-blocked items

Carried over from Round 1. These are **known, accepted stubs** — they are not Round 2 bugs unless a Round 2 deliverable depends on them, in which case promote the item to the bug table / blockers list and say so here.

| # | Item | State at Round 1 close | Round 2 expectation |
|---|---|---|---|
| S1 | **Rust shell compile** | Tauri shell code is written but has never been compiled on this VM — no Rust toolchain installed here; compile is **Windows / CI only** | Unchanged in Round 2 unless CI lands. Do not file Linux build failures of `src-tauri/` as bugs |
| S2 | **Tauri icons + updater signing key** | `src-tauri/icons/` holds only a `README.txt` placeholder; no production icons, and no Tauri updater signing keypair exists yet | Blocks packaging (Round 6), not Round 2 integration. Must exist before any distributed build |
| S3 | **OAuth connect endpoint** | Stub only — the YouTube OAuth/PKCE flow code exists in the providers layer, but the connect endpoint that starts the flow from the UI is not wired | Needed before any real-channel publish test; hermetic / fake-provider integration tests must not depend on it |
| S4 | **faster-whisper real model path** | Subtitle/transcription path is stubbed / faked in tests; the real faster-whisper model load-and-transcribe path is unverified | Round 2 wires real Whisper — first real-model failure goes in the bug table above, not here |
| S5 | **Approvals table (migration 0002)** | Does not exist in Round 1 — `backend/migrations/versions/` contains only `0001_initial_schema.py`; approval persistence was deferred with an explicit "migration 0002" decision | ✅ **Landed and verified in Round 2** — `0002_approvals.py` present, applies in a booted DB, and approval decisions persist across a sidecar restart (acceptance D, 3/3) |

---

## Verification runs

Coordinator records each full `tools/round2_verify.sh` run here (date, verdict, failed-step count, log path). Worker spot-checks do not count as verification runs.

| Date | Run by | Verdict | Failed steps | Notes |
|---|---|---|---|---|
| 2026-10-23 (~21:05–21:15 PKT) | Coordinator (final pass; same commands as `tools/round2_verify.sh`, run step-by-step on the settled tree) | ✅ PASS | 0 | Acceptance `tests/integration`: **18 passed**. Backend suite `pytest backend/tests`: **579 passed, 8 skipped, 0 failed** (96.9 s). Root suite `pytest tests`: **44 passed, 1 skipped**. `npm run typecheck`: exit 0. `npm run build`: ✓ built. Media probe: facts 41.933 s / cricket 56.900 s, H.264 720p + AAC 48 kHz (per Worker 4's ffprobe validation, acceptance E 8/8). Round 1 baselines re-confirmed mid-round by Worker 5 (clean R1 tree: 466 passed — no R1 regressions) |
