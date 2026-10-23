# RELEASE — Release-Day Checklist, Rollout Rings & Rollback Playbooks

> The runbook for shipping a YouTube Agent release. Adapted from roadmap section **§36 §26.9** (release-day checklist), **§26.6** (rollout rings / rollback), **§26.3** (signing / SmartScreen), **§26.7** (uninstall), and Master Roadmap **Part 11.8 / 11.6**. Build mechanics live in `BUILD.md`; local setup lives in `DEV_SETUP.md`.
>
> **The rule that governs this whole file:** halt criteria are written down **before** promotion, key custody is verified **before** release, and manifests go live **last**. A release that skips a gate is not “fast” — it is unrecoverable in exactly the ways this file exists to prevent.

---

## 1. Release-day checklist (§26.9)

Work top to bottom. Do not promote any manifest until every box in §1.1–§1.5 is checked for this release.

### 1.1 Build & sign in CI — inside-out, always

- [ ] CI on a **Windows runner**: build UI → PyInstaller sidecar (`--onedir`) → **sign inner binaries** (SHA-256 + RFC 3161 timestamp) → `tauri build` → **sign installer + updater artefacts** → emit `latest.json` + `backend.json`
- [ ] `signtool verify /pa /all` passes on **every** artefact (shell, sidecar, uninstaller, installer, updater artefacts)
- [ ] SHA-256 hashes for all artefacts recorded for the release page
- [ ] FFmpeg version + hash in the build manifest match the pinned build (see `BUILD.md` §1)
- [ ] Version numbers agree everywhere: installer filename `YouTubeAgent_<ver>_x64-setup.exe`, `latest.json`, `backend.json`, in-app `system/info`
- [ ] **No manual signing step on any laptop.** If a human had to sign something by hand, stop — that is a pipeline bug to fix before release, not a step to repeat

### 1.2 Pre-publish scans

- [ ] **VirusTotal scan** of the installer + sidecar submitted; results reviewed (not just “submitted”)
- [ ] If Defender / any major engine flags the PyInstaller sidecar: file submitted to **Microsoft for malware analysis before the release is announced**, and the `--onedir` individual file (not just the installer blob) is the thing submitted
- [ ] No unsigned artefact is reachable from any public URL — signing starts from the **first** public build; there is no unsigned “beta” of this product

### 1.3 Clean-VM install tests — Windows 10 **and** Windows 11

Run the full matrix on **both** OS versions, on **clean VMs**, **per-user, with NO admin rights**. A test run as admin, or on a dev machine with runtimes already present, does not count.

| # | Case | Pass condition |
|---|---|---|
| 1 | **Fresh install** | Installs to `%LOCALAPPDATA%\Programs\YouTubeAgent` with no UAC prompt; first-run wizard completes; sidecar reaches `/health/ready`; bundled FFmpeg probe passes |
| 2 | **Upgrade over previous version** | DB, API keys / OAuth tokens, settings, and `backend\current.txt` all survive; in-flight / queued jobs are still present after relaunch; no data loss |
| 3 | **Backend-only update** (Channel B) | New backend downloads, SHA-256 + minisign verify, extracts side-by-side, migrations run against a DB backup, health + FFmpeg smoke test pass, `current.txt` flips — shell version unchanged |
| 4 | **Simulated failed backend update** | Corrupted / mis-signed / failing-health backend package is **rejected**: staging deleted, previous sidecar restarted, DB backup restored if migrations ran, user-visible message says *“update could not be applied — you’re still on vX, nothing was changed”*, and `current.txt` never moved |
| 5 | **Uninstall — default (keep data)** | Binaries, shortcuts, registry keys, Task Scheduler entries, and autostart are removed; `%APPDATA%\YouTubeAgent` (settings, DB, logs) is **kept**; secrets are **kept**; **rendered videos + project files in the output folder are untouched** |
| 6 | **Uninstall — full delete** (answer Yes to “Also delete settings, channel connections and history?”) | Same as case 5, **plus** `%APPDATA%` state removed and API keys / YouTube OAuth refresh tokens deleted from Credential Manager / DPAPI store. Videos still untouched — there is **no** uninstall option that deletes user videos |
| 7 | **Offline install** | Install succeeds with no network, using the `embedBootstrapper` build. (WebView2 is pre-installed on Win11 — this case matters most on Win10; if the runtime itself needs downloading, record exactly where the offline path stops and say so in the release notes rather than claiming full offline support.) |
| 8 | **Silent install** (`/S`) | Install + uninstall both complete silently — this is also the smoke path future Store/IT deployment depends on |
| 9 | **Update does not interrupt work** | With a render / upload in flight, the update downloads but does **not** install; the *“will install when the current video finishes”* state is shown and install happens only when the job queue is idle |
| 10 | **Reinstall after uninstall (data kept)** | First run detects the previous setup and offers *“We found your previous setup — restore it?”* (wizard Step 0) |

Also verify on both OS versions: WebView2 handling (ancient runtime is **upgraded** via `minimumWebview2Version`, not a mysterious failure), VC++ Redistributable remediation via the POSTINSTALL hook (passive, no forced reboot), and that shared components (WebView2 runtime, VC++ Redist) are **left installed** after uninstall.

### 1.4 SmartScreen expectations — set them before users meet them

Reputation starts at **zero** for a new publisher and is earned per-publisher + per-file-hash from download volume — it cannot be bought (EV no longer bypasses it; see `BUILD.md` §5.2). For early releases this is expected behaviour, not a defect.

- [ ] Release notes for this version include the SmartScreen note, verbatim in substance:
  > *“New publisher — you may see a Windows SmartScreen warning on first install. Choose **More info → Run anyway**, and verify the publisher name shown is **<exact publisher name string on the certificate>** before continuing.”*
- [ ] Download page shows the annotated SmartScreen screens (“More info → Run anyway”), the verified publisher name users should see, and the published SHA-256 hashes
- [ ] Publisher name string in the notes is copied **character-for-character** from the signing certificate — a mismatch here trains users to ignore the one check that protects them
- [ ] All download / marketing links point at the **one canonical** artefact URL (no mirrors — mirror sprawl dilutes reputation)

### 1.5 Key escrow verification — release-blocking

A release that only one laptop, or one person, can sign or update is a release-process failure. Verify **before** promotion that a **second team member** can independently recover each of:

- [ ] **Tauri updater private key** + password (`TAURI_SIGNING_PRIVATE_KEY`, `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`) — CI secrets + offline escrow copy in the project password manager. ⚠️ Losing this key **permanently orphans the installed base**: those installs can never be updated, only reinstalled.
- [ ] **Backend minisign private key** (Channel B — a **separate** key from the updater key) — same escrow standard
- [ ] **Authenticode / Azure Artifact Signing access** — the second member can actually reach the signing identity (Azure HSM-backed or OV cloud-HSM/token path) and complete a test sign

Escrow check method: the second member performs the recovery (opens the vault entry / accesses the signing service) during the release call — “it’s in the password manager, probably” is not a verification.

### 1.6 Release ticket — filled in before any promotion

- [ ] Release ticket exists with: version(s) (app + backend), artefact hashes, VM test results (§1.3), VirusTotal link
- [ ] **Halt / kill criteria written into the ticket** (see §3) — numbers, thresholds, and who may call a halt
- [ ] Rollback targets named: previous backend version (for `backend.json` repoint) and previous known-good shell code (for a forward-fix repackage)
- [ ] Ring 0 soak start time recorded (promotion clock starts there, not at build time)

---

## 2. Rollout rings (§26.6 / §11.6)

Rings apply to **both update channels identically** (shell via `latest.json`, backend via `backend.json`). Because manifests can be generated dynamically per request, the server decides per-install which version is “latest” — bucketing, promotion, and freezes are **server configuration changes, not new builds**.

| Ring | Audience | Minimum soak | Advance gate |
|---|---|---|---|
| **Ring 0 — Internal** | Dev machines + clean Win10 / Win11 VMs | **≥ 24 h** | Install / upgrade / uninstall smoke suite green (the §1.3 matrix) |
| **Ring 1 — Beta** | Opt-in users (Settings → “Join beta updates”) **+ 5% deterministic slice** of stable installs (bucketed by hashed install-ID) | **48 h** | **All** must hold: update success **≥ 98%** · crash-free sessions **≥ 99%** · **zero** sidecar-health failures attributable to the release |
| **Ring 2 — Broad** | **25% → 50%** of stable installs, one step at a time | **24–48 h per step** | Same gates as Ring 1, **re-measured at each step** — a gate passed at 25% does not carry to 50% |
| **Ring 3 — Full** | 100% of installs | — | Manifest promoted to the default response |

Rules:

- **Never skip a ring or a step to “catch up”.** A late release is a scheduling fact; a bad release pushed to 100% is an incident.
- Deterministic bucketing means the same install stays in the same slice — do not re-randomise mid-rollout.
- The pre-migration DB backup for backend updates is retained until the release reaches **100% + 7 days** (see §4).

---

## 3. Halt criteria & the pre-registered kill criteria concept

### Concept

Every release **pre-registers** its kill criteria: the concrete numbers — crash rate, failed-update rate, sidecar-health failures — that, if crossed, halt the rollout. They are written in the release ticket ( §1.6) **before** Ring 0 promotion.

Why pre-register: a halt decision made against numbers written down *before* rollout is an engineering decision. The same decision improvised *during* an incident — under pressure, with sunk cost arguing to continue — is how bad releases reach 100%. If it is not written down beforehand, it does not exist.

### Halt mechanics

1. **HALT = stop promoting the manifest (ring freeze).** No client action is needed. Installs that have not yet seen the bad version simply never see it.
2. Freezing is always available and always reversible — when in doubt, freeze first and investigate second. Investigation does not require the rollout to keep moving.
3. A halt is called against the **pre-registered criteria** (e.g. update success drops below 98%, crash-free below 99%, *any* attributable sidecar-health failure in Ring 1). The person named in the ticket may call it; no second approval is needed to halt — halting is the safe direction. **Resuming** after a halt requires the cause to be identified and the criteria to hold again over a fresh soak at the current ring.
4. Default starting criteria for any release (tighten per release, never loosen mid-rollout):
   - [ ] Update success rate < **98%** at the current ring → **halt**
   - [ ] Crash-free sessions < **99%** → **halt**
   - [ ] Any sidecar-health failure attributable to this release → **halt** (zero-tolerance in Ring 0/1)
   - [ ] Any report of data loss, duplicate upload, or gate bypass attributable to this release → **halt immediately, regardless of rates**
   - [ ] Failed-update auto-rollback (Channel B) not firing in the field as it did in VM case §1.3 #4 → **halt**

### After a halt

- [ ] Record: time, ring/step, measured numbers vs pre-registered criteria, decision-maker
- [ ] Decide: fix-forward (new version, restart at Ring 0) vs rollback (§4) — a halt alone does not move already-updated installs backward
- [ ] Already-updated installs are assessed explicitly: are they safe to leave on the bad version until a fix ships, or must a rollback playbook run?

---

## 4. Rollback playbooks

### 4.1 Backend rollback — the fast path (seconds)

Use for: any bad backend release (prompts, providers, pipeline logic, FFmpeg bundle, migrations behaving badly in the field).

1. **Repoint `backend.json`** on the update endpoint to the **previous known-good version** (URL, SHA-256, and minisign signature for that version — the retained release, not a rebuild).
2. Clients that already swapped detect the older target on their **next update check**, and **re-flip `current.txt`** to the retained previous release — **no download required**, because the two previous releases are kept on disk.
3. **Effect time: seconds to minutes** (bounded by the client check interval), not a release cycle.
4. Verify: spot-check that updated clients report the previous `backendVersion` in `system/info`, the sidecar passes `/health`, and the DB state is consistent (migrations are forward-only + additive in the updater era, and the pre-migration backup exists until 100% + 7 days — restore from it only if the additive guarantee was actually broken, and treat that as an incident, not a routine step).
5. Freeze further backend promotion until the fix ships and re-enters at **Ring 0**.

> This speed is the reason Channel B exists. If a fix can ship as a backend package, it must — do not escalate a Python problem into a shell release.

### 4.2 Shell rollback — the slow path (use sparingly)

The Tauri updater normally moves **forward only**. In preference order:

#### (a) Forward-fix repackage — **preferred**

1. Re-issue the **previous known-good code** under a **higher version number** (e.g. bad `1.5.0` → ship `1.5.1` containing `1.4.2`’s code, plus the minimal fix if it is already understood).
2. Build + sign through the normal CI pipeline (inside-out, §1.1) and roll out through the **normal rings** — it may move faster through Ring 0/1 only if the ticket records why the risk is genuinely lower (byte-identical payload to a previously soaked release).
3. Why preferred: it uses the normal update path, needs no special client flags, and keeps the Authenticode + minisign reputation history intact.

#### (b) Signed downgrade — emergency only

1. Configure the updater (via the signed, dynamically generated manifest) to accept a **server-directed older version**, for a **bounded window only**.
2. Preconditions: signed dynamic manifest in place; the downgrade target is fully signed (Authenticode + minisign); the window and target are recorded in the incident ticket **before** enabling.
3. **Remove the downgrade allowance from server config immediately after** the window closes. A permanently downgrade-friendly client is a security liability — never leave it enabled “just in case”.

#### Data safety on any shell rollback

- DB migrations are **forward-only and additive** — a rolled-back shell/backend pair must still open the current DB. A release containing a destructive schema drop in the rollback window is not shippable.
- After any shell rollback, the shell health-checks the sidecar on relaunch; a failed check surfaces the **Rollback / Update-the-app** repair action, never a crash loop.

### 4.3 What rollback is *not*

- Not a substitute for halting: **halt first** (stop the spread), then roll back (fix those already affected).
- Not a manifest edit made casually: every repoint / repackage / downgrade window is recorded in the release/incident ticket with time, actor, and target version.
- Not a reason to rotate signing keys or change publisher identity: that destroys SmartScreen carry-over and, for the updater key, can orphan installs. Key rotation is its own planned operation, never an incident improvisation.

---

## 5. Post-release

- [ ] Ring 3 promotion time + final gate numbers recorded in the release ticket
- [ ] Download page + release notes confirmed live with correct hashes, publisher name, and SmartScreen note
- [ ] Backend releases on user machines will prune to current + 2 previous automatically — no action needed, but confirm the Settings disk guardrail reflects the new release’s cost
- [ ] Pre-migration DB backup retention clock noted: keep until this release has been at 100% for **7 days**
- [ ] Any field failure, user correction, or near-miss from this release has a regression case added (golden dataset / fault corpus) — a fixed bug must add the case that would have caught it
- [ ] Retrospective note in the ticket: did the pre-registered kill criteria match reality? Tighten the defaults in §3 if they were too loose; never loosen them retroactively to make a release look clean

---

## 6. One-page summary

```
BUILD (CI, Windows)   Build → sign inner PEs → tauri build → sign installer/updater
                      → verify /pa /all → emit latest.json + backend.json
SCAN                  VirusTotal (+ Microsoft submission if the sidecar is flagged)
TEST                  Clean Win10 AND Win11 VMs, per-user / no admin:
                      install · upgrade · backend-only · FAILED backend (auto-rollback)
                      · uninstall (keep + full-delete) · offline · silent
ESCROW                Second member recovers: updater key · backend key · signing access
WRITE IT DOWN         Halt / kill criteria + rollback targets in the ticket — BEFORE promotion
ROLLOUT               Ring 0 (≥24 h) → Ring 1 (5%, 48 h) → Ring 2 (25% → 50%) → Ring 3 (100%)
                      Gates every step: update success ≥98% · crash-free ≥99% · zero attributable sidecar failures
IF IT GOES WRONG      HALT (freeze manifest) → backend: repoint backend.json (seconds)
                      → shell: forward-fix repackage (preferred) / signed downgrade (emergency, bounded)
```
