# ROUND 6 — Acceptance Summary (verification worker: compliance / invariants / soak / rename / diagnostics / alembic + full regression)

Tree under test: git master @ `c99e10e` (Round 5) + uncommitted Round 6
working tree (138 dirty paths, content-stable from ~12:26 UTC; two
verification docs added later by sibling workers). Nothing was
committed by this worker. Baselines (Round 5): backend 2187 passed /
8 skipped (2195 total), root 44 passed / 1 skipped, UI typecheck+build
green.

Environment (applies to every number below): 2-vCPU / 8 GB VM shared
by all Round 6 workers. The VM rebooted ~11:56, ~12:37, ~14:20, ~15:17
and ~15:22 UTC during this pass, and the known intermittent EPERM /
disk-I/O write fault was active in bursts. Regression runs were
therefore executed serially with `TMPDIR=.tmp-yta` + per-run
`--basetemp`, and every non-pass was classified by standalone serial
re-runs, per the round's verification protocol.

---

## 1. Full regression (serial: backend → root → UI)

### Backend — 2449 collected (+254 vs Round 5's 2195)

Four independent complete runs of the settled tree were observed (this
worker's run 1 + three sibling runs), plus this worker's run 2 which
was killed by a VM reboot at 88%:

| Run | Passed | Failed | Errors | Skipped | Duration |
|---|---|---|---|---|---|
| This worker, run 1 (13:11–14:12 UTC) | 1845 | 133 | 463 | 8 | 3655 s |
| Sibling (sl009-full2) | 2302 | 50 | 89 | 8 | 5497 s |
| Sibling (w3-final-be) | 2250 | 56 | 135 | 8 | 5002 s |
| Sibling (full-backend4) | 2301 | 50 | 89 | 9 deselected | 3909 s |
| This worker, run 2 (killed by reboot at 88%) | — | 2 F + 8 E seen in first 88% | | | 51 min partial |

Failure classification (run 1 log forensics + cross-run set analysis):

- Dominant error signatures in run 1: 509×
  `sqlite3.OperationalError: disk I/O error`, 57×
  `PermissionError: [Errno 1] Operation not permitted`, plus ffmpeg
  subprocesses failing with "Operation not permitted" — the known VM
  write fault, not application defects. The fault struck each run in
  a different window, which is why passed counts vary 1845–2302.
- Cross-run: union of all failures across the 4 complete runs = 597
  distinct tests; intersection (failed in ALL 4 runs) = 138. Every
  intersection cluster was re-run standalone on a quiet box:
  - style-lab block (`test_style_lab_api/claimer/e2e/isolation/media/
    schema/store`, `test_stylelab_closure_adversarial`,
    `test_stylelab_profile_boundary`): **green standalone** (e.g.
    71 passed in one batch; a store/media/schema/tts batch went
    94 passed / 2 failed, the 2 being one stale-head test below and
    one tts-calibration pairing flake, both classified separately
    in this list).
  - `test_schedules_api`, `test_uninstall_policy`: **green
    standalone** (the schedules file is also the brief's known
    full-suite ordering flake).
  - `test_ws` replay tests: green standalone.
    `test_check_heartbeats_closes_stale_connections` failed
    standalone only while VM uptime was < ~1030 s after a reboot —
    the brief's known heartbeat flake, with the exact documented
    signature (`last_pong` vs monotonic threshold).
  - `test_tts_calibration_planning`: the known caplog-ordering flake —
    green solo (1 passed) and whole-file (32 passed); fails only in
    arbitrary pairings / full-suite context.
  - **2 deterministic failures — real defects (test expectations):**
    `test_style_lab_schema.py:209` and
    `test_execution_approvals.py:229` assert Alembic head `"0010"`;
    the head is `"0011"`. They fail solo, in batches, and in every
    full run. Logged as **R6-005**.
- Run 2 (post-reboot, quieter box) reached 88% with only 10 non-pass:
  the 8 `test_schedules_api` setup errors at the 88% progress line
  (position-identified against `--collect-only` order; the same tests
  are green standalone) and 2 failures inside
  `test_execution_*`/`test_provider_connection` neighbourhoods, all 60
  candidate tests green standalone. A third full run was started but
  the VM rebooted again at ~15:22 UTC, 5 minutes after the previous
  reboot; no complete post-fault full run exists. Projected clean-run
  outcome from all evidence above: **2449 collected → 2439–2447
  passed, 8 skipped, exactly 2 deterministic failures (R6-005)**, with
  the two known flakes (tts caplog, ws heartbeat) window-dependent.

Verdict: **PASS with defects** — no production-code regression found;
the suite cannot go fully green until R6-005's two stale assertions
are updated, and full-run counts on this VM are fault-window
lotteries until the EPERM fault is addressed.

### Root suite — PASS, exact baseline
This worker, clean window: **44 passed, 1 skipped in 35.71 s**
(skip = the known golden-datasets seed-stage skip).

### UI — PASS
`npm run typecheck` exit 0; `npm run build` exit 0 (vite built in
38.47 s; only the pre-existing >500 kB chunk warning).

---

## 2. Acceptance items

### (a) Compliance gate — PASS
- Targeted run by this worker (all green, part of a 103-test targeted
  batch): `tests/test_compliance_gate.py`,
  `tests/test_compliance_api.py`,
  `tests/providers/test_compliance_upload.py`.
- Proven behaviours: while overall compliance != passed, a public
  publish is refused with the typed reason code
  `compliance_not_passed`; uploads on the fake-provider path are
  forced to Private with the forcing disclosed
  (`test_publish_stage_forced_private_disclosed_pre_pass`,
  `test_finaliser_simulated_publish_discloses_forced_private_pre_pass`);
  after all four checklist items pass (each pass requires evidence and
  is a logged transition), requested visibility is honoured normally
  at every layer (gate, workflow, publish stage, finaliser, real-shaped
  uploader). A configured-but-unprovable checker resolves to
  not-passed (fail-closed); no free-form edit endpoint exists.
- Settings UI (code read + typecheck/build green): `ComplianceCard` in
  `src/screens/Settings.tsx` renders the checklist state from
  `GET /settings/compliance` — overall badge (Passed / In progress /
  Not started), per-item rows, the "Public publishing is blocked…
  uploads are forced to Private" banner pre-pass, transition history,
  and an error state that restates the blocked default.

### (b) Invariants proof pack — PASS (with the pack's own documented gaps; one doc finding stale)
- `docs/ROUND6_INVARIANTS.md` exists; this worker re-ran its four
  test modules: **64/64 passed** (secrets 12, loopback auth 8,
  publish gate 32, disconnect+signing 12).
- Loopback auth sweep is genuine: the test enumerates operations from
  `app.openapi()` at runtime (this worker reproduced the enumeration
  independently: **117 paths / 131 operations**) and probes every one
  with missing + invalid tokens expecting typed 401s. Sole exemption
  `GET /health/live` is pinned by test (`EXEMPT_PATHS == {"/health/live"}`,
  verified directly) and justified in the doc (shell polls liveness
  before the token exists; response is exactly `{"status": "ok"}`).
  Framework endpoints (`/openapi.json`, `/docs`, `/redoc`) are behind
  the same gate; WS ticket boundary proven separately.
- Plaintext-secret sweep: the pack's strict scopes (source, docs,
  outputs) report 0 hits and its positive controls prove the scanner
  detects planted fakes. This worker spot-checked adjudicated
  allowlist entries — `test_channels_connect.py:36,38,116` (synthetic
  `ya29.test-…`/`<redacted>` fixtures), `test_comments_fetch.py:354`
  (fake-marked Bearer fixture), `test_config.py:94` (synthetic JWT
  fixture) — all genuinely fake/short, as documented.
- The pack's findings register is honest and stands, with one
  exception: **R6-F1 (HIGH)** — a real OAuth client secret in
  plaintext in the repo-root dev `.env` — was confirmed by this worker
  without recording the value (`.env:5`, fingerprint prefix `GOCSPX`,
  value length 35 unquoted; file is git-ignored at `.gitignore:66`,
  mode 0600, untracked — all as the doc states). Parent/user decision
  (rotate + move to keyring, or formally accept the dev exception)
  remains open. R6-F2 (disconnect route never revokes/deletes),
  R6-F4 (no local OAuth scope check), R6-F3/F5 (LOW) were not
  contradicted by anything this worker ran. **R6-F6 is stale**: the
  updater pubkey is in fact wired into `tauri.conf.json` (152 chars,
  identical to `src-tauri/keys/updater-pubkey.txt`) — logged here as
  R6-006.

### (c) Soak — FAIL on stability, PASS on honesty (contention-confounded)
Artefacts: `outputs/round6/soak/soak_result.json`,
`soak_summary.md`, `console_run2.log`, `console_run3.log`,
`incident_run1/`, `incident_run2/`, `state/`.
- Final reported run: started 2026-10-23T11:58:54Z, finished
  12:23:22Z, **23.6 min driving vs 20.0 min target** — the duration
  claim is honest (it exceeds, not understates, the target) and the
  wall-clock fields are internally consistent.
- Stability numbers are internally consistent between JSON and
  summary, and they are bad: **0/4 cycles reached a terminal state**;
  HTTP 18 requests, 16 timeouts (88.89%); WebSocket 0 clean / 9
  failed; bad-ticket rejected 3/3, accepted 0 (auth held). Memory:
  sidecar RSS 63,536 → peak 163,376 → end 101,060 kB, cycle-end slope
  −415.2 kB/cycle (no leak signal). DB integrity `ok`, 0 FK
  violations — independently re-verified by this worker against
  `state/data/app.db` (626,688 bytes, 35 tables).
- Shutdown: SIGTERM did not stop the sidecar within 25 s; SIGKILL
  required (exit −9); pid dead and port closed afterwards, but the
  sidecar's ffmpeg survived as an orphan.
- Two reporting inaccuracies (R6-008): `integrity.db_bytes` says
  4,096 vs the real 626,688 bytes; the "17 orphan ffmpeg during run"
  counts repeated sightings of one sibling-test orphan, not the
  app's own processes.
- Confounder: the soak shared this 2-vCPU VM with 4–6 test suites
  and cargo builds (load 10–19); identical ReadTimeout symptoms hit
  ordinary suites in the same window. The run does not prove the
  sidecar wedges on a quiet machine — it proves it did not survive
  this one. **A quiet-box soak re-run is the top follow-up**; the
  SIGTERM latency (shell budget is 10 s) needs a direct look either
  way.

### (d) Rename SL-009 — code PASS, documentation FAIL → PARTIAL
- This worker's own grep: zero `app.stylelab` imports remain in
  `backend/app`, `backend/tests`, `src`.
- `import app.style_analysis` works and exports `StyleAnalysisStore`
  (from `app.style_analysis.storage`); `import app.stylelab` raises
  `ModuleNotFoundError`; intake package `app.style_lab` imports fine
  and its only diff is docstring/import updates referencing the
  renamed engine (not a rename of the intake package).
- Style-lab test files were updated and pass standalone (see §1).
- FAIL sub-criterion: `docs/STYLELAB_BUGS.md` does **not** mark SL-009
  RESOLVED — the entry still reads as an open recommendation. Logged
  as R6-007.

### (e) Diagnostics bundle — PASS
This worker ran `tests/test_diagnostics.py` (in the 103-test targeted
batch, all green), which proves via the API itself:
- `GET /api/v1/diagnostics` requires the launch token (401
  `missing_token` unauthenticated);
- preview bytes are byte-identical to share bytes
  (`test_preview_bytes_identical_to_share_bytes` — same content across
  `/diagnostics`, `/diagnostics/preview`, and a repeat fetch, with
  time and log source pinned);
- the seeded fake secret (`FAKE_SECRET_FOR_TEST_12345`, registered
  through `app.redaction.register_secret` and planted in log lines)
  is absent from the bundle bytes at both builder and endpoint level,
  while the `[REDACTED]` marker is present and the surrounding log
  text survives;
- no user content leaks (distinctive channel/project strings seeded
  in the DB never appear; only row counts), home-dir prefixes are
  scrubbed, and the bundle carries all required sections
  (app/runtime/system/health/counts/config_state/logs).

### (f) Alembic — PASS
- `alembic heads` → single head `0011`; history is a single linear
  chain 0001→0011 (verified from the version files and `alembic
  history`).
- This worker's fresh-DB probe (`YTA_DB_URL` → empty SQLite file):
  `upgrade head` applies 0001→0011 and creates `compliance_items` +
  `compliance_transitions`; `downgrade 0010` removes both tables and
  stamps 0010; a second `upgrade head` re-applies cleanly and stamps
  0011. The compliance tests additionally pin that migration 0011's
  DDL is identical to `app.compliance.schema`
  (`test_migration_0011_carries_identical_ddl`).

---

## 3. Credential hygiene sweep (final)
- This worker's own pattern sweep (Google API key / client secret /
  token shapes, private-key blocks, AWS, long JWT/Bearer) over all
  131 changed/new Round 6 files: **0 hits**. Over the new Round 6
  artefacts (`outputs/round6/**` text files + `docs/ROUND6_*`,
  53 files): **0 hits**. The invariants pack's broader sweep
  (tracked source + docs + 1,093 output artefacts) also reports 0.
- Sole plaintext secret in the tree remains the pre-existing dev
  `.env` OAuth client secret (R6-F1, HIGH, documented above; values
  were never printed or written by this worker — file:line +
  redacted fingerprint only). The sibling verification worker
  additionally found no secret material inside the 525 MB PyInstaller
  bundle (see R6-002 for its separate git-ignore hygiene issue).

## 4. What remains genuinely open
1. **R6-005** — update the two stale `"0010"` head assertions
   (test-only; blocks a fully green full run).
2. **R6-F1 (HIGH)** — parent/user decision on the plaintext dev
   `.env` client secret (rotate + keyring, or formal acceptance).
3. **Soak re-run on a quiet box** — to separate app stability from VM
   contention; plus the SIGTERM/graceful-shutdown latency and the
   ffmpeg orphan left after SIGKILL.
4. **R6-F2 / R6-F4** (from the invariants pack) — wire the disconnect
   route to `TokenManager.disconnect`; add a local OAuth scope check
   before upload-path token use.
5. **SL-009 doc closure** (R6-007) and the stale R6-F6 entry
   (R6-006) — documentation only.
6. Sibling-worker findings R6-001 (uninstaller/scheduled-task name
   mismatch, MEDIUM), R6-002 (525 MB bundle not git-ignored,
   MEDIUM), R6-003 (backup fallback schema version stale at 10),
   R6-004 (docstring nit), R6-009 above (duplicate operation ID) —
   see `docs/ROUND6_BUGS.md`.
7. The VM's EPERM/disk-I/O fault and reboot cycle: every full-suite
   number in this round is window-dependent until the environment is
   fixed; a single clean full run (all suites serial, one at a time)
   should be re-attempted on a healthy box before shipping.

---

## 5. Coordinator addendum — second verification team (shell / freeze / CI / regression cross-check / Rust)

Appended by the Round 6 verification coordinator. Scope: the items
the first pass did not cover — (a) Tauri shell, (b) sidecar freeze
clean-room, (c) CI/release, plus an independent regression
cross-check and the Rust reality check. Bug references are to the
coordinator addendum in `docs/ROUND6_BUGS.md` (R6-010+).

### (a) Shell — PASS (all items, caveats only)
- `tauri.conf.json`: valid JSON ($schema config/2); bundle targets
  `["nsis"]` with per-user `installMode: currentUser` (matches the
  project's own preflight gate); `externalBin: ["binaries/agent-backend"]`
  consistent with `sidecar.rs` `SIDECAR_NAME` and the staging rename
  chain in `packaging/build_sidecar.sh` (spec output `yta_sidecar` →
  staged `agent-backend-<triple>` + full tree at `agent-backend/`).
- CSP restrictive, quoted verbatim in the worker report:
  `default-src 'self'; … connect-src 'self' http://127.0.0.1:* ws://127.0.0.1:*`
  — loopback-only network, no wildcard sources.
- Updater: endpoints are real HTTPS (updates.youtubeagent.app +
  GitHub fallback); pubkey non-empty (152 chars), byte-identical to
  `src-tauri/keys/updater-pubkey.txt`. **Caveat: it is the Round 6
  DEV keypair and the endpoints are placeholders — rotate to the
  production keypair / real CDN before first public release**
  (documented in `src-tauri/keys/UPDATER_CONFIG.md` and
  `docs/UPDATER_KEY_RUNBOOK.md` §3).
- Versions: all resolve to **0.1.0** (tauri.conf `"../package.json"`
  → package.json 0.1.0; Cargo.toml 0.1.0; backend `__init__` /
  pyproject 0.1.0; CHANGELOG `## [0.1.0]`).
- Icons: complete — 32x32, 128x128, 128x128@2x (256), icon.png
  (512), icon.icns present; **icon.ico parsed: 8 resolutions
  (16/20/24/32/48/64/128/256, 32 bpp)**. Minor: `icon.png` exists
  but is not listed in `bundle.icon[]`.
- Lifecycle (the roadmap protocol, all verified in
  `src-tauri/src/sidecar.rs`): launch token minted per (re)start
  (32 CSPRNG bytes) and passed **via `YTA_TOKEN` env only** —
  whole-repo grep confirms no argv/file/log path; the PID file
  carries pid/boot_id/exe/start-time and no token. Readiness:
  `/health/live` poll every 100 ms with ×2 backoff, then
  `/health/ready` with token, under a 30 s cold-start budget.
  Exit-code match implements the table (0 → restart-as-unexpected,
  2 → fatal_config no-restart, 3 → port-bind respawn on fresh port,
  4 → `database_newer_than_binary` Failed, 5 → bad_launch_env
  no-restart, other/signal → crash restart with 1s→2s→5s backoff,
  max 3 per 5 min). Dev fallback: no Rust-side fallback exists at
  all (stale processes are orphan-swept, never connected to); the
  WebView fallback is build-flag gated (`import.meta.env.DEV` +
  `!isTauriRuntime()`, dead-code-eliminated in release).
  `--scheduled-run`/`--dry-run` parsed shell-side (sidecar.rs
  L1058-1062) and forwarded to the one-shot sidecar (L1300-1311).
- Capabilities: single `default.json`, deliberately minimal — no
  `shell:*`, no `fs:*`, no `http:*` permissions for the WebView.

### (b) Sidecar freeze — PASS with one MEDIUM finding (R6-013)
Independent clean-room run (only the frozen artefacts copied into a
fresh dir; fresh `YTA_DATA_DIR`; environment confirmed free of
GEMINI/PIXABAY/YOUTUBE keys by name-only inspection):
- Full onedir tree runs standalone: protocol line
  `{"event":"listening","port":18743,…}` emitted; **cold start to
  `/health/live` 200 measured at 1.87 s** (worker's own timing).
- Auth against the frozen binary: `/health/ready` without token →
  401 `missing_token`; with `X-Local-Token` → 200
  `{"status":"ready","db":"ok","migrations":"ok"}` (R6-018: Bearer
  alone is NOT accepted). Authenticated reads (onboarding status,
  providers, diagnostics) all 200 and honestly report
  not-configured providers — no repo `.env` leakage.
- Shutdown at idle: SIGTERM → exit in **0.41 s, exit code 0**,
  orderly uvicorn shutdown logs. (The soak's mid-render SIGTERM
  failure is a different, load/render-state case — see §3 of the
  first pass; idle behaviour is clean.)
- `YTA_DATA_DIR` honoured end-to-end: the fresh data dir contained
  only `app.db` (475,136 B) + `logs/`; repo `git status` count
  unchanged (142 → 142).
- FFmpeg resolution: contract order is env override
  (`YTA_FFMPEG_PATH`) → bundled `sys.executable`-relative
  `runtime/` paths → PATH. The frozen tree bundles **no** ffmpeg;
  the run used PATH `/usr/bin/ffmpeg` 8.1.2 (system-check reported
  `pass` with a real −21.75 LUFS loudnorm probe). The
  bundled-adjacent branch is therefore **unexercised** — on Windows
  the bundle must actually carry `runtime/` (see R6-013 for the
  sibling-tree risk of the same class).
- Labelled keyless smoke produce: **none exists** — the only
  candidate endpoint (`POST /api/v1/onboarding/test-video`) runs
  the real pipeline and needs real keys. Recorded as an honest gap,
  not a failure of the freeze.
- Measured sizes: staged exe 26,512,536 B (26 MB); full onedir tree
  520,698,708 B (500 MB), 1,226 files; build stamps `VERSION`
  0.1.0, git_commit `c99e10e0`, PyInstaller 6.22.3, `--onedir`.

### (c) CI / release — PASS after in-round fixes (2 FAILs remain, both LOW-design)
- Both workflows parse (python yaml); permissions minimal
  (`contents: read` default; only `release` job `contents: write`).
- Signing-absent behaviour is loud at every layer (header contract,
  `check-secrets` warning, UNSIGNED step-summary banners,
  `SIGNING-STATUS.txt` marker, UNSIGNED warning in draft release
  notes; nothing ever labels an unsigned artefact signed). Updater
  signing likewise: absence warns loudly; configured-but-missing
  `.sig` hard-fails ("refusing to ship an updater manifest without
  its signature"). **No literal secret material in either YAML**
  (48 `secrets.*` name references only).
- Preflight: landed (`packaging/preflight.sh` + `.ps1`, 6 checks)
  and **coordinator re-run on the settled tree: 6 passed, 0 failed,
  exit 0** (R6-010/R6-011/R6-012 were found failing mid-round and
  fixed before settlement).
- Runbook: all required topics present (prerequisites, build,
  signing, SMOKE clean-VM matrix, rollback incl. monotonic-updater
  caveat, version bump) and SmartScreen honesty is exemplary
  (§6.2/§6.3). Staleness items: R6-016.
- Remaining: R6-015 (floating `@stable` pin in ci.yml; no SHA pins),
  R6-017 (`backend.json` empty signature placeholder), R6-019
  (draft release not signing-gated, mitigated).

### (d) Regression cross-check — agrees with §1
Independent second run (chunked serial; monolith killed by VM
reboot): 2362 passed / 5 failed / 30 errors / 8 skipped on a
2405-test manifest; after standalone classification the only
deterministic failures are R6-005's two stale head assertions.
Root 44/1, UI green. No production-code regression. New item:
R6-014 (in-suite real-produce anomaly).

### (e) Rust reality check — INCONCLUSIVE (environment), CI is authoritative
Toolchain installs in isolation; dependency resolution and 57
dependency crates compile with no project-code errors surfaced; the
toolchain directory was externally deleted mid-run and gtk/webkit
dev libraries are absent on this VM, so the project's own crate was
never compiled (detail in `docs/ROUND6_BUGS.md`, coordinator
addendum). Static pass on the current tree: zero
TODO/FIXME/`unimplemented!`/`todo!` in `src-tauri/src/`;
Cargo.toml parses (tauri 2.12.5 dependency set).

### (f) What ONLY the first real Windows CI run can prove
1. The `youtube-agent` Rust crate compiles and links (WebView2 /
   MSVC toolchain) — never yet compiled anywhere.
2. Tauri bundling actually ships the sidecar's full onedir tree
   beside the externalBin exe (R6-013) and the bundled
   ffmpeg `runtime/` (unexercised branch, §5b).
3. NSIS installer behaviour end-to-end: per-user install, hooks
   (incl. the R6-001 scheduled-task name/folder mismatch in the
   PREUNINSTALL hook — statically proven mismatched, dynamically
   unprovable here), uninstall purge-marker flow.
4. `register_scheduler.ps1` actually registering/firing on a real
   Windows Task Scheduler (script is statically coherent, never
   executed).
5. Authenticode + updater signing with real secrets, SmartScreen
   behaviour, and the update flow against the real endpoints
   (current pubkey is a DEV key, endpoints are placeholders).
6. Windows Credential Manager as the secret backend (this VM's
   Linux keyring is the fail backend; all keyring paths here were
   verified only to the honest `store_unavailable` surface).
7. The soak on a quiet machine (stability verdict is
   contention-confounded) and mid-render SIGTERM behaviour on
   Windows.


---

## 6. Closure-pass addendum (2026-10-23, post-verification)

Appended by the Round 6 closure coordinator. Seven parallel closure
workers (W1–W7) fixed the open items on the settled tree; full
evidence per item is in `docs/ROUND6_BUGS.md` ("Closure pass —
coordinator register update"). Per-slice serial test runs only
(TMPDIR + --basetemp on workspace disk); no full-suite run was
attempted by the closure pass, and no commit was made.

Status of §4's open list:

1. **R6-005 — CLOSED.** Both head assertions now `"0011"`; both files
   green standalone (9 + 14 passed). A clean serial backend run is no
   longer blocked by this item.
2. **R6-F1 (HIGH) — STILL OPEN, unchanged.** Parent/user decision on
   the dev `.env` client secret; the closure pass did not open the
   file. This is now the only HIGH left in the round.
3. **Soak re-run — STILL OPEN.** Not attempted by the closure pass
   (the box remained shared/contended throughout). The
   SIGTERM-latency and post-SIGKILL ffmpeg-orphan questions remain
   exactly as §2(c) records them.
4. **R6-F2 / R6-F4 — CLOSED.** Disconnect route wired to
   `TokenManager.disconnect` (revoke-before-delete proven by the
   flipped positive test); local OAuth scope check
   (`require_upload_scope`, typed `NeedsReauth`, zero HTTP on refusal)
   enforced at the client and uploader entry points.
5. **R6-006 / R6-007 — CLOSED.** Invariants doc corrected (pubkey
   verified 152 chars, byte-identical to the txt file); SL-009 doc
   closure verified (the entry was already marked RESOLVED; claims
   independently re-verified).
6. **R6-001 / R6-002 / R6-003 / R6-004 / R6-009 — ALL CLOSED.**
   Uninstaller deletes the real task name; the 525 MB bundle is
   git-ignored (status proof: only README remains); backup fallback
   is 11; docstring names POSTUNINSTALL; schedules operation IDs are
   explicit and unique (OpenAPI regenerated: 131 ops, 0 duplicates).
7. **Environment — unchanged caveat.** The EPERM fault was active
   during parts of the closure pass too; every worker classified by
   standalone re-runs. A single clean full serial regression on the
   closure tree remains the pre-commit step.

Additionally closed beyond §4's list (from the shell/freeze/CI team
reports): **G1** updater UI wiring (event listener, banner, Settings
Updates + Autostart cards; UI typecheck + build green); **G2 /
R6-018** Bearer token acceptance (constant-time, sweep still green);
**G3** exit code 4 now emitted with the literal
`database_newer_than_binary` stderr marker, including unknown/future
revision strings; **R6-013** sidecar onedir tree shipping implemented
via `bundle.resources` + resource-dir spawn preference in sidecar.rs
(static-reviewed only — first Windows CI build is still the
authoritative compile/bundle proof); **D5** backend.json phantom
asset removed; the **ffmpeg pin** reconciled to the immutable BtbN
tag with real SHA-256 hashes; the Windows `which` test defect fixed
at the source (all 29 skip-list entries deleted); **ffmpeg resolver
adoption completed** (system_check, style_analysis, style_lab
service, /tmp fallbacks → data-dir tmp); and the **Gate B
conditional-vs-strict divergence** found by the freeze smoke is
aligned onto one shared evaluator verdict with explicit
`conditional_pass` labelling (326 tests green in W7's slices).

What ONLY the first real Windows CI run can prove (§5(f)) is
unchanged by this pass, and the soak (§4.3) and R6-F1 (§4.2) remain
the two genuinely open items.
