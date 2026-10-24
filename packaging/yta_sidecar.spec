# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the YouTube Agent sidecar — Round 6 freeze.

Build (from the repo root, using the backend venv):

    backend/.venv/bin/python -m PyInstaller packaging/yta_sidecar.spec \
        --distpath packaging/dist-sidecar --workpath packaging/build-sidecar \
        --noconfirm

Layout contract (roadmap Part 11: PyInstaller --onedir + Tauri NSIS):

* Entry point is the app's real main: ``backend/app/main.py``, i.e. the
  equivalent of ``python -m app.main`` / the ``youtube-agent-backend``
  console script (``app.main:run``).  The Tauri shell spawns the
  resulting ``yta_sidecar`` executable, passes YTA_PORT / YTA_TOKEN /
  YTA_BOOT_ID / YTA_DATA_DIR in the environment, and reads the single
  ``{"event": "listening", ...}`` stdout line.  ``console=True`` is
  therefore mandatory.
* ``--onedir``: the output is ``dist-sidecar/yta_sidecar/`` containing
  the executable plus ``_internal/`` (PYZ, shared libs, datas).  The
  shell/NSIS layer ships that whole directory (Tauri ``externalBin``
  renames the exe to its target-triple name at packaging time — that
  rename is the shell team's step, not this spec's).
* Resource datas are mapped in ``packaging/RESOURCE_AUDIT.md``.  One
  item does NOT fit the ``_internal`` layout: the agent prompts
  (audit item 1 / hazard H1) are resolved by
  ``app/agents/prompt_assembly.py`` as ``parents[3] / "prompts"``,
  which in a frozen onedir build is the BUNDLE ROOT (the folder
  containing the executable), not ``_internal``.  This spec bundles
  the mirror copy at ``_internal/prompts``; the build/installer MUST
  also place ``prompts/`` beside the executable.  The proof build in
  ``packaging/dist-sidecar/`` was produced with exactly that copy
  step, and ``packaging/MANIFEST_CHECKLIST.md`` verifies both
  locations.
* Whisper model weights are deliberately NOT bundled (audit item
  10): they download on first use to ``YTA_WHISPER_MODEL_DIR`` or
  ``<bundle root>/models/whisper`` (hazard H2).
* No ``.env`` file is EVER bundled (audit item 11): production
  secrets live in the OS keyring.
* FFmpeg/ffprobe are NOT bundled here: the sidecar invokes the bare
  ``ffmpeg``/``ffprobe`` names from PATH; the media-bundling worker
  places those binaries beside the sidecar.
"""

import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

SPEC_DIR = Path(SPECPATH).resolve()
REPO_ROOT = SPEC_DIR.parent
BACKEND = REPO_ROOT / "backend"

# ---------------------------------------------------------------------------
# FIX (Round 6, frozen-bundle health-router defect): make the backend
# importable BEFORE collect_submodules("app") runs below.
#
# Root cause of the missing app.routers.health (and 29 other modules):
# a spec's body executes top-to-bottom in the PyInstaller driver
# process, and the Analysis(...) constructor at the bottom of this
# file is what extends sys.path with the spec's pathex — i.e. AFTER
# every top-level statement here has already run. The
# collect_submodules("app") call in section 1 therefore ran with
# whatever sys.path the LAUNCHER happened to provide. Launched
# without backend/ on the path (plain `python -m PyInstaller`, or a
# build script that did not export PYTHONPATH), the collection
# SILENTLY returned zero modules — no warning, no error — so the
# bundle's app modules were only the ones static analysis could
# reach from the entry script. Every module the app loads BY STRING
# at runtime (app.routers.health via importlib in app.main, the
# app.providers.images.* stack, app.publish_window.*, several
# app.integration.* stage modules, app.style_analysis.* siblings,
# app.providers.tts.calibration) was absent from the PYZ; the frozen
# server booted and served the statically-reachable API but
# /health/* 404'd. Verified empirically: collect_submodules("app")
# returns 0 modules with cwd=repo-root and no PYTHONPATH, and the
# full 320 (health included) once backend/ is on sys.path.
# The build scripts' PYTHONPATH export was a launcher-side
# workaround; this insertion is the permanent fix — the spec is now
# self-sufficient no matter how it is launched.
# ---------------------------------------------------------------------------
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _existing(*names):
    """Hidden imports filtered to modules that are actually installed.

    PyInstaller only warns on a missing hidden import, but a spec that
    names modules this venv does not have (uvloop, httptools, wsproto,
    arabic_reshaper, python-bidi) would be lying about the bundle.
    """
    def _found(name):
        try:
            return importlib.util.find_spec(name) is not None
        except Exception:  # submodule of a missing parent raises, not None
            return False

    return [n for n in names if _found(n)]


datas = []
binaries = []
hiddenimports = []

# ---------------------------------------------------------------------------
# 1. The app package itself.
#
# app/main.py loads routers with importlib.import_module() by string,
# and integration/stylelab/learning/reporting modules resolve stage
# siblings the same way (see RESOURCE_AUDIT.md, "Dynamic imports").
# Static analysis cannot see any of that, so EVERY app submodule is a
# hidden import.
# ---------------------------------------------------------------------------
_collected_app = collect_submodules("app")

# Fail LOUDLY, in seconds, if the collection ever comes back empty or
# missing the health router again (see the FIX note at BACKEND above):
# a silent empty collection costs a full build + a runtime 404.
_CRITICAL_APP_MODULES = (
    "app.main",
    "app.routers.health",
    "app.routers.system",
    "app.routers.ws",
)
_missing_critical = [m for m in _CRITICAL_APP_MODULES if m not in _collected_app]
if not _collected_app or _missing_critical:
    raise RuntimeError(
        "yta_sidecar.spec: collect_submodules('app') returned "
        f"{len(_collected_app)} modules, missing critical: "
        f"{_missing_critical or '[]'} — backend/ is not importable at "
        "spec-exec time (sys.path fix above regressed?). Refusing to "
        "freeze a bundle whose runtime-loaded modules would be absent."
    )
hiddenimports += _collected_app

# Belt-and-braces: the exact modules proven missing from the frozen
# PYZ of the defective builds (full collect-vs-PYZ diff, Round 6
# proof-freeze fix addendum: 30 names = app.main, which normally
# rides as the entry script — listing it also places a PYZ copy,
# which is inert since nothing imports app.main — plus the 29
# string-loaded modules: app.routers.health and 28 others).
# collect_submodules above already returns every one of these;
# naming them explicitly keeps them in the bundle even if
# collection behaviour changes.
hiddenimports += [
    "app.main",
    "app.routers.health",
    "app.integration.brightness",
    "app.integration.competitor_analyse",
    "app.integration.llm_stub",
    "app.integration.run",
    "app.integration.stock_fetch",
    "app.integration.stock_ledger",
    "app.integration.stock_query",
    "app.providers.images",
    "app.providers.images.allowlist",
    "app.providers.images.base",
    "app.providers.images.cache",
    "app.providers.images.comfyui",
    "app.providers.images.errors",
    "app.providers.images.flux_api",
    "app.providers.images.models",
    "app.providers.images.router",
    "app.providers.images.safety",
    "app.providers.images.style",
    "app.providers.images.workflows",
    "app.providers.tts.calibration",
    "app.publish_window",
    "app.publish_window.adapter",
    "app.publish_window.service",
    "app.style_analysis.dressing",
    "app.style_analysis.scenes",
    "app.style_analysis.speech",
    "app.style_analysis.thumbnail",
    "app.style_analysis.visual_classify",
]

# ---------------------------------------------------------------------------
# 2. Repo data files (audit items 1-8).  Destinations are bundle-relative
#    (i.e. under _internal/ in the onedir output).
# ---------------------------------------------------------------------------
datas += [
    # Item 1: agent prompts — mirror copy; bundle-root copy is the
    # location the frozen code actually reads (hazard H1, see header).
    (str(REPO_ROOT / "prompts"), "prompts"),
    # Items 2+3: LLM registry + price catalog -> _internal/config/
    # (frozen parents[3] of the loaders is _internal).
    (str(BACKEND / "config" / "models.yaml"), "config"),
    (str(BACKEND / "config" / "prices.yaml"), "config"),
    # Item 4: TTS calibration data -> next to app/providers/tts code.
    (
        str(BACKEND / "app" / "providers" / "tts" / "calibration_data.json"),
        "app/providers/tts",
    ),
    # Item 5: TTS calibration sample texts.
    (
        str(BACKEND / "app" / "providers" / "tts" / "sample_texts"),
        "app/providers/tts/sample_texts",
    ),
    # Item 6: pronunciation dictionaries.
    (
        str(BACKEND / "app" / "providers" / "tts" / "dictionaries"),
        "app/providers/tts/dictionaries",
    ),
    # Item 7: ComfyUI/FLUX workflow template.
    (
        str(BACKEND / "app" / "providers" / "images" / "workflow_templates"),
        "app/providers/images/workflow_templates",
    ),
    # Item 8: Alembic migrations (env.py, versions/*.py, script.py.mako)
    # — loaded from disk by Alembic and glob-scanned by app/backup.py.
    (str(BACKEND / "migrations"), "migrations"),
    (str(BACKEND / "alembic.ini"), "."),
]

# ---------------------------------------------------------------------------
# 3. Third-party packages with data files, native libs or entry points
#    that static analysis misses (audit item 9).
# ---------------------------------------------------------------------------
_COLLECT_ALL = [
    "certifi",        # cacert.pem for httpx TLS
    "faster_whisper", # assets/silero_vad_v6.onnx + native deps
    "ctranslate2",    # native .so/.libs
    "onnxruntime",    # native runtime + data (faster-whisper dep)
    "av",             # PyAV native libs (pinned >=12,<14, see pyproject)
    "tokenizers",     # Rust extension (huggingface dep)
    "alembic",        # templates/*.mako for migration tooling
    "mako",           # alembic template engine
    "keyring",        # entry-point backends (Windows Credential Manager)
    "edge_tts",       # TTS provider
]
for _pkg in _COLLECT_ALL:
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:
        # Package absent in this environment: fall back to the pieces.
        datas += collect_data_files(_pkg, on_error="ignore")
        binaries += collect_dynamic_libs(_pkg, on_error="ignore")
        hiddenimports += collect_submodules(_pkg, on_error="ignore")

# Packages where submodules + metadata suffice.
for _pkg in (
    "uvicorn",
    "fastapi",
    "starlette",
    "pydantic",
    "langgraph",
    "langchain_core",
    "langchain_protocol",
    "langsmith",
    "huggingface_hub",
    "sqlalchemy",
    "httpx",
    "websockets",
    "multipart",  # python-multipart (FastAPI form parsing)
    "yaml",       # PyYAML
):
    hiddenimports += collect_submodules(_pkg, on_error="ignore")

# Distribution metadata read at runtime via importlib.metadata /
# entry points (keyring backends, huggingface hub, langgraph).
for _dist in (
    "keyring",
    "huggingface_hub",
    "langgraph",
    "langgraph-checkpoint-sqlite",
    "faster-whisper",
    "edge-tts",
    "uvicorn",
    "fastapi",
):
    try:
        datas += copy_metadata(_dist)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 4. Explicit hidden imports: runtime-selected modules.
# ---------------------------------------------------------------------------
hiddenimports += _existing(
    # Uvicorn: app/main.py calls uvicorn.run(app_object); the loop /
    # protocol / lifespan implementations are chosen BY STRING inside
    # uvicorn at runtime.  Only what this venv actually has is listed
    # (asyncio loop + h11 + websockets; no uvloop/httptools/wsproto).
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.loops.uvloop",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # LangGraph checkpointer: lazily imported in
    # app/orchestration/graph.py (SqliteSaver, else MemorySaver).
    "langgraph.checkpoint.sqlite",
    "langgraph.checkpoint.memory",
    "langgraph.types",
    "aiosqlite",  # langgraph-checkpoint-sqlite async backend
    # SQLAlchemy dialect actually used (SQLite via app.db / alembic).
    "sqlalchemy.dialects.sqlite",
    # Keyring backends (entry points; Windows is the production one).
    "keyring.backends.Windows",
    "keyring.backends.macOS",
    "keyring.backends.SecretService",
    "keyring.backends.fail",
    "keyring.backends.chainer",
    "secretstorage",
    "jeepney",
    # Pillow plugins used by thumbnails / text overlay.
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageFont",
    "PIL.ImageOps",
    "PIL.PngImagePlugin",
    "PIL.JpegImagePlugin",
    # Optional Arabic shaping — NOT installed in this venv, so
    # _existing() drops these today; they activate automatically if
    # the deps are ever added (see audit, "Fonts"/shaping note).
    "arabic_reshaper",
    "bidi.algorithm",
    # faster-whisper stack, imported lazily inside functions.
    "faster_whisper",
    "ctranslate2",
    "onnxruntime",
    "av",
    "tokenizers",
    "huggingface_hub",
)

# Deduplicate while keeping order stable.
hiddenimports = list(dict.fromkeys(hiddenimports))

# ---------------------------------------------------------------------------
# 5. Excludes: test/dev-only weight that nothing in the sidecar imports.
#    (Deliberately conservative: no stdlib module that a dependency
#    might reach for dynamically is excluded.)
# ---------------------------------------------------------------------------
excludes = [
    "pytest",
    "_pytest",
    "pytest_asyncio",
    # NOTE: "pip"/"wheel" are NOT excluded on purpose — the setuptools
    # hook aliases "wheel" and PyInstaller aborts the build with
    # ValueError: Target module "wheel" already imported as
    # ExcludedModule (verified in the Round 6 proof build).
    "piper",       # piper-tts sits in the dev venv; the app never imports it
    "piper_tts",
    "py_spy",      # dev profiler present in the venv only
    "IPython",
    "jupyter",
    "notebook",
    "matplotlib",
    "tkinter",
    "_tkinter",
    "turtle",
    "idlelib",
    "test",        # stdlib test suite
    "lib2to3",
]

a = Analysis(
    [str(BACKEND / "app" / "main.py")],
    pathex=[str(BACKEND)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="yta_sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX + AV false positives: roadmap packaging choice is plain onedir
    console=True,  # the shell reads the {"event": "listening"} stdout line
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # Tauri/NSIS owns product iconography; sidecar is a console child
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="yta_sidecar",
)
