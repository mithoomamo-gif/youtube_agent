# BUILD — Packaging, Signing & Update Channels

> How YouTube Agent goes from source to a signed Windows installer, and how an installed copy updates itself — without admin rights and without interrupting a render or an upload.
>
> Sources: Master Roadmap **Part 11** (packaging) + roadmap section **§36** (§26.1–§26.9) and **§06** (stack/packaging). Where Part 11 and §36 differ in wording, Part 11 governs — it already resolves those differences, and this file follows Part 11.
>
> Audience: anyone touching CI, the Tauri bundler config, PyInstaller, signing, or the update endpoints. For local dev setup, read `DEV_SETUP.md` first. For the release-day runbook, read `RELEASE.md`.

---

## 1. The shipping artefacts (§26.1 / §11.1)

Everything a release consists of. If it is not in this table, it does not ship.

| Artefact | Built by | Signing | Delivery |
|---|---|---|---|
| `YouTubeAgent_<ver>_x64-setup.exe` — NSIS installer | `tauri build` on `windows-latest` CI | Authenticode **+** Tauri updater `.sig` (minisign) | Download page / GitHub Releases / CDN |
| Main app `.exe` — Tauri/Rust shell + embedded React build (`youtube-agent.exe`) | Cargo / Tauri | Authenticode **before** bundling | Inside installer |
| Python sidecar `.exe` — `agent-backend-x86_64-pc-windows-msvc.exe`, via `externalBin` | PyInstaller `--onedir` | Authenticode **before** bundling | Inside installer; later also via the backend channel (Channel B) |
| `ffmpeg.exe` / `ffprobe.exe` — bundled build | Pinned upstream build; version recorded in the build manifest | Ship a copy we sign **or** a distributor-signed build; SHA-256 hash pinned in the build manifest | Inside installer; later via the backend channel |
| Uninstaller `.exe` — NSIS-generated (`uninstall.exe`) | Tauri bundler | Authenticode | Installed alongside the app |
| `latest.json` — Tauri updater manifest | CI release step | Contains minisign signature strings per artefact | Update endpoint (HTTPS) |
| `backend.json` — Python-package manifest | CI release step | SHA-256 **+** minisign signature (**separate key** from the updater key) | Update endpoint (HTTPS) |

### Sidecar naming is a contract, not decoration

Tauri `externalBin` requires the sidecar binary to carry the Rust target-triple suffix at the exact path declared in `tauri.conf.json`:

```
agent-backend-x86_64-pc-windows-msvc.exe
```

The shell is responsible for **spawning and killing** the sidecar. No orphan Python processes may survive app exit (enforced via a Windows Job Object around the whole worker tree, plus the sidecar’s parent watchdog — see `DEV_SETUP.md` §5.1).

---

## 2. PyInstaller: `--onedir`, NOT `--onefile`

**Decision: `--onedir`. Non-negotiable for releases.**

| | `--onefile` ❌ | `--onedir` ✅ |
|---|---|---|
| Launch | Unpacks the whole bundle to `%TEMP%` on **every** launch — slow cold starts | Stable on-disk layout — fast starts |
| Antivirus | The self-extracting bootloader is a classic AV false-positive trigger, especially unsigned/heuristically scanned | Individual files are scannable — and submittable to Microsoft for analysis — one by one |
| Signing | One blob; inner files get no individual signature or reputation | Every shipped `.exe` / `.dll` is individually Authenticode-signable and reputation-scannable |
| Updates | Any change = replace the whole blob | Fits the versioned backend-package layout Channel B depends on |

The `--onedir` output tree *is* the backend package payload: it is what the installer ships first, and what Channel B later swaps atomically, version by version.

---

## 3. Installer: NSIS, per-user (§26.2 / §11.2)

- **Bundler target: NSIS `-setup.exe` only** for direct distribution in v1. A WiX `.msi` is deferred to a later enterprise/Store SKU. Tauri can build both, but NSIS is the one that supports per-user install + the Tauri updater flow cleanly; an `.msi` can only be produced on Windows.
- **`nsis.installMode = currentUser`** (Tauri default):
  - Installs to `%LOCALAPPDATA%\Programs\YouTubeAgent`
  - Requires **no Administrator privileges**
  - Critically: lets the auto-updater replace files later **without a UAC prompt**. A `perMachine` (Program Files) install would make every update an elevation event and force admin at install time too.
- **Compression: `lzma`** (NSIS maximum). The payload is dominated by the Python runtime + FFmpeg; download size is a first-order conversion factor.
- **Shortcuts:** Start Menu folder always; Desktop shortcut is an optional checkbox, **default OFF** — this is a tray-resident utility, not a document app.
- **Silent install** works out of the box with `/S` — required for clean-VM smoke tests and future Store/IT deployment.
- **Languages:** English in the first release. The language list is a config array; the OS language is auto-detected and a display-language selector is available (Tauri’s NSIS support produces a single multi-language installer).
- **Custom steps via `installerHooks`** (`src-tauri/windows/hooks.nsh`) — **never** a forked NSIS template. Tauri’s `NSIS_HOOK_PREINSTALL / POSTINSTALL / PREUNINSTALL / POSTUNINSTALL` are the sanctioned extension points.

### Installer hooks actually used

| Hook | Job |
|---|---|
| `PREINSTALL` | **Upgrade hygiene.** A previous version is running (tray app / sidecar) → prompt to close it. **Never** yank files from under a running render. |
| `POSTINSTALL` | **Dependency remediation.** Check the VC++ Redistributable registry key; if absent, run the bundled `vc_redist.x64.msi` with `/passive /norestart` from `$INSTDIR\resources`, then delete the staged copy. Check before installing, keep it passive, never force a reboot, clean up the staged installer. |
| `PREUNINSTALL` | **Teardown.** Stop the sidecar process and remove the app’s Task Scheduler entries + autostart registration **before** files are deleted, so no scheduled task is left pointing at a deleted executable. |

No hook may fail silently: a dependency failure raises a plain-language message (“Some features may not work…”) and writes to the install log.

### Disk layout — what the installer may and may never touch

```
%LOCALAPPDATA%\Programs\YouTubeAgent\     ← program files (installer-owned)
├── youtube-agent.exe                      signed shell
├── agent-backend.exe                      signed sidecar (externalBin name resolved at build)
├── runtime\
│   ├── ffmpeg.exe
│   └── ffprobe.exe
├── backend\                               versioned backend-package payload
│   ├── current.txt                        one line: active backend version (atomic switch)
│   └── releases\1.4.0\ … 1.4.1\ …         immutable, side-by-side
└── uninstall.exe                          signed

%APPDATA%\YouTubeAgent\                    ← app state — installer NEVER writes here
└── config, secret pointers, SQLite DB, logs

%USERPROFILE%\Videos\YouTubeAgent\         ← default output — installer/uninstaller NEVER touches
└── rendered videos + project files (user-changeable location)
```

Keeping program files under `%LOCALAPPDATA%\Programs` and state under `%APPDATA%` is exactly what makes per-user updates, atomic backend swaps, and “keep my data” uninstalls possible without elevation. The full uninstall matrix (what is removed / kept / never touched) is in `RELEASE.md` and §26.7.

---

## 4. WebView2 strategy

Tauri’s four `webviewInstallMode` options, with size costs from the official docs as recorded in §36 / §11.2:

| Mode | Internet at install | Size cost | Verdict |
|---|---|---|---|
| `downloadBootstrapper` (Tauri default) | Yes | +0 MB | Acceptable fallback; smallest installer |
| **`embedBootstrapper`** | Yes — for the runtime download itself | **+~1.8 MB** | ✅ **Recommended default.** Bootstrapper inside the installer; the only network dependency at runtime is Microsoft’s runtime download itself. The mode Tauri recommends for maximum compatibility. |
| `offlineInstaller` | No | +~127 MB | Optional second SKU (“offline installer”) for poor connections / offline PCs. Do **not** make everyone download it. Release-day offline testing uses the `embedBootstrapper` build (see `RELEASE.md`). |
| `fixedRuntime` | No | +~180 MB | ❌ **Rejected.** Pins an aging WebView2 that we would then have to patch ourselves; the evergreen runtime is patched by Windows. |
| `skip` | — | — | ❌ **Never.** The app will not run where the runtime is missing. |

Also:

- Set **`minimumWebview2Version`** so the installer *upgrades* an ancient runtime instead of failing mysteriously.
- WebView2 is pre-installed on Windows 11 — the bootstrapper path mainly matters for Windows 10 machines.
- Note for anyone cross-checking sources: §06 rounds `embedBootstrapper` to “~2 MB” and puts `fixedRuntime` at “~120 MB”. The measured figures used by Part 11 / §36 (+~1.8 MB / +~127 MB offline / +~180 MB fixed) are the ones this project builds against.

---

## 5. Code signing ( §26.3 / §11.3)

### 5.1 Inside-out order — non-negotiable

A signature on the outer installer does **not** cover its contents. The whole point is the order:

- [ ] **1. Build** shell + sidecar; stage the pinned FFmpeg build.
- [ ] **2. Sign every inner PE file first** — main `.exe`, sidecar `.exe`, uninstaller inputs, any shipped `.dll` / helper `.exe` — with a SHA-256 file digest + RFC 3161 timestamp:
  ```
  signtool sign /fd SHA256 /tr <timestamp-url> /td SHA256 <file>
  ```
  Always timestamp: without it, the signature dies with the certificate and Windows treats the binary as unsigned. SHA-1-only signing is deprecated/distrusted — do not use it.
- [ ] **3. Build the NSIS installer from already-signed files**, then sign the installer itself, then generate + sign the Tauri updater artefacts.
- [ ] **4. Gate:** `signtool verify /pa /all` on **every** artefact. Any failure stops the release.
  ```
  signtool verify /pa /all <artefact>
  ```
- [ ] **5. Publish SHA-256 hashes** on the release page alongside the artefacts.

Signing is a CI step on the Windows runner — never a manual step on a developer laptop (see `RELEASE.md` for the escrow rule).

### 5.2 Signing options

Prices verified Oct 2026 — **re-check before purchase.**

| Option | Cost | Eligibility / notes | Verdict |
|---|---|---|---|
| **Azure Artifact Signing** (formerly Trusted Signing) — Basic | **~$9.99/mo** (≈ $119.88/yr), up to 5,000 signatures/mo; Premium $99.99/mo for 100,000; $0.005 per extra signature | Organisations in USA, Canada, EU, UK; **individuals USA + Canada only**. Keys in Microsoft-managed HSMs; certs short-lived (~72-hour validity, renewed automatically), never leave the HSM; integrates directly with GitHub Actions / Azure DevOps. SmartScreen reputation builds over time; initial warnings expected (same as OV). | ✅ **Best option if the publishing entity is eligible** — cheapest, no token logistics, CI-native |
| **OV certificate** (DigiCert, Sectigo, SSL.com, Certum, …) | **~$150–300/yr** per Microsoft’s comparison table; street prices vary widely by reseller + term | **Worldwide** — the fallback for publishers outside Azure’s eligibility map. Since June 2023 the CA/B Forum requires the private key on an HSM / hardware token or cloud HSM → CI signing means a cloud-HSM plan or a token attached to the build agent. SmartScreen identical to Azure. | ✅ **Default recommendation if Azure eligibility fails.** Buy the longest practical term; keep the **publisher name string identical forever** |
| **EV certificate** | $400+/yr | Worldwide; organisations only; hardware token/HSM mandatory | ❌ **Do not buy for this app.** Microsoft removed EV’s SmartScreen advantage in **August 2024** — EV is now the *same* as OV for SmartScreen, and Microsoft’s own guidance is that paying the EV premium solely to avoid SmartScreen warnings is no longer justified. Remaining EV reasons (kernel-driver signing, enterprise procurement) do not apply. Reseller pages still advertising “instant SmartScreen reputation” for EV are selling pre-2024 behaviour — treat as outdated marketing. |
| **Microsoft Store (MSIX, Store re-signs)** | Free aside from the Store developer account fee | Store review; MSIX packaging; no SmartScreen warnings (covered by Microsoft’s certificate) | Later SKU, not v1 — a legitimate phase-2 channel once direct-download reputation exists |
| **Self-signed / unsigned** | Free | Strong SmartScreen block (“Windows protected your PC”, “Run anyway” required; enterprise policy can block entirely) | ❌ Dev builds only, **never** distributed |

### 5.3 SmartScreen reputation — the part money cannot shortcut

Reputation accumulates **per-publisher + per-file-hash** from download volume and behaviour, starting from **zero**, and carries between versions **only** when both are signed with the **same publisher identity**.

1. **Sign from the very first public build.** Never distribute an unsigned “beta” of the same product — it teaches SmartScreen (and AV) the wrong hash history.
2. **One publisher identity, forever.** Same legal name across renewals and migrations. OV → Azure *is* a publisher change — if a migration is likely, do it **before** public launch, not after.
3. **Expect warnings in the first weeks** and design around them: the download page shows the exact SmartScreen screens with “More info → Run anyway” annotated, shows the verified publisher name users should see, and publishes hashes for verification. Release notes say the same (see `RELEASE.md`).
4. **Concentrate reputation:** one canonical download domain / CDN URL pattern, no mirror sprawl. Every link points at the same artefact.
5. **Updates help, not hurt — if signed consistently.** Each update is a new hash with less reputation than its parent, but it inherits publisher-level trust.
6. **False-positive hygiene:** VirusTotal-scan every release pre-publish; if Defender flags the PyInstaller sidecar, submit the file to Microsoft for malware analysis **before** announcing the release. The `--onedir` layout is what makes individual-file submission clean.
7. **Never** buy download-bot “reputation services”, rotate certificates to escape a bad hash, or ship unsigned updaters. All three destroy the only asset that matters: one boring, continuous signing history.

---

## 6. Two update channels

Most fixes and almost all product evolution (agent prompts, provider adapters, pipeline logic) live in Python. Forcing a full ~200 MB+ installer + a new SmartScreen hash for every prompt tweak would be slow, heavy, and reputation-expensive — so there are **two** channels, with **two different minisign keys**, so a compromise of one cannot forge the other.

> **Authenticode ≠ minisign.** Windows / SmartScreen verify the Authenticode certificate. Each updater verifies its own minisign signature against its embedded pubkey and refuses any artefact that does not match — regardless of Authenticode.

### 6.1 Channel A — Tauri updater (shell + UI)

**Mechanism** (Tauri v2, `tauri-plugin-updater` in `tauri.conf.json` under `plugins.updater`):

- `pubkey`: minisign public key, base64, embedded in the binary.
- `endpoints`: array, tried in order with fallback. Primary CDN manifest → GitHub Releases `latest/download/latest.json` as fallback. Endpoints may embed `{{target}}`, `{{arch}}`, `{{current_version}}`.
- Windows `installMode: "passive"` — progress shown, no user interaction required (alternatives: `basicUi`, `quiet`).
- The endpoint returns **`latest.json`**: version, notes, per-platform URLs + signatures. The manifest **may be generated dynamically per request** — that is precisely what enables server-driven phased rollouts and rollbacks (see `RELEASE.md`).
- Frontend capabilities required: `updater:default`, `updater:allow-check`, `updater:allow-download-and-install`, plus `process:allow-restart` for post-update relaunch.

**Key custody — release-blocking:**

- The keypair is generated once with the Tauri signer (`tauri signer generate`).
- The **private key + password live only in CI secrets** (`TAURI_SIGNING_PRIVATE_KEY`, `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`), with an offline escrow copy in the project password manager.
- ⚠️ **Losing this key permanently orphans the installed base** — an app whose embedded pubkey no longer matches cannot be updated, only reinstalled. Escrow is verified on every release day (see `RELEASE.md`).

**App-side update policy** (product decisions, not Tauri defaults):

1. Check on launch, then every **6 hours** while tray-resident (endpoint is cheap; the payload is not downloaded on check).
2. Download in the background; **install only when idle.** A render / TTS job / upload in flight produces the prompt *“Update ready — will install when the current video finishes”* and installation is deferred until the job queue is empty. **An update must never kill a 20-minute render.**
3. The prompt is a non-modal tray/dashboard card, never a blocking dialog on launch. “Restart to update” relaunches via the process plugin. A forced-update path exists **only** for security fixes, and even then only after the idle check.
4. After relaunch, the shell health-checks the sidecar (`/health` with the per-launch token). A failed post-update health check surfaces a **Rollback** action instead of a dead app.

### 6.2 Channel B — Python backend package (separate, faster cadence)

**Layout on disk:**

```
backend\
├── current.txt          one line: the active version — the ATOMIC SWITCH
└── releases\
    ├── 1.4.0\ …         immutable, side-by-side
    ├── 1.4.1\ …
    └── 1.4.2\ …
```

**Manifest — `backend.json`**, served next to `latest.json`:

```json
{
  "backendVersion": "1.4.2",
  "url": "https://updates.example.com/backend/backend-1.4.2.zip",
  "sha256": "…",
  "signature": "… minisign, BACKEND key (separate from the Tauri updater key) …",
  "minShellVersion": "1.2.0",
  "ffmpegVersion": "7.x (bundled in package)",
  "notes": "…"
}
```

**Package contents:** the PyInstaller sidecar build output (`--onedir` tree); bundled `ffmpeg.exe` / `ffprobe.exe` (the sidecar is their only caller, so they version with the backend, not the shell); Alembic migration scripts; agent prompt/config files.

- FFmpeg hardware encoders (`h264_nvenc`, `h264_qsv`, `h264_amf`) are **compile-time** features of the shipped binary — the package must ship a full-featured Windows build. First-run detection verifies what is actually present at runtime.
- **Separate minisign keypair** from the Tauri updater key: different blast radius, independently rotatable. The **shell** (not downloadable code) is the verifier — a compromised backend package cannot forge shell updates, and vice versa.

**Install algorithm (atomic, self-healing):**

1. The shell downloads the zip to a staging dir; verifies **SHA-256, then the minisign signature**. Any mismatch: delete, log, stay on the current version.
2. Extract to `releases\<new-version>\` — **never** in-place over the running version.
3. **Compatibility gate:** `minShellVersion` vs the running shell. Shell too old → trigger Channel A first, defer the backend swap.
4. Stop the sidecar gracefully (drain / wait for idle — the same rule as Channel A), **back up the SQLite DB**, run migrations against the new code.
5. Start the new sidecar; require a passing `/health` + a smoke self-test (FFmpeg version probe) within the timeout.
6. **Only then** flip `current.txt` and mark the update successful.

Any failure before step 6: discard the new release dir, restart the previous sidecar, restore the DB backup if migrations ran, and report *“update could not be applied — you’re still on vX, nothing was changed.”*

**Retention & version contract:**

- Keep **two previous** backend releases on disk for instant rollback; older ones are pruned (each is large — a disk guardrail in Settings shows the cost).
- The shell refuses to start a backend whose `minShellVersion` exceeds it; the backend refuses a DB schema newer than it understands. Both refusals show a repair action (“Update the app” / “Roll back backend”), never a crash loop.
- DB migrations in the updater era are **forward-only and additive** (no destructive drops in a release that might roll back). The pre-migration backup is retained until the release reaches 100% + 7 days.

---

## 7. CI build pipeline (Windows runner)

The release pipeline is a single ordered flow. Signing is **inside-out, always** (see §5.1 and `RELEASE.md`):

```
GitHub Actions — windows-latest
  1. Build UI                     (npm ci → npm run build)
  2. Build sidecar                (PyInstaller --onedir, target-triple name)
  3. Stage pinned FFmpeg          (version + hash recorded in build manifest)
  4. Sign inner PEs               (SHA-256 + RFC 3161 timestamp)
  5. tauri build                  (NSIS, per-user, embedBootstrapper, lzma)
  6. Sign installer + updater artefacts
  7. signtool verify /pa /all     (gate — any failure stops the release)
  8. Emit latest.json + backend.json
  9. Publish to CDN / Releases    (manifests go live LAST, per rollout ring — see RELEASE.md)
```

Dev builds skip steps 4 and 6–7 (unsigned, never distributed). There is no supported “build the installer on Linux” path: NSIS per-user packaging, `signtool`, and WebView2 verification are Windows steps, and the current Linux dev VM has no Rust toolchain (see `DEV_SETUP.md`).

---

## 8. Honest size figures

Do not quote the Tauri shell size as the app size.

| Figure | Honest number |
|---|---|
| Tauri shell installer alone (empty app class) | ~3–15 MB |
| **Installed size, this app** | **~180–300 MB** — Python runtime + LangGraph dependency tree + FFmpeg dominate, for *every* shell choice |
| Download size | Smaller than installed (NSIS `lzma`), still first-order: treat it as a conversion factor, not a footnote |
| WebView2 `embedBootstrapper` addition | +~1.8 MB |
| Backend package per release (Channel B) | Large (same order as the backend payload) — hence: keep only current + 2 previous, prune the rest, show the disk cost in Settings |
| A full-installer update for a Python-only fix (what Channel B exists to avoid) | ~200 MB+ per update, plus a fresh SmartScreen hash |

Hardware context for testers: minimum tier 4-core / 8 GB RAM / Intel iGPU (QSV); recommended 6–8 cores / 16 GB RAM / NVIDIA GTX 1660+ or RTX (NVENC). Agent count (35) does not change between tiers — only the heavy-pool widths do.

---

## 9. Build checklist (per release candidate)

- [ ] FFmpeg build pinned; version + SHA-256 recorded in the build manifest
- [ ] Sidecar built `--onedir` with the exact `externalBin` target-triple name in `tauri.conf.json`
- [ ] `nsis.installMode = currentUser`, compression `lzma`, Desktop shortcut default OFF
- [ ] `webviewInstallMode = embedBootstrapper`; `minimumWebview2Version` set
- [ ] Installer hooks present (`hooks.nsh`): PREINSTALL close-prompt, POSTINSTALL VC++ check, PREUNINSTALL teardown
- [ ] Inner PEs signed **before** bundling; installer + updater artefacts signed after
- [ ] `signtool verify /pa /all` green on every artefact
- [ ] `latest.json` and `backend.json` generated, signed with their **own** keys, and staged — not yet promoted (promotion is a `RELEASE.md` step)
- [ ] Installed-size figure measured on the clean VM and recorded in the release ticket

Next: `RELEASE.md` — release-day checklist, rollout rings, and rollback playbooks.
