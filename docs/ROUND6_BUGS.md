# Round 6 — Verification Findings (bugs + acceptance)

Verification worker report, Round 6 (Windows Packaging + Hardening).
Tree under test: git master @ c99e10e + the uncommitted Round 6 working
tree (138 dirty paths; content-stable from ~12:32 UTC, re-checked
repeatedly during the pass). Baselines from Round 5: backend 2187
passed, root 44 passed / 1 skipped, UI typecheck+build green.

Environment note (applies to every number below): this VM has 2 cores.
During the regression window, FOUR full backend suites (three other
workers' + this verification run) executed concurrently, plus two VM
restarts. From ~13:5x UTC an intermittent EPERM write fault (the known
environment fault: `write(2)` returns "Operation not permitted" for
interpreter processes — python and node — while shell/coreutils writes
succeed) was active; a plain `tempfile.mkdtemp()` probe fails with it
and the full-suite log contains 154 EPERM mentions. Failures were
therefore classified by standalone serial re-runs after the fault
window, per the round's verification protocol.

---

## Bugs

### R6-001 — MEDIUM — Uninstaller removes a scheduled task that is never created; the real task survives uninstall
- `packaging/register_scheduler.ps1` registers the task named
  **`YouTube Agent - Scheduled Run`** in the **root** task folder
  (`Register-ScheduledTask -TaskName 'YouTube Agent - Scheduled Run'`,
  no `-TaskPath`).
- `src-tauri/windows/hooks.nsh` `NSIS_HOOK_PREUNINSTALL` deletes
  `schtasks /Delete /TN "\YouTubeAgent\DailyAutoRun"` and sweeps
  `Get-ScheduledTask -TaskPath '\YouTubeAgent\'` — a different name in
  a different folder. Its comment claims "DailyAutoRun is the task the
  app registers (§11.9: tasks launch the app headless with
  --headless --run-schedule)"; the actual registered task launches
  with `--scheduled-run`.
- `register_scheduler.ps1`'s own header likewise claims "The
  uninstaller's PREUNINSTALL hook performs the same removal" as
  `-Unregister` — it does not.
- Consequence: after uninstall, the real task remains and fires every
  30 minutes (plus at logon) against a deleted executable, forever.
  `docs/ROUND6_SHELL_CONFORMANCE.md` §11.7 claims tasks are removed on
  uninstall — not true for the task the app actually registers.
- Reproduction: read the two files side by side (static; the .ps1
  cannot execute on this Linux VM). No other file in the tree
  references `DailyAutoRun`.
- Fix direction: make the hook delete `\YouTube Agent - Scheduled Run`
  (root folder) by its exact name, or register the task under the
  `\YouTubeAgent\` folder with a name the hook knows — one contract,
  both sides.

### R6-002 — MEDIUM — 525 MB PyInstaller bundle in `src-tauri/binaries/` is not git-ignored
- `git ls-files --others --exclude-standard -- src-tauri/binaries`
  lists **1062 files / 525 MB** (the staged `agent-backend/` onedir
  tree plus the `agent-backend-x86_64-unknown-linux-gnu` ELF).
- Round 6's `.gitignore` addition covers only `src-tauri/binaries/*.exe`
  (conformance fix F5) and `packaging/vendor/`. The onedir tree and the
  non-.exe sidecar binary are not covered.
- `src-tauri/binaries/README.md` states the directory's "only committed
  file is this README" and `docs/ROUND6_DRYRUN.md` confirms the build
  stages "the complete tree under src-tauri/binaries/agent-backend/" —
  so a routine `git add -A` at commit time would commit ~525 MB of
  build artefacts, contradicting the stated contract.
- No secret material was found inside the bundle (no .env / key files;
  only library files such as certifi's cacert.pem) — this is a
  tree-hygiene / commit-safety defect, not a leak.
- Fix direction: ignore `src-tauri/binaries/agent-backend/` and the
  target-triple sidecar binaries (all platforms), keeping only
  `README.md`.

### R6-003 — LOW — `backup.py` fallback schema version is stale after migration 0011
- `backend/app/backup.py`: `_FALLBACK_SCHEMA_VERSION = 10` with the
  comment "Must equal the Alembic head revision — 0010 at Round 6",
  and the module docstring still describes schema versions
  `0001`..`0010`. Round 6 itself added migration
  `0011_compliance.py`, so the head is 11.
- Runtime impact today: none observed — `current_db_schema_version()`
  derives 11 from `migrations/versions/`, and the PyInstaller spec
  bundles `migrations/` (verified: the staged bundle contains
  `migrations/versions/0011_compliance.py`), so the fallback is not
  exercised in the packaged layout either. It becomes a live defect
  only in a layout that drops the migrations directory: the packaged
  app would then refuse to restore backups its own schema-11 database
  produced (11 > fallback 10).
- Fix direction: bump the fallback to 11 and correct the docstring, or
  fail loudly when the migrations directory is absent.

### R6-004 — LOW — `uninstall_policy.py` docstring names the wrong NSIS hook
- Module docstring says the marker is checked by
  `NSIS_HOOK_PREUNINSTALL` in `src-tauri/windows/hooks.nsh`. The marker
  check actually lives in `NSIS_HOOK_POSTUNINSTALL` (hooks.nsh line
  ~294), as `docs/WINDOWS_DATA.md` §7 correctly states. Documentation
  only; the mechanism itself is consistent (see acceptance item 7).

### R6-005 — MEDIUM (test-expectation) — Two older tests hard-code the pre-Round-6 Alembic head "0010"
- Round 6 added migration `0011_compliance.py`; the chain upgrades
  cleanly 0001 → 0011 (verified in both tests' own captured alembic
  logs). Two older tests assert the head is still `0010` and now fail
  deterministically, standalone, on a quiet machine:
  - `backend/tests/test_execution_approvals.py:229` —
    `test_migration_0002_upgrade_and_downgrade`: `assert version ==
    "0010"` → got `'0011'`. (All of the test's real subject matter —
    approvals table, columns, indexes, `provider_credentials.last4` —
    passes; only the final head-string assertion is stale.)
  - `backend/tests/test_style_lab_schema.py:207-209` —
    `test_migration_0004_upgrade_from_empty_and_downgrade`: same
    assertion, same result.
- These are the ONLY two code-vs-test mismatches found in the whole
  settled tree (grep confirms no other `"0010"` head assertions;
  `test_backup_restore.py`'s `0010` is an intentional stamp parameter
  and `test_compliance_gate.py` correctly asserts 0011's
  down_revision is `0010`).
- Classification: test-expectation defect, NOT a production-code
  defect. Fix: update both assertions to `"0011"` (one line each).
  The verification worker did not edit them — flagged here for the
  fix owner, per protocol.
- Effect on the suite: a clean serial backend run of the settled tree
  is red by exactly these 2 tests until fixed.

### R6-006 — LOW (RESOLVED during verification) — Worker scratch dir at repo root
- An untracked `.tmp-sl009/` directory (a worker's pytest basetemp
  that escaped the `.tmp-yta/` convention) was observed at the
  repository root mid-pass. Its owner removed it before this report
  was finished (verified absent at final check). No action remains;
  recorded only because it was part of the tree during verification.

### Observation (not a bug) — backup bundle carries the DB verbatim
- `build_export` strips secret-shaped fields from the readable JSON
  snapshots (verified: a planted credential-shaped value in a channel
  `config_json` is absent from `data/channels.json`), but
  `database/app.db` is a verbatim SQLite snapshot — by design, since
  the DB is specified to hold only credential *references*
  (`provider_credentials.keyring_ref`), never values. A credential
  value that ever landed in a `*_json` column through some other bug
  would travel inside `database/app.db`. Recorded so the boundary is
  explicit; the stated contract ("no secret values in the bundle")
  holds for every value the app itself stores.

---

## Regression classification (item 1 detail)

Full backend suite (serial, this worker's own run, on the settled
tree but concurrent with three other workers' full suites on a 2-core
VM, inside the EPERM fault window): **60 failed, 2213 passed,
8 skipped, 168 errors** of 2449 collected (Round 5 baseline: 2187
passed; the suite grew by ~260 tests: Round 6 + compliance +
invariants). The other workers' concurrent runs showed the same
signature (50 failed / 89 errors; 56 failed / 135 errors) — a shared
environment effect, not three independent code states.

Every one of the 228 failed/error test ids (25 files) was re-run
standalone, serially, after the fault lifted:

- **226 ids: ENVIRONMENTAL** — all green standalone. Causes: the EPERM
  write fault (154 EPERM mentions in the full-run log; failures are
  `PermissionError` on writes, sqlite "attempt to write a readonly
  database", downstream 503s) plus 4-way CPU contention on a 2-core
  box. Includes the documented flakes:
  `test_tts_calibration_planning.py` (26 ids; standalone 32 passed in
  1.3 s) and the Style Lab media/store/api/isolation/boundary files.
- **1 id: ENVIRONMENTAL (known uptime flake)** —
  `test_ws.py::test_check_heartbeats_closes_stale_connections`
  asserts a pong timestamp > 1030 s of monotonic time; failed at VM
  uptime ~240 s and ~840 s after the restarts, **passed** at uptime
  > 1030 s (re-run confirmed).
- **2 ids: TEST-EXPECTATION defects** — R6-005 (stale `"0010"` head
  assertions). The only genuine red in the tree.

Derived clean-run expectation for the settled tree: **2447 passed,
2 failed (R6-005), 8 skipped** — vs baseline 2187 passed, i.e. no
regressions; all growth is new Round 6 coverage passing.

Root suite (standalone): **44 passed, 1 skipped** — exact baseline.
UI: `tsc -b --force` green; `npm run build` green (built in 59 s;
chunk-size warning only). (The first build attempt inside the EPERM
window failed at vite config load with the same EPERM write error;
the post-fault retry is the green one reported here.)

Round 6 targeted set (standalone, pre-fault): test_log_management +
test_data_dir + test_data_dir_contract + test_uninstall_policy +
test_scheduled_run + test_onboarding_system_check + test_diagnostics
+ test_backup_restore = **117 passed**.

## Acceptance summary

1. Full regression — PASS WITH CLASSIFICATION: no code regressions;
   226 environmental (EPERM fault + 4-way contention), 1 known uptime
   flake (green > 1030 s), 2 stale test expectations (R6-005).
   Root suite exact baseline; UI typecheck+build green.
2. Data layout — PASS. `resolve_data_dir` honours `YTA_DATA_DIR` on
   every platform (runtime-probed); Windows `%APPDATA%\YouTubeAgent`
   and reconstructed-roaming branches verified with injected env;
   dev/Linux default `~/.youtube-agent` unchanged (existing DB/logs
   in place); `docs/WINDOWS_DATA.md` matches the resolver and the
   `DataDirLayout` sub-paths (db/logs/outputs/cache/models) — its §6
   audit's "resolved" items were spot-verified in code
   (config/db/comment_intel/retention/style_analysis/claimer).
3. Logs — PASS. `LOG_MAX_BYTES = 10 * 1024 * 1024`,
   `LOG_BACKUP_COUNT = 5` in `app/logging_setup.py`, wired into the
   real `RotatingFileHandler` in `configure_logging` (asserted by
   test). Redaction proof test passes (seeded fake secret emitted via
   logger, exception and split-write stderr never reaches
   backend.log/stderr.log); grep of 362 produced log files for both
   seeded fake secrets (log-management + diagnostics seeds): 0 hits.
4. Backup — PASS (R6-003 low + DB-verbatim observation). Own
   round-trip on a seeded DB: export → wipe → import → channel row
   back, counts match; manifest `included` list == actual zip
   contents exactly (manifest.json + database/app.db + 4 snapshots);
   newer-schema bundle (schema 12 vs supported 11) refused with
   `backup_from_newer_app` ("made by a newer version of the app …
   Update the app to restore it — nothing was changed") and the live
   DB byte-identical after the refusal; secret-shaped values stripped
   from readable snapshots.
5. Wizard / system checks — PASS. Live probes in a test shell:
   `PATH` stripped of ffmpeg → check reports real `fail`
   ("ffmpeg was not found on PATH", measured path None); real ffmpeg
   → `pass` with a real loudnorm measurement (−21.75 LUFS on the
   440 Hz probe tone); WebView2 on Linux → `unverified` ("Cannot
   verify on this OS…"); disk check reports measured free bytes with
   warn < 10 GiB / fail < 1 GiB. No hard-coded success in
   `app/system_check.py` (every `pass` is downstream of a real probe)
   or in the onboarding UI/router (router calls `run_all_checks`).
6. Scheduler — PASS for the run path; FAIL on uninstall coherence
   (R6-001). `packaging/register_scheduler.ps1` read in full:
   statically coherent (documented ScheduledTasks cmdlets, per-user
   Limited principal, `--scheduled-run` action, logon + 30 min
   triggers, idempotent `-Force`, honest "never been run" note; it
   cannot execute on this Linux VM). Live CLI proof against a seeded
   DB via the real entry point (`python -m app.main`): dry-run lists
   the due schedule (missed_slots 2) with zero side effects; real run
   materialises with catch_up_once (checked 1, materialised 1,
   collapsed 1, failed 0, `schedule_runs` row written, `next_run_at`
   advanced); **STOP ALL blocks the run** (`status blocked`,
   `blocked_by stop_all`, no new rows, exit 0); `--dry-run` alone
   exits 64. `test_scheduled_run.py` (16 tests) green standalone.
7. Uninstall marker — PASS (R6-004 doc nit). Marker name
   `uninstall-purge.marker` is identical in backend code
   (`app/uninstall_policy.py` MARKER_FILENAME), the settings router,
   the UI wire fallback (`src/api/queries/providers.ts`), the NSIS
   hook (`hooks.nsh` POSTUNINSTALL `IfFileExists`), and
   `docs/WINDOWS_DATA.md` §7. Default state verified: no
   `privacy.remove_data_on_uninstall` row in either live DB
   (`~/.youtube-agent/app.db`, `.dev-data/app.db`) and no marker file
   in either data root. Hook logic matches the doc (marker → purge +
   credential delete; silent + no marker → keep; prompt defaults NO).
8. Credential hygiene — PASS for secret values; FAIL on artefact
   hygiene (R6-002; R6-006 resolved during the pass). Pattern sweep (Google/AWS/GitHub/OpenAI/
   Slack key shapes, private-key headers, credential assignments)
   over all 156 new/changed source files: 0 real secrets (3 hits, all
   test fixtures for the secret scanner itself). Sweep of 6222
   artefact files (.tmp-yta logs, outputs, packaging): 0 hits.
   `src-tauri/keys/` holds only the minisign PUBLIC key + a config
   doc — no private key material. Repo `.env` files are git-ignored,
   mode 600. The staged sidecar bundle contains no .env/key files.

---

## Addendum — second verification worker (compliance / invariants / soak / rename / diagnostics / alembic scope)

Appended by the Round 6 verification worker for the compliance-gate,
invariants, soak, SL-009 rename, diagnostics and alembic items. Numbering
continues from R6-005 to avoid colliding with the findings above. The
regression evidence behind these entries is consolidated in
`docs/ROUND6_ACCEPTANCE.md`.

Environment note for this addendum: the VM rebooted four times during
the pass (~11:56, ~12:37, ~14:20, ~15:17 UTC, plus a further restart at
~15:22) and the known intermittent EPERM / disk-I/O write fault was
active in bursts (509 `sqlite3.OperationalError: disk I/O error` and 57
`PermissionError: [Errno 1]` in this worker's first full-suite log
alone). Every non-pass below was classified by standalone serial
re-runs, per protocol.

### R6-005 — MEDIUM — Two pre-existing tests assert the stale Alembic head "0010" and fail deterministically since migration 0011 landed
- `backend/tests/test_style_lab_schema.py:209`
  (`test_migration_0004_upgrade_from_empty_and_downgrade`) asserts the
  version after `upgrade head` is `"0010"`; the tree's head is now
  `"0011"` (Round 6 compliance migration). Solo re-run:
  `AssertionError: assert '0011' == '0010'` — deterministic, not a flake.
- `backend/tests/test_execution_approvals.py:229`
  (`test_migration_0002_upgrade_and_downgrade`) has the same stale
  assertion and fails identically, solo and in every full-suite run
  observed (it is in the 4-run failure intersection of 138 tests).
- Classification: test-expectation defect. Production code is correct —
  `alembic heads` shows the single head `0011` and the 0011
  upgrade/downgrade cycle is clean on a fresh DB (verified separately).
- Consequence: no full backend run can go fully green until these two
  assertions are updated (assert `"0011"`, or better, derive the head
  from the migration directory). Same maintenance pattern the Style Lab
  verification already hit when 0005 landed (see STYLELAB_BUGS SL-008
  note). Not fixed by this worker: verification is report-only, and the
  fix belongs with the team that owns the new head.

### R6-006 — LOW — `docs/ROUND6_INVARIANTS.md` finding R6-F6 is stale: the Tauri updater pubkey IS wired in
- The invariants doc states `plugins.updater.pubkey` in
  `src-tauri/tauri.conf.json` is an EMPTY string and the generated key
  at `src-tauri/keys/updater-pubkey.txt` is "not yet copied into
  tauri.conf.json" (finding R6-F6, MEDIUM).
- Verified against the current tree: `tauri.conf.json` (mtime 11:09 UTC)
  carries a 152-character pubkey, byte-identical to
  `src-tauri/keys/updater-pubkey.txt`. The packaging preflight's
  updater check also passes on this point.
- The invariants config-point test itself is state-flexible
  (`test_updater_pubkey_state_is_empty_or_a_valid_minisign_key`) and
  passes either way, so the proof pack is green — only the document's
  finding register overstates an already-closed gap. CI-side signing
  secrets remain unverifiable from this VM, as the doc says.

### R6-007 — LOW — SL-009 rename is fully executed in code but `docs/STYLELAB_BUGS.md` never marks it RESOLVED
- Code state (verified): zero `app.stylelab` imports remain anywhere in
  `backend/app`, `backend/tests`, `src` (this worker's own grep);
  `import app.stylelab` raises `ModuleNotFoundError`; the engine
  answers as `app.style_analysis` with `StyleAnalysisStore` exported;
  the intake package `app.style_lab` is intact (its only diff is
  docstring/import-candidate updates pointing at the renamed engine).
- But the SL-009 entry in `docs/STYLELAB_BUGS.md` still reads as an open
  LOW with "Recommendation (recorded, NOT executed…)" — the closure
  annotation every other resolved SL item carries is missing. Anyone
  triaging from the bugs file will re-investigate a finished rename.

### R6-008 — LOW — Soak report: two figures do not match the artefacts on disk
- `outputs/round6/soak/soak_result.json` reports
  `integrity.db_bytes = 4096`; the actual soak DB
  (`outputs/round6/soak/state/data/app.db`) is 626,688 bytes. The
  integrity claims themselves are TRUE — this worker re-ran
  `PRAGMA integrity_check` (`ok`) and `PRAGMA foreign_key_check`
  (clean) against that file — but the byte figure is wrong (the harness
  evidently stat'ed the DB before it grew / before checkpoint).
- "Orphan ffmpeg during run: 17" counts repeated *observations* of a
  single ffmpeg process (pid 9860) that belonged to a **sibling test
  suite's** debris (`.tmp-yta/w6full2/test_real_produce_video…`), not
  to the soak sidecar. Distinct foreign orphan: 1, and not the app's.
  The after-shutdown orphan (1) is real and is the soak app's own
  ffmpeg (pid 23990) — see the soak verdict in ROUND6_ACCEPTANCE.md.

### R6-009 — LOW — Duplicate FastAPI operation ID for the schedules run-now route
- Observed in this worker's targeted run:
  `UserWarning: Duplicate Operation ID
  run_now_api_v1_schedules__schedule_id__run_now_post for function
  run_now at app/routers/schedules.py` (FastAPI openapi utils).
  Harmless at runtime (the invariants loopback sweep still enumerates
  131 operations and all reject unauthenticated requests), but it makes
  generated client operation names ambiguous. Likely a second `run_now`
  handler name introduced with the Round 6 scheduled-run work.

### Soak stability — FAIL (honestly reported; contention-confounded)
- Recorded separately from the numbered bugs because the primary
  evidence is the soak worker's own artefacts: the final soak
  (23.6 min driving vs 20.0 min target) reached **0/4 cycles** terminal,
  with 16/18 HTTP requests timing out (88.89%), 0/9 clean WebSocket
  sessions, and the sidecar ignoring SIGTERM for 25 s (SIGKILL
  required), leaving its ffmpeg orphaned. Bad-ticket rejection worked
  (3/3 rejected, 0 accepted) and no memory-leak signal was observed
  (cycle-end RSS slope −415.2 kB/cycle).
- Confounder, stated plainly: the soak ran while 4–6 sibling suites and
  cargo builds shared this 2-vCPU VM (load 10–19), and identical
  ReadTimeout symptoms hit ordinary test suites in the same window.
  The numbers do NOT prove the sidecar wedges on a quiet machine; they
  DO prove it did not survive this one. A quiet-box re-run is required
  before this is treated as a launch blocker — and the
  SIGTERM/graceful-shutdown latency deserves a direct look regardless,
  because the shell's own shutdown budget (per the conformance doc) is
  10 s, less than half what the sidecar needed here.

---

## Coordinator addendum — second verification team (regression / shell / freeze / CI / Rust)

Appended by the Round 6 verification coordinator consolidating the
parallel verification team (6 workers: full regression, Tauri shell,
sidecar freeze clean-room, CI/release, data/hardening, Rust check).
Numbering continues at R6-010. Editorial note: this file already
contains two numbering collisions from the first pass (R6-005 and
R6-006 each appear twice, in the main register and in the addendum
register); both refer to real, distinct findings — read by heading,
not by number alone.

### Resolved during the round (found failing mid-round, fixed before the settled tree)

#### R6-010 — RESOLVED — Preflight could never pass: `tauri.conf.json` version indirection vs literal comparison
- The CI verification worker found `bash packaging/preflight.sh`
  exiting 1: check (a) compared `tauri.conf.json`'s
  `"version": "../package.json"` literally against the literal sources
  (all agree at 0.1.0), and check (e) changelog cascaded from it. The
  release workflow's own version resolver had the same flaw.
- Coordinator re-run on the settled tree (2026-10-23 ~15:40 UTC):
  **6 passed, 0 failed, exit 0** — preflight now resolves the
  reference (`Target version: 0.1.0`). Closed; recorded for
  traceability because any mid-round snapshot still shows the failure.

#### R6-011 — RESOLVED — `release` job used `needs.build.outputs.version` without `build` in its `needs`
- Early snapshot: `release` declared `needs: [sign, check-secrets]`
  while consuming `${{ needs.build.outputs.version }}` for artifact
  names / `RELEASE_VERSION` / tag — would have resolved empty.
- Settled tree: `.github/workflows/release.yml:929` now reads
  `needs: [build, sign, check-secrets]`. Closed.

#### R6-012 — RESOLVED — Release dependencies missing at first snapshot
- `packaging/build_sidecar.ps1`, `packaging/ffmpeg-pin.json`,
  `packaging/windows-test-skips.txt` were absent when the CI worker
  first looked (release.yml hard-fails without the .ps1). All three
  are present on the settled tree (coordinator-verified). The
  `backend/agent-backend.spec` reference from the skeleton era is
  superseded: the workflow now builds via
  `packaging/build_sidecar.ps1` + `packaging/yta_sidecar.spec`.

### Open findings from the second team

#### R6-013 — MEDIUM — The staged `externalBin` single exe is NOT self-contained (PyInstaller `--onedir`)
- Clean-room proof (freeze worker): copying ONLY
  `src-tauri/binaries/agent-backend-x86_64-unknown-linux-gnu`
  (26,512,536 B) into an empty dir and running it exits **255 in
  <0.1 s**: `[PYI-ERROR] Failed to load Python shared library
  '…/_internal/libpython3.12.so.1.0'`. The real deliverable unit is
  the sibling onedir tree `src-tauri/binaries/agent-backend/`
  (500 MB, 1,226 files), which runs clean (see acceptance addendum).
- Risk: Tauri `externalBin` semantics copy the single named file. If
  the NSIS bundle does not also ship the whole `agent-backend/` tree
  beside it, the installed sidecar dies instantly. The staging
  contract is documented (`src-tauri/binaries/README.md`,
  `packaging/build_sidecar.sh:317-325`), but only the first real
  Windows CI build + install smoke can prove the bundle carries the
  tree. Fix direction: confirm bundle resources include the tree, or
  switch the freeze to `--onefile`.

#### R6-014 — MEDIUM (test infrastructure) — Lifecycle test performs a real 122-scene produce inside the full suite
- In the regression worker's chunked serial run, a lifecycle-area
  test (~#325 of its chunk) ran a **full real produce for ~50 min**
  (266 MB premix, loudnorm, segment renders, draft.mp4 completed)
  before VM-reboot kills — twice, with identical dot profiles. The
  same file passes standalone in seconds and no failure was ever
  recorded for it. Mechanism unresolved (suspected cross-test shared
  state leaking a real-produce configuration into the suite context).
- Consequence: full-suite wall-clock and CI timeouts are hostage to
  this test until the leak is found. Flagged for the execution team.

#### R6-015 — LOW — Action pinning policy violated in `ci.yml`; no SHA pins anywhere
- `dtolnay/rust-toolchain@stable` (`ci.yml:275`) is a floating ref,
  contradicting the policy the new `release.yml` header itself states
  ("never a floating branch"). All other 24 `uses:` across both
  workflows are major-tag pinned; **0 of 25 are SHA-pinned**.
  Permissions are minimal in both files (verified: `contents: read`
  default; only the `release` job has `contents: write`). Fix: swap
  ci.yml to `actions-rust-lang/setup-rust-toolchain@v1` (as
  release.yml uses) or SHA-pin.

#### R6-016 — LOW — Runbook / changelog staleness vs the landed workflow
- `docs/RELEASE_RUNBOOK.md` §2 (version procedure: claims
  tauri.conf carries literal `0.1.0` and package.json is `0.0.0`
  "known drift" — both false now), §2.3 Blockers A/B/C (icons /
  pubkey / `example.invalid` endpoint — all fixed on the tree),
  §4.2 + §10 (claims the pipeline "is a skeleton", Azure signing a
  "marked placeholder", two jobs — the landed release.yml is a
  6-job production workflow with real `azure/trusted-signing-action`),
  §5.4 (draft-release behaviour sentence no longer matches).
- `CHANGELOG.md:19-26` `[Unreleased]` "Known release blockers" still
  lists the icons/pubkey/endpoint blockers as open.
- SmartScreen content (§6) was verified exemplary and is NOT stale:
  it states plainly that a signed-but-new publisher should expect
  the prompt and forbids writing "signed, so no warnings."

#### R6-017 — LOW — `backend.json` updater manifest emitted with an empty signature (disclosed)
- The release workflow emits `backend.json` with `signature = ""`,
  with an in-file comment that the minisign signature with the
  separate BACKEND keypair is to be "wire[d] in the release
  environment". Tauri updater `.sig` handling itself is correct and
  loud (absence warns; configured-but-missing `.sig` hard-fails).
  Until the backend keypair is wired, backend-only updates cannot be
  signature-verified.

#### R6-018 — LOW — Backend auth accepts only `X-Local-Token`; `Authorization: Bearer` is not accepted
- Proven in the clean-room run: `GET /health/ready` with
  `Authorization: Bearer <token>` → 401 `missing_token`; with
  `X-Local-Token` → 200. Source: `backend/app/middleware.py`
  `LaunchTokenMiddleware` checks only `x-local-token`. The shell
  works (its `mini_http.rs` sends both headers), so this is a
  contract/documentation alignment item: any doc or client that
  relies on Bearer alone will fail.

#### R6-019 — LOW — Draft GitHub Release creation is not gated on signing
- An unsigned tag build still creates a (draft) GitHub Release.
  Mitigations are real: always `--draft`, UNSIGNED banner in the
  release notes, `SIGNING-STATUS.txt` marker in the bundle, human
  publishes. Recorded as a deliberate-design caveat, weaker than the
  old skeleton's `has_any_signing` gate.

#### R6-020 — LOW — `outputs/round6/soak/soak_summary.md` is a stale mid-run snapshot
- The summary on disk (15.3 min, 0/2 cycles, integrity null)
  disagrees with the final `soak_result.json` (23.6 min, 0/4,
  integrity ok) + `ANALYST_NOTES.md`, which are the authoritative
  record. Companion to R6-008 (figure mismatches inside the soak
  artefacts themselves).

### Second-team regression cross-check (independent of the first pass)
- Separate worker, chunked serial methodology (monolith attempt was
  killed by a VM reboot at 47%): 2405 collected at its 12:05
  manifest → **2362 passed, 5 failed, 30 errors, 8 skipped**. After
  standalone classification: the ONLY deterministic failures are the
  same two stale-head tests as R6-005; the rest are chunk-invocation
  fixture artifacts (22, all standalone-green), the briefed
  schedules ordering errors (8, standalone-green), the known ws
  uptime flake, the EPERM window, and two invariant tests that ran
  mid-landing (green on the settled tree). Root **44 passed /
  1 skipped**, UI typecheck + build **exit 0**. Conclusion agrees
  with the first pass: no production-code regression.

### Rust reality check (best effort — NOT a compile proof)
- Isolated rustup toolchain installed (rustc/cargo 1.99.1);
  dependency resolution + downloads succeeded (tauri 2.12.5,
  tauri-plugin-updater 2.14.0, wry 0.57.0, webkit2gtk crate 2.0.2);
  57 dependency crates compiled incl. glib-sys/gobject-sys with zero
  project-code errors surfaced.
- The isolated toolchain directory was then **deleted externally
  mid-run** (infrastructure fault, 11:06 UTC) and `cargo check` died
  inside `gio-sys` (`rustc … never executed / No such file or
  directory`) before ever reaching the project's own crate. gtk /
  webkit dev libraries are also absent on this VM
  (`gtk+-3.0`, `webkit2gtk-4.1`, `libsoup-3.0` not in pkg-config).
- Verdict: the `youtube-agent` crate has **never been compiled
  anywhere** as far as this round can show. The Windows CI build is
  the first and only authoritative compile.

---

## Closure pass — coordinator register update (2026-10-23, post-verification)

Appended by the Round 6 closure coordinator after 7 parallel closure
workers (W1–W7) landed fixes on the settled tree. Statuses below are
RESOLVED with evidence; test counts are the workers' serial runs
(TMPDIR + --basetemp under `.tmp-yta/<worker>`, standalone re-runs
before any failure classification). Nothing was committed by the
closure pass; HEAD remains `c99e10e`.

### R6-001 — RESOLVED (W2)
- `src-tauri/windows/hooks.nsh:245-256` (PREUNINSTALL §3): the hook now
  deletes the task the app actually creates —
  `schtasks /Delete /TN "YouTube Agent - Scheduled Run" /F` (root
  folder), and the PowerShell sweep uses
  `Get-ScheduledTask -TaskName 'YouTube Agent - Scheduled Run'`; the
  stale comment (DailyAutoRun / `--headless --run-schedule`) corrected
  to the register script's real contract (`--scheduled-run`, no
  `-TaskPath`). `packaging/register_scheduler.ps1`'s `-Unregister`
  header claim is now literally true; no edit needed there. Tree-wide
  grep: zero `DailyAutoRun` references remain in code/config.
- CI-only proof still owed: the hook actually deleting the task on a
  real Windows install/uninstall (hooks.nsh cannot execute here).

### R6-002 — RESOLVED (W1)
- `.gitignore` now carries `src-tauri/binaries/agent-backend/`,
  `src-tauri/binaries/agent-backend-*`, `src-tauri/binaries/*` with
  `!src-tauri/binaries/README.md`, plus anchored
  `packaging/dist-sidecar/` (the latter was already covered by the
  pre-existing unanchored `dist-sidecar/` rule — proven by
  check-ignore; the explicit rule is belt-and-braces).
- Proof: `git check-ignore -v` matches files inside the bundle tree
  and the Linux ELF; README.md is NOT ignored (exit 1);
  `git status --porcelain -uall -- src-tauri/binaries
  packaging/dist-sidecar` lists exactly `?? src-tauri/binaries/README.md`.
  Untracked-file count overall dropped 1230 → 172 (binaries subtree
  1062 → 1).

### R6-003 — RESOLVED (W1)
- `backend/app/backup.py`: `_FALLBACK_SCHEMA_VERSION = 11` (line ~87),
  comment updated to "0011 at Round 6"; module docstring range
  ``0001``..``0011`` (line 30) and the `database_schema_version`
  docstring range corrected likewise (line 166). Runtime check:
  fallback and derived `CURRENT_DB_SCHEMA_VERSION` both 11.
  `tests/test_backup_restore.py`: 9 passed.

### R6-004 — RESOLVED (W1)
- `backend/app/uninstall_policy.py:17` docstring now names
  `NSIS_HOOK_POSTUNINSTALL` (verified: the marker check
  `IfFileExists …uninstall-purge.marker` is at hooks.nsh:294 inside
  the POSTUNINSTALL macro; PREUNINSTALL at :228 never touches it).

### R6-005 — RESOLVED (W1)
- `backend/tests/test_execution_approvals.py:229` and
  `backend/tests/test_style_lab_schema.py:209`: head assertions
  `"0010"` → `"0011"` (+ comments). Both files re-run standalone:
  9 passed and 14 passed respectively, fully green. The tree's only
  deterministic red is gone.

### R6-006 — RESOLVED (W4, in docs/ROUND6_INVARIANTS.md)
- Verified against the tree (length + match only; value never
  printed): `plugins.updater.pubkey` in tauri.conf.json = 152 chars,
  byte-identical to `src-tauri/keys/updater-pubkey.txt` (152 chars).
  The invariants doc's R6-F6 finding is rewritten as CLOSED
  (verdict-summary row, both Invariant-3 config-table rows, the
  finding at :224, the register row at :368). Honest caveat kept: the
  matching `TAURI_SIGNING_PRIVATE_KEY` in CI secrets + escrow remains
  unverifiable from this VM.

### R6-007 — RESOLVED (W1, verification only — doc was already correct)
- The SL-009 entry in `docs/STYLELAB_BUGS.md:220` was already marked
  RESOLVED (Round 6) by the rename worker; W1 independently
  re-verified every claim: zero `app.stylelab` / `import stylelab`
  matches in backend/app, backend/tests, src; `backend/app/stylelab/`
  absent, `backend/app/style_analysis/` present (12 modules);
  `import app.stylelab` raises ModuleNotFoundError;
  `StyleAnalysisStore` and `StyleLabStore` are distinct importable
  classes. No edit was needed.

### R6-009 — RESOLVED (W1)
- Cause: one `run_now` handler stacked under two route decorators
  (`/run-now` + `/run_now`) in `backend/app/routers/schedules.py`;
  FastAPI sanitised both to the same operation ID. Fix (:814-821):
  explicit `operation_id="run_schedule_now"` and
  `operation_id="run_schedule_now_underscore_alias"`. Regenerated
  OpenAPI with warnings-as-errors: 131 operations, zero duplicates
  anywhere. `test_schedules_api.py` standalone: 8 passed;
  scheduled_run + execution_scheduler slices: 37 passed.

### R6-013 — IMPLEMENTED (W2), CI proof still owed
- `src-tauri/tauri.conf.json:39-41`: `bundle.resources` maps
  `"binaries/agent-backend/": "agent-backend/"` (directory-map form,
  recursive; deliberately not a glob) so the whole onedir tree ships.
  Trade-off documented: the staged dir must exist at bundle time —
  an unstaged `tauri build` now fails loudly instead of silently
  shipping the dead lone exe.
- `src-tauri/src/sidecar.rs`: new `resource_sidecar_exe()` (:463) +
  spawn preference (:1345-1365) — if the shell exe's dir lacks
  `_internal/`, resolve `resource_dir()/agent-backend/` and spawn the
  staged copy (either exe name) only when its tree has `_internal/`
  beside it. API shapes verified against tauri-plugin-shell 2.x /
  docs.rs source. **Static-reviewed only — no cargo on this VM;
  compilation NOT claimed.**
- Mechanism + the exact CI-proof list are documented in
  `docs/ROUND6_SHELL_CONFORMANCE.md` §8. First windows-latest build
  remains the authoritative proof (compile, installer contains the
  ~500 MB tree, installed first launch reaches READY via the resource
  copy, upgrade/uninstall handle the tree).

### R6-017 — PARTIALLY MOOTED by D5 (W2)
- D5 fix: `.github/workflows/release.yml` (release job) no longer
  fabricates a `backend-<version>.zip` url/sha256 for `backend.json`;
  the manifest now carries only fields for artefacts that exist
  (`backendVersion`, `signature`, `minShellVersion`, `ffmpegVersion`,
  `notes`), with an in-code note to re-add url/sha256 only alongside a
  step that builds the package. YAML re-validated (6 jobs parse).
- The disclosed empty `signature = ""` placeholder (separate backend
  minisign keypair) remains, now correctly read as a signature
  placeholder for a FUTURE backend package, not for an advertised
  asset. Wording in this register updated accordingly.

### R6-018 / G2 — RESOLVED (W4)
- `backend/app/middleware.py`: `LaunchTokenMiddleware` now accepts
  `Authorization: Bearer <token>` in addition to `X-Local-Token`
  (helpers `_bearer_token` :77, `_provided_tokens` :95; check at
  ~:185-215). Both candidates compared with `hmac.compare_digest`;
  documented precedence: either valid token authenticates (no
  ranking — the shell sends both with the same token). Non-Bearer
  Authorization counts as no token (`missing_token`). Semantics
  otherwise unchanged (401 missing/invalid as before).
- 6 new tests in `tests/test_auth_health.py`; the full loopback auth
  sweep (`test_round6_invariants_loopback_auth.py`, every OpenAPI
  operation × missing+invalid) still passes — Bearer opens no path.
  Contract documented in `docs/ROUND6_INVARIANTS.md` (Invariant 2,
  "Token header contract" block at :148).

### R6-F2 (invariants gap) — RESOLVED (W3)
- `DELETE /api/v1/channels/{id}` (`backend/app/routers/channels.py`,
  `disconnect_channel` :939) now calls
  `TokenManager.disconnect(youtube_channel_id)` (:984) — revoking at
  Google first, then deleting the local secret — BEFORE the unchanged
  soft-delete of the channel row. Channel resolved via `_find_channel`
  (blobs are keyed `youtube:channel:<youtube_id>`); store/HTTP
  resolved exactly as `revalidate_channel` does; client credentials
  are read-if-present but never required (no 409 can block a
  disconnect); a channel with no stored blob is the primitive's no-op
  (no HTTP, no error, row still soft-deleted).
- Gap test flipped to positive:
  `test_router_disconnect_revokes_then_deletes_secret_and_soft_deletes`
  (test_round6_invariants_disconnect_signing.py:193) asserts the
  ordered event sequence [revoke → delete], secret gone, row
  soft-deleted; plus a graceful no-credential test (:255) and the
  caller-existence test (:290). `__gap_r6_f3` characterisations
  untouched (F3 remains open by design).

### R6-F4 (invariants gap) — RESOLVED (W3)
- Local OAuth scope check before upload-path token use, using the
  connect code's own constant `SCOPE_UPLOAD`
  (`providers/youtube/oauth.py:50`, imported, never re-typed):
  `TokenManager.require_upload_scope()` (tokens.py:197) raises typed
  `NeedsReauth` when the blob is absent / marked needs_reauth /
  lacks the upload scope — purely local, zero HTTP.
- Enforcement points: `YouTubeClient.request` (client.py:143-156)
  for `videos.insert` before quota reservation and before `_send`;
  `ResumableUploader.upload` (:399), `initiate_session` (:296) and
  `_raw_put` (:569) via the `_require_upload_scope` helper
  (upload.py:258), which also refuses when no token manager/grant is
  wired at all. Non-upload reads are deliberately NOT gated (a
  readonly grant still reads).
- Gap test flipped to positive:
  `test_l5_out_of_scope_grant_is_refused_locally_on_upload`
  (test_round6_invariants_publish_gate.py:533) — readonly-only grant
  → `NeedsReauth` from both `client.request("videos.insert")` and
  `ResumableUploader.upload` with a counting MockTransport asserting
  ZERO HTTP calls; control: the same grant still reads
  (`videos.list`, 1 HTTP call). `__gap_r6_f5` untouched.

### G1 (shell report gap) — Updater UI wiring — RESOLVED (W5)
- `src/api/tauri.ts`: typed IPC for `check_for_updates` /
  `install_update` (fail-closed `UpdateCheckResult` normalisers),
  `autostartGet`/`autostartSet`, `getAppVersion()`; the old void stub
  replaced. New `src/api/updater.ts`: zustand store + singleton
  `startUpdaterListener()` mirroring the backend bootstrap, listening
  for `updater://update-available` (payload `{version, notes}` as
  emitted by `src-tauri/src/updater.rs`).
- New `src/components/shell/UpdateBanner.tsx` (dismissible,
  session-only dismissal, install errors surfaced inline, honest
  passive-flow copy), mounted in `BackendGate.tsx` under the degraded
  banner. Settings gains `UpdatesCard` + `AutostartCard`
  (`src/screens/Settings.tsx` :1388/:1538) in the existing
  stacked-card idiom, with the §11.9 opt-in/per-user note.
- Dev (non-Tauri) mode: listener is a no-op, banner can never render,
  cards disabled with explanatory text, no fake state. `npm run
  typecheck` + `npm run build` green; production bundle re-grepped —
  dev-fallback strings absent, updater/autostart symbols present.
- Register note: the idle gate is UI-side by design (install only on
  explicit click, with the "not while rendering/uploading"
  instruction); there is no programmatic queue-idle check before
  `install_update` — recorded as a possible future hardening, not a
  defect.

### G3 (shell report gap) — exit code 4 — RESOLVED (W4)
- The numeric guard already existed (`_refuse_if_schema_too_new`,
  `EXIT_SCHEMA_TOO_NEW = 4`, main.py:48, called from `run()` :493
  before `create_app()` and before any listening line). Two gaps
  closed in `backend/app/main.py`:
  (1) the literal marker — stderr now reads
  `fatal: database_newer_than_binary: …` (:390) before
  `SystemExit(4)`;
  (2) unknown/future revision strings escaped the numeric rule
  (`int()` fallback in backup.py read them as old) — added
  `_known_schema_revisions()` (:281, parsed from the shipped
  migration files) + `_existing_db_revision()` (:309); the guard
  (:353) now refuses when the recorded revision is numerically newer
  OR not in the binary's known revision set. Placement is before
  `init_db`, so nothing ever touches a newer DB; mechanism is the
  same `SystemExit(EXIT_*)` path as codes 2/3/5, proven at subprocess
  level (returncode 4). 5 new/extended tests in
  `tests/test_sidecar_protocol.py`.

### FFmpeg resolver adoption — COMPLETED (W6)
- `backend/app/system_check.py`: literal-"ffmpeg" check replaced by
  `app.ffmpeg_resolution` (import :46; resolve :210, `probe_loudnorm`
  :248). The measured-LUFS reporting the wizard asserts is preserved
  (probe_loudnorm reports filter presence; the production LUFS parse
  path is kept, now fed the resolved binary — see :259 comment).
  External result shapes unchanged.
- `backend/app/style_analysis/*`: all execution defaults migrated —
  `visual_classify.py:71`, `speech.py:77`, `scenes.py:96`,
  `media.py:80`, `dressing.py:79`, `engine.py:66` import
  `ffmpeg_executable`/`ffprobe_executable` from
  `..ffmpeg_resolution`; `backend/app/style_lab/service.py:95`
  imports `ffprobe_executable`. Remaining `"ffmpeg"`/`"ffprobe"`
  string literals in scenes.py (:270/:405/:548) are runner tool-name
  labels passed to `_run(...)`, not executables — deliberately left.
- /tmp fallbacks: zero `/tmp/voice` / `/tmp/visuals` literals remain
  in backend/app. The ctx-less fallbacks in
  `integration/voice_stage.py:117-121` and
  `integration/visuals_stage.py:280-284` now resolve to
  `<data dir>/tmp/<name>` via `app.data_dir.data_dir_layout().tmp_dir`;
  behaviour with a StageContext present is unchanged. Proven by
  `tests/test_stage_ctx_less_fallback.py` (4 passed) with
  `YTA_DATA_DIR` pointed at a temp location.
- W6 slices (serial): onboarding system-check + ffmpeg_resolution 44
  passed; ctx-less fallback 4 passed; stylelab/style_lab slices 360
  passed; `tests/integration/` 347 passed / 1 skipped; final combined
  re-run 78 passed. Total distinct slices: 755 passed, 1 skipped,
  0 failed.

### Gate B evaluator divergence (freeze-smoke finding) — RESOLVED (W7)
- Characterisation (corrects the smoke's framing): there was ONE
  evaluator, but the two paths fed it different inputs/context and
  labelled its result differently.
  (1) Provenance: only the package path canonicalised licence-row
  provider `local_plate` → `generated` (Tier-1) before
  `finalize_publish`; the finaliser passed raw rows, `local_plate`
  had no tier entry → `PROVENANCE_TIER_UNKNOWN` → G2 BLOCK
  (`G2_PROVENANCE_FAILED`) → `VERDICT_NOT_PASS`. Reproduced exactly
  pre-fix.
  (2) Conditional context: the finaliser called `finalize_publish`
  with no `mode` (default review_first), no `gate_b_approval`, and
  QC info stripped of check codes — an approved conditional
  re-blocked with `CONDITIONAL_APPROVAL_REQUIRED`, and a minor-only
  WARN couldn't be classified conditional at all.
  (3) Labelling: package surfaces copied only the bare
  `gate_b_passed` bool; finaliser refusals joined only problem codes,
  losing the typed token reason.
- Fix: canonicalisation moved INTO the shared finalize
  (`integration/publish_stage.py:269-282`, applied :447;
  `local_plate` added to `_PROVIDER_TIERS`); both surfaces derive
  labels from the same `GateBPolicyResult` via new shared helpers
  `gate_b_verdict_label()` / `collect_gate_b_reason_codes()`
  (`pipeline/gates/aggregator.py:463,486`) → verdicts
  `pass | conditional_pass | conditional_blocked | block` with
  reason codes (token reasons first). `PublishResult` +
  frozen-manifest `gate_b` block carry `verdict` + `reason_codes`.
  The finaliser (`execution/publish_finaliser.py`) now passes the
  project's publish_mode, the recorded Gate B approval, and full QC
  checks; refusals report verdict + full reason codes. The package
  stage (`stage_bridge.run_package`) passes the project mode and
  honestly labels a pending conditional; report.md renders e.g.
  `PASS — conditional_pass [BLACK_SEGMENT_BRIEF]` — never a bare
  PASS when a label exists. Round-4 semantics preserved: conditional
  still needs human approval in review mode and always blocks
  full-auto; the finaliser remains the publish-time authority
  applying the SAME evaluator.
- Proof: new `tests/integration/test_gate_b_package_finaliser_alignment.py`
  (5 tests: provenance agreement incl. the actual smoke registry,
  conditional label/release agreement, auto-mode block agreement,
  report-surface labelling). W7 slices: 326 passed, 0 failed.
- Known remainder: the monolith `produce.py` path still calls
  `finalize_publish` without mode/approval and surfaces only the
  bool in its result model — it benefits from the shared
  canonicalisation and the labelled manifest, but its in-memory
  result is not yet label-carrying. The DAG package stage + finaliser
  (the smoke path) are fully aligned. Recorded as a follow-up, not a
  divergence: the finaliser's verdict can no longer contradict the
  frozen manifest either path produced.

### Closure test totals (per worker, serial, standalone-classified)
- W1: execution_approvals 9, style_lab_schema 14, backup_restore 9,
  schedules slices 45 → 77 passed.
- W2: test_render + test_render_quality_tiers 29 passed (the exact 29
  formerly skip-listed); windows-test-skips.txt reduced to
  comment-only (loader simulated: 0 entries).
- W3: invariants disconnect+publish-gate 45, channels slices 48,
  youtube provider slices 50, publishing 20, safety slice 40 →
  203 passed.
- W4: auth_health + sidecar_protocol 41, invariants loopback +
  disconnect 21, final combined 62 passed (slices overlap; combined
  figure is the clean one).
- W5: UI typecheck + build green (2076 modules; only the pre-existing
  chunk-size warning).
- W6: 755 passed, 1 skipped (see above).
- W7: 326 passed (see above).
- No worker ran the FULL backend suite (closure discipline was
  per-slice serial runs on a shared, intermittently EPERM-faulting
  box); a full serial regression on the closure tree remains the
  parent's pre-commit step.
