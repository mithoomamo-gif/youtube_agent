# Round 6 — Frozen Bundle Manifest Proof

Build under test (this worker's proof build):

- Command: `./packaging/build_sidecar.sh` (sibling build-scripts
  worker's entry point; it invokes PyInstaller with
  `packaging/yta_sidecar.spec`, `--distpath packaging/dist-sidecar`,
  `--workpath packaging/build-sidecar`, then performs the spec's
  hazard-H1 prompts copy + version stamping + Tauri staging)
- PyInstaller: **6.22.3** (already installed in `backend/.venv` —
  verified `pip list`: pyinstaller 6.22.3 + pyinstaller-hooks-contrib
  2026.9 + altgraph; **no install was performed by this worker**)
- Python: 3.12.3, platform Linux-7.0.0 x86_64 (proof build on the dev
  VM; the Windows freeze uses the same spec via `build_sidecar.ps1`)
- Source tree: git HEAD `c99e10e` + uncommitted Round-6 sibling work
  in the working tree (the freeze builds the working tree, as shipped)
- Result: **EXIT 0 — "SIDECAR FREEZE COMPLETE"**
- Bundle: `packaging/dist-sidecar/yta_sidecar/`
- **Total bundle size: 520,458,212 bytes (500 MiB on disk, 1,226 files)**
  — stamped version 0.1.0, `VERSION` + `build-info.json` present;
  also staged by the build script to
  `src-tauri/binaries/agent-backend-x86_64-unknown-linux-gnu` (+ tree)
- Verification: every audit item from `packaging/RESOURCE_AUDIT.md`
  checked on disk in the produced bundle — **104 checks, 104 PRESENT,
  0 MISSING** (full table below)

Spec build history (honesty log): attempt 1 was killed by a tool
timeout at ~335 s; attempt 2 failed in the spec itself —
`find_spec("bidi.algorithm")` raises `ModuleNotFoundError` for a
missing parent instead of returning `None` (fixed: `_existing()` now
guards with try/except); attempt 3 failed at the setuptools `wheel`
hook — excluding `wheel` makes PyInstaller abort with
`ValueError: Target module "wheel" already imported as
ExcludedModule` (fixed: `pip`/`wheel` removed from excludes, with a
comment in the spec); attempt 4 was killed by a service restart at
~1,085 s of build time; attempt 5 (via `build_sidecar.sh`) succeeded.

## Checklist (audit item → bundle path → result)

Prompts (audit #1): all 36 files verified at BOTH the frozen-expected
bundle-root location (the copy the frozen code actually reads,
hazard H1) and the `_internal` mirror — all PRESENT. Files:
`README.md` + `ai_image/ai_image.md`, `analytics/analytics.md`,
`assembly_editing/assembly_editing.md`, `audio_mixing/audio_mixing.md`,
`comments/comments.md`, `competitor_analysis/competitor_analysis.md`,
`copyright/copyright.md`, `editor/editor.md`, `fact_check/fact_check.md`,
`feedback_loop/feedback_loop.md`, `hook_retention/hook_retention.md`,
`keyword_research/keyword_research.md`, `metadata_seo/metadata_seo.md`,
`music_sfx/music_sfx.md`, `niche_profiling/niche_profiling.md`,
`outline/outline.md`, `policy/policy.md`, `pronunciation/pronunciation.md`,
`qc/qc.md`, `reporting/reporting.md`, `retention_render/retention_render.md`,
`scene_planning/scene_planning.md`,
`scheduling_community/scheduling_community.md`,
`script_writing/script_writing.md`, `stock_sourcing/stock_sourcing.md`,
`subtitles/subtitles.md`, `thumbnail_design/thumbnail_design.md`,
`thumbnail_text_ctr/thumbnail_text_ctr.md`, `title/title.md`,
`topic_ideation/topic_ideation.md`, `trend_research/trend_research.md`,
`tts/tts.md`, `upload/upload.md`, `voice_direction/voice_direction.md`,
`watchdog/watchdog.md` — bundle paths `prompts/<same>` and
`_internal/prompts/<same>`: **36/36 × 2 = 72 PRESENT**.

| Audit item | Bundle path | Result |
|---|---|---|
| #2 LLM registry | `_internal/config/models.yaml` | PRESENT |
| #3 price catalog | `_internal/config/prices.yaml` | PRESENT |
| #4 TTS calibration | `_internal/app/providers/tts/calibration_data.json` | PRESENT |
| #5 sample text | `_internal/app/providers/tts/sample_texts/english_facts.txt` | PRESENT |
| #5 sample text | `_internal/app/providers/tts/sample_texts/roman_urdu_facts.txt` | PRESENT |
| #5 sample text | `_internal/app/providers/tts/sample_texts/urdu_script_facts.txt` | PRESENT |
| #6 dictionary | `_internal/app/providers/tts/dictionaries/global.yaml` | PRESENT |
| #6 dictionary | `_internal/app/providers/tts/dictionaries/facts.yaml` | PRESENT |
| #6 dictionary | `_internal/app/providers/tts/dictionaries/crime.yaml` | PRESENT |
| #6 dictionary | `_internal/app/providers/tts/dictionaries/health.yaml` | PRESENT |
| #6 dictionary | `_internal/app/providers/tts/dictionaries/islamic_stories.yaml` | PRESENT |
| #6 dictionary | `_internal/app/providers/tts/dictionaries/tech.yaml` | PRESENT |
| #7 workflow template | `_internal/app/providers/images/workflow_templates/flux_schnell_txt2img_v1.json` | PRESENT |
| #8 migration | `_internal/migrations/versions/0001_initial_schema.py` | PRESENT |
| #8 migration | `_internal/migrations/versions/0002_approvals.py` | PRESENT |
| #8 migration | `_internal/migrations/versions/0003_budget_reservations.py` | PRESENT |
| #8 migration | `_internal/migrations/versions/0004_style_lab.py` | PRESENT |
| #8 migration | `_internal/migrations/versions/0005_learning_rules.py` | PRESENT |
| #8 migration | `_internal/migrations/versions/0006_comments.py` | PRESENT |
| #8 migration | `_internal/migrations/versions/0007_analytics.py` | PRESENT |
| #8 migration | `_internal/migrations/versions/0008_channel_quality_tier.py` | PRESENT |
| #8 migration | `_internal/migrations/versions/0009_calibration_log.py` | PRESENT |
| #8 migration | `_internal/migrations/versions/0010_reports.py` | PRESENT |
| #8 migration | `_internal/migrations/versions/0011_compliance.py` | PRESENT (replacement run only — see addendum) |
| #8 migration env | `_internal/migrations/env.py` | PRESENT |
| #8 migration template | `_internal/migrations/script.py.mako` | PRESENT |
| #8 alembic config | `_internal/alembic.ini` | PRESENT |
| #9 TLS bundle | `_internal/certifi/cacert.pem` | PRESENT |
| #9 whisper VAD model | `_internal/faster_whisper/assets/silero_vad_v6.onnx` | PRESENT |
| #9 alembic templates | `_internal/alembic/templates/` | PRESENT |
| entry point | `yta_sidecar` (executable) | PRESENT |
| build stamp | `VERSION` | PRESENT |
| build stamp | `build-info.json` | PRESENT |

Totals: **104 checks — 104 PRESENT, 0 MISSING.**

Deliberately NOT in the bundle (by design, see audit): Whisper model
weights (audit #10, download-on-first-use), `.env` (audit #11,
secrets), FFmpeg/ffprobe binaries (media-bundling worker; the
sidecar invokes them from PATH).

## Frozen-runtime observations from this worker

- First smoke launch of my build (data dir under `.tmp-yta/`):
  the frozen executable booted far enough to run `app.db.init_db`
  and create `app.db` (+ WAL) and `logs/` in the supplied
  `YTA_DATA_DIR` before the launching shell was terminated by the
  tool harness — i.e. the PYZ, datas and DB wiring all resolve
  frozen. A full listening-line/health proof is the frozen-proof
  sibling worker's deliverable (they were running an instance from
  a copied bundle on port 18741 concurrently).
- NOTE for whoever re-verifies: at 12:26 a sibling worker re-ran
  `build_sidecar.sh`, which cleans + rebuilds
  `packaging/dist-sidecar/` from the same spec — the bundle on disk
  after that run is that rebuild, expected to be identical in
  layout (same spec, same script).

## Replacement-worker re-proof (2026-10-23, after infra restart)

The original worker was killed by an infrastructure restart. On
takeover, `packaging/dist-sidecar/` and `packaging/build-sidecar/`
were EMPTY (a ~12:39 re-run of `build_sidecar.sh` had cleaned them
and died before PyInstaller produced output); only the Tauri-staged
copy under `src-tauri/binaries/` survived. The replacement worker:

- Re-audited the tree (see the addendum in `RESOURCE_AUDIT.md`):
  migration `0011_compliance.py` and `app/main.py:229`'s migrations
  scan had landed after the first audit, and `app.stylelab` had been
  renamed to `app.style_analysis`. The spec needed NO changes (it
  bundles the whole `migrations/` directory and
  `collect_submodules("app")`).
- Re-ran `./packaging/build_sidecar.sh` from scratch: **EXIT 0**,
  "SIDECAR FREEZE COMPLETE", PyInstaller 6.22.3, Python 3.12.3.
- Bundle on disk now: `packaging/dist-sidecar/yta_sidecar/` —
  **520,458,212 bytes (500 MiB, 1,226 files)**, version stamp 0.1.0,
  also re-staged to `src-tauri/binaries/`.
- Manifest verification was scripted against the SOURCE tree
  (every source prompt/migration file enumerated, not a fixed list):
  **105 checks, 105 PRESENT, 0 MISSING** — the 104 checks above,
  plus `_internal/migrations/versions/0011_compliance.py`.
  PyInstaller's only hidden-import warnings were `MySQLdb` and
  `pysqlite2` (optional SQLAlchemy dialect alternatives the app
  never uses; SQLite goes through the stdlib driver).
- Frozen smoke (this worker): with `YTA_DATA_DIR` under `.tmp-yta/`,
  the frozen executable created `app.db` (+ WAL) and `logs/` in the
  data dir; a launch against an already-occupied port exited with
  the documented bind-failure code 3 and the fatal bind message.
  Full listening-line/health proof remains the frozen-proof sibling
  worker's deliverable.

## Health-router fix addendum (2026-10-23, FIX worker)

The frozen-proof sibling found the bundle built above serves the API
but returns 404 on `/health/live` and `/health/ready`
(`outputs/round6/proof-freeze/PROOF_SUMMARY.md`, CONDITIONAL FAIL).
Root cause, found by full `collect_submodules('app')` (320) vs PYZ
diff: the PYZ held only **290** app modules — **30 missing**, every
one loaded by string at runtime (`app.routers.health`,
`app.providers.images.*` ×12, `app.publish_window.*` ×3,
`app.integration.*` stage modules ×7,
`app.style_analysis.*` siblings ×5, `app.providers.tts.calibration`,
plus `app.main`). The spec called `collect_submodules("app")` at
spec-exec time, **before** `Analysis` applies the spec's `pathex`;
launched without `backend/` on `sys.path` the collection silently
returned **zero** modules (proven: 0 vs 320 in the venv), so only
statically-reachable modules were frozen.

Fix (spec only, `packaging/yta_sidecar.spec`; no app-code change):
insert `BACKEND` into `sys.path` before collecting, add a fail-loud
`RuntimeError` guard if the collection is empty or missing critical
routers, and name all 30 modules as explicit hidden imports. The
build scripts' `PYTHONPATH` workaround stays in place (harmless).

Rebuilt via `./packaging/build_sidecar.sh` (build stamp
`2026-10-23T13:46:23Z`, git `c99e10e0`, the last build to touch
`dist-sidecar/`): **EXIT 0**, completeness gate passed, **new bundle
520,698,708 bytes (500M, 1,226 files)**, re-staged to
`src-tauri/binaries/`. Post-build diff: collect 320 = PYZ-00.toc 320
= PYZ-00.pyz 320 — **0 missing, `app.routers.health` present**.

Frozen re-proof (frozen binary only, `env -i`, fresh token/data dir,
`YTA_PORT=0`), run twice (in place + byte-verified isolated copy) —
**PASS**: listening line printed; **cold start launch →
/health/ready 200 = 7.222 s / 7.954 s**; `/health/live` → 200
`{"status":"ok"}`; `/health/ready` (token) → 200
`{"status":"ready","db":"ok","migrations":"ok"}`; no/wrong token →
401; zero router-missing warnings; SIGTERM → exit 0; no orphans.
Full evidence: `outputs/round6/proof-freeze/FIX_ADDENDUM.md` +
`fix_*` files there; working set under `.tmp-yta/proof-fix/`.
