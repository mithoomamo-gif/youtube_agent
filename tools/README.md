# Dev tools — YouTube Agent

One-command dev workflow for the whole repo. Pick the entry point for your OS —
both do the same things, with the same friendly guards when a piece of the
project hasn't landed yet.

| OS | Entry point |
|---|---|
| **Windows** (shipping target) | PowerShell: `.\tools\dev.ps1 <target>` |
| **Linux / macOS / dev VM** | `make <target>` (repo-root `Makefile`) |
| **Any OS** — environment check | `python tools/check_env.py` ( `python3` on Linux/macOS) |

> Platform note: the product is a Windows desktop app (Tauri 2 + React/TS +
> Python FastAPI sidecar), but backend and frontend development work fine on
> Linux/macOS. Only building/running the Tauri shell (`src-tauri/`) needs the
> Rust toolchain — `check_env.py` warns about that instead of failing.

## Targets

| Target | What it does |
|---|---|
| `help` | List targets (default for both entry points). |
| `setup` | Create `.venv/`, `pip install` backend requirements if present, `npm install` if `package.json` is present. |
| `test` | `python -m pytest tests/ -q` — uses `.venv` Python if present, else system Python. |
| `test-unit` | Same, filtered: `pytest tests/ -q -m "unit or contract"` |
| `lint` | `ruff check backend/ tests/` if ruff is available (otherwise a clear skip), plus `npm run typecheck` if that script is defined. |
| `backend` | Run the FastAPI sidecar in dev mode via `python -m app.main` (cwd `backend/`, `YTA_*` env contract from `docs/DEV_SETUP.md` §5.1: `YTA_PORT` default 8765, `YTA_TOKEN=dev-token`, `YTA_DETACHED=1`; host hardcoded to 127.0.0.1 in `app.main` — there is no module-level `app` object, so `uvicorn …:app` does not work). No `--reload` equivalent — restart manually after code changes. |
| `ui` | `npm run dev` (Vite) — requires `package.json` with a `dev` script. |
| `clean` | Remove caches only: `__pycache__/`, `.pytest_cache/`, `node_modules/.vite/`. Never deletes user data, media, databases, `.venv/`, or `node_modules/` itself. |

## Quick start

```powershell
# Windows
.\tools\dev.ps1 setup
python tools\check_env.py
.\tools\dev.ps1 backend   # terminal 1
.\tools\dev.ps1 ui        # terminal 2
```

```bash
# Linux / macOS
make setup
python3 tools/check_env.py
make backend   # terminal 1
make ui        # terminal 2
```

## Files in this folder

- `dev.ps1` — PowerShell mirror of the Makefile targets, for Windows devs.
- `check_env.py` — cross-platform toolchain report (Python ≥ 3.12, Node, npm,
  cargo/rustc, ffmpeg, git) with a ✓/✗/⚠ table. Exits non-zero only if the
  hard requirement (Python) fails.
- `README.md` — this page.
