<#
.SYNOPSIS
    YouTube Agent — Windows-native dev workflow (PowerShell mirror of the Makefile).

.DESCRIPTION
    The shipped product targets Windows, so this is the primary dev entry point
    on Windows. On Linux/macOS use the Makefile instead (see tools/README.md).

    Every command is guarded: if a piece built by another team has not landed
    yet, you get a clear message and a non-zero exit, not a stack trace.

.EXAMPLE
    .\tools\dev.ps1 setup
    .\tools\dev.ps1 test
    .\tools\dev.ps1 lint
    .\tools\dev.ps1 backend
    .\tools\dev.ps1 ui
    .\tools\dev.ps1 help
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("help", "setup", "test", "test-unit", "lint", "backend", "ui", "clean")]
    [string]$Target = "help"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# --- Helpers -------------------------------------------------------------------
function Fail([string]$Message) {
    Write-Host "✗ $Message" -ForegroundColor Red
    exit 1
}
function Warn([string]$Message) {
    Write-Host "⚠ $Message" -ForegroundColor Yellow
}
function Info([string]$Message) {
    Write-Host "→ $Message"
}
function Get-VenvPython {
    $candidate = Join-Path $RepoRoot "backend\.venv\Scripts\python.exe"
    if (Test-Path $candidate) { return $candidate }
    $candidate = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $candidate) { return $candidate }
    return $null
}
function Get-Python {
    $venv = Get-VenvPython
    if ($venv) { return $venv }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $cmd3 = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd3) { return $cmd3.Source }
    return $null
}
function Assert-TestsDir {
    if (-not (Test-Path (Join-Path $RepoRoot "tests") -PathType Container)) {
        Fail "tests/ directory not found — the test suite has not landed yet. Expected layout: tests/ with pytest tests at the repo root."
    }
}

# --- Targets -------------------------------------------------------------------
function Invoke-Help {
    Write-Host "YouTube Agent — dev commands (Windows / PowerShell)"
    Write-Host ""
    Write-Host "  .\tools\dev.ps1 setup      Create .venv + install backend deps + npm install (when present)"
    Write-Host "  .\tools\dev.ps1 test       Run the full pytest suite (tests/)"
    Write-Host "  .\tools\dev.ps1 test-unit  Run only tests marked unit or contract"
    Write-Host "  .\tools\dev.ps1 lint       ruff check backend/ tests/ + npm run typecheck (when defined)"
    Write-Host "  .\tools\dev.ps1 backend    Run the FastAPI sidecar in dev mode (python -m app.main)"
    Write-Host "  .\tools\dev.ps1 ui         Run the frontend dev server (npm run dev)"
    Write-Host "  .\tools\dev.ps1 clean      Remove caches only (__pycache__, .pytest_cache, node_modules/.vite)"
    Write-Host "  .\tools\dev.ps1 help       Show this help"
    Write-Host ""
    Write-Host "Linux/macOS:  make <target>          Environment check:  python tools/check_env.py"
}

function Invoke-Setup {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) {
        # Fall back to the py launcher
        $py = Get-Command py -ErrorAction SilentlyContinue
        if (-not $py) { Fail "Python not found (need Python >= 3.12). Install it from https://www.python.org, then re-run .\tools\dev.ps1 setup" }
    }
    $pyExe = $py.Source
    Info "Python: $(& $pyExe --version 2>&1)"

    $venvPython = Get-VenvPython
    if (-not $venvPython) {
        Info "Creating virtualenv in .venv/ ..."
        & $pyExe -m venv .venv
        if ($LASTEXITCODE -ne 0) { Fail "Failed to create venv — see output above." }
        $venvPython = Get-VenvPython
    } else {
        Info "Virtualenv already exists at .venv/ (reusing it)"
    }
    $venvPip = Join-Path $RepoRoot ".venv\Scripts\pip.exe"

    $reqFiles = @("backend\requirements.txt", "backend\requirements-dev.txt", "requirements.txt") |
        Where-Object { Test-Path $_ }
    if ($reqFiles.Count -gt 0) {
        foreach ($f in $reqFiles) {
            Info "Installing Python deps from $f ..."
            & $venvPip install -r $f
            if ($LASTEXITCODE -ne 0) { Fail "pip install failed for $f — see output above." }
        }
    } elseif (Test-Path "backend\pyproject.toml") {
        Info "Installing Python deps from backend/pyproject.toml (editable install) ..."
        & $venvPip install -e backend/
        if ($LASTEXITCODE -ne 0) { Fail "pip install failed for backend/pyproject.toml" }
    } else {
        Warn "No backend requirements found yet (looked for backend/requirements.txt, requirements.txt, backend/pyproject.toml). Skipping pip install — the backend has probably not landed yet."
    }

    if (Test-Path "package.json") {
        if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
            Fail "package.json found but npm is not on PATH. Install Node.js 20+ (built with Node 24), then re-run setup."
        }
        Info "Installing frontend deps (npm install) ..."
        npm install
        if ($LASTEXITCODE -ne 0) { Fail "npm install failed — see output above." }
    } else {
        Warn "No package.json found yet — skipping npm install. The frontend (src/) has probably not landed yet."
    }
    Write-Host "✓ Setup done. Tip: run python tools/check_env.py to verify your toolchain." -ForegroundColor Green
}

function Invoke-Test {
    Assert-TestsDir
    $pyExe = Get-Python
    if (-not $pyExe) { Fail "No Python found. Run .\tools\dev.ps1 setup first." }
    Info "Running: $pyExe -m pytest tests/ -q"
    & $pyExe -m pytest tests/ -q
    exit $LASTEXITCODE
}

function Invoke-TestUnit {
    Assert-TestsDir
    $pyExe = Get-Python
    if (-not $pyExe) { Fail "No Python found. Run .\tools\dev.ps1 setup first." }
    Info "Running: $pyExe -m pytest tests/ -q -m `"unit or contract`""
    & $pyExe -m pytest tests/ -q -m "unit or contract"
    exit $LASTEXITCODE
}

function Invoke-Lint {
    $pyExe = Get-Python
    $targets = @()
    if (Test-Path "backend" -PathType Container) { $targets += "backend" }
    if (Test-Path "tests" -PathType Container) { $targets += "tests" }

    if ($targets.Count -eq 0) {
        Warn "Neither backend/ nor tests/ exists yet — skipping ruff."
    } elseif (Get-Command ruff -ErrorAction SilentlyContinue) {
        Info "ruff check $($targets -join ' ')"
        ruff check @targets
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } elseif ($pyExe) {
        & $pyExe -m ruff --version *> $null
        if ($LASTEXITCODE -eq 0) {
            Info "$pyExe -m ruff check $($targets -join ' ')"
            & $pyExe -m ruff check @targets
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        } else {
            Warn "ruff not available (no ruff on PATH and '$pyExe -m ruff' failed) — skipping Python lint. Install it with: $pyExe -m pip install ruff"
        }
    } else {
        Warn "No Python found — skipping Python lint."
    }

    if (Test-Path "package.json") {
        $pkg = Get-Content "package.json" -Raw
        if ($pkg -match '"typecheck"\s*:') {
            if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { Fail "typecheck script defined but npm is not on PATH. Install Node.js, then re-run lint." }
            if (-not (Test-Path "node_modules\.bin\tsc.cmd")) { Fail "typecheck script defined but frontend deps are not installed (node_modules/.bin/tsc.cmd missing). Run .\tools\dev.ps1 setup first." }
            Info "npm run typecheck"
            npm run typecheck
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        } else {
            Warn 'package.json has no "typecheck" script — skipping frontend typecheck.'
        }
    } else {
        Warn "No package.json found yet — skipping frontend typecheck."
    }
}

function Invoke-Backend {
    if (-not (Test-Path "backend\app\main.py" -PathType Leaf)) {
        Fail "backend/app/main.py not found — the FastAPI sidecar has not landed yet. Expected layout: backend/app/main.py (entry point: python -m app.main)."
    }
    $pyExe = Get-Python
    if (-not $pyExe) { Fail "No Python found. Run .\tools\dev.ps1 setup first." }

    # Canonical launch (docs/DEV_SETUP.md §5.1): app.main defines
    # create_app()/run() only — there is NO module-level `app` object, so
    # `uvicorn <module>:app` does NOT work. Launch via `python -m app.main`
    # with the YTA_* env contract: host is hardcoded to 127.0.0.1 in
    # app.main, the port comes from YTA_PORT, and YTA_TOKEN is required.
    # NOTE: --reload has no equivalent under `python -m app.main`
    # (uvicorn is invoked programmatically by run() without reload);
    # restart this target manually after code changes.
    if (-not $env:YTA_PORT) { $env:YTA_PORT = "8765" }
    if (-not $env:YTA_TOKEN) { $env:YTA_TOKEN = "dev-token" }
    if (-not $env:YTA_BOOT_ID) { $env:YTA_BOOT_ID = [guid]::NewGuid().ToString() }
    if (-not $env:YTA_DATA_DIR) { $env:YTA_DATA_DIR = Join-Path $RepoRoot "backend\.dev-data" }
    if (-not $env:YTA_DETACHED) { $env:YTA_DETACHED = "1" }
    if (-not $env:YTA_ALLOW_DEV_ORIGINS) { $env:YTA_ALLOW_DEV_ORIGINS = "1" }
    if (-not $env:YTA_LOG_LEVEL) { $env:YTA_LOG_LEVEL = "debug" }

    Info "Starting backend: (backend/) $pyExe -m app.main  [127.0.0.1:$env:YTA_PORT, YTA_DATA_DIR=$env:YTA_DATA_DIR; no --reload — restart manually]"
    Push-Location (Join-Path $RepoRoot "backend")
    try {
        & $pyExe -m app.main
        exit $LASTEXITCODE
    } finally {
        Pop-Location
    }
}

function Invoke-Ui {
    if (-not (Test-Path "package.json")) {
        Fail "package.json not found — the frontend (React/TypeScript/Vite in src/) has not landed yet. Expected: package.json at the repo root with a `"dev`" script (Vite)."
    }
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Fail "npm is not on PATH. Install Node.js 20+ (built with Node 24), then re-run ui."
    }
    $pkg = Get-Content "package.json" -Raw
    if ($pkg -notmatch '"dev"\s*:') {
        Fail 'package.json has no "dev" script — cannot start the UI. Add one (e.g. Vite: "dev": "vite") or start it manually.'
    }
    if (-not (Test-Path "node_modules\.bin\vite.cmd")) { Fail "Frontend deps are not installed (node_modules/.bin/vite.cmd missing). Run .\tools\dev.ps1 setup first." }
    Info "npm run dev"
    npm run dev
    exit $LASTEXITCODE
}

function Invoke-Clean {
    Info "Removing Python / pytest caches ..."
    Get-ChildItem -Path . -Directory -Filter "__pycache__" -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\.venv\\' -and $_.FullName -notmatch '\\node_modules\\' } |
        ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    foreach ($d in @(".pytest_cache", ".ruff_cache", ".mypy_cache")) {
        if (Test-Path $d) { Remove-Item $d -Recurse -Force }
    }
    Info "Removing Vite cache (node_modules/.vite) ..."
    if (Test-Path "node_modules\.vite") { Remove-Item "node_modules\.vite" -Recurse -Force }
    Write-Host "✓ Clean done. (Kept: .venv/, node_modules/, databases, media and user data.)" -ForegroundColor Green
}

# --- Dispatch ------------------------------------------------------------------
switch ($Target) {
    "help"      { Invoke-Help }
    "setup"     { Invoke-Setup }
    "test"      { Invoke-Test }
    "test-unit" { Invoke-TestUnit }
    "lint"      { Invoke-Lint }
    "backend"   { Invoke-Backend }
    "ui"        { Invoke-Ui }
    "clean"     { Invoke-Clean }
}
