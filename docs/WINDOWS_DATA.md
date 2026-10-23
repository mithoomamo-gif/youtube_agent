# Windows Data Directory — Layout Spec (Round 6)

Status: **normative for the packaged Windows build.** Implemented by the
ONE canonical resolver, `backend/app/data_dir.py::resolve_data_dir`
(re-exported via `app.config`), and mirrored in code by
`app.data_dir.DataDirLayout` / `Settings.layout`. If this document and
the resolver ever disagree, the resolver is wrong — fix it, not the doc.

---

## 1. The resolver contract

`resolve_data_dir(env=None, *, platform=None, home=None) -> Path`

Resolution order — first match wins:

| # | Condition | Result |
|---|-----------|--------|
| 1 | `YTA_DATA_DIR` set (non-blank) in the environment | That path, verbatim after `~` expansion — **on every platform** |
| 2 | Windows (`os.name == "nt"` / `win32`) and `APPDATA` set | `%APPDATA%\YouTubeAgent` |
| 3 | Windows and `APPDATA` absent | `<home>\AppData\Roaming\YouTubeAgent` (reconstructed roaming profile) |
| 4 | Non-Windows and `APPDATA` set (legacy compat, Rounds 1–5 behaviour) | `<APPDATA>/YouTubeAgent` |
| 5 | Non-Windows otherwise (dev / Linux / macOS) | `~/.youtube-agent` — **the historical dev default, unchanged** |

Rules:

- **One resolver.** `app.config.load_settings` builds
  `Settings.data_dir` from it; `Settings.db_path` / `log_dir` /
  `layout` derive from that; `app.db.default_db_path`, the Style Lab
  storage/claimer/intake fallbacks, comment-intel storage and
  retention-sections storage all derive from it. No module may
  re-implement `APPDATA`/`XDG_DATA_HOME`/`Path.home()` data-root
  logic (see §6 for the audited exceptions still open).
- **Pure function.** The resolver creates nothing, moves nothing and
  reads no files. Callers keep their existing `mkdir` behaviour.
- **The packaged runtime sets `YTA_DATA_DIR`.** The Tauri shell /
  freeze packaging passes the resolved Windows root
  (`%APPDATA%\YouTubeAgent`, obtained from the OS — Tauri
  `app_data_dir()` or the shell's own `%APPDATA%` expansion) to the
  sidecar as `YTA_DATA_DIR` on every launch. That makes rule 1 the
  operative path in production and keeps the sidecar's behaviour
  identical under test (tests set the same variable).
- **Secrets never live here.** API keys and OAuth refresh tokens live
  ONLY in the OS credential store (Windows Credential Manager, service
  name `YouTubeAgent`, via `app.secrets_store`). The data dir holds at
  most opaque credential *references* inside `app.db`. A `.env` file is
  a dev-checkout artefact (repo root), never part of the packaged
  layout.

## 2. Directory tree under the data root

Windows root shown; the same tree applies under any resolved root.

```
%APPDATA%\YouTubeAgent\
├── app.db                        USER DATA  main SQLite DB (WAL mode:
│                                            app.db-wal / app.db-shm alongside)
├── idempotency.db                USER DATA  HTTP idempotency keys
├── logs\
│   └── backend.log               LOGS       JSON-lines, rotating 10 MB × 5
│                                            (+ rotated backend.log.1..5)
├── projects\                     USER DATA  per-project working trees
│   └── <project_id>\
│       ├── content\                         research/script checkpoints (JSON)
│       ├── audio\                           voiceover WAV/MP3 + voice.json
│       ├── visuals\                         scene visuals + visuals.json
│       ├── render\                          assembled/final MP4s + assemble.json
│       ├── pack\                            thumbnail PNGs, captions, SEO pack
│       └── bridge_state.json                stage checkpoint index
├── style_lab\                    USER DATA  Style Lab intake (underscore pkg)
│   ├── competitors\<owner>\<competitor_id>\ attached competitor videos +
│   │                                        thumbnails (content-addressed)
│   └── tmp\                                 upload spool files (transient)
├── stylelab\                     USER DATA  Style Lab analysis results
│   ├── index.json                           (no-underscore pkg)
│   └── <competitor_key>\analysis.json + breakdown.md
├── comment_intel\                USER DATA  comment-analysis rollups
│   ├── index.json
│   └── <scope>-<key>\analysis.json
├── analytics\
│   └── retention_sections\       USER DATA  retention-by-section analyses
│       ├── index.json
│       └── <key>\analysis.json + annotations.md
├── reports\                      USER DATA  generated channel/video reports
│                                            (*.md + *.json receipts)
├── cache\
│   └── stock\                    CACHE      stock-footage cache (2 GiB cap,
│                                            LRU) — when YTA_DATA_DIR is set;
│                                            dev default stays in the checkout
├── models\
│   └── whisper\                  CACHE      faster-whisper weights — when
│                                            YTA_DATA_DIR is set (or
│                                            YTA_WHISPER_MODEL_DIR overrides);
│                                            dev default stays <repo>\models
└── tmp\                          SCRATCH    app-private spool/scratch
                                             (safe to delete while stopped)
```

Related locations **outside** the data root (by design, unchanged):

| Location | What | Why outside |
|----------|------|-------------|
| OS credential store (Credential Manager) | API keys, OAuth refresh tokens | Secrets never touch the filesystem data dir |
| `<install dir>\` (Program Files / app bundle) | App binaries, `prompts\`, `backend\config\models.yaml`, `prices.yaml`, TTS calibration data | Read-only shipped assets, versioned with the app |
| Dev checkout only: `<repo>\outputs\…`, `<repo>\models\whisper\`, `<repo>\backend\cache\stock\`, `<repo>\.dev-data\` | Proof/run outputs, whisper weights, stock cache, dev sidecar state | Dev-only artefacts; see §5 migration note and §6 follow-ups |

## 3. User data vs cache vs logs — retention expectations

| Class | Paths | Delete while app stopped? | Notes |
|-------|-------|---------------------------|-------|
| **User data** | `app.db`, `idempotency.db`, `projects\`, `style_lab\`, `stylelab\`, `comment_intel\`, `analytics\`, `reports\` | **No** — this is the user's channels, projects, history and learning state | Back up by copying the whole root |
| **Logs** | `logs\` | Yes — diagnostics only | Bounded: 10 MB × 5 files ≈ 60 MB max |
| **Scratch** | `tmp\`, `style_lab\tmp\` | Yes | Spool files for in-flight uploads; may contain partial uploads after a crash |
| **Cache** | `cache\stock\`, `models\whisper\` | Yes | Populated under the root whenever `YTA_DATA_DIR` is set (i.e. always in packaged runs). With the override unset (dev), both keep their historical checkout locations — see §6 items 2–3 |

Size expectations (measured on the dev VM, Rounds 2–5, order-of-magnitude
guidance for the installer/support docs):

- `app.db`: ~0.4 MB after seeding; grows with projects/jobs/analytics —
  expect tens of MB in normal use, not GB.
- One finished video project under `projects\<id>\`: ~35 MB final MP4
  (720p draft tier) to ~120 MB (1080p master tier), plus scene visuals
  and intermediates — budget **0.2–1 GB per project** while its working
  tree is retained.
- Style Lab attachments: user-supplied competitor videos, hard cap
  **2 GiB per video**, thumbnails **16 MiB** (enforced at intake).
- Logs: hard-bounded ≈ 60 MB (rotation).
- Free-space guidance for packaging: the data root is **not**
  size-capped as a whole today; the first-run wizard/support docs
  should recommend ≥ 10 GB free on the profile volume for active use.

## 4. Windows ACL notes

- `%APPDATA%` (`C:\Users\<user>\AppData\Roaming`) is per-user by
  construction: the default Windows ACL grants the owning user (and
  SYSTEM/Administrators) access and no other interactive user. The app
  therefore inherits **user-only access** without creating custom ACLs,
  which is the correct posture for a directory whose DB references
  credential-store entries and holds unpublished video projects.
- The installer must **not** widen ACLs on the data root, and must not
  relocate the root to a shared location (`ProgramData`, install dir)
  without an explicit, documented decision — that would expose one
  Windows user's projects/DB to other users of the machine.
- If `YTA_DATA_DIR` is pointed somewhere custom (portable installs,
  enterprise policy), ACL responsibility moves with it: the docs for
  that deployment must state the directory should be readable/writable
  only by the user account running the app. The resolver itself sets
  no ACLs.
- Roaming caveat: `%APPDATA%\Roaming` can roam/sync in domain
  environments. Large `projects\` working trees roaming is wasteful;
  if that becomes a real deployment, the follow-up is a split layout
  (DB/settings in Roaming, `projects\`/`cache\` under
  `%LOCALAPPDATA%\YouTubeAgent`) via the same resolver — **not**
  implemented in Round 6, recorded here so it is a decision, not a
  surprise.

## 5. Migration note — no silent moves

- **Existing dev data stays where it is.** On this dev VM the sidecar
  has always run with `YTA_DATA_DIR` set explicitly (`.dev-data\` in
  the checkout) or fallen back to `~/.youtube-agent`; both behaviours
  are preserved bit-for-bit by the resolver (rules 1 and 5). Nothing
  in Round 6 copies, moves or deletes any existing data directory.
- The canonicalisation changed only **last-resort fallbacks that
  previously diverged** and only ran when settings loading had already
  failed: `app.db`'s fallback (`~/.local/share/YouTubeAgent`) and
  comment-intel's fallback (same XDG-style root) now resolve through
  the canonical root instead. No shipped path (settings-driven) moved.
- A future packaged-Windows first run starts with an **empty**
  `%APPDATA%\YouTubeAgent`; importing a dev checkout's data is a
  manual copy of the tree in §2. There is deliberately **no** automatic
  migration/import in Round 6.
- The DB schema migrates in place (alembic chain, single head) wherever
  `app.db` already lives; the data-dir work does not touch schema.

## 6. Audit — hard-coded paths (Round 6)

Fixed (routed through `resolve_data_dir`):

| Site | Before | Now |
|------|--------|-----|
| `backend/app/config.py` (`_default_data_dir`, `load_settings`, `Settings.data_dir` default) | own `APPDATA`/`Path.home()` logic | delegates to the resolver |
| `backend/app/db.py::default_db_path` fallback | own `APPDATA`/`XDG_DATA_HOME` logic → divergent `~/.local/share/YouTubeAgent/app.db` | resolver + `app.db` |
| `backend/app/comment_intel/storage.py::default_comment_intel_dir` | `YTA_DATA_DIR` else divergent `~/.local/share/YouTubeAgent/comment_intel` | resolver + `comment_intel` |
| `backend/app/stylelab/storage.py::default_stylelab_dir` fallback | literal `~/.youtube-agent/stylelab` | resolver + `stylelab` |
| `backend/app/retention_sections/storage.py::default_retention_sections_dir` fallback | literal `~/.youtube-agent/analytics/retention_sections` | resolver + that suffix |
| `backend/app/stylelab/claimer.py::_resolve_data_dir` fallback | env var, else `db.default_db_path().parent` | resolver |
| `backend/app/style_lab/service.py::_data_dir` fallback | env var, else `db.default_db_path().parent` | resolver |

Audited, already canonical (derive from `Settings.data_dir` /
`YTA_DATA_DIR`-first logic equivalent to the resolver) — left as-is:
`app.main` (DB + log dir via settings), `app.style_lab` media spool
(`<data_dir>/style_lab/tmp`), `app.stylelab.media` (caller-supplied
data dir, relative storage paths validated under it),
`app.integration.stage_bridge.get_base_dir`'s env branch,
`app.reporting.service._resolve_reports_dir`'s env branch,
`app.idempotency` (explicit `YTA_DATA_DIR/idempotency.db`).

Follow-ups (deliberately NOT changed in Round 6 — behaviour or
ownership risk; listed for the coordinator):

1. `app.integration.stage_bridge.get_base_dir` — final fallback is
   `<cwd>/outputs/projects` when settings cannot load, and
   `YTA_PROJECTS_DIR` takes precedence over the data root. Routing
   fully through the resolver changes token-less tooling behaviour;
   needs its own decision.
2. ~~`app.integration.stock_fetch.DEFAULT_CACHE_DIR`~~ — **RESOLVED**
   (data-dir contract pass): `default_cache_dir()` now returns
   `<data root>/cache/stock` when `YTA_DATA_DIR` is explicitly set
   and the historical `<repo>/backend/cache/stock` when it is unset,
   so the dev cache layout (~1.3 GB on this VM) is NOT invalidated
   and packaged builds never write into the install tree. The
   `DEFAULT_CACHE_DIR` constant remains as the dev fallback.
3. ~~`app.providers.transcription.whisper_faster.default_model_dir`~~ —
   **RESOLVED** the same way: `YTA_WHISPER_MODEL_DIR` still wins;
   then `<data root>/models/whisper` when `YTA_DATA_DIR` is set;
   then the historical `<repo>/models/whisper` (~0.9 GB on this VM).
4. `app.integration.run` CLI default output — **RESOLVED** the same
   way (`<data root>/projects/cli_<niche>` when the override is set;
   `<repo>/outputs/round2/…` unchanged when unset).
   `app.integration.content`'s `Path.cwd()` base remains open: it is
   only the channel-default *file* lookup base for style profiles in
   standalone content calls (explicit spec path and
   `YTA_STYLE_PROFILE_PATH` are exact); the canonical style-profile
   store is the `style_profiles` table in `app.db`.
5. `app.idempotency._default_db_path` — returns `None` (→ in-memory)
   when `YTA_DATA_DIR` is unset instead of resolving the canonical
   root. Changing it makes the store always file-backed; safe-looking
   but a semantic change beyond this workstream.
6. `src-tauri/src/lib.rs` — the shell still has the phase-1 TODO for
   actually spawning the sidecar; when that lands it must pass
   `YTA_DATA_DIR` = the OS-resolved `%APPDATA%\YouTubeAgent`
   (packaging/freeze team's workstream, per the Round 6 split).
7. Repo-relative shipped assets (`prompts\`, `backend\config\*.yaml`,
   TTS `sample_texts`/`calibration_data.json`, ComfyUI workflow
   templates) resolve via `Path(__file__)` — correct for installed
   assets, listed here only to close the audit: they are **not** data
   paths and must never move under the data root.

Verification: `backend/tests/test_data_dir.py` (resolver precedence),
`backend/tests/test_data_dir_contract.py` (consumer defaults for
items 2–4 above, plus a subprocess boot proof: `python -m app.main`
with `YTA_DATA_DIR` at a fresh directory creates `app.db` +
`logs/backend.log` there, answers `/health/ready` with `db: ok`,
and writes nothing new into the repo tree/CWD).

## 7. Uninstall data policy

**Default: uninstalling preserves user data.** Removing the app
(binaries, shortcuts, registry keys, scheduled tasks) does **not**
delete the data root (`%APPDATA%\YouTubeAgent`) — channels, projects
and video history, Style Lab profiles, settings, the database and
logs all survive, so a reinstall or upgrade picks up where the
creator left off. This matches the uninstall matrix in
`docs/RELEASE.md` (§26.7, case 5).

**Purge happens only on explicit opt-in.** In Settings → Data &
Privacy, the toggle **"Remove all my data on uninstall"** (default
OFF) controls it:

- Settings flag key: `privacy.remove_data_on_uninstall`
  (persisted in the `settings` table; typed API:
  `GET|PUT /api/v1/settings/privacy`; the generic `PUT /settings`
  honours the same key).
- When the toggle is ON, the backend writes a marker file named
  **`uninstall-purge.marker`** into the data root. When it is OFF,
  the marker is removed. The backend also reconciles the marker
  against the persisted flag at startup, so the two cannot drift.
- The NSIS uninstaller hook (`NSIS_HOOK_POSTUNINSTALL` in
  `src-tauri/windows/hooks.nsh`, shell/packaging team) checks for
  the marker's existence: **if the marker is present**, uninstall
  purges the data directory (and the stored credentials) after the
  app itself has been removed, without asking again — the toggle
  was the user's decision. If the marker is absent (the default), a
  silent uninstall always keeps the data, and an interactive
  uninstall may ask "Also delete settings, channel connections and
  history?" with **No as the default** — answering No (or not being
  asked) keeps everything.
- Toggling the setting deletes nothing by itself; it only takes
  effect at uninstall time, and turning it back off before
  uninstalling keeps the data. A purge cannot be undone.
- Rendered/exported videos live outside the data root (default
  `%USERPROFILE%\Videos\YouTubeAgent`) and are never deleted by
  uninstalling, in either mode.

Implementation: `backend/app/uninstall_policy.py` (marker name, flag
key, write/remove/reconcile), `backend/app/routers/settings.py`
(typed privacy endpoints + generic-settings sync), Settings UI
`DataPrivacyCard` in `src/screens/Settings.tsx`. Tests:
`backend/tests/test_uninstall_policy.py`.
