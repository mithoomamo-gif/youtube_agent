# RELEASE RUNBOOK — The Operator's Path to Shipping a Release

> **What this file is:** the step-by-step, in-order procedure a human operator
> follows to ship one release of YouTube Agent, from "we decided to release"
> to "users are updating" — and back again if it goes wrong.
>
> **What this file is not:** a duplicate of `RELEASE.md`. That file is the
> policy and depth reference — rollout rings, pre-registered halt criteria,
> rollback playbooks, the clean-VM test matrix, the SmartScreen release-notes
> wording. This runbook **points at** `RELEASE.md` / `BUILD.md` sections
> instead of copying them. When the two disagree, `RELEASE.md` governs
> policy and `BUILD.md` governs build mechanics; this runbook governs
> *sequence*.
>
> **Honesty banner — read before your first release.** As of this writing the
> release pipeline (`.github/workflows/release.yml`) is a **skeleton**. No
> signing secrets are configured, no real certificate exists, no Windows CI
> release run has ever completed end-to-end, and `src-tauri/icons/` contains
> only a `README.txt`. Every step below that depends on those things says so
> at the step. **Nothing in this runbook is a claim that signing, verification,
> or a Windows build has ever succeeded — until a human runs it and records
> the result, it has not.** Section 10 lists everything that can only be
> proven on the first real run; treat the first tag as a rehearsal.

**Companion documents**

| Document | Use it for |
|---|---|
| `docs/RELEASE.md` | Rings & gates (§2), halt criteria (§3), rollback playbooks (§4), release-day checklist incl. clean-VM matrix (§1.3), key escrow (§1.5) |
| `docs/BUILD.md` | Artefact list (§1), inside-out signing order (§5.1), signing options & prices (§5.2), update channels (§6), CI pipeline order (§7) |
| `docs/DEV_SETUP.md` | Local toolchain, sidecar dev mode, test commands (§6) |
| `.github/workflows/release.yml` | The actual pipeline; its header comment is the authoritative secret-name list |
| `MASTER_ROADMAP.md` Part 11 (`~/workspace/youtube-windows-agent/`) | The design source for packaging, signing, updates |

**Roles used below**

- **Operator** — the person running this runbook for this release.
- **Second person** — a different team member who performs the escrow
  recovery check (§1.4, §9). Cannot be the operator.
- **Release ticket** — the per-release record required by `RELEASE.md` §1.6.
  Create it before Step 1; every checkbox below that says "record" writes
  into it.

---

## 1. Preconditions & accounts

Do this section **once per publishing setup**, then re-verify the marked
items on **every** release. Nothing here involves a secret *value* ever
appearing in the repo, in this runbook, in chat, or in the release ticket —
secret **names** only.

### 1.1 GitHub repository access

- [ ] Operator has **write** access to the repository and permission to push
      tags matching `v*` (the release workflow triggers on tag push).
- [ ] Operator can open **Settings → Secrets and variables → Actions** (repo
      admin, or an admin available during setup). This is where every secret
      in §1.3 is created — by name, value pasted only into GitHub's secret
      field, nowhere else.
- [ ] Operator can read **Actions** runs and download workflow artefacts.
- [ ] Branch protection / required checks on `master` are understood: the
      tag must point at a commit that passed the normal CI test gate
      (`.github/workflows/ci.yml`). Never tag an untested commit to "save a
      run" — §4's triage assumes the tag's commit was already green.

### 1.2 Choose the Authenticode signing identity — ONE, before first public release

`BUILD.md` §5.2 has the full comparison (prices verified Oct 2026 —
**re-check before purchase**). The decision, in short:

| Option | When it applies | Cost class |
|---|---|---|
| **Azure Trusted Signing / Azure Artifact Signing (Basic)** | Publishing entity is eligible: organisations in USA, Canada, EU, UK; individuals in USA + Canada only. Keys stay in Microsoft-managed HSMs; CI-native. | ~$9.99/mo class (~$120/yr), signature quota included |
| **OV code-signing certificate** (DigiCert, Sectigo, SSL.com, Certum, …) | The fallback when Azure eligibility fails — available worldwide. Since June 2023 the private key must live on an HSM / hardware token / cloud HSM, so CI signing means a cloud-HSM plan or a token attached to the build agent. | ~$150–300/yr class, varies by reseller/term |
| **EV certificate** | **Do not buy for this app.** Microsoft removed EV's SmartScreen advantage in August 2024; EV now behaves identically to OV for SmartScreen (`BUILD.md` §5.2). | — |

Rules that outrank the choice itself:

1. **Decide before the first public build, and do not migrate casually.**
   SmartScreen reputation attaches to the publisher identity; switching
   OV → Azure later is a publisher change and resets what you earned
   (`BUILD.md` §5.3).
2. **The publisher name string on the certificate is permanent.** It is what
   users will be told to check on the SmartScreen screen (§6), copied
   character-for-character into release notes (`RELEASE.md` §1.4). Record
   the exact string in the release ticket once the certificate exists.
3. An Azure account (for Trusted Signing) or a CA account + validated
   organisation/identity (for OV) takes **time** — identity validation is
   measured in days, not minutes. Start it before you need it; a release
   date chosen before validation completes is a date you will miss.

### 1.3 CI secrets — names only

Create these in **GitHub → Settings → Secrets and variables → Actions**.
The authoritative list is the header comment of
`.github/workflows/release.yml`; this table mirrors it. **Never** put a
value in the repo, a workflow file, an issue, or the release ticket.

**Authenticode — Option A, Azure Artifact Signing** (all six for the Azure
path; the workflow's `check-secrets` probe keys off the first three):

- `AZURE_ARTIFACT_SIGNING_ENDPOINT`
- `AZURE_ARTIFACT_SIGNING_ACCOUNT_NAME`
- `AZURE_ARTIFACT_SIGNING_CERTIFICATE_PROFILE_NAME`
- `AZURE_ARTIFACT_SIGNING_TENANT_ID`
- `AZURE_ARTIFACT_SIGNING_CLIENT_ID`
- `AZURE_ARTIFACT_SIGNING_CLIENT_SECRET`

**Authenticode — Option B, OV certificate** (cloud HSM / PFX path):

- `WINDOWS_CERT_BASE64_PFX`
- `WINDOWS_CERT_PASSWORD`
- `WINDOWS_CERT_THUMBPRINT`

**Tauri updater (minisign) — required for any build the updater can serve:**

- `TAURI_SIGNING_PRIVATE_KEY`
- `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`

**Backend channel (Channel B):** `backend.json` is signed with a **separate**
minisign keypair from the updater key (`BUILD.md` §6). Its secret names are
wired alongside the above when the backend channel goes live; do not reuse
the updater key for it, and do not invent names for it in the workflow
before the backend-channel work lands.

**Current state (verify, don't trust this line):** none of the above are
configured yet. The workflow knows this about itself — its `check-secrets`
job probes presence (never values) and reports skeleton mode. That is the
correct behaviour until §1.2 and §1.4 are done.

### 1.4 Tauri updater keypair — generation and custody

This is the single most dangerous artefact in the project. There are **two
independent signature layers** on every release (Roadmap Part 11): the
Authenticode certificate (§1.2) convinces *Windows*; the minisign updater
key convinces *already-installed copies of the app*. They are not
interchangeable, and the updater key has no recovery authority above it.

1. Generate the keypair **once**, on a trusted machine, with the Tauri
   signer (`tauri signer generate`, from `@tauri-apps/cli`). Follow the
   CLI's own prompts for the key password.
2. Put the **private key + password** into the CI secrets named in §1.3
   (`TAURI_SIGNING_PRIVATE_KEY`, `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`).
3. Put the **public key** into `src-tauri/tauri.conf.json` →
   `plugins.updater.pubkey` (see §2.3 — until this is done the pubkey is
   `""` and no update can ever verify).
4. **Offline escrow, same day:** store a second copy of the private key +
   password in the project's password manager / offline escrow, held
   independently of any one laptop. CI secrets are a *use* location, not
   the only copy — a deleted repo secret must not be an extinction event.
5. **Second-person recovery check (release-blocking, every release):** the
   second person — not the operator — physically performs the recovery:
   opens the escrow entry, retrieves the key material, and confirms the
   CI secrets are present by name. "It's in the password manager,
   probably" is not a check (`RELEASE.md` §1.5 gives the full escrow list,
   including the backend minisign key and signing-service access).

> ⚠️ **Losing the updater private key permanently orphans the installed
> base.** Copies of the app in the field embed the public key; if no private
> key exists that matches it, those installs can **never** be updated
> again — the only path left for those users is a manual uninstall /
> reinstall. There is no reset, no override, and no support ticket that
> changes this. Treat custody accordingly.

### 1.5 Update hosting — decided before the first signed build

- [ ] A real **HTTPS** host exists for the updater manifest (`latest.json`,
      and `backend.json` for Channel B). HTTPS only — the manifest is never
      served over plain HTTP (Roadmap Part 11: HTTPS-only manifest,
      fail-closed updates).
- [ ] The URL is the one that will be written into `tauri.conf.json` →
      `plugins.updater.endpoints` (§2.3). The workflow also stages
      `latest.json` on the GitHub Release, and the GitHub Releases
      `latest/download/latest.json` URL is the documented fallback endpoint
      (`BUILD.md` §6.1) — decide primary vs fallback now, not during an
      incident.
- [ ] Whoever operates that host can replace `latest.json` quickly and
      reversibly — §7 and §8 both depend on it.

---

## 2. Version bump — the single-source procedure

There is no single version file; there is a **single version value** that
must appear identically in every file below. The release workflow resolves
its version from the **git tag** first, so a mismatch doesn't fail loudly in
CI — it fails confusingly, in filenames, manifests, and the in-app
`system/info` handshake. Bump in exactly this order, then let preflight
(§2.4) prove the agreement.

### 2.1 Files that carry the version

| # | File | Field | Value today (verify in your checkout) |
|---|---|---|---|
| 1 | `src-tauri/tauri.conf.json` | top-level `"version"` | `"../package.json"` — a **reference**, not a literal: Tauri reads the version from `package.json` (row 3), so this file follows automatically and is never edited for a bump |
| 2 | `src-tauri/Cargo.toml` | `[package] version` | `0.1.0` |
| 3 | `package.json` | top-level `"version"` | `0.1.0` — **the literal version of record** (the file `tauri.conf.json` points at) |
| 4 | `backend/app/__init__.py` | `__version__` | `0.1.0` — surfaces as the backend's FastAPI version and `system/info` `backend_version` |
| 5 | `backend/pyproject.toml` | `[project] version` | `0.1.0` |

Notes:

- **Single literal source:** the literal version lives in `package.json`
  (row 3). `tauri.conf.json`'s `"version"` is the reference
  `"../package.json"`, so the Tauri version follows `package.json`
  automatically — never edit the reference. `release.yml`'s config
  fallback resolves the same reference when no tag/input version exists.
- Shell version and backend version are conceptually independent (Channel B
  lets the backend move faster — `BUILD.md` §6.2), but for a combined
  release they are bumped together to the same value. A backend-only
  release bumps rows 4–5 only and follows `RELEASE.md` §4.1 / the Channel B
  procedure, not this runbook's tag flow.
- The tag (§4) is `v` + this exact value, e.g. version `0.1.0` → tag
  `v0.1.0`. No other format.

### 2.2 Bump procedure

1. Decide the new version (semver: patch for fixes, minor for features;
   for the rehearsal release use a pre-release suffix — see §10).
2. Leave row 1 (`tauri.conf.json`) untouched — its `"version"` stays as
   the `"../package.json"` reference and follows row 3 automatically.
3. Edit row 2 (`Cargo.toml`) to the same value. If your checkout has a
   committed `Cargo.lock`, let Cargo update the workspace package entry on
   the next build rather than hand-editing the lockfile.
4. Edit row 3 (`package.json`) to the new version — this is the literal
   edit; row 1 (`tauri.conf.json`) picks it up via its reference.
5. Edit rows 4–5 (backend `__init__.py`, `pyproject.toml`) to the same
   value for a combined release.
6. Add the changelog entry (§3) in the **same commit**.
7. Commit the bump as one commit (message: `release: v<version>`), push,
   and confirm the normal CI workflow is green on that commit **before**
   tagging.

### 2.3 Known blockers that must be cleared before the FIRST real release

These are not hypothetical. Each is true of the checkout today, and each
will block or corrupt a first release if tagged over. Preflight (§2.4)
checks all three; the fixes are operator actions, listed here so they are
done deliberately, not discovered mid-run.

**Blocker A — App icons are missing.**
`src-tauri/icons/` contains only `README.txt`. `tauri.conf.json` →
`bundle.icon` references `icons/icon.png` and `icons/icon.ico`, neither of
which exists, so `tauri build` cannot produce a bundle.

- Fix (exactly as `src-tauri/icons/README.txt` prescribes — do not
  hand-draw substitutes and ship them):
  1. Place the approved source artwork at `src-tauri/icons/source-1024.png`
     (1024 × 1024 px PNG).
  2. From the repo root: `npm run tauri icon -- src-tauri/icons/source-1024.png`
     — this generates the full icon set into `src-tauri/icons/`.
  3. Commit the generated files.
- Owner of the blocker: whoever owns the final artwork. Until the artwork
  exists, **no release tag** — a rehearsal included (§10) still needs real
  icons, because icon generation is one of the things the rehearsal exists
  to prove.

**Blocker B — Updater public key is empty.**
`tauri.conf.json` → `plugins.updater.pubkey` is `""`.

- Fix: complete §1.4 (generate the keypair), then paste the **public** key
  string as the `pubkey` value and commit. An empty pubkey means the
  updater in every installed copy can never verify an update — shipping
  with it empty burns the first install base.

**Blocker C — Updater endpoint is a placeholder.**
`tauri.conf.json` → `plugins.updater.endpoints` is
`["https://example.invalid/releases/latest.json"]` — deliberately
unroutable (RFC 2606 `.invalid`), so the updater fails its check rather
than pointing anywhere guessed.

- Fix: replace it with the real HTTPS manifest URL decided in §1.5
  (primary CDN/host first; GitHub Releases fallback second, per
  `BUILD.md` §6.1) and commit. Verify the URL returns the staged manifest
  shape after §7 — before that, it is expected to 404; what matters at
  bump time is that the *address* is the real one, because it is compiled
  into every installed copy.

### 2.4 Preflight — run it, and what each failure class means

Run the packaging preflight from the repo root:

```powershell
# Windows
.\packaging\preflight.ps1
```
```bash
# Linux/macOS dev machines (checks that don't need Windows tooling)
./packaging/preflight.sh
```

> If the script is not present in your checkout, it has not landed yet —
> **do not skip preflight**; perform every check in the table below by hand
> and record the results in the release ticket. The script is an
> automation of this table, not a different standard.

Preflight must pass **before** the tag is pushed. On failure:

| Failure class | What it means | Operator action |
|---|---|---|
| Version mismatch across §2.1 files | The bump was partial — most often `package.json` (the `0.0.0` drift) or the backend pair was missed | Fix the lagging file to the release version; re-run. Never tag over a mismatch |
| Icons missing (`icon.png` / `icon.ico` absent) | Blocker A (§2.3) — bundling is impossible | Run the icon generation procedure in §2.3; commit; re-run |
| Updater `pubkey` empty | Blocker B (§2.3) — installs could never verify updates | Complete §1.4 keypair generation; set the pubkey; re-run. **No waiver exists for a public release** |
| Updater endpoint is `example.invalid` / not HTTPS | Blocker C (§2.3) — installs would check a dead or insecure address forever | Set the real HTTPS endpoint from §1.5; re-run |
| FFmpeg pin mismatch | The staged FFmpeg build's version/hash does not match the pinned build recorded for the release (`BUILD.md` §1, §7) | Re-stage the pinned build; if the pin itself is being changed deliberately, that is a code change — separate commit, recorded in the ticket, not a release-day improvisation |
| Changelog entry missing for this version | §3 not done | Add the entry (§3); re-run |
| Dirty working tree / HEAD ≠ the CI-green commit | You are about to tag something other than what was tested | Stop. Commit or discard deliberately; tag only the green commit (§1.1) |

- [ ] Preflight green (or hand-checked table green) — record output in the
      release ticket.

---

## 3. Changelog

The repo does not yet carry a `CHANGELOG.md` — **the first release creates
it.** Do not let that make the first entry sloppy; it sets the format.

1. Create (first release) or open `CHANGELOG.md` at the repo root.
2. Follow **Keep a Changelog** format: an `## [Unreleased]` section on top,
   then one section per release, newest first:
   `## [<version>] - <YYYY-MM-DD>`, with grouped subsections as applicable —
   `Added`, `Changed`, `Fixed`, `Removed`, `Security`.
3. Move this release's entries from `Unreleased` into a new
   `## [<version>] - <release date>` section. The version string must match
   the tag **exactly** (`0.1.0` in the heading, `v0.1.0` as the tag).
4. Write entries for **users**, not for the team: what changed in behaviour,
   what a user should do differently. Internal refactors belong in git
   history, not here. The CI-generated GitHub Release notes (`--generate-notes`)
   are a supplement, not a substitute — the changelog is the curated record.
5. Include, in the release's GitHub notes (and linked from the changelog
   entry for the first public releases), the SmartScreen substance from
   `RELEASE.md` §1.4 — publisher name copied character-for-character from
   the certificate.

- [ ] Changelog entry for this version exists, dated, and matches the tag.

---

## 4. Tag & trigger CI

### 4.1 Tag and push

```bash
# From the bump commit (§2.2 step 7) — confirm you are on it first:
git rev-parse HEAD            # must equal the CI-green commit
git tag -a v<version> -m "YouTube Agent v<version>"
git push origin v<version>
```

- Tag pushes matching `v*` trigger `.github/workflows/release.yml`. A
  manual `workflow_dispatch` run also exists (inputs: `version`, `ring` —
  Ring 0 / Ring 1 only, `publish`) — use it for rehearsals; the tag flow is
  the release flow.
- **Watch the run:** GitHub → **Actions** → the *Release* workflow run for
  your tag. The run is in the repo's Actions tab from the moment the tag
  lands; if no run appears within a couple of minutes, check the tag
  actually pushed (`git ls-remote --tags`) before re-pushing anything.
- A release run is **never cancelled mid-signing** by a newer run (the
  workflow sets `cancel-in-progress: false` on the release concurrency
  group). Do not cancel one by hand except to stop a run you know is
  building the wrong commit — and record that in the ticket if you do.

### 4.2 What each job / step means

The workflow has two jobs. `check-secrets` runs first and gates everything
signing-related; the `release` job does the work as a **single sequential
job on purpose** — the inside-out signing order (`BUILD.md` §5.1) must not
be split across runners.

| Stage | What it does | Success looks like |
|---|---|---|
| `check-secrets` | Probes which secret **groups** exist (Azure / OV cert / Tauri updater) and outputs booleans. Never prints a value | Booleans match §1.3 reality. In skeleton mode it posts a loud `::notice::` saying signing will be skipped |
| Inside-out 1/7 — Build UI | `npm run build` (type-check + Vite production build) | UI bundle produced |
| Inside-out 2/7 — Build sidecar | PyInstaller `--onedir` from `backend/agent-backend.spec`; stages the binary under the exact `externalBin` target-triple name (`agent-backend-x86_64-pc-windows-msvc.exe`) | Sidecar staged under `src-tauri/binaries/` |
| Stage FFmpeg | Copies the pinned FFmpeg/ffprobe build into the bundle staging | FFmpeg staged; a `::notice::` here means **no pinned build was found — that is a release blocker**, not a warning to wave through |
| Inside-out 3/7 — Sign inner binaries | Authenticode-signs every inner PE (sidecar, FFmpeg, helpers) with SHA-256 + RFC 3161 timestamp — **before** bundling | Each inner file signed. If no Authenticode is wired, the step is replaced by an explicit SKIPPED notice step |
| Inside-out 4/7 — Tauri build | Builds the shell + NSIS per-user installer from the already-signed files; when the updater key is wired, also produces the updater artefacts (`.sig`) | Installer under `src-tauri/target/release/bundle/`. **Skeleton behaviour:** if `TAURI_SIGNING_PRIVATE_KEY` is absent, this step *skips itself with a warning* — no installer is produced at all |
| Inside-out 5/7 — Sign installer + verify | Signs the installer/updater artefacts, then runs `signtool verify /pa /all` on each as a gate | "All installer artefacts signed and verified" — or the SKIPPED notice variant when no Authenticode is wired |
| Inside-out 6/7 — Emit manifests | Writes `latest.json` (with the `.sig` contents as the signature string), `backend.json`, and `SHA256SUMS.txt` into `release-manifests/` | Manifests written. An **empty signature** in `latest.json` produces a warning and means **do not publish this manifest** |
| Inside-out 7/7 — Scans (placeholder) | VirusTotal / Defender steps are **placeholders** in the skeleton — notices, plus a best-effort local Defender scan if available | Informational only. The gating scans are the operator's (§9), not this step's |
| Upload artefacts | Uploads bundle + `release-manifests/` as a workflow artefact (30-day retention) | Artefact `youtube-agent-<version>` downloadable from the run |
| Create GitHub Release | **Only** when signing is wired **and** (tag push or manual `publish`) — creates a **draft** release with `--generate-notes` | Draft release exists (§7). Otherwise the SKIPPED notice step explains why |
| Staged rollout note | Prints the Ring 0/Ring 1 summary into the log | Informational — promotion itself is manual (§7) |

**Expected duration (estimate):** the `release` job's configured timeout
is 120 minutes. A healthy first runs should be expected to take **tens of
minutes** (Rust/Tauri compile + PyInstaller freeze dominate) — treat any
precise number as an **estimate until the first real runs establish a
baseline**, and record actual stage timings from the rehearsal (§10) in
the ticket for the next operator.

### 4.3 Failure triage

| Symptom in the run | Class | What to do |
|---|---|---|
| UI build / type-check fails; or the tag's commit wasn't green on `ci.yml` | **Test/quality gate fail** | This is the gate working. Fix on `master` through the normal process, re-run CI, and ship the fix as part of this version **before** tagging — or bump to a new version if the tag already exists (tags are immutable in practice; never move a pushed tag) |
| PyInstaller step warns *"produced no agent-backend exe"* or the spec build errors | **Sidecar freeze fail** | Read the PyInstaller log in the step output; reproduce locally on Windows with the same spec (`backend/agent-backend.spec`). Common causes: missing hidden import, a dependency with native wheels not collected. Do not work around it by switching to `--onefile` — `--onedir` is non-negotiable (`BUILD.md` §2) |
| FFmpeg staging prints the *"No pinned FFmpeg build found"* notice, or staged version/hash ≠ pin | **FFmpeg pin mismatch** | Stop the release. Stage the pinned build where the workflow looks (`tools/ffmpeg`, `backend/vendor/ffmpeg`) or fix the pin deliberately via a reviewed change. Shipping an unpinned/unrecorded FFmpeg is not an option (`BUILD.md` §7 checklist) |
| `tauri build` fails (bundler, NSIS, icon, WebView2 errors) | **Tauri build fail** | Read the bundler error literally — missing icons (§2.3 Blocker A) and endpoint/pubkey problems surface here. Fix in config/source, commit, re-tag as a new version if the tag was already pushed |
| Signing steps show the **SKIPPED notice** variants | **Signing skipped — skeleton mode** | Not a failure of the run; a statement that §1.2/§1.3/§1.4 are not done. Artefacts (if any) are **unsigned internal artefacts only** (§5.4). Complete §1 and re-run — do not publish from this run |
| A signing step itself **fails** (signtool error, Azure signing error, verify gate throws) | **Signing failed** | Fundamentally different from skipped: the identity exists but signing broke. Stop. Do not retry-publish, do not fall back to shipping unsigned. Diagnose from the step log (expired validation, HSM/token access, timestamp server, wrong profile name in the Azure secrets). Record the cause in the ticket |
| `latest.json` emitted with empty signature warning | **Manifest unusable** | The updater key wasn't wired when `tauri build` ran, so no `.sig` existed. The manifest must not be published (§7). Fix §1.3/§1.4 and rebuild |
| `check-secrets` booleans don't match the secrets you believe you set | **Secret wiring mismatch** | Check names character-for-character against §1.3 (the Azure probe needs the endpoint + account + profile trio). Values are never echoed — probe presence only, fix names/scope (repo vs environment secrets), re-run |

- [ ] Run green, **or** every failure above classified and recorded.
- [ ] Download the workflow artefact (`youtube-agent-<version>`) — §5
      verifies from these files, not from the log's claims.

---

## 5. Signing verification — post-build, by a human

CI signing is necessary; it is not sufficient. Before anything is
published, the operator verifies the **downloaded artefacts** (§4.3) — the
exact bytes users would receive. Run this on a **Windows machine** with the
Windows SDK installed (`signtool.exe` ships with it; `DEV_SETUP.md` §1).
If `signtool` is not on `PATH`, it lives under
`C:\Program Files (x86)\Windows Kits\10\bin\<version>\x64\signtool.exe`.

> **Until §1 is fully wired, this section's honest result is: "artefacts
> are UNSIGNED — verification cannot pass, and the artefacts are internal
> only."** The workflow says the same thing loudly via its SKIPPED notices.
> Record that outcome in the ticket; do not soften it, and do not proceed
> to §7 with unsigned artefacts. `RELEASE.md` §1.2 is absolute on this: no
> unsigned artefact is reachable from any public URL, and there is no
> unsigned "beta" of this product.

### 5.1 Authenticode — installer and updater artefacts

For **each** artefact (the NSIS installer
`YouTubeAgent_<version>_x64-setup.exe`, and every signed `.exe` the bundle
contains):

```powershell
signtool verify /pa /all YouTubeAgent_<version>_x64-setup.exe
```

Expected output shape (wording varies slightly by SDK version):

```
File: YouTubeAgent_<version>_x64-setup.exe
Index  Algorithm  Timestamp
========================================
0      sha256     RFC3161

Successfully verified: YouTubeAgent_<version>_x64-setup.exe
```

Pass conditions — **all** of them:

- Exit status success and the words **"Successfully verified"** for the file.
- The signature chain resolves to the publisher identity from §1.2 — check
  the signer/publisher name shown matches the recorded publisher string
  **character-for-character** (this is the same string users will be told
  to check, §6 / `RELEASE.md` §1.4).
- A timestamp is present (RFC 3161). An untimestamped signature dies with
  the certificate; CI signs with one (`BUILD.md` §5.1) — its absence means
  the artefact did not come through the pipeline.
- `/all` matters: it verifies **every** signature on the file, not just the
  first. Do not drop it.

Any failure — "No signature found", chain errors, hash mismatch — means
**stop**. The artefact is not published, in any ring, under any deadline.
Record the exact `signtool` output in the ticket.

### 5.2 Updater (minisign) signature — the second layer

Authenticode says nothing about whether installed apps will accept an
update; the minisign layer decides that (`BUILD.md` §6).

1. Open `release-manifests/latest.json` from the downloaded artefacts.
2. Confirm `platforms."windows-x86_64".signature` is a **non-empty**
   string. Empty = the `.sig` was never produced (§4.3) = manifest is
   unpublishable.
3. Confirm that signature corresponds to **this** installer: it is the
   verbatim contents of the `YouTubeAgent_<version>_x64-setup.exe.sig`
   file produced next to the installer by the same `tauri build`. If the
   `.sig` file is present in the bundle artefacts, compare the strings —
   they must match exactly, character for character.
4. Confirm `latest.json`'s `version` equals the release version and its
   `url` points at the artefact location users will actually download
   from (§7). A correct signature over the wrong URL is still a broken
   release.
5. The end-to-end proof of this layer is functional, not visual: in Ring 0
   (§7), an installed previous version must **accept** this update, and —
   equally important — a deliberately corrupted copy of the manifest or
   installer must be **refused** by the updater. If you have never seen
   the refusal, you have not tested the layer; the clean-VM matrix in
   `RELEASE.md` §1.3 (case 4, simulated failed update) covers the backend
   analogue.

### 5.3 SHA-256 checksums

1. In the downloaded artefacts, `release-manifests/SHA256SUMS.txt` lists
   the SHA-256 of every bundle artefact.
2. Recompute locally and compare — every line, not a sample:

```powershell
Get-FileHash .\YouTubeAgent_<version>_x64-setup.exe -Algorithm SHA256
# Compare the .Hash value against the installer's line in SHA256SUMS.txt
```

3. The hashes that will be **published** (§7) are the ones from this file,
   verified here. The download page and release notes carry them so users
   can do the same comparison (`RELEASE.md` §1.4).

### 5.4 The unsigned case — stated plainly

If signing secrets are not configured, the workflow **skips** signing,
says so in `::notice::` / `::warning::` output at every skipped step, and
any artefacts produced are **UNSIGNED**. Concretely that means:

- `signtool verify` on them reports no signature — there is nothing to
  verify, and §5.1 cannot pass.
- They are **internal artefacts only**: developer inspection, pipeline
  rehearsal (§10). They are not uploaded to any public URL, not linked,
  not sent to beta users, and not promoted through any ring
  (`RELEASE.md` §1.2).
- The draft GitHub Release step also does not run in skeleton mode
  (§4.2) — if you find yourself creating a release by hand from unsigned
  artefacts, you have left this runbook. Stop.

- [ ] §5.1 passed on every artefact, publisher string recorded.
- [ ] §5.2 signature field present and matching; version/URL correct.
- [ ] §5.3 every hash recomputed and matching.
- [ ] **Or:** unsigned outcome recorded, artefacts kept internal, release
      stops here until §1 is completed.

---

## 6. SmartScreen reality — what the user actually sees

No euphemisms in this section, because release notes and support answers
will be written from it. SmartScreen reputation starts at **zero** for a
new publisher and accrues per-publisher + per-file-hash from download
volume and time (`BUILD.md` §5.3). It **cannot be bought**, and since
August 2024 an EV certificate no longer bypasses it. Plan communications
around the screens below, not around hoping users won't see them.

### 6.1 (a) Unsigned installer — first run

A Windows user who downloads and double-clicks an **unsigned** installer
sees a full-screen-style SmartScreen prompt:

> **Windows protected your PC**
> Microsoft Defender SmartScreen prevented an unrecognized app from
> starting. Running this app might put your PC at risk.

- There is **no publisher name** to check — the app is unattributed.
- The only ways forward are **More info → Run anyway** (if policy allows)
  or closing the prompt. On machines under enterprise policy, unsigned
  apps can be **blocked entirely** with no Run anyway path.
- The download itself may also be flagged by the browser as uncommon /
  not commonly downloaded before the installer is even opened.

This is why §5.4 exists: this screen, with no publisher identity behind
it, is not a first impression this product ships with. **Unsigned builds
are never distributed.**

### 6.2 (b) Signed installer (OV or Azure Trusted Signing) with no reputation yet

Signing changes *who vouches*, not *how much history exists*. On the
first downloads of a correctly signed installer from a new publisher,
users should expect **the same SmartScreen prompt**:

> **Windows protected your PC**
> Microsoft Defender SmartScreen prevented an unrecognized app from
> starting. Running this app might put your PC at risk.

…with the decisive difference that expanding **More info** now shows:

- **Publisher:** the exact publisher name string from the certificate
  (§1.2) — the string release notes tell users to verify before
  continuing (`RELEASE.md` §1.4).
- A **Run anyway** button that proceeds with the install.

The user's path is: **More info → check the publisher name matches what
the download page says → Run anyway.** The download page must show this
sequence with annotated screenshots and the verified publisher name
(`RELEASE.md` §1.4) — for early releases, that page is part of the
installer, functionally speaking.

### 6.3 How it improves — and what is NOT promised

- Reputation accrues as the **same publisher identity** accumulates
  downloads of well-behaved signed files over time. Eventually the prompt
  stops appearing for that publisher's files. There is no threshold the
  operator can watch count up, no application form, and no date anyone
  can promise — anyone who gives you a number of downloads or days is
  guessing.
- **Every version is a new file hash** with less reputation than its
  parent; consistent signing is what lets publisher-level trust carry
  over (`BUILD.md` §5.3). This is another reason never to change publisher
  identity casually (§1.2).
- **Azure Trusted Signing / EV notes:** Azure Trusted Signing (including
  its short-lived, HSM-backed certificates) builds reputation the same
  way OV does — initial warnings are expected, identical in shape to
  §6.2. EV confers **no** SmartScreen advantage (removed August 2024;
  `BUILD.md` §5.2). Neither option, and no option at any price, removes
  the early prompts entirely.
- **Therefore the honest sentence for every early release note is:**
  *"You may see a Windows SmartScreen warning on first install — this is
  expected for a new publisher. Choose More info, verify the publisher is
  `<exact publisher string>`, then Run anyway."* (`RELEASE.md` §1.4 has
  the checklist form of this.) Never write "signed, so no warnings."

---

## 7. Publish — draft review, then manifest last

CI does not publish. When signing is wired and the run came from a tag,
CI creates a **draft** GitHub Release and stops (`.github/workflows/release.yml`,
"Create GitHub Release" step). Everything in this section is a human act,
in this order.

### 7.1 Review the draft GitHub Release

- [ ] Open the draft release for `v<version>` on GitHub. Confirm the tag,
      title (`YouTube Agent v<version>`), and target commit are this
      release and no other.
- [ ] Confirm the attached assets include: the NSIS installer, the updater
      `.sig`, `latest.json`, `backend.json`, and `SHA256SUMS.txt` (from
      `release-manifests/`). Anything missing → do not improvise uploads
      from a laptop; fix the pipeline run and regenerate the draft.
- [ ] Cross-check the assets against §5: the installer's hash must equal
      the verified hash from §5.3 — i.e. the file on the release is the
      file you verified, byte for byte.
- [ ] Replace/complete the auto-generated notes with the curated release
      notes: changelog substance (§3), the SmartScreen note with the
      exact publisher string (§6.3), and the published SHA-256 hashes
      (§5.3).
- [ ] Publish the draft (**Publish release**). The release page is now
      public — but note carefully: **publishing the GitHub Release does
      not yet offer the update to any installed user.** That happens only
      in §7.2.

### 7.2 The updater manifest goes live LAST

The updater only ever reads `latest.json` from the endpoint compiled into
the app (§2.3 Blocker C). The GitHub Release asset copy is the staged
source (and the fallback endpoint's backing store); the **live** manifest
is the one served at the configured HTTPS endpoint.

1. Confirm the staged `latest.json` one final time: `version`,
   `platforms."windows-x86_64".url`, and non-empty `signature` (§5.2).
2. Upload/serve that exact file at the updater endpoint from §1.5 so that
   the endpoint URL in `tauri.conf.json` returns it. (If the endpoint *is*
   the GitHub Releases `latest/download/latest.json` URL, publishing the
   release in §7.1 already moved it — verify by fetching the URL and
   comparing the body to the staged file; do not assume.)
3. Do the same, on its own cadence, for `backend.json` if this release
   includes a Channel B backend package (`BUILD.md` §6.2) — signed with
   the **backend** key, never the updater key.
4. **This step is the release going live.** Everything before it was
   preparation that users could not see; everything after it is a rollout
   that must be watched. Record the time in the release ticket — Ring 0's
   soak clock starts here (`RELEASE.md` §1.6).

**Why last:** a manifest pointing at artefacts that are not yet verified,
scanned (§9), and downloadable fails **closed** in the worst way — users'
updaters either error or, worse, succeed against the wrong bytes. The
artefacts, hashes, and release page land first; the pointer moves last.

### 7.3 Rollout rings — pointer, not procedure

Promotion beyond this point follows **`RELEASE.md` §2 (rings) and §3
(halt criteria)** and is deliberately not re-specified here. The operator
facts this runbook contributes:

- This workflow only ever produces **Ring 0 / Ring 1** artefacts; Ring 0 is
  internal (≥ 24 h soak, clean-VM smoke green), Ring 1 adds beta opt-ins +
  a 5% deterministic slice (48 h; gates: update success ≥ 98%, crash-free
  ≥ 99%, zero attributable sidecar-health failures).
- **Promotion is a manifest change, not a rebuild.** Advancing a ring
  means changing which version the update endpoint serves to which slice
  of installs. No new tag, no new CI run.
- **Halt = freeze manifest promotion.** If the pre-registered halt
  criteria (`RELEASE.md` §3, written into the ticket *before* promotion)
  are crossed, stop advancing the manifest. Installed users who have not
  yet been offered the version simply never see it. Halting is always the
  safe direction and needs no second approval; resuming does (§8).

- [ ] Draft reviewed and published (§7.1).
- [ ] Live manifest fetch-verified at the endpoint (§7.2).
- [ ] Promotion time, ring, and pre-registered halt criteria recorded in
      the ticket.

---

## 8. Rollback — when a live release turns out bad

**First move is always the halt (§7.3): freeze manifest promotion.** That
stops the spread in seconds and buys time. Rollback — dealing with users
already on the bad version — is decided second, against the ticket's
pre-registered criteria. Full playbooks: `RELEASE.md` §4. What follows is
the operator summary and the one mechanical fact that shapes everything.

### 8.1 The downgrade fact — why "just re-point the manifest" does not work for the shell

The Tauri updater compares version numbers **monotonically**: an install
on `0.3.0` offered a manifest saying `0.2.0` treats it as *no update
available* and **will not downgrade**. Repointing `latest.json` at the old
release therefore:

- ✅ **halts** the rollout — users still on older versions never move up
  to the bad one;
- ❌ does **nothing** for users already on the bad version — they stay on
  it until offered something with a **higher** version number.

(`RELEASE.md` §4.2 documents a server-directed signed-downgrade window as
an emergency-only mechanism with strict preconditions and a bounded
window. It is not the default path and is never left enabled.)

### 8.2 The practical shell rollback: forward-fix repackage

1. Halt (§7.3). Record ring, measured numbers vs the pre-registered
   criteria, and decision-maker in the ticket.
2. Take the **previous known-good code** (plus the minimal fix, if already
   understood) and bump it to a **new, higher version** than the bad one
   (bad `0.3.0` → rollback release `0.3.1` containing the known-good
   payload). Follow §2–§5 of this runbook for it — build and sign through
   the normal pipeline, inside-out; a rollback is not an excuse to bypass
   §5 verification.
3. Publish and promote through the rings (`RELEASE.md` §2). The ticket
   may justify faster Ring 0/1 passage **only** where the payload is
   genuinely the previously soaked one — record the reasoning.
4. Users on the bad version update *forward* to the rollback release
   through the normal updater path — same Authenticode + minisign chain,
   no special client behaviour.

### 8.3 Backend rollback — the fast path (use it when it applies)

If the defect lives in the Python backend (prompts, providers, pipeline —
most defects do), do **not** escalate it into a shell release. Channel B
rolls back in seconds by repointing `backend.json` at the retained
previous backend version; clients re-flip `current.txt` to the previous
on-disk release on their next check, with no download. Procedure and
verification: `RELEASE.md` §4.1. Freeze further backend promotion until
the fix re-enters at Ring 0.

### 8.4 Already-updated users, and the database caveat

- **Already-updated users stay on the bad version until §8.2 (or §8.3)
  reaches them.** Assess explicitly whether that is safe for the soak
  period: a cosmetic defect usually is; a data-corrupting or
  gate-bypassing one triggers §3's immediate-halt class in `RELEASE.md`
  and may justify the emergency downgrade window instead. "We halted" is
  never the whole answer — the ticket must state what already-updated
  users experience and for how long.
- **Migrations are forward-only in practice.** Alembic migrations in this
  project are written additive/forward-only for the updater era
  (`BUILD.md` §6.2), precisely so a rolled-back binary can open a
  newer-schema database. Consequences for the operator:
  - A rollback release (§8.2/§8.3) **must tolerate the newer schema** —
    before promoting it, confirm the known-good code being re-issued runs
    against a database migrated by the bad version. Test this in Ring 0
    against a migrated DB copy, not in production.
  - Never "fix" a bad release by hand-running downgrade migrations on
    user machines, and never ship a release whose migration destructively
    drops schema that a rollback target still needs — that combination
    makes rollback impossible and is a ship-blocker at review time
    (`RELEASE.md` §4.2, data safety).
  - The pre-migration DB backup taken by the backend update algorithm is
    retained until the release reaches 100% + 7 days (`RELEASE.md` §2) —
    it is the last-resort restore for a genuinely broken additive
    guarantee, and using it is an incident, not a routine step.

- [ ] Halt recorded; already-updated impact stated in the ticket.
- [ ] Path chosen: backend repoint (§8.3) / forward-fix repackage (§8.2) /
      emergency downgrade (`RELEASE.md` §4.2b) — with reasons.
- [ ] Rollback target verified against the newer DB schema before
      promotion.

---

## 9. Pre-launch checklist — the gate before the first PUBLIC release

`RELEASE.md` §1 is the per-release checklist; this section is the
**one-time, first-public-release** gate that sits in front of it. Every
box must be checked before the first non-internal tag is treated as
public. If any box cannot be checked, the release is internal-only,
whatever the calendar says.

### 9.1 The compliance gate — uploads stay Private

- [ ] **Compliance audit gate confirmed.** The app ships with YouTube
      publishing restricted — uploads stay **Private** — until the
      project's **compliance audit** passes. Before the first public
      release, the operator must confirm the gate's exact in-app
      state/flag **with the hardening team's deliverable** and record what
      they confirmed (and from which document/build) in the release
      ticket. **This runbook deliberately does not name a flag**: the
      name belongs to the hardening team's deliverable, and an operator
      who finds a differently-named control than expected must stop and
      reconcile, not guess. A public release must never silently widen
      what the app will publish.

### 9.2 Release-readiness items

- [ ] **Icons present:** real generated icons committed in
      `src-tauri/icons/` (§2.3 Blocker A) — not the `README.txt`-only
      placeholder state.
- [ ] **Updater endpoint is the real HTTPS host:** `tauri.conf.json`
      endpoints contain no `example.invalid`, are HTTPS, and return the
      staged manifest (§2.3 Blocker C, §7.2).
- [ ] **Updater pubkey set** and matching the escrowed private key
      (§1.4, §2.3 Blocker B).
- [ ] **Escrow check done:** the second person has physically recovered
      the updater private key, the backend minisign key, and demonstrated
      signing-service access — during this release cycle, not from memory
      (§1.4, `RELEASE.md` §1.5).
- [ ] **Signing identity live:** §1.2 decided and validated; the §1.3
      secrets present (names verified in GitHub Settings); `check-secrets`
      on a rehearsal run reports signing as configured — not skeleton
      mode.
- [ ] **VirusTotal scan:** installer **and** sidecar submitted to
      VirusTotal and the results **reviewed** (not merely submitted);
      report links recorded in the ticket. If Defender or any major
      engine flags the PyInstaller sidecar, the `--onedir` individual file
      is submitted to Microsoft for malware analysis **before** the
      release is announced (`RELEASE.md` §1.2).
- [ ] **Clean-VM install tests passed** on **Windows 10 and Windows 11**,
      per-user, **no admin rights**, using the full matrix in
      `RELEASE.md` §1.3 — at minimum for a first release: fresh install,
      upgrade-over-previous, backend-only update, simulated *failed*
      backend update (auto-rollback), both uninstall modes, offline
      install, and silent install. A dev machine with runtimes already
      present does not count as a test.
- [ ] **Download page ready:** canonical artefact URL only (no mirrors),
      published SHA-256 hashes, verified publisher name, and the annotated
      SmartScreen "More info → Run anyway" walkthrough (§6,
      `RELEASE.md` §1.4).
- [ ] **Changelog + version agreement:** §2 and §3 complete; preflight
      green.

---

## 10. First-release expectations — the rehearsal

The first real tag proves things no document, Linux VM run, or code
review can. Until it happens, every item below is **unverified**, and this
runbook does not pretend otherwise. Tag the first one as a rehearsal —
e.g. `v0.1.0-rc` style pre-release (or `v0.1.0` built but **not
announced**) — whose artefacts go through §5 verification and Ring 0
internal testing only.

**Things that can only be proven on the first real Windows CI run:**

1. **Rust/Tauri compiles on `windows-latest`** — the shell has never been
   built by a real toolchain in CI (the dev VM has no Rust toolchain;
   `DEV_SETUP.md` §1). Expect first-run compile/config errors; that is the
   rehearsal working, not failing.
2. **WebView2 `embedBootstrapper` behaviour** — how the bootstrapper
   actually behaves on a clean Windows 10 machine without the runtime,
   vs Windows 11 where it is pre-installed (`BUILD.md` §4), including the
   `minimumWebview2Version` upgrade path.
3. **PyInstaller freeze on Windows** — the `--onedir` sidecar build from
   `backend/agent-backend.spec` on a real Windows runner: hidden imports,
   native wheels, and whether the staged `externalBin` layout is exactly
   what the bundler expects.
4. **`signtool` availability and the signing calls themselves** — once
   secrets exist: runner SDK layout, the Azure signing client invocation
   (currently a marked placeholder block in the workflow) or the OV
   PFX/thumbprint path, RFC 3161 timestamping, and the
   `verify /pa /all` gate passing on real signatures for the first time.
5. **NSIS per-user install without admin** — the generated installer
   installing to `%LOCALAPPDATA%\Programs\YouTubeAgent` with no UAC
   prompt, hooks (`hooks.nsh`) firing correctly, and the updater's
   passive install mode working over the top of it.
6. **Icon generation → bundle acceptance** — that the `tauri icon` output
   (§2.3) is what the bundler and Windows shell actually display.
7. **Updater round-trip** — an installed rehearsal build checking the
   **real** endpoint, verifying the **real** minisign signature against
   the embedded pubkey, and applying an update — plus refusing a tampered
   one (§5.2).
8. **Real run duration** — the §4.2 stage timings that turn this runbook's
   duration estimate into a measured baseline.

**Rehearsal rules:**

- [ ] Rehearsal artefacts are verified (§5) and installed on internal
      machines / clean VMs (Ring 0) — and **not announced, not linked
      publicly, and not promoted** beyond Ring 0.
- [ ] Every first-run failure is triaged via §4.3 and fixed in the
      pipeline/config — the rehearsal's output is a trustworthy pipeline,
      not a shippable binary.
- [ ] Actual timings, failure notes, and the exact fixes are recorded in
      the release ticket and, where they change procedure, folded back
      into this runbook.
- [ ] Only after a clean rehearsal does the first **public** release run
      this runbook end-to-end against §9's gate.

---

## Appendix A — Quick reference

**Version lives in:** `src-tauri/tauri.conf.json` · `src-tauri/Cargo.toml` ·
`package.json` · `backend/app/__init__.py` · `backend/pyproject.toml` (§2.1)

**Secret names live in:** GitHub Settings → Secrets and variables → Actions;
authoritative list = header of `.github/workflows/release.yml` (§1.3)

**Signing order (inside-out, always):** build UI → PyInstaller sidecar
(`--onedir`) → sign inner binaries → `tauri build` → sign installer +
updater artefacts → `signtool verify /pa /all` → emit `latest.json` +
`backend.json` → manifest live **last** (`BUILD.md` §5.1, §7)

**If in doubt:** halt (freeze the manifest) first, investigate second
(`RELEASE.md` §3). Halting is always reversible; a bad promotion is not.
