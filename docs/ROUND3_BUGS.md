# ROUND 3 — Bug Log & Verification Tracker (backend-gaps team)

> **Project:** YouTube Agent (`~/workspace/youtube-agent-app/`)
> **Round:** 3 — backend gaps (quota exposure, budget settings + enforcement-lite, channel connect PKCE, real provider /test) + parallel UI/provider/integration teams
> **Date opened:** 2026-10-23
> **Scope of this file:** end-to-end acceptance + regression for the four backend-gaps deliverables, verified against a real booted sidecar (env contract: `YTA_PORT/YTA_TOKEN/YTA_BOOT_ID/YTA_DATA_DIR`, entry `python -m app.main`).
> **Round 2 baselines:** backend **579 passed / 8 skipped** · root **44 passed / 1 skipped** · acceptance (`tests/integration`) **18/18**.

**Severity legend:** `blocker` = Round 3 cannot be called done while open · `major` = a deliverable works partially or a suite is red · `minor` = works, but degraded / noisy · `trivial` = cosmetic / docs.

---

## Acceptance results (backend-gaps, live booted sidecar)

Verified with a scripted live run against a fresh data dir on workspace disk (secret backend `env`, no provider/YouTube credentials present): **12/12 checks passed**.

| # | Check | Result | Evidence |
|---|---|---|---|
| A1 | `GET /api/v1/usage/quota`, empty DB | ✅ pass | buckets `uploads {used 0, limit 100}`, `search {used 0, limit 100}`, `shared {used 0, limit 10000}`, `resets_at=2026-10-23T07:00:00Z` (next Pacific midnight), `seconds_until_reset=33348` |
| A2 | `GET /api/v1/queue/snapshot` embeds quota | ✅ pass | `snapshot.quota` byte-identical to the `/usage/quota` payload (not `{}`), empty and seeded |
| A3 | Quota totals move after seeding `api_usage` | ✅ pass | seeded 2× `videos.insert` (units 1), `search.list` units 3 + one `units=NULL` row, `thumbnails.set` units 5, one `gemini` row → `uploads=2, search=4` (stored 3 + NULL→per-method fallback 1), `shared=5`; non-YouTube row excluded |
| B1 | `GET /api/v1/settings/budget` defaults | ✅ pass | `{per_video_cap_usd 1.00, daily_cap_usd 10.00, monthly_cap_usd 30.00, warn_pct 70, soft_pct 90}` |
| B2 | `PUT /api/v1/settings/budget` → GET reflects | ✅ pass | PUT `{2.50 / 20.00 / 60.00 / 60 / 85}` → GET returns exactly that |
| B3 | Budget persists across sidecar **restart** (same data dir) | ✅ pass | after kill + reboot with new token/boot_id, GET still returns the PUT policy (SQLite-backed, not in-memory) |
| B4 | Runner enforcement-lite | ✅ pass (via team's tests) | `backend/tests/test_budget_caps.py`: `test_over_cap_start_parks_at_gate_a_with_budget_approval_and_warning_event`, `test_stage_research_raises_gate_pending_over_cap`, `test_approval_bypasses_budget_gate_on_next_start` — over-cap estimate parks at Gate A with a `budget_pending` approval + `budget.warning` event, no DAG created |
| C | `POST /api/v1/channels/connect`, no YouTube client credentials | ✅ pass | HTTP **409**, `error.code = youtube_client_not_configured`, `details.missing_refs = [youtube/client_id, youtube/client_secret]`, **7 ordered setup steps** in `details.setup_steps`. Mocked-Google success + revalidate paths: `backend/tests/test_channels_connect.py` — 7 passed (incl. `test_connect_success_stores_tokens_and_creates_channel_row`, `test_revalidate_invalid_grant_flips_to_needs_reauth`) |
| D | `POST /api/v1/providers/gemini/test`, no key | ✅ pass | HTTP 200, `{"status": "not_configured", "ok": false, "detail": "No API key configured for gemini."}`; same for pixabay / elevenlabs / openai. The old string `stub — real adapter pending` appears **nowhere** in responses and nowhere in `backend/app/` or `src/` (grep-verified) |
| E | Full regression | ✅ pass (see Verification runs) | backend, root, integration acceptance, `npm run typecheck`, `npm run build` |

Targeted team tests (quota + budget + channels + provider-connection + the ws test below): **63 passed**.

---

## Bug table

| ID | Severity | File | Failing test | Error | Suspected owner | Status |
|---|---|---|---|---|---|---|
| R3-001 | minor (test-infra, not app code) | `backend/tests/pipeline/` (render/scenes/subtitles/thumbnail) | 22 tests in one contended full-suite run: 10 FAILED + 12 ERROR (e.g. `test_render.py::test_progress_callback_fires`, `test_thumbnail.py::test_build_jpeg`) | `OSError: [Errno 28] No space left on device` and `app.pipeline.render.renderer.RenderError: ffmpeg failed (exit 228)` — the 512 MB `/tmp` tmpfs filled while ~7 sibling suites ran in parallel with default `TMPDIR=/tmp` | Test infra / all teams (use workspace-disk `TMPDIR` + `--basetemp`, as several Round 3 workers already do) | **verified not-a-code-bug** — `tests/pipeline` re-run standalone with `TMPDIR` on workspace disk: **232 passed, 0 failed**; final clean full-suite re-run also green (see Verification runs) |
| R3-002 | minor (flake observation) | `backend/tests/test_ws.py` | `test_check_heartbeats_closes_stale_connections` | `AssertionError` — failed once in the pre-landing baseline full-suite run (578 passed / 1 failed, Round 2 code, before any Round 3 file landed); passed standalone, in the targeted 63-test run, and in both subsequent full-suite runs, all under heavier load | WS hub / backend core team | open — not reproducing; monitor (same class as R2-003/R2-004 flakes; do not dismiss if it recurs in a clean run) |

No defects were found in the four backend-gaps deliverables themselves: every Round 3 backend-gaps test (`test_quota_view.py`, `test_budget_caps.py`, `test_channels_connect.py`, `test_provider_connection.py`) passed in every run, and all 12 live acceptance checks passed against the booted sidecar.

---

## Verification runs

| Run | When | Result |
|---|---|---|
| Backend suite, pre-landing baseline (Round 2 tree) | before sibling landings | 578 passed, **1 failed** (R3-002 flake), 8 skipped — vs Round 2 baseline 579 passed |
| Root suite, mid-landing | while sibling files were still landing | 43 passed, 1 failed, 1 skipped — failure not attributable (tree changed mid-run); superseded by the final root run below |
| Live acceptance (booted sidecar, A–D) | after landings | **12/12 passed** (table above) |
| Targeted team tests (quota/budget/channels/provider/ws) | after landings | **63 passed** |
| Backend suite, first post-landing full run (default `TMPDIR=/tmp`, ~7 parallel sibling suites) | after landings | 834 passed, 10 failed + 12 errors — **all** in `tests/pipeline`, all `Errno 28` (R3-001) |
| `tests/pipeline` standalone, `TMPDIR` on workspace disk | after R3-001 | **232 passed** |
| Backend suite, clean re-run (`TMPDIR` on workspace disk) | final | **856 passed, 8 skipped, 0 failed** in 1255.69s (856 = the contended run's 834 passes + its 22 infra casualties, all now passing) |
| Root suite, final | after landings | **44 passed, 1 skipped** (= Round 2 baseline exactly) |
| Root `tests/integration` acceptance subset | final | **22 tests collected, all passing** within the root run (Round 2: 18 — grew with Round 3 boot/quota cases) |
| `npm run typecheck` | after UI landings | ✅ pass (`tsc -b`, exit 0) |
| `npm run build` | after UI landings | ✅ pass (chunk-size >500 kB warning only, pre-existing) |

---

## Notes for the coordinator

- `models/` (464 MB local model cache) was added to `.gitignore` by a sibling team during this round; verified present in the Round 3 diff.
- Booted-sidecar acceptance used `YTA_SECRET_BACKEND=env` with a credential-free environment so "no credentials in the store" is deterministic on headless Linux (the OS-keyring backend is unavailable on this VM — known since Round 1; the Windows target uses Credential Manager). The 409 / `not_configured` contracts were exercised exactly as a credential-free Windows install would hit them.
- Scratch artefacts intentionally **not** committed: `backend/.test-tmp/`, `backend/tests/integration/.tmp_stock_visuals/` (test scratch dirs).
