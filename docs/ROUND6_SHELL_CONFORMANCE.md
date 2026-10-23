# Round 6 — Tauri Shell Conformance (Validation + Integration)

**Scope:** the Tauri 2 shell (`src-tauri/`) against the Master Roadmap
(`~/workspace/youtube-windows-agent/MASTER_ROADMAP.md`) §4.5, §4.6, §4.7 and
Part 11 (§11.2, §11.4, §11.7), plus the fixed shell↔frontend IPC contract.

**Environment caveat (read first):** this validation ran on the Linux build
VM, which has **no Rust toolchain**. No Rust file in this round has been
compiled anywhere yet. Every Rust verdict below is a line-by-line static
review against Tauri 2 / plugin / windows-sys API documentation (docs.rs),
not a compiler verdict. Items that only a Windows CI build can settle are
listed explicitly in §6 — the first `cargo build` on a Windows runner is a
hard gate, not a formality.

Baselines at validation time (settled tree): `npm run typecheck` **pass**,
`npm run build` **pass**, `node scripts/check-versions.mjs` **pass**
(dev and `--release`), `tauri.conf.json` validates with **0 errors**
against the official schema (`https://schema.tauri.app/config/2`,
schema id `…/config/2.12.5`, fetched live).

---

## 1. Requirement map

### §4.5 Launch-token security

| Requirement | Implemented where | Statically verified how |
|---|---|---|
| Token: 32 CSPRNG bytes per launch, base64url (43 chars) | `src-tauri/src/sidecar.rs:336` `generate_launch_token` (rand `thread_rng` = OS CSPRNG, hand-rolled base64url) | Read: 32-byte buffer, encoder emits 43 chars for 32 bytes; minted fresh in `spawn_sidecar` for **every** (re)start |
| Fresh `boot_id` per (re)start | `sidecar.rs:346` `generate_boot_id`; pushed to WebView in `BackendStatus.boot_id` | Read spawn + status code paths |
| Shell → sidecar via **environment only** (`YTA_TOKEN`), never argv / file / URL | `sidecar.rs:1224` `spawn_sidecar`: token inserted into the spawn-call env map only; argv carries only the non-secret `--scheduled-run` / `--dry-run` flags | Grep of whole `src-tauri/src`: token appears in no argv construction, no file write, no URL. PID file (`write_pid_file`) contains pid/boot_id/exe/start-time only |
| Shell → WebView via Tauri IPC from managed state; never in bundle / localStorage | `src-tauri/src/lib.rs:172` `get_backend_connection -> Option<BackendConnection>`; frontend `src/api/tauri.ts` holds it in module memory only | Read both sides; grep found no `localStorage` persistence of the token in `src/` |
| Token never logged/printed | — | Grep: no `println!`/`eprintln!`/log-format line in `src-tauri/src/*.rs` includes a token value; supervisor log lines record port + boot_id + status only. Sidecar stdout/stderr are tee'd to the rotating log verbatim — see Honest Gaps (§7.3) |
| Old token dies with its process; no renewal path | `sidecar.rs` `handle_terminated`/`become_ready`: connection cleared (`publish_connection(None)`) on any termination; new spawn mints anew | Read state-machine paths |
| Validation (constant-time compare, 401 counting, Host/Origin/CORS, loopback-only bind) | **Backend-owned** (FastAPI middleware, Rounds 1–5); shell side contributes: literal `127.0.0.1` URLs everywhere (`become_ready`, `mini_http.rs`), Host header pinned to `127.0.0.1:<port>` (`src-tauri/src/mini_http.rs`) | Read `mini_http.rs` (token sent as `Authorization: Bearer` **and** `X-Local-Token`, matching the shipped middleware) |

### §4.6 Sidecar lifecycle

| Requirement | Implemented where | Statically verified how |
|---|---|---|
| State machine STOPPED → SPAWNING → STARTING → READY ⇄ DEGRADED → DRAINING → STOPPING → STOPPED, CRASHED → restart | `sidecar.rs` `Phase` enum (~L965) + `Supervisor` (~L1000); public states emitted via `set_status` (~L1111) | Read full supervisor; UI state strings match the frontend union in `src/api/tauri.ts` exactly |
| Single-instance first; second launch focuses existing window, never spawns a second sidecar | `lib.rs:216` — `tauri_plugin_single_instance` is the **first** plugin registered; `--scheduled-run` second launches return early (no window pop) | Read builder chain order |
| Orphan sweep: kill leftover only if exe path **and** start time match the PID record (PID-reuse safe) | `sidecar.rs:622` `orphan_sweep`, `:406`-area PID record helpers | Read: both matches required, recorded exe must look like the sidecar, record always discarded afterwards |
| Port: bind-test `127.0.0.1:0`; exit 3 → fresh port | `spawn_sidecar` (TcpListener bind/drop); `handle_terminated` code-3 arm | Read |
| Env contract: `YTA_PORT`, `YTA_TOKEN`, `YTA_BOOT_ID`, `YTA_DATA_DIR`, `YTA_PARENT_PID`, `YTA_DETACHED` (headless/scheduled only), `YTA_LOG_LEVEL` | `spawn_sidecar` env map | Read: all seven present; `YTA_DETACHED=1` only when `--headless` or `--scheduled-run` |
| Spawn via Tauri `externalBin`; stdout/stderr → rotating logs 10 MB × 5; one structured startup line | `sidecar.rs` `RotatingLog` (~L470), spawn via `app.shell().sidecar("agent-backend")`, listening-line verification `check_listening_line` (~L1410) | Read; `tauri.conf.json bundle.externalBin = ["binaries/agent-backend"]` |
| Readiness: poll `/health/live` 100 ms ×2 backoff → `/health/ready` with token; **READY also requires the verified listening line**; 30 s cold-start budget | `sidecar.rs:1674` `poll_readiness`, `:1727` `become_ready`, timer arm in `tick_timers` | Read |
| Steady state: re-probe every 10 s; 1 failure nothing, 2 = DEGRADED, 3 = restart | `sidecar.rs:1751` `probe_steady` | Read |
| Exit codes: 0 unexpected = crash · 2 no restart · 3 fresh port · 4 no restart + `database_newer_than_binary` · 5 no restart · other = crash | `sidecar.rs:1496` `handle_terminated` | Read arm by arm against §4.6 |
| Restart policy: backoff 1 s → 2 s → 5 s, max 3 in 5 min, then stable FAILED | `sidecar.rs:1588` `schedule_restart` (sliding window, `RESTART_BACKOFFS`) | Read |
| Restart ⇒ new port + new token + new boot_id; shell pushes `backend://connection-changed`; WebView resyncs by snapshot | `become_ready` publishes + emits; frontend `src/api/bootstrap.ts` subscribes and re-fetches | Read both sides; event names verified: `backend://status-changed`, `backend://connection-changed` |
| Shutdown sequence: `POST /api/v1/system/shutdown` → `SHUTDOWN\n` stdin fallback → 10 s budget → Job Object close (final) / child kill | `sidecar.rs:1819` `graceful_stop`; initiators: tray Quit (`tray.rs` ID_QUIT, synchronous `control_shutdown` at `sidecar.rs:243`), `RunEvent::ExitRequested` backstop (`lib.rs:311`) | Read all three paths; stdin write uses `CommandChild::write` via `as_mut` ✓ |
| Windows Job Object: whole tree in a job with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` owned by the shell | `sidecar.rs:853` `JobObject` (created lazily, assigned per spawn) — **all code `#[cfg(windows)]`-gated**, no-op stub elsewhere | Read; see Blocker fix F3 in §4 — the windows-sys handle code as first landed would not have compiled on Windows |
| Parent watchdog (sidecar-side, `YTA_PARENT_PID`) | Shell supplies the env var; watchdog itself is backend behaviour | Env read verified; behaviour is CI/backend-side |
| Window never shows a working dashboard against a non-READY backend | Window `visible: false` in `tauri.conf.json`; revealed by supervisor only on first READY/FAILED (`set_status`), suppressed entirely for scheduled-run launches (`suppress_window_reveal`) | Read |
| Tray residency: window close hides to tray, does not quit | `lib.rs` `on_window_event` CloseRequested → prevent + hide; tray menu Open / Pause All / STOP ALL / Quit (`src-tauri/src/tray.rs`) | Read |
| Pause/stop all jobs | Commands `lib.rs:196`/`:204` → `sidecar::control_pause_all`/`control_stop_all` → persisted backend execution flags via `PUT /api/v1/settings` (`apply_execution_flag`, `sidecar.rs:300`) — **gap-driven**: no dedicated REST endpoint exists and the WS command door is unwired in the backend; the settings surface is the one real path (documented in the function) | Read + backend `routers/settings.py` cross-check by the lifecycle worker |
| Scheduled-run wake (§11.9): Task Scheduler `--scheduled-run` one-shot, then normal server sidecar | `Supervisor::new` argv parse; one-shot spawn carries the flag in argv, no readiness polling / no cold-start kill (`tick_timers` guard), termination hands over to the normal spawn (`handle_terminated` scheduled arm); packaging side: `packaging/register_scheduler.ps1` | Read both sides of the contract |

### §4.7 Versioning / handshake

| Requirement | Implemented where | Verified how |
|---|---|---|
| Handshake first: `GET /api/v1/system/info` → app/backend/api/ws versions, `min_ui_api`, `boot_id`, capabilities | Frontend `src/api/bootstrap.ts` + `src/api/client.ts` (`toSystemInfo`, floor check, out-of-sync screen via `BackendGate`) — shell's part is delivering `boot_id` + connection events | Read frontend normalisers (accept snake_case wire shapes) |
| Capability flags, not version arithmetic | Frontend consumes `capabilities[]` from system/info | Read |
| App version coherence (installer / manifest / Cargo) | `scripts/check-versions.mjs`: package.json is the source of truth; `tauri.conf.json` version is the `"../package.json"` pointer; Cargo `[package]` version must match; identifier pinned | **Ran it**: all PASS, dev and `--release` (output in §5) |

### Part 11 — packaging / updater / uninstall

| Requirement | Implemented where | Verified how |
|---|---|---|
| §11.2 NSIS per-user, lzma, Start Menu folder, English, embedBootstrapper, `minimumWebview2Version` | `tauri.conf.json bundle.windows` | JSON read + live schema validation (0 errors) |
| §11.2 installer hooks via `installerHooks: windows/hooks.nsh` | `src-tauri/windows/hooks.nsh` defines all four macros: `NSIS_HOOK_PREINSTALL` (prompt-then-close running app), `POSTINSTALL` (VC++ redist remediation, staged exe, never silent-fail), `PREUNINSTALL` (stop processes, delete autostart value + `\YouTubeAgent\` scheduled tasks **before** file deletion), `POSTUNINSTALL` (data prompt) | Read the whole file line by line |
| §11.2 on-disk layout / externalBin naming | `src-tauri/binaries/README.md` staging contract (target-triple name `agent-backend-x86_64-pc-windows-msvc.exe`, PyInstaller `--onedir`, inside-out signing order) | Read; `.gitignore` now covers the staged exe (fix F5) |
| §11.4 Updater: endpoints in order (primary CDN manifest with `{{target}}/{{arch}}/{{current_version}}` templating → GitHub Releases fallback), `windows.installMode: passive` | `tauri.conf.json plugins.updater` | Read; both endpoints HTTPS; domains are documented placeholders until the release CDN/repo exist (`src-tauri/keys/UPDATER_CONFIG.md`) |
| §11.4 minisign pubkey embedded; private key only in CI secrets + escrow | Pubkey merged into `tauri.conf.json` (152 chars, byte-identical to `src-tauri/keys/updater-pubkey.txt` and to the fragment in `keys/UPDATER_CONFIG.md`); custody in `docs/UPDATER_KEY_RUNBOOK.md` | Compared strings programmatically; secret-hygiene sweep in §5 |
| §11.4 policy: check on launch (30 s defer) + every 6 h, background never installs, install only via user/idle-gated command, monotonic-version guard, fail-closed on any verification/manifest error | `src-tauri/src/updater.rs`: `init` (:316), `check_for_updates` (:220), `install_update` (:262, re-check → guard → `download_and_install` → `app.restart()`), `UPDATE_AVAILABLE_EVENT = "updater://update-available"` (:45) | Read whole module |
| §11.4 frontend update card / install affordance | **Not wired** — see Honest Gaps §7.1 | Grep: no `updater://` listener and no `install_update` invoke anywhere in `src/` |
| §11.7 Uninstall: binaries + shortcuts + tasks + autostart removed; `%APPDATA%\YouTubeAgent` kept by default, deleted only on explicit Yes (prompt default NO, or the in-app opt-in marker); credentials deleted only on full delete; **videos never touched**; shared runtimes left installed | `hooks.nsh` POSTUNINSTALL: prompt `MB_DEFBUTTON2`, silent+no-marker ⇒ keep; marker contract `uninstall-purge.marker` matches backend `app/uninstall_policy.py` (`MARKER_FILENAME`); `cmdkey /delete:"YouTubeAgent"` only on the purge path; no instruction in the file references the output folder | Read hooks + backend marker file name cross-check |
| Icons exist for every `bundle.icon` entry | `src-tauri/icons/`: 32x32.png (32×32), 128x128.png (128×128), 128x128@2x.png (256×256), icon.ico (multi-size), icon.icns — all present, dimensions verified with PIL; generated from `source-1024.png` (1024×1024) | File inventory + image headers |

### Fixed IPC contract (shell ↔ frontend)

Commands registered in `lib.rs:285` `generate_handler!` (13): `get_backend_connection`
(`Option<BackendConnection>`, null until READY), `get_backend_status`,
`retry_backend_start`, `pause_all_jobs`, `stop_all_jobs`,
`features::{open_output_folder, open_logs_folder, pick_files, pick_folder,
autostart_get, autostart_set}`, `updater::{check_for_updates, install_update}`.
Events: `backend://status-changed`, `backend://connection-changed`,
`updater://update-available`. AppState shape (`lib.rs`): `connection:
RwLock<Option<BackendConnection>>`, `status: RwLock<BackendStatus>`,
`control: SupervisorControl` — matches every consumer (sidecar, tray,
commands). Plugin registration order: single-instance **first**, then
updater, process, shell, dialog, autostart (`lib.rs:210` `run()`).
Capabilities (`capabilities/default.json`, unchanged from HEAD) grant the
WebView **no** fs/shell/http/dialog permissions — dialog and autostart are
reached only through Rust-side plugin APIs in `features.rs`
(`DialogExt::blocking_pick_*` on a blocking thread, autostart `ManagerExt`).

---

## 2. Static validation results

- `python -m json.tool` on `tauri.conf.json` and `capabilities/default.json`: valid.
- Live schema validation (`https://schema.tauri.app/config/2`, Draft7): **0 errors**.
- CSP: byte-identical to HEAD (not weakened).
- `node scripts/check-versions.mjs` (verbatim):

```
PASS  package.json version = 0.1.0 (source of truth)
PASS  tauri.conf.json version is pointer "../package.json" -> 0.1.0
PASS  tauri.conf.json version matches package.json (0.1.0)
PASS  identifier unchanged (com.youtubeagent.app)
PASS  bundle.externalBin includes sidecar: ["binaries/agent-backend"]
PASS  Cargo.toml [package] version matches package.json (0.1.0)
PASS  plugins.updater.pubkey is set (152 chars)

check-versions: all versions agree on 0.1.0
```

(`--release` mode also exits 0 now that the pubkey is merged.)

- Secret hygiene: `git check-ignore` confirms `.secrets/` is ignored;
  `.secrets/tauri-signing.key` (the Round 6 **DEV** key, 1 line, mode 600)
  and its `.pub` are **not tracked and not staged**. Grep of the whole
  `src-tauri/` tree + `scripts/`: `PRIVATE KEY` / `TAURI_SIGNING` values
  appear **nowhere** as key material — only as secret *names* in docs and
  as `${{ secrets.TAURI_SIGNING_* }}` references in
  `.github/workflows/release.yml`. The only long base64 string in the
  shell tree is the **public** updater key (by design).
- Launch token: never in argv, never in a file, never printed (see §1/§4.5).

## 3. Baselines (settled tree, this validation's own runs)

- `npm run typecheck` — **pass** (exit 0).
- `npm run build` — **pass** (only the pre-existing >500 kB chunk-size advisory).
- Backend test suite — **not run by this team**: the shell team's changes do
  not touch `backend/`. The wider Round 6 working tree *does* contain
  extensive backend changes, but they belong to the parallel Round 6
  workstreams (compliance gate, backup/diagnostics, `data_dir`,
  scheduled-run, SL-009 rename `app.stylelab` → `app.style_analysis` —
  whose renames are already staged in the index), not to the shell team.
  Their verification is those teams' own reports.

## 4. Fixes applied by validation (integration defects only)

- **F1 — `tray-icon` Cargo feature (BLOCKER).** `src-tauri/Cargo.toml`
  declared `tauri = { version = "2", features = [] }` and `tauri.conf.json`
  has no `app.trayIcon` section, so the `tray-icon` feature was not enabled
  and `tray.rs` (`tauri::tray`) could not compile. The tray is built in
  code via `TrayIconBuilder`, so no declarative config is needed.
  Fix: `features = ["tray-icon"]`.
- **F2 — Tray status staleness (MAJOR).** The supervisor's status setter
  emitted `backend://status-changed` but never refreshed the tray, so the
  tray status item/tooltip only updated on init, tray actions, and pointer
  Enter. Fix: `crate::tray::refresh_status(&self.app)` is now called in
  `Supervisor::set_status` immediately after the status is published and
  the event emitted (sidecar.rs ~L1132). It is a silent no-op before
  `tray::init` runs and reads the just-updated snapshot, so it cannot
  recurse or deadlock on state locks.
- **F3 — windows-sys handle API cluster (BLOCKER, Windows target only).**
  The `#[cfg(windows)]` code in `sidecar.rs` was written against the
  `windows` crate's handle types, but the dependency is `windows-sys`
  0.59, where `HANDLE`/`PWSTR`/`PCWSTR` are raw-pointer type aliases
  (verified on docs.rs). Fixed all 10 sites: `handle.0.is_null()` →
  `handle.is_null()` (×3), `process.0.is_null()` → `process.is_null()`,
  `job.0.is_null()` → `job.is_null()`, `job.0 as usize` → `job as usize`,
  `HANDLE(bits as *mut c_void)` → `bits as HANDLE` (×2),
  `PWSTR(buffer.as_mut_ptr())` → `buffer.as_mut_ptr()`,
  `PCWSTR::null()` → `std::ptr::null()`; removed two `c_void` imports
  left unused. Linux builds were unaffected (cfg'd out) — which is
  exactly why this survived to validation.
- **F4 — Missing `Manager` import (BLOCKER).** `updater.rs` called
  `app.package_info()`, a `tauri::Manager` trait method, without the
  trait in scope. Fix: added `Manager` to the `tauri::{…}` import.
- **F5 — `.gitignore` coverage.** Added `src-tauri/binaries/*.exe`
  (flagged by `binaries/README.md`); verified with `git check-ignore`
  that the staged sidecar path now matches while `README.md` stays
  committable.
- **F6 — `features::data_dir` divergence (MINOR).** It resolved the data
  dir from `APPDATA` only, ignoring the `YTA_DATA_DIR` override the
  supervisor honours — "Open logs folder" could open the wrong folder.
  Fix: delegate to the canonical `crate::sidecar::data_dir()` resolver.

(The updater pubkey merge into `tauri.conf.json` was **not** needed from
validation: a sibling pass landed it during this review; verified
byte-identical to `src-tauri/keys/updater-pubkey.txt`.)

## 5. Remaining Rust-review uncertainties (uncompiled, standard patterns)

These were checked against docs.rs / crate sources and follow the
documented shapes, but only a compiler settles them:

1. `Menu::with_items` with `&dyn IsMenuItem` trait-object coercions
   (`tray.rs build_menu`) and the `app.tray_by_id(TRAY_ID)` generic form.
2. `tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, Some(vec!["--autostart"]))`
   argument types (`lib.rs`).
3. The autostart plugin's Windows Run **value name** vs the name the
   NSIS PREUNINSTALL hook deletes (`"YouTube Agent"`): if the plugin
   registers a different value name, the hook's delete silently misses.
   Verify the actual registry value after a real enable on Windows.
4. `str::strip_prefix(['v', 'V'])` array-char pattern and
   `download_and_install` closure signatures in `updater.rs` (shapes
   match the plugin docs).
5. Tray menu rebuild (`set_menu`) invoked from the supervisor thread on
   every status transition (fix F2): Tauri tray handles are documented
   as callable from any thread, but the Windows CI run must prove the
   tray-Quit drain completes inside its budget with this call in the
   loop.

## 6. CI-ONLY — what the Windows CI run must prove

1. `cargo build` / `tauri build` compiles clean (settles §5 and the
   F1/F3/F4 fixes).
2. Actual sidecar spawn via `externalBin` on a clean machine; readiness
   sequence reaches READY; window reveals only then.
3. Job Object kill: terminate the shell process abruptly mid-render →
   no `agent-backend.exe` or FFmpeg descendant survives.
4. Orphan sweep: kill the shell uncleanly, relaunch → leftover sidecar
   from the PID record is terminated; a *reused* PID is not touched.
5. Restart budget + exit-code matrix (seed exits 2/3/4/5/crash) behave
   per §4.6, including the `database_newer_than_binary` routing.
6. NSIS install / upgrade / uninstall on clean Win10 + Win11 VMs,
   per-user, no admin: hooks run, VC++ remediation path, silent `/S`
   never prompts, uninstall keeps data by default, purge path removes
   data + credentials, **rendered videos untouched**.
7. Updater round-trip with a signed manifest: check → monotonic guard →
   passive install → relaunch → post-update sidecar health; tampered
   manifest/signature fails closed.
8. Tray Quit drain: full graceful shutdown inside the 10 s budget,
   including with F2's refresh-on-transition active.
9. Autostart enable → logoff/logon → app starts tray-resident; the Run
   value name matches what PREUNINSTALL deletes (§5.3).

## 7. Honest gaps

1. **Updater UI is unwired (MAJOR, frontend-owned).** The Rust side is
   complete (background checks, `updater://update-available`,
   `check_for_updates`, `install_update`), but nothing in `src/`
   listens for the event or invokes `install_update`, and
   `check_for_updates` has no UI caller. §11.4's non-modal update card
   and idle-gated install prompt do not exist yet. Until they do, the
   update channel can detect but never offer an update.
2. **Placeholder updater endpoints.** Both endpoints are documented
   placeholders (a named future CDN domain and a placeholder GitHub
   owner/repo). They are HTTPS and fail closed, but no real update can
   be served until the release pipeline exists. The merged pubkey is
   the Round 6 **DEV** key; the production keypair must be generated
   in CI before first public release (runbook §3) and the pubkey
   replaced — the DEV private key must then be destroyed.
3. **Sidecar output tee'd verbatim to the log.** The supervisor writes
   every sidecar stdout/stderr line into `logs/sidecar.log`. The shell
   itself never logs the token, but if a future backend change ever
   printed its launch environment, the token would land in that log.
   Backend logging hygiene is the control; noted here because the log
   folder is user-shareable via Diagnostics bundles.
4. **Pause/stop ride the settings endpoint** (see §1/§4.6): functional
   and documented in-code, but a dedicated backend endpoint (or wiring
   `ws_command_handler`) would remove the dual-key-spelling workaround
   in `apply_execution_flag`.
5. **NSIS hook deviations from the roadmap text (MINOR).** The
   POSTINSTALL hook stages `vc_redist.x64.exe` (the current Microsoft
   distribution form) where §11.2 says `vc_redist.x64.msi`; switches
   used (`/passive /norestart`) are valid for the exe. The PREINSTALL
   still-running message says "right-click > Exit" while the tray menu
   item is labelled "Quit" — cosmetic text mismatch.
6. **Nothing in this round has run on Windows.** See the environment
   caveat at the top: §6 is not optional follow-up, it is the release
   gate for the entire shell.

---

## 8. Round 6 closure — R6-013: the `--onedir` tree now ships (static-reviewed)

**Defect.** Tauri `externalBin` ships a lone exe, but the sidecar is a
PyInstaller `--onedir` bundle: the lone exe exits 255 in <0.1 s
(`Failed to load Python shared library …/_internal/…`) without its
sibling tree (`src-tauri/binaries/agent-backend/`, containing
`_internal/`, `prompts/`, stamps and the tree's own exe copy).

**Fix (three parts, all static-reviewed — no Rust toolchain on the
build VM, nothing here has been compiled):**

1. **`src-tauri/tauri.conf.json` — `bundle.resources` mapping.**
   `"binaries/agent-backend/": "agent-backend/"` (map form, not a
   glob: a directory source is copied recursively to the target in
   the install resources). On Windows `resource_dir()` is the shell
   exe's directory, so the tree installs as
   `%LOCALAPPDATA%\Programs\YouTubeAgent\agent-backend\…`.
   **Staging precondition (deliberate, fail-loud):** the source
   directory must exist at bundle time, i.e. `packaging/build_sidecar.*`
   (release workflow Inside-out 2/5) must run before `tauri build`
   (4/5) — a fresh-checkout `tauri build` without staging fails on
   the missing resource path instead of silently shipping the lone,
   dead exe. That ordering already holds in `release.yml`.
2. **`src-tauri/src/sidecar.rs` — `resource_sidecar_exe` + spawn.**
   At spawn, if the `externalBin` exe's directory has no `_internal/`
   and `<resource dir>/agent-backend/` contains an exe copy
   (`agent-backend[.exe]` first, then the freeze spec's
   `yta_sidecar[.exe]`) with `_internal/` beside it, the supervisor
   spawns THAT copy by path via the shell plugin's
   `Shell::command(impl AsRef<OsStr>) -> process::Command` (same
   `Command` type the spawn sequence already drives; plugin-shell 2.x
   API verified against docs.rs source) and logs the chosen path.
   In every other case — self-contained `externalBin` layout, dev
   runs, no staged tree — resolution is the previous `sidecar()`
   path, unchanged. The resolver uses `AppHandle::path()`
   (`tauri::Manager`, already imported in this module) →
   `PathResolver::resource_dir()`.
3. **This section** is the documentation part of the fix.

**What the first Windows CI run must still prove for R6-013**
(in addition to §6 as a whole):

1. `cargo build` / `tauri build` compiles the new resolver clean
   (settles the `path()` / `resource_dir()` / `command()` shapes).
2. The NSIS installer actually contains the whole tree under
   `agent-backend\` (incl. `_internal\`, ~500 MB / ~1,200 files) —
   inspect the installed layout on a clean VM, not just the bundle
   log.
3. Installed-app first launch spawns the RESOURCE copy (the
   supervisor log line "spawning staged onedir sidecar from
   resources: …") and reaches READY; the lone `externalBin` exe is
   never the one executed.
4. Uninstall removes the resource tree with the rest of the install
   dir, and an upgrade replaces it atomically (no stale `_internal`
   mixing old/new trees).
5. A `tauri build` attempted without the staging step fails loudly
   on the missing resource directory (the precondition above), and
   the release workflow's staged order (2/5 before 4/5) is the path
   every release actually takes.
