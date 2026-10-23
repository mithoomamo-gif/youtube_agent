# ROUND 6 — CI Release Pipeline Dry-Run Rehearsal Report

**Team:** CI Release Pipeline (dry-run worker) · **Date:** 2026-10-23 · **Repo:** `~/workspace/youtube-agent-app`, git `master`, HEAD `c99e10e090a156979c118c422b9883bc22735289`
**Rule honoured throughout:** only what genuinely runs on this Linux VM was rehearsed. No Windows step is claimed to have run here. Nothing was committed.

## Environment notes (read first — they explain the timings)

- Node `v24.22.1`, npm `10.9.4`, Python `3.12.3`. Test interpreter is `backend/.venv/bin/python` (system `python3` has **no pytest** — first attempt failed verbatim with `/usr/bin/python3: No module named pytest`).
- **The VM restarted mid-rehearsal** (an `uptime` check at 12:00 UTC showed "up 4 min") and ran at **load average ~24** throughout because 30+ sibling Round 6 agents share this machine. Wall-clock timings below vary wildly for that reason; exit codes and outputs are the evidence, not the seconds.
- **The main working tree changed underneath this rehearsal.** It was clean at HEAD when I started; sibling Round 6 workers then landed extensive uncommitted work in it (new `packaging/`, `CHANGELOG.md`, migration `0011`, generated icons, a rewritten `release.yml`, a version-reference change in `tauri.conf.json`, …). Where pristine HEAD and the current main tree differ, both states are reported and labelled. I touched none of the sibling files.

---

## 1. Clean-checkout simulation — PASS

- `git worktree add ~/workspace/youtube-agent-app-r6dryrun HEAD` → detached HEAD at `c99e10e090a156979c118c422b9883bc22735289`.
- **`npm ci` — exit 0.** Verbatim tail:
  ```
  added 99 packages in 2m
  18 packages are looking for funding
    run `npm fund` for details
  ```
  (Shell-measured 99 s on that attempt; npm's own summary rounds to "2m". Network/cache allowed a real clean install — no fallback to the main tree was needed.)
- **`npm run build` (= `tsc -b && vite build`) — exit 0 in every run.** Attributable timed re-run (python `subprocess`, wall clock): **179.6 s**, vite step itself `✓ built in 4.46s`. One additional delivered build log showed vite `✓ built in 4.32s`; a further run under peak contention took 316 s wall with vite at 2m 14s. Output tail (timed run):
  ```
  vite v6.4.5 building for production...
  ✓ 2067 modules transformed.
  dist/index.html                   0.49 kB │ gzip:   0.31 kB
  dist/assets/index-BIF93l_a.css   36.08 kB │ gzip:   7.14 kB
  dist/assets/core-BUhCcD2K.js      2.70 kB │ gzip:   1.11 kB
  dist/assets/window-9QKMM9Hf.js   15.46 kB │ gzip:   3.90 kB
  dist/assets/index-BQ_kwAej.js  1,110.60 kB │ gzip: 302.64 kB
  (!) Some chunks are larger than 500 kB after minification. Consider: …
  ✓ built in 4.46s
  ```
  `dist/` was produced in the worktree (`index.html` + `assets/`). The >500 kB chunk warning is pre-existing and non-fatal.
  *Addendum (late duplicate delivery):* the runtime delivered a second execution of the same timed build command after this report was first written — again **exit 0**, 321.3 s wall, vite `✓ built in 1m 56s`, main chunk `index-Ci-cXtBD.js` at 702.65 kB, `dist/` total 744K on disk. Chunk size/hash and timings therefore vary between executions under this VM's load and restart churn; the invariant across every execution is exit 0, 2067 modules transformed, and a complete `dist/`.
- **Worktree removed when done:** `git worktree remove` exit 0; `git worktree list` afterwards shows only the main tree. Final tree is clean of the worktree.

## 2. Preflight — RAN (exit 1: 4 passed, 2 failed), verbatim

`packaging/preflight.sh` did **not** exist at rehearsal start; it landed from the sibling packaging worker while other steps ran. I ran it twice in the main tree; output was identical both times, verbatim:

```
YouTube Agent — release preflight
Root: /home/hatch/workspace/youtube-agent-app
Target version: ../package.json
----------------------------------------------------------------
[FAIL] a. Version consistency — tauri.conf.json='../package.json' but mismatched: Cargo.toml='0.1.0' package.json='0.1.0' backend/app/__init__.py __version__='0.1.0' backend/pyproject.toml='0.1.0'
[PASS] b. Icon set present — every bundle.icon file exists and is non-empty
[PASS] c. Updater pubkey + endpoints — pubkey is non-empty and all updater endpoints are https:// on non-placeholder hosts
[PASS] d. Migration chain single head — single migration head: 0011 (chain of 11 revisions, 1 root, no duplicates or dangling links)
[FAIL] e. Changelog entry — CHANGELOG.md has no '## [../package.json]' (or '## ../package.json') heading
[PASS] f. NSIS target + per-user install mode — bundle.targets includes 'nsis' and bundle.windows.nsis.installMode = 'currentUser'
----------------------------------------------------------------
Summary: 4 passed, 2 failed
Preflight FAILED — resolve the FAIL item(s) above before releasing.
exit code: 1
```

How to read this (important — it differs from the at-HEAD expectation):

- The rehearsal brief expected icons/pubkey to FAIL. At **pristine HEAD** they would have: `src-tauri/icons/` contained only `README.txt`, updater `pubkey` was `""`, and the sole endpoint was `https://example.invalid/releases/latest.json`. By the time preflight existed and ran, sibling workers had landed generated icons and a real updater configuration in the working tree (endpoints on `updates.youtubeagent.app` + GitHub Releases fallback; pubkey non-empty, length 152), so checks **b** and **c** PASS in the main tree as it now stands.
- Both FAILs have **one root cause**: the sibling shell work set the top-level `version` in `src-tauri/tauri.conf.json` to the Tauri path-reference string `"../package.json"`. Preflight compares that string literally, so (a) reports a "mismatch" even though every literal mirror agrees at `0.1.0` (`Cargo.toml`, `package.json`, `backend/app/__init__.py __version__`, `backend/pyproject.toml`), and (e) looks for a `## [../package.json]` changelog heading even though `CHANGELOG.md` exists with `## [0.1.0] — pending (not yet released)`. **Fix belongs in preflight (resolve the `../package.json` reference before comparing) or in using a literal version in `tauri.conf.json`.** This is a real release-gate bug the rehearsal exists to catch — not a rehearsal failure.

## 3. Workflow parse — PASS (with the YAML 1.1 `on:` gotcha confirmed)

Both files load with `yaml.safe_load` (PyYAML), in pristine-HEAD and main-tree copies alike.

- **The `on:` gotcha is real:** in both files the triggers key parses as the **boolean `True`**, not the string `"on"` (YAML 1.1). Top-level keys: `['name', True, 'concurrency', 'permissions', 'jobs']`. Any tooling that looks up `data["on"]` will silently find nothing; GitHub's own parser handles it correctly, so the workflows are valid — but local parsers must use `data[True]`.
- Pristine HEAD `release.yml`: name `Release`; jobs `['check-secrets', 'release']`; triggers (under `True`) `['workflow_dispatch', 'push']`.
- Pristine HEAD `ci.yml`: name `CI`; jobs `['python', 'frontend', 'tauri-build-check', 'secret-scan']`; triggers `['push', 'pull_request', 'workflow_dispatch']`.
- Main tree `release.yml` (rewritten by a sibling during the rehearsal): jobs `['check-secrets', 'preflight', 'verify', 'build', 'sign', 'release']` — also parses OK, same `True`-key behaviour. `ci.yml` unchanged in structure.

## 4. Sidecar freeze handoff point — RECORDED (consume-only; no freeze script written by me)

- **Existence:** at rehearsal start `packaging/build_sidecar.ps1` was **absent** ("pending sibling deliverable", expected path `packaging/build_sidecar.ps1`). During the rehearsal the freeze team landed it, plus `packaging/build_sidecar.sh` (Linux mirror), `packaging/yta_sidecar.spec`, and `packaging/preflight.ps1` / `preflight.sh`. Final state: **present — no longer pending.** I only read these files.
- **Exact CI invocation** (current main-tree `.github/workflows/release.yml`, job `build`, step `Inside-out 2/5 — Build Python sidecar via packaging/build_sidecar.ps1 (PyInstaller --onedir)`, `shell: pwsh`, env `RELEASE_VERSION: ${{ steps.version.outputs.version }}`):
  ```powershell
  if (-not (Test-Path "packaging/build_sidecar.ps1")) {
    Write-Host "::error::packaging/build_sidecar.ps1 is missing. It is a required release dependency (owned by the freeze team); there is no sidecar to bundle without it."
    exit 1
  }
  & ./packaging/build_sidecar.ps1
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  ```
  No arguments, repo root; a missing script is now a hard release error.
  (At pristine HEAD, `release.yml` instead inlined PyInstaller: first existing candidate of `backend/agent-backend.spec`, `backend/main.py`, `backend/app/main.py`, `backend/__main__.py`, run as `pyinstaller --noconfirm --clean --onedir --name agent-backend <entry>`, staging `dist/agent-backend*.exe` to `src-tauri/binaries/agent-backend-x86_64-pc-windows-msvc.exe`.)
- **Expected output artefact:** `packaging/yta_sidecar.spec` is run with `--distpath packaging/dist-sidecar --workpath packaging/build-sidecar`; the PyInstaller `--onedir` output directory is **`packaging/dist-sidecar/yta_sidecar/`** containing the `yta_sidecar` executable (`yta_sidecar.exe` on Windows) plus `_internal/`. `build_sidecar.ps1` then copies `prompts/` beside the executable (spec hazard H1), stamps `VERSION` + `build-info.json` into the bundle, and stages for Tauri as `src-tauri/binaries/agent-backend-x86_64-pc-windows-msvc.exe` plus the complete tree under `src-tauri/binaries/agent-backend/`.
- **Backend entry point CONFIRMED:** `backend/app/main.py` exists at pristine HEAD and in the main tree — FastAPI factory + process entry, equivalent to `python -m app.main` / console script `app.main:run`.
- **Port/token contract CONFIRMED documented:** `backend/app/main.py` docstring — bind `127.0.0.1` only (exit 2 on non-loopback), missing `YTA_TOKEN` → exit 5, port-bind failure → exit 3, exactly one stdout protocol line `{"event": "listening", "port": <port>, "boot_id": "<boot_id>"}`; `docs/DEV_SETUP.md` — port via `YTA_PORT`, token via `YTA_TOKEN` env only (never CLI/file/URL), sent as the `X-Local-Token` header on every `/api/v1/*` request, WebSocket via `POST /api/v1/ws-ticket` → single-use 60 s ticket sent as the first WS frame (else close `4401`). The spec header adds `YTA_BOOT_ID` / `YTA_DATA_DIR` to the env the shell passes.
- The freeze itself was **not** executed by me (consume-only scope; the `.ps1` header itself states it was written on a Linux VM where PowerShell is not available and has never been executed on this machine).

## 5. Migration head — SINGLE HEAD CONFIRMED (parse-based)

Method: regex-parsed the **annotated** assignments (`revision: str = "…"` / `down_revision: … = "…"`) in `backend/migrations/versions/*.py` — a plain `revision =` regex matches nothing in this codebase. (A clean `alembic heads` CLI capture could not be obtained during the rehearsal due to the VM restart/session timeouts; the parse below plus preflight check d are the evidence.)

- **Pristine HEAD (worktree): 10 revisions, single head `0010`.** Chain tail: `0010 → 0009 → 0008 → 0007 → 0006 → 0005 → 0004 → 0003 → 0002 → 0001` (root). Matches the expected head `0010` exactly.
- **Main working tree (current): 11 revisions, single head `0011`** — sibling-added `0011_compliance.py` (`down_revision = "0010"`), chain `0011 → 0010 → … → 0001`, independently corroborated by preflight check d (PASS, quoted in §2).

## 6. Regression spot-check — RUN (numbers below; attribution included)

Root suite, post-VM-restart run, log read directly from disk
(`backend/.venv/bin/python -m pytest tests -q -p no:cacheprovider --basetemp=.tmp-yta/r6dry/base`, `TMPDIR` under `.tmp-yta/`):

> **1 failed, 41 passed, 1 skipped, 2 errors in 250.39 s** (45 collected — same total as the Round 5 baseline of 44 passed + 1 skipped)

- `SKIPPED` — `tests/golden/test_golden_datasets.py:105`, the known seed-stage gate skip. Expected.
- `ERROR` ×2 — `tests/integration/test_approvals_persistence.py::test_migration_0002_applied_in_booted_db` and `::test_approval_decision_persists_across_restart`, both in the shared module fixture: the real sidecar booted and answered (`/health/live` 200, `/health/ready` 200, `POST /api/v1/niches` 201, `POST /api/v1/projects` 201, and the captured log shows `pipeline/start` returning **202 Accepted**), then the test client hit `httpx.ReadTimeout` reading under load.
- `FAILED` ×1 — `tests/integration/test_project_execution_ws.py::test_project_execution_and_websocket_events`: same pattern (channels 200, ws-ticket 200, projects 201, then `ReadTimeout` on the `pipeline/start` POST).
- Two earlier pre-restart runs of the same suite landed in the same place (41 passed / 2 failed / 1 skipped in 149.61 s; 40 passed / 1 skipped / 4 errors in 285.20 s), every non-pass in these same real-sidecar integration files and every unit/contract/golden/fault-corpus test passing in every run. **Attribution: load/timeout flake on a VM at load ~24 that also restarted mid-rehearsal — not a code regression in the rehearsed tree.** These three tests should be re-run on a quiet machine before anyone treats them as signal.

Backend subset (`backend/tests -k 'migration or schema'`, same venv/basetemp discipline):

> **3 failed, 103 passed, 2343 deselected (106 selected) in 231.00 s**

- `tests/test_execution_approvals.py::test_migration_0002_upgrade_and_downgrade` and `tests/test_style_lab_schema.py::test_migration_0004_upgrade_from_empty_and_downgrade` — both fail with `AssertionError: assert '0011' == '0010'`: they hard-expect the final migration head to be `0010`, and the sibling-added `0011_compliance` migration in the main tree moved it. **Pristine HEAD is unaffected; the migration owner needs to update these two expectations as part of landing 0011.**
- `tests/test_round6_invariants_secrets.py::test_secrets_store_never_persists_to_sqlite_schema` (a new Round 6 sibling test) — asserts `'keyring_ref' in` the `provider_credentials` DDL; the current schema DDL has no such column. An intra-round mismatch between sibling test and sibling schema work for the parent to route — not caused by, and not fixable within, this rehearsal.

Full backend suite was not run (out of scope for this rehearsal, and not meaningful at load ~24).

---

## Steps that could NOT be rehearsed here (Windows-only / not run)

- Executing `packaging/build_sidecar.ps1` (PowerShell on Windows) and the PyInstaller freeze producing a Windows `.exe`.
- `tauri build` / `cargo check` of the Rust shell (no Rust toolchain on this VM).
- Authenticode signing (`signtool`, Azure Artifact Signing or OV/HSM), Tauri updater minisign signing, `latest.json` / `backend.json` manifest emission and signature verification.
- NSIS installer build, per-user install, silent (`/S`) install/uninstall.
- The clean-VM install/upgrade/uninstall matrix on Windows 10 and Windows 11 (`docs/RELEASE.md` §1.3), WebView2 bootstrapper behaviour.
- VirusTotal submission, Microsoft malware-analysis submission, SmartScreen behaviour.
- GitHub Actions execution itself — both workflows were parsed locally only, never run on a runner.

## Deliverable state

- This report is the only file I created: `docs/ROUND6_DRYRUN.md` (uncommitted, as instructed — **no commits made**).
- The dry-run worktree `~/workspace/youtube-agent-app-r6dryrun` has been removed; `git worktree list` shows only the main tree.
- All other uncommitted changes visible in the tree are sibling Round 6 workers' work, left untouched.

---

## Post-rehearsal addendum — 2026-10-23 (FIX pass, after the rehearsal above)

> This addendum was appended after the rehearsal by the CI Release Pipeline
> FIX worker. The original rehearsal text above is unchanged.

The validation + rehearsal passes found real defects in the release
pipeline files; the following fixes were applied (working tree only,
nothing committed):

- **D1 (HIGH) — `release` job version wiring (`.github/workflows/release.yml`):**
  the `release` job used `${{ needs.build.outputs.version }}` but declared
  only `needs: [sign, check-secrets]`; the `needs` context contains only
  DIRECT dependencies, so the version resolved empty. Fixed: the job now
  declares `needs: [build, sign, check-secrets]`.
- **D2 (MEDIUM) — build-job version fallback (same file):** the fallback read
  `tauri.conf.json`'s `version` literally, which is now the Tauri path
  reference `"../package.json"`. Fixed: a value starting with `../` or
  ending in `.json` is resolved by reading the referenced file's `version`
  (path relative to `src-tauri/`), keeping tag → workflow input → config
  order; the semver sanity check now **throws** on an empty or
  non-semver resolved version instead of warning through.
- **D3 (MEDIUM) — signing probes (same file, `check-secrets` + the `sign`
  job's re-probe):** Azure now counts as configured only when ALL six
  `AZURE_ARTIFACT_SIGNING_*` secrets are present (endpoint, account,
  profile, tenant, client id, client secret — the three identity secrets
  are now also mapped into both probe steps' `env:`), and the OV path
  only when `WINDOWS_CERT_BASE64_PFX` **and** `WINDOWS_CERT_PASSWORD` are
  present (a thumbprint alone cannot sign in CI). Partial configuration
  reports 'not configured' and surfaces as the loud UNSIGNED path.
  Probes remain presence-only; no values are printed.
- **Loader fix (same file, `verify` job):** the Windows skip-list loader
  treated each whole line of `packaging/windows-test-skips.txt` as a node
  id, but the file format is `<node-id>  # <reason>`, so no entry could
  ever match. Fixed: the loader strips everything from the first ` #`
  onward (and still skips blank / pure comment lines) before matching;
  the stale-entry warning behaviour is unchanged. A mirror simulation of
  the new loader logic yields exactly 29 clean node ids from the real
  file, none containing `#`.
- **Preflight indirection fix (`packaging/preflight.sh` +
  `packaging/preflight.ps1`, same semantics):** both scripts now resolve
  the Tauri version indirection — when `tauri.conf.json`'s version ends
  in `.json`, the referenced file (relative to `src-tauri/`) is read and
  its version is used as the tauri version everywhere (default target +
  check a). A missing/unparseable referenced file fails check (a) with
  that reason. All other checks are unchanged in behaviour.

**Post-fix `preflight.sh` run on the real tree — verbatim (exit 0, 6/6):**

```
YouTube Agent — release preflight
Root: /home/hatch/workspace/youtube-agent-app
Target version: 0.1.0
----------------------------------------------------------------
[PASS] a. Version consistency — tauri.conf.json, Cargo.toml, package.json and backend version sources all = 0.1.0
[PASS] b. Icon set present — every bundle.icon file exists and is non-empty
[PASS] c. Updater pubkey + endpoints — pubkey is non-empty and all updater endpoints are https:// on non-placeholder hosts
[PASS] d. Migration chain single head — single migration head: 0011 (chain of 11 revisions, 1 root, no duplicates or dangling links)
[PASS] e. Changelog entry — CHANGELOG.md has a heading for version 0.1.0
[PASS] f. NSIS target + per-user install mode — bundle.targets includes 'nsis' and bundle.windows.nsis.installMode = 'currentUser'
----------------------------------------------------------------
Summary: 6 passed, 0 failed
Preflight PASSED — all checks green for version 0.1.0.
exit code: 0
```

Negative controls re-run post-fix (both still fail loudly, as designed):
wrong version argument (`./packaging/preflight.sh 9.9.9`) → check (e)
FAILs, summary 5 passed / 1 failed, exit 1; fixture tree
(`YTA_PREFLIGHT_ROOT`) with a branched migration (`0099`,
`down_revision = "0010"`) → check (d) FAILs with "expected exactly 1
migration head, found 2: 0011 0099", exit 1. A third fixture with
`tauri.conf.json` referencing a missing file fails check (a) with the
resolve reason. `preflight.ps1` carries the identical fix but could not
be executed here (no PowerShell on this Linux VM); it was verified by
inspection and delimiter balance. `release.yml` re-validated with
`yaml.safe_load`: parses OK; needs graph — verify: `[preflight]`,
build: `[verify, check-secrets]`, sign: `[build, check-secrets]`,
release: `[build, sign, check-secrets]`.
