# YouTube Agent — one-command dev workflow (Linux / macOS / dev-VM entry point)
# Windows developers: use tools/dev.ps1 instead — see tools/README.md
# The shipped product targets Windows (Tauri 2), but this Makefile is the
# honest entry point wherever GNU Make + a POSIX shell are available.
# Every target is guarded: if a piece built by another team has not landed
# yet, you get a clear message and a non-zero exit, not a stack trace.

.DEFAULT_GOAL := help

# ---- Tool detection -------------------------------------------------------
PYTHON3 ?= python3
NPM     ?= npm
NODE    ?= node

# Virtualenv created by 'make setup' (POSIX layout; Windows uses Scripts/).
VENV_DIR    := .venv
VENV_PYTHON := $(VENV_DIR)/bin/python
VENV_PIP    := $(VENV_DIR)/bin/pip

# Python for ad-hoc commands: prefer the venv when it exists, else python3.
# Recipes also re-check at runtime so a venv created mid-session is picked up.
ifeq ($(wildcard $(VENV_PYTHON)),)
PYTHON := $(PYTHON3)
else
PYTHON := $(VENV_PYTHON)
endif

.PHONY: help setup test test-unit lint backend ui clean

# ---- help (default) -------------------------------------------------------
help:
	@echo "YouTube Agent — dev commands"
	@echo ""
	@echo "  make setup      Create .venv + install backend deps + npm install (when present)"
	@echo "  make test       Run the full pytest suite (tests/)"
	@echo "  make test-unit  Run only tests marked unit or contract"
	@echo "  make lint       ruff check backend/ tests/ + npm run typecheck (when defined)"
	@echo "  make backend    Run the FastAPI sidecar in dev mode (python -m app.main)"
	@echo "  make ui         Run the frontend dev server (npm run dev)"
	@echo "  make clean      Remove caches only (__pycache__, .pytest_cache, node_modules/.vite)"
	@echo "  make help       Show this help"
	@echo ""
	@echo "Windows (PowerShell):  .\tools\dev.ps1 <setup|test|lint|backend|ui>"
	@echo "Environment check:     python3 tools/check_env.py"

# ---- setup ----------------------------------------------------------------
setup:
	@command -v $(PYTHON3) >/dev/null 2>&1 || { echo "✗ Python not found (need Python >= 3.12 on PATH as python3). Install it, then re-run 'make setup'."; exit 1; }
	@echo "→ Python: $$($(PYTHON3) --version 2>&1)"
	@if [ ! -x "$(VENV_PYTHON)" ]; then \
	    echo "→ Creating virtualenv in $(VENV_DIR)/ ..."; \
	    $(PYTHON3) -m venv $(VENV_DIR) || { echo "✗ Failed to create venv. Is python3-venv installed? On Debian/Ubuntu: sudo apt install python3-venv"; exit 1; }; \
	  else \
	    echo "→ Virtualenv already exists at $(VENV_DIR)/ (reusing it)"; \
	  fi
	@REQ=""; \
	  for f in backend/requirements.txt backend/requirements-dev.txt requirements.txt; do \
	    if [ -f "$$f" ]; then REQ="$$REQ $$f"; fi; \
	  done; \
	  if [ -n "$$REQ" ]; then \
	    for f in $$REQ; do \
	      echo "→ Installing Python deps from $$f ..."; \
	      $(VENV_PIP) install -r "$$f" || { echo "✗ pip install failed for $$f — see output above."; exit 1; }; \
	    done; \
	  elif [ -f backend/pyproject.toml ]; then \
	    echo "⚠ No requirements.txt found; backend/pyproject.toml exists but is not a requirements file."; \
	    echo "  Skipping automatic pip install. Once backend packaging is ready, install manually: $(VENV_PIP) install -e backend/"; \
	  else \
	    echo "⚠ No backend requirements found yet (looked for backend/requirements.txt, requirements.txt, backend/pyproject.toml)."; \
	    echo "  Skipping pip install — the backend has probably not landed yet. Re-run 'make setup' once it does."; \
	  fi
	@if [ -f package.json ]; then \
	    command -v $(NPM) >/dev/null 2>&1 || { echo "✗ package.json found but npm is not on PATH. Install Node.js 20+ (this project was built with Node 24), then re-run 'make setup'."; exit 1; }; \
	    echo "→ Installing frontend deps (npm install) ..."; \
	    $(NPM) install || { echo "✗ npm install failed — see output above."; exit 1; }; \
	  else \
	    echo "⚠ No package.json found yet — skipping npm install. The frontend (src/) has probably not landed yet."; \
	  fi
	@echo "✓ Setup done. Tip: run python3 tools/check_env.py to verify your toolchain."

# ---- test -----------------------------------------------------------------
test:
	@if [ ! -d tests ]; then \
	    echo "✗ tests/ directory not found — the test suite has not landed yet."; \
	    echo "  Expected layout: tests/ with pytest tests at the repo root."; \
	    echo "  Nothing to run; this is a guard, not a failure of your environment."; \
	    exit 1; \
	  fi
	@PY="$(PYTHON)"; if [ -x "$(VENV_PYTHON)" ]; then PY="$(VENV_PYTHON)"; fi; \
	  command -v $$PY >/dev/null 2>&1 || PY="$(PYTHON3)"; \
	  echo "→ Running: $$PY -m pytest tests/ -q"; \
	  $$PY -m pytest tests/ -q || { ec=$$?; if [ $$ec -eq 5 ]; then echo "⚠ pytest collected no tests yet (tests/ exists but is empty)."; fi; exit $$ec; }

test-unit:
	@if [ ! -d tests ]; then \
	    echo "✗ tests/ directory not found — the test suite has not landed yet."; \
	    echo "  Expected layout: tests/ with pytest tests at the repo root."; \
	    exit 1; \
	  fi
	@PY="$(PYTHON)"; if [ -x "$(VENV_PYTHON)" ]; then PY="$(VENV_PYTHON)"; fi; \
	  echo "→ Running: $$PY -m pytest tests/ -q -m \"unit or contract\""; \
	  $$PY -m pytest tests/ -q -m "unit or contract"

# ---- lint -----------------------------------------------------------------
lint:
	@PY="$(PYTHON)"; if [ -x "$(VENV_PYTHON)" ]; then PY="$(VENV_PYTHON)"; fi; \
	  TARGETS=""; [ -d backend ] && TARGETS="$$TARGETS backend"; [ -d tests ] && TARGETS="$$TARGETS tests"; \
	  if [ -z "$$TARGETS" ]; then \
	    echo "⚠ Neither backend/ nor tests/ exists yet — skipping ruff."; \
	  elif command -v ruff >/dev/null 2>&1; then \
	    echo "→ ruff check$$TARGETS"; ruff check $$TARGETS; \
	  elif $$PY -m ruff --version >/dev/null 2>&1; then \
	    echo "→ $$PY -m ruff check$$TARGETS"; $$PY -m ruff check $$TARGETS; \
	  else \
	    echo "⚠ ruff not available (no 'ruff' on PATH and '$$PY -m ruff' failed) — skipping Python lint."; \
	    echo "  Install it with: $$PY -m pip install ruff   (or re-run 'make setup' once dev deps land)"; \
	  fi
	@if [ -f package.json ]; then \
	    if grep -q '"typecheck"[[:space:]]*:' package.json; then \
	      command -v $(NPM) >/dev/null 2>&1 || { echo "✗ typecheck script defined but npm is not on PATH. Install Node.js, then re-run 'make lint'."; exit 1; }; \
	      if [ ! -e node_modules/.bin/tsc ]; then echo "✗ typecheck script defined but frontend deps are not installed (node_modules/.bin/tsc missing). Run 'make setup' first."; exit 1; fi; \
	      echo "→ npm run typecheck"; \
	      $(NPM) run typecheck; \
	    else \
	      echo "⚠ package.json has no \"typecheck\" script — skipping frontend typecheck."; \
	    fi; \
	  else \
	    echo "⚠ No package.json found yet — skipping frontend typecheck."; \
	  fi

# ---- backend --------------------------------------------------------------
# Canonical launch (docs/DEV_SETUP.md §5.1): the sidecar has NO module-level
# `app` object — app.main defines create_app()/run() only — so
# `uvicorn <module>:app` does NOT work. Launch via `python -m app.main`
# with the YTA_* env contract: host is hardcoded to 127.0.0.1 in app.main,
# the port comes from YTA_PORT, and YTA_TOKEN is required.
# NOTE: --reload has no equivalent under `python -m app.main` (uvicorn is
# invoked programmatically by run() without reload); restart the target
# manually after code changes.
backend:
	@if [ ! -f backend/app/main.py ]; then \
	    echo "✗ backend/app/main.py not found — the FastAPI sidecar has not landed yet."; \
	    echo "  Expected layout: backend/app/main.py (entry point: python -m app.main)."; \
	    exit 1; \
	  fi
	@PY="$(PYTHON)"; \
	  if [ -x "backend/$(VENV_DIR)/bin/python" ]; then PY="backend/$(VENV_DIR)/bin/python"; \
	  elif [ -x "$(VENV_PYTHON)" ]; then PY="$(VENV_PYTHON)"; fi; \
	  case "$$PY" in /*) ;; */*) PY="$$PWD/$$PY";; *) ;; esac; \
	  export YTA_PORT="$${YTA_PORT:-8765}"; \
	  export YTA_TOKEN="$${YTA_TOKEN:-dev-token}"; \
	  if [ -z "$$YTA_BOOT_ID" ]; then YTA_BOOT_ID="$$($$PY -c 'import uuid; print(uuid.uuid4())')"; export YTA_BOOT_ID; fi; \
	  export YTA_DATA_DIR="$${YTA_DATA_DIR:-$$PWD/backend/.dev-data}"; \
	  export YTA_DETACHED="$${YTA_DETACHED:-1}"; \
	  export YTA_LOG_LEVEL="$${YTA_LOG_LEVEL:-debug}"; \
	  echo "→ Starting backend: (cd backend && YTA_PORT=$$YTA_PORT YTA_DATA_DIR=$$YTA_DATA_DIR $$PY -m app.main)"; \
	  echo "  Bind: 127.0.0.1:$$YTA_PORT (host hardcoded in app.main; no --reload — restart manually after code changes)"; \
	  cd backend && exec "$$PY" -m app.main

# ---- ui -------------------------------------------------------------------
ui:
	@if [ ! -f package.json ]; then \
	    echo "✗ package.json not found — the frontend (React/TypeScript/Vite in src/) has not landed yet."; \
	    echo "  Expected: package.json at the repo root with a \"dev\" script (Vite)."; \
	    exit 1; \
	  fi
	@command -v $(NPM) >/dev/null 2>&1 || { echo "✗ npm is not on PATH. Install Node.js 20+ (built with Node 24), then re-run 'make ui'."; exit 1; }
	@grep -q '"dev"[[:space:]]*:' package.json || { echo "✗ package.json has no \"dev\" script — cannot start the UI. Add one (e.g. Vite: \"dev\": \"vite\") or start it manually."; exit 1; }
	@if [ ! -e node_modules/.bin/vite ]; then echo "✗ Frontend deps are not installed (node_modules/.bin/vite missing). Run 'make setup' first."; exit 1; fi
	@echo "→ npm run dev"
	$(NPM) run dev

# ---- clean ----------------------------------------------------------------
# Caches only. Never touches user data, media, .venv, node_modules itself,
# the SQLite DB, or any generated videos.
clean:
	@echo "→ Removing Python / pytest caches ..."
	@find . -type d -name __pycache__ -not -path "./.venv/*" -not -path "./node_modules/*" -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache .ruff_cache .mypy_cache
	@find backend tests -type d -name .pytest_cache -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "→ Removing Vite cache (node_modules/.vite) ..."
	@rm -rf node_modules/.vite
	@echo "✓ Clean done. (Kept: .venv/, node_modules/, databases, media and user data.)"
