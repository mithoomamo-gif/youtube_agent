# Round 6 — Sidecar Runtime Resource Audit

Scope: everything the FastAPI sidecar (`backend/`, entry `app.main:run`)
loads at runtime that is **not plain importable Python**, plus every
resource loaded through a `Path(__file__)` / `os.path.dirname(__file__)`
computation (those are the ones that move when the app is frozen).
Grep basis: `open(`, `Path(__file__)`, `read_text/read_bytes`,
`yaml.safe_load`, `json.load`, `importlib.import_module`, `__import__`
over `backend/app` + `backend/migrations` (excluding `__pycache__`),
re-run after sibling Round-6 files landed (`app/backup.py`,
`app/data_dir.py`, `app/uninstall_policy.py`, `app/diagnostics.py`,
`app/execution/scheduled_run.py` — none of those add file resources
beyond item 8 below).

Frozen-path arithmetic used throughout: PyInstaller `--onedir` puts
data files under `_internal/` (`sys._MEIPASS`), and a module that lives
at `backend/app/<…>/x.py` in dev lives at `_internal/app/<…>/x.py`
frozen. So `parents[N]` walks land one level differently — each item
below states the dev path, the loader, and the **frozen-expected**
location, which is what `yta_sidecar.spec` + the manifest checklist
verify.

## Audit list

| # | Resource (dev path) | Loader (code location) | How it is read | Frozen-expected location |
|---|---|---|---|---|
| 1 | `prompts/<slug>/<task>.md` — 35 agent prompt files + `prompts/README.md` (36 files, ~160 KB) at **repo root**, NOT under `backend/` | `backend/app/agents/prompt_assembly.py:87` `default_prompts_root()` = `Path(__file__).resolve().parents[3] / "prompts"`; `:180` `path.read_text()`; `list_prompts()` globs the tree. Prompt slugs are referenced from `backend/app/agents/registry.py` | Markdown + YAML front-matter, per LLM call | **`<bundle root>/prompts/`** — dev `parents[3]` = repo root; frozen `parents[3]` of `_internal/app/agents/prompt_assembly.py` = bundle root (parent of `_internal`). ⚠️ See Hazard H1: a plain `datas` entry lands in `_internal/prompts`, one level too deep. Spec bundles the mirror in `_internal/prompts`; the build MUST also place `prompts/` beside the executable (done in the proof build, §Manifest). |
| 2 | `backend/config/models.yaml` — LLM model registry / prices | `backend/app/providers/llm/registry.py:21` `DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[3] / "config" / "models.yaml"`; `:149` `yaml.safe_load(self.path.read_text())`, hot-reloaded on mtime | YAML, at provider init + on change | `_internal/config/models.yaml` (frozen `parents[3]` of `_internal/app/providers/llm/registry.py` = `_internal`) ✅ plain `datas` works |
| 3 | `backend/config/prices.yaml` — Budget Guard price catalog | `backend/app/providers/budget/catalog.py:24` `DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[3] / "config" / "prices.yaml"`; `:101` `yaml.safe_load(self._path.read_text())`, hot-reloaded | YAML | `_internal/config/prices.yaml` ✅ same as #2 |
| 4 | `backend/app/providers/tts/calibration_data.json` — measured TTS WPM calibration | `backend/app/providers/tts/calibration.py:61` `CANONICAL_PATH = Path(__file__).parent / "calibration_data.json"`; `backend/app/providers/tts/calibration_store.py:86` `DEFAULT_CALIBRATION_PATH = Path(__file__).with_name("calibration_data.json")`, `:228` loader | JSON, read at planning time; ⚠️ also **written** by `calibration.py:301,312` `write_json(…, canonical_path)` — see Hazard H4 | `_internal/app/providers/tts/calibration_data.json` ✅ `datas` works for reading |
| 5 | `backend/app/providers/tts/sample_texts/{english_facts,roman_urdu_facts,urdu_script_facts}.txt` (3 files) | `backend/app/providers/tts/calibration.py:60` `SAMPLE_TEXTS_DIR = Path(__file__).parent / "sample_texts"`; `:96` `load_sample_text()` `read_text()` | Plain text, calibration runs | `_internal/app/providers/tts/sample_texts/` ✅ |
| 6 | `backend/app/providers/tts/dictionaries/{global,facts,crime,health,islamic_stories,tech}.yaml` (6 files) — pronunciation dictionaries | `backend/app/providers/tts/dictionary.py:120` `base = os.path.join(os.path.dirname(__file__), "dictionaries")`; `:228` `open(path)` + `yaml.safe_load` in `load_yaml_entries()` | YAML, per TTS compile | `_internal/app/providers/tts/dictionaries/` ✅ |
| 7 | `backend/app/providers/images/workflow_templates/flux_schnell_txt2img_v1.json` — ComfyUI/FLUX workflow template | `backend/app/providers/images/workflows.py:25` `path = Path(__file__).parent / "workflow_templates" / name`; `json.load` | JSON template with `{{TOKEN}}` substitution | `_internal/app/providers/images/workflow_templates/` ✅ |
| 8 | `backend/migrations/` — `env.py`, `versions/0001_initial_schema.py` … `0011_compliance.py` (11 revisions; `0011_compliance.py` landed after the first audit pass), `script.py.mako`; plus `backend/alembic.ini` (`script_location = migrations`) | Alembic loads revision files **from disk by directory scan**, not by import (`from alembic import op` inside each revision). Runtime sidecar does NOT import Alembic (schema is created by `app.db.init_db` from `app/schema_ddl.py`, plain Python), BUT `backend/app/backup.py:143` `current_db_schema_version()` globs `Path(__file__).resolve().parent.parent / "migrations" / "versions"` and regex-reads every `*.py` to find the head revision (falls back to `_FALLBACK_SCHEMA_VERSION = 10` when absent — stale vs the 0011 head, see Hazard H9), and `backend/app/main.py:229` `_supported_schema_version()` (Round-6 downgrade guard) performs the same scan as its fallback | Directory scan + `read_text` (backup/restore + startup schema guard paths); Alembic CLI for manual upgrades | `_internal/migrations/` + `_internal/alembic.ini` (frozen `parent.parent` of `_internal/app/backup.py` / `_internal/app/main.py` = `_internal`) ✅ `datas` works (whole directory is bundled, so 0011 is included automatically — verified in the replacement manifest proof) |
| 9 | Third-party package data inside the venv (not repo files): `certifi/cacert.pem` (httpx/http_client TLS), `faster_whisper/assets/silero_vad_v6.onnx`, `ctranslate2` + `onnxruntime` + `av` (`av.libs`) + `tokenizers` native libraries, `alembic/templates/**` (Mako), `langgraph`/`langchain_core` package data, `keyring` entry-point backends, `tzdata`-style data — none on this Linux venv beyond the above | Loaded by the libraries themselves via `importlib.resources` / package-relative paths / entry points | Various | Handled in the spec via `collect_all` / `collect_data_files` / `collect_dynamic_libs` + `copy_metadata` for: `certifi, faster_whisper, ctranslate2, onnxruntime, av, tokenizers, huggingface_hub, alembic, mako, langgraph, langgraph_checkpoint_sqlite (module: langgraph.checkpoint.sqlite), langchain_core, keyring, edge_tts, uvicorn, fastapi, starlette, sqlalchemy, pydantic` |
| 10 | Whisper model weights — `models/whisper/` at repo root (HF cache layout, tiny/base/small present in dev, ~75 MB–1 GB+) | `backend/app/providers/transcription/whisper_faster.py:59` `default_model_dir()`: `$YTA_WHISPER_MODEL_DIR` else `Path(__file__).resolve().parents[4] / "models" / "whisper"`; passed as `download_root` to `faster_whisper.WhisperModel` | Downloaded on first use; directory is **created** (`mkdir(parents=True)`) | **Deliberately NOT bundled** (user-scale download, per-model choice). Frozen `parents[4]` = bundle root → `<bundle root>/models/whisper`. ⚠️ Hazard H2. Packaged installs should set `YTA_WHISPER_MODEL_DIR` into the data dir. |
| 11 | `.env` file (DEV ONLY, secrets — must NEVER be bundled) | `backend/app/providers/secrets_env.py:210` `_discover_dotenv()`: `Path.cwd() / ".env"`, then `Path(__file__).resolve().parents[3] / ".env"` (comment says "repo root"; arithmetic actually gives repo root in dev, bundle root frozen) | Parsed manually when `YTA_SECRET_BACKEND=env` | Not in `datas`. Production uses the OS keyring (`app/secrets_store.py`). Frozen fallback looks at `<bundle root>/.env` — Hazard H5 (dev-only path). |

### Explicitly NOT resources (checked, no action)

- **Niches**: `backend/app/niches/builtins.py`, `presets.py` (TTS),
  `agents/registry.py` — all niche/agent definitions are plain Python
  literals. No JSON/YAML niche files exist.
- **Schema**: `backend/app/schema_ddl.py` is a Python string constant —
  importable code, travels with the PYZ.
- **Static assets / templates**: the sidecar serves no `StaticFiles`,
  no Jinja, no HTML (grep: no `StaticFiles`/`FileResponse` templates).
  UI is the separate Tauri/React bundle.
- **Fonts**: NO font files in the repo. Thumbnails/text overlay use
  Pillow with caller-supplied paths or system-font globs
  (`backend/app/pipeline/thumbnail/text_render.py:35` `_FALLBACK_FONT_GLOBS`:
  `/usr/share/fonts/**/DejaVuSans-Bold.ttf`, `…/LiberationSans-Bold.ttf`,
  `…/NotoSans-Bold.ttf`, `C:/Windows/Fonts/arialbd.ttf`), final fallback
  `ImageFont.load_default()`. ⚠️ Hazard H6 for Windows packaging.
- **FFmpeg/ffprobe**: invoked as bare `"ffmpeg"`/`"ffprobe"` strings on
  PATH everywhere (e.g. `pipeline/render/qc.py:35`, `renderer.py`,
  `pipeline/audio/_ffmpeg.py`, `providers/tts/calibration.py` probe).
  Not a Python resource; sibling FFmpeg-bundling worker owns putting
  the binaries beside the sidecar / on PATH. The spec does not bundle
  them.
- **Arabic shaping**: `arabic_reshaper` + `python-bidi` are optional
  imports in `thumbnail/text_render.py:77` (`shaping_available()`), and
  are **not installed** in this venv and not in `pyproject.toml` —
  Urdu thumbnail text currently renders unshaped. Not a spec problem;
  flagged here because a spec `hiddenimport` for them would be a lie.

### Dynamic imports the spec must cover (static analysis misses these)

- `app/main.py:64` `_include_router()` — `importlib.import_module("app.routers.health")`,
  `app.routers.ws` (×2 attrs), `app.routers.api`. ALL routers therefore
  hidden.
- `app/integration/produce.py:143` `_STAGE_MODULES` + `stage_bridge.py:103`:
  `app.integration.content / voice_stage / visuals_stage / assemble_stage /
  publish_stage / pack` via `importlib.import_module`.
- `app/integration/visuals_stage.py:401` `_import_sibling()` — dynamic
  `app.integration.<tail>` (stock_query etc.).
- `app/style_analysis/engine.py:242` — `app.style_analysis.<modname>`
  siblings (schema, media, …; package renamed from `app.stylelab`
  in the SL-009 rename); `app/style_lab/service.py:136,166` — tries
  `app.style_analysis.media` first, then the legacy
  `app.style_lab.media` name (dead fallback — no such module exists).
- `app/learning/service.py:202`, `app/reporting/engine.py:904` — sibling
  seam loaders (`app.learning.*`, `app.analytics.calibration`, …).
- `app/execution/integration_client.py:48` — `app.integration`.
- `app/orchestration/graph.py:157,170` — lazy
  `langgraph.checkpoint.sqlite.SqliteSaver` / `langgraph.checkpoint.memory.MemorySaver`,
  `langgraph.types.interrupt` (lazy, inside functions).
- Uvicorn internals selected by string at runtime: loops
  (`uvicorn.loops.auto/asyncio`; `uvloop` NOT installed here), HTTP
  protocols (`h11_impl` — installed; `httptools_impl` — `httptools` NOT
  installed), websockets (`websockets_impl` — installed; `wsproto_impl`
  — `wsproto` NOT installed), `uvicorn.lifespan.on/off`,
  `uvicorn.logging`. `app/main.py` calls `uvicorn.run(app_object)` (not a
  string target), with `log_config=None` (sibling Round-6 edit), no workers.
- Keyring backends via entry points: `keyring.backends.Windows` (prod on
  Windows), `keyring.backends.SecretService`, `keyring.backends.fail`,
  `secretstorage`/`jeepney` on Linux.

**Spec strategy for all of the above:** `collect_submodules("app")` as
hidden imports (the whole backend package, so every dynamically named
sibling is in the PYZ), plus per-package `collect_submodules` and a
`find_spec`-guarded explicit list, so missing optional modules
(uvloop/httptools/wsproto/arabic_reshaper) never become fake imports.

## Frozen-path hazards (for the data-dir / protocol / shell workers)

- **H1 — CRITICAL — prompts root (item 1).** `prompt_assembly.py:87`
  `parents[3]` resolves to the **bundle root**, not `_MEIPASS`.
  PyInstaller `datas` alone puts prompts at `_internal/prompts` and
  every LLM call then fails to find its prompt file. Interim layout
  fix used by the proof build: copy `prompts/` beside the executable
  (installer/build-script step). Proper fix (one line, NOT applied —
  belongs to the data-dir/protocol owners): resolve via
  `getattr(sys, "_MEIPASS", …)` / `sys.executable` parent when
  `sys.frozen`, or honour a `YTA_PROMPTS_DIR` env var.
- **H2 — Whisper weights (item 10).** `whisper_faster.py:59`
  `parents[4]` = bundle root frozen; weights would download into the
  install directory (read-only under a per-machine install, wiped on
  update). Shell should set `YTA_WHISPER_MODEL_DIR` to the data dir.
- **H3 — Stock cache.** `backend/app/integration/stock_fetch.py:144`
  `DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[2] / "cache" / "stock"`
  → frozen: `_internal/cache/stock` (inside the bundle; dev: `backend/cache/stock`).
  Cache writes (up to the 2 GiB cap) would land in the install tree.
  Needs a data-dir redirect (env override exists only for the cap,
  `YTA_STOCK_CACHE_MAX_BYTES`, not the location).
- **H4 — Calibration write-back (item 4).** `calibration.py:301` writes
  the updated `calibration_data.json` back next to the code
  (`CANONICAL_PATH`). Frozen, that is a write into `_internal/…`:
  fails on a read-only install and is lost on upgrade. The measured
  file should live in / be overlaid from the data dir.
- **H5 — `.env` discovery (item 11).** `secrets_env.py:210`
  `parents[3] / ".env"` → bundle root frozen. Dev-only backend
  (`YTA_SECRET_BACKEND=env`); harmless if no `.env` is shipped, but a
  stray `.env` beside the exe would be silently honoured in prod.
- **H6 — Fonts.** No bundled fonts; Windows fallback glob is only
  `C:/Windows/Fonts/arialbd.ttf` (no Nastaliq/Urdu font), so Urdu
  thumbnails depend on profile-supplied font paths that do not exist
  in the package yet. Packaging should bundle a Latin + a Nastaliq
  font and set the profile paths (thumbnail team / media bundling).
- **H7 — Dev CLI output dir.** `backend/app/integration/run.py:92`
  `_default_base_dir()` = `parents[2].parent / "outputs" / "round2" / …`
  → frozen: `<bundle root>/outputs/…`. Dev-CLI only (`python -m
  app.integration.run`), not the sidecar serving path; listed for
  completeness.
- **H8 — Alembic CLI.** `backend/migrations/env.py:24` inserts
  `parent.parent` (dev: `backend/`) into `sys.path`, and
  `alembic.ini: script_location = migrations` is relative to the ini.
  Both work frozen IF `migrations/` + `alembic.ini` sit together in
  `_internal` (they do, item 8) and Alembic is run with
  cwd/`-c _internal/alembic.ini`. The sidecar itself never runs
  Alembic at runtime (schema via `app.db.init_db`), so this only
  affects manual upgrade tooling.

- **H9 — Stale schema fallback (found by the replacement-worker
  re-audit).** `backend/app/backup.py:87`
  `_FALLBACK_SCHEMA_VERSION = 10`, but the migration head is now
  `0011` (`backend/migrations/versions/0011_compliance.py`). The
  fallback only fires when `migrations/versions/` is absent, which
  the bundled layout prevents — but if the directory were ever
  dropped from the bundle, `current_db_schema_version()` (and with
  it `app/main.py`'s downgrade guard) would silently report 10 and
  could misjudge an 0011 database. One-line fix belongs to the
  backup/data-dir owners; NOT applied here.

Replacement-worker addendum (2026-10-23): the original audit/spec
worker was killed by an infrastructure restart. On takeover,
`packaging/dist-sidecar/` and `packaging/build-sidecar/` were EMPTY
(a sibling re-run of `build_sidecar.sh` at ~12:39 had cleaned them
and died before PyInstaller produced output), so the manifest proof
was re-run from scratch against the current tree (which by then
included migration 0011, the `stylelab` → `style_analysis` rename,
and `app/main.py`'s schema scan). The spec itself needed no changes
for any of these: it bundles the whole `migrations/` directory and
`collect_submodules("app")`. See `MANIFEST_CHECKLIST.md` for the
replacement proof run.

No backend code was changed by this audit/spec work.
