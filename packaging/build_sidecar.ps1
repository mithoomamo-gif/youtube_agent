<#
.SYNOPSIS
    YouTube Agent — sidecar freeze (Windows / CI entry point).

.DESCRIPTION
    Mirror of packaging/build_sidecar.sh (the Linux dev/VM entry point);
    the two scripts implement the SAME steps. This .ps1 is the script
    .github/workflows/release.yml invokes ("Inside-out 2/5"):

        & ./packaging/build_sidecar.ps1        # no arguments, repo root
        # env: RELEASE_VERSION = <version resolved from tag/input>

    It must exit non-zero on any failure (the workflow checks
    $LASTEXITCODE) and it owns, per the workflow contract: the freeze
    itself, staging the --onedir tree, and staging the target-triple
    externalBin where tauri.conf.json expects it.

    Steps, in order:
      1. Resolve the release version from the version of record (below)
         and verify every mirror file agrees — a mismatch fails loudly.
      2. Clean the previous freeze output (packaging/dist-sidecar/ and
         packaging/build-sidecar/ only — both are build artefacts).
      3. Run PyInstaller against packaging/yta_sidecar.spec (--onedir,
         per roadmap Part 11 / docs/BUILD.md section 2; --onefile is
         non-negotiable NO). PYTHONPATH is pointed at backend/ for
         the freeze: the spec collects the app package with
         collect_submodules("app") at spec-exec time, when
         PyInstaller has NOT yet applied the spec's pathex — without
         PYTHONPATH the collection silently returns ZERO modules and
         every module the app loads by string at runtime
         (app.routers.health et al.) is missing from the bundle. A
         post-freeze completeness gate (step 4b) fails loudly if
         that ever regresses. The permanent home for this fix is the
         spec itself (insert BACKEND into sys.path before
         collecting); until the spec owner lands it, these scripts
         carry the workaround.
      4. Perform the spec's hazard-H1 copy step: prompts/ is placed
         BESIDE the executable (bundle root), because the frozen code
         resolves it as parents[3] / "prompts" of app/agents — the
         spec also bundles a mirror at _internal/prompts, but the
         bundle-root copy is the one the frozen code actually reads.
      5. Stamp the version into the bundle: <bundle>/VERSION (plain
         text) and <bundle>/build-info.json (version, git commit,
         build time, toolchain). The Windows exe version RESOURCE is
         NOT set here: PyInstaller takes it from a version= file in
         the spec, which is the spec owner's file — the spec process
         is given YTA_VERSION / YTA_GIT_COMMIT / YTA_BUILD_TIME in its
         environment so a future spec revision can stamp the resource
         from the same values these scripts stamp into the files.
      6. Stage the frozen tree for the Tauri shell under
         src-tauri/binaries/ (externalBin layout, below).
      7. Print the final bundle path + total size.

    Version single source of truth:
      docs/RELEASE_RUNBOOK.md section 2 designates
      src-tauri/tauri.conf.json (top-level "version") as the release
      version of record, with Cargo.toml, package.json,
      backend/app/__init__.py (__version__) and backend/pyproject.toml
      as mirrors that must agree (packaging/preflight.ps1 checks the
      same set). tauri.conf.json currently carries the Tauri path
      reference "../package.json", i.e. it delegates the literal value
      to package.json — this script follows that reference. There is
      deliberately NO separate packaging/VERSION file: the runbook
      locks "no single version file; a single version value", and a
      sixth copy would be one more mirror to drift.
      The CI environment variable RELEASE_VERSION (set by release.yml
      from the git tag / workflow input; also accepted via -Version)
      overrides the STAMPED version only; the mirror-consistency check
      always runs against the record, and a stamp version that
      disagrees with the record warns loudly (the runbook bump
      procedure commits the bump before tagging, so they are expected
      to match).

    Python: backend/.venv\Scripts\python.exe when present, else
    `python` on PATH, else the `py` launcher. On the CI runner the
    workflow installs PyInstaller + the backend into the system
    python before invoking this script; locally, if the chosen
    interpreter cannot run PyInstaller, a pinned PyInstaller
    (>=6.11,<7) is installed into that interpreter first.

    Tauri staging layout (src-tauri/binaries/):
      agent-backend-<target-triple>.exe   the sidecar exe under the
                                          exact externalBin name
                                          tauri.conf.json expects
                                          (Tauri renames it to
                                          agent-backend.exe at bundle
                                          time; this is the pre-stage)
      agent-backend/                      the complete --onedir tree
                                          (_internal/, prompts/,
                                          stamps), so the exe's
                                          siblings travel with it
      Override the triple with $env:YTA_TARGET_TRIPLE if ever needed.

    Idempotent: every run cleans and rebuilds from scratch. Fails
    loudly (throw / exit 1) on any error. No secrets are read or
    printed.

.NOTES
    Written on a Linux build VM where PowerShell is NOT available —
    this file has never been executed on this machine. It was authored
    carefully against the same contract as the executed .sh mirror
    and the repo's existing PowerShell style (tools/dev.ps1,
    packaging/preflight.ps1). First real execution happens in the
    release workflow on windows-latest (or a Windows dev machine).
#>
[CmdletBinding()]
param(
    # Stamp-version override. Defaults to $env:RELEASE_VERSION (the CI
    # contract); empty means "use the version of record".
    [string]$Version = $env:RELEASE_VERSION,

    # Explicit interpreter path; empty means auto-detect.
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$SpecFile  = Join-Path $RepoRoot "packaging\yta_sidecar.spec"
$DistRoot  = Join-Path $RepoRoot "packaging\dist-sidecar"
$WorkRoot  = Join-Path $RepoRoot "packaging\build-sidecar"
$BundleName = "yta_sidecar"
$BundleDir = Join-Path $DistRoot $BundleName
$StageDir  = Join-Path $RepoRoot "src-tauri\binaries"
$PyInstallerPin = "pyinstaller>=6.11,<7"

function Fail([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}
function Info([string]$Message) {
    Write-Host "==> $Message"
}
function Warn([string]$Message) {
    Write-Host "WARNING: $Message" -ForegroundColor Yellow
}
function Get-FileText([string]$Path) {
    if (Test-Path $Path -PathType Leaf) { return Get-Content $Path -Raw }
    return ""
}
function Get-RegexVersion([string]$Text, [string]$Pattern) {
    if ($Text -match $Pattern) { return $Matches[1] }
    return ""
}

# ---------------------------------------------------------------------------
# 0. Interpreter
# ---------------------------------------------------------------------------
$PythonExe = $null
$PythonArgs = @()
if (-not [string]::IsNullOrWhiteSpace($Python)) {
    if (-not (Test-Path $Python -PathType Leaf)) { Fail "explicit -Python interpreter not found: $Python" }
    $PythonExe = $Python
} elseif (Test-Path (Join-Path $RepoRoot "backend\.venv\Scripts\python.exe") -PathType Leaf) {
    $PythonExe = Join-Path $RepoRoot "backend\.venv\Scripts\python.exe"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonExe = (Get-Command python).Source
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonExe = (Get-Command py).Source
    $PythonArgs = @("-3")
} else {
    Fail "no Python interpreter found (looked for backend\.venv\Scripts\python.exe, then python / py on PATH)"
}
$PythonVersion = (& $PythonExe @PythonArgs --version 2>&1 | Select-Object -First 1)
Info "Python: $PythonExe ($PythonVersion)"

# ---------------------------------------------------------------------------
# 1. Version: resolve from the version of record + verify all mirrors.
#    The mirror set intentionally matches packaging/preflight.ps1.
# ---------------------------------------------------------------------------
$TauriConfPath = Join-Path $RepoRoot "src-tauri\tauri.conf.json"
if (-not (Test-Path $TauriConfPath -PathType Leaf)) { Fail "src-tauri\tauri.conf.json not found under $RepoRoot" }
try {
    $TauriConf = Get-Content $TauriConfPath -Raw | ConvertFrom-Json
} catch {
    Fail "could not parse src-tauri\tauri.conf.json: $($_.Exception.Message)"
}
$RawRecord = [string]$TauriConf.version
if ([string]::IsNullOrWhiteSpace($RawRecord)) { Fail "src-tauri\tauri.conf.json has no top-level `"version`"" }

if ($RawRecord -like "*.json") {
    # Tauri path reference (current tree: "../package.json") — the
    # referenced file carries the literal version of record.
    $RecordFile = [System.IO.Path]::GetFullPath((Join-Path (Join-Path $RepoRoot "src-tauri") $RawRecord))
    if (-not (Test-Path $RecordFile -PathType Leaf)) { Fail "tauri.conf.json version references $RawRecord, which does not resolve to a file" }
    try {
        $RecordVersion = [string]((Get-Content $RecordFile -Raw | ConvertFrom-Json).version)
    } catch {
        Fail "could not read version from ${RecordFile}: $($_.Exception.Message)"
    }
    $RecordDesc = "src-tauri\tauri.conf.json -> $RawRecord"
} else {
    $RecordVersion = $RawRecord
    $RecordDesc = "src-tauri\tauri.conf.json"
}
if ([string]::IsNullOrWhiteSpace($RecordVersion)) { Fail "version of record resolved to an empty string ($RecordDesc)" }
if ($RecordVersion -notmatch '^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$') {
    Fail "version of record '$RecordVersion' ($RecordDesc) is not semver X.Y.Z"
}

$PkgVersion = ""
try { $PkgVersion = [string]((Get-Content (Join-Path $RepoRoot "package.json") -Raw | ConvertFrom-Json).version) } catch { $PkgVersion = "" }
$CargoVersion   = Get-RegexVersion (Get-FileText (Join-Path $RepoRoot "src-tauri\Cargo.toml"))       '(?m)^\s*version\s*=\s*"([^"]*)"'
$BackendInit    = Get-RegexVersion (Get-FileText (Join-Path $RepoRoot "backend\app\__init__.py"))    '(?m)^\s*__version__\s*=\s*"([^"]*)"'
$PyprojectVer   = Get-RegexVersion (Get-FileText (Join-Path $RepoRoot "backend\pyproject.toml"))     '(?m)^\s*version\s*=\s*"([^"]*)"'

$Mismatches = @()
if ($PkgVersion   -ne $RecordVersion) { $Mismatches += "package.json='$PkgVersion'" }
if ($CargoVersion -ne $RecordVersion) { $Mismatches += "src-tauri/Cargo.toml='$CargoVersion'" }
if ($BackendInit  -ne $RecordVersion) { $Mismatches += "backend/app/__init__.py __version__='$BackendInit'" }
if ($PyprojectVer -ne $RecordVersion) { $Mismatches += "backend/pyproject.toml='$PyprojectVer'" }
if ($Mismatches.Count -gt 0) {
    Fail "version drift: version of record ($RecordDesc) = '$RecordVersion' but mismatched mirror(s): $($Mismatches -join ' ') — fix the lagging file(s) (docs/RELEASE_RUNBOOK.md section 2), never build over a mismatch"
}
Info "Version of record: $RecordVersion ($RecordDesc) — all mirrors agree"

$StampVersion = $RecordVersion
if (-not [string]::IsNullOrWhiteSpace($Version)) {
    $StampVersion = $Version -replace '^v', ''
    if ($StampVersion -ne $RecordVersion) {
        Warn "RELEASE_VERSION/-Version '$StampVersion' differs from the version of record '$RecordVersion'; stamping '$StampVersion' (CI tag/input wins for the stamp, per release.yml). The mirror check above still passed against the record."
    }
    Info "Stamp version (from RELEASE_VERSION/-Version): $StampVersion"
}

# ---------------------------------------------------------------------------
# 2. Spec + PyInstaller prerequisites
# ---------------------------------------------------------------------------
if (-not (Test-Path $SpecFile -PathType Leaf)) {
    Fail "spec not found: packaging\yta_sidecar.spec (owned by the sidecar-freeze spec worker; there is nothing to freeze without it)"
}

& $PythonExe @PythonArgs -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    Info "PyInstaller not available in this interpreter — installing pinned '$PyInstallerPin'"
    & $PythonExe @PythonArgs -m pip install $PyInstallerPin
    if ($LASTEXITCODE -ne 0) { Fail "pip install of PyInstaller failed" }
}
$PyInstallerVersion = (& $PythonExe @PythonArgs -m PyInstaller --version 2>&1 | Select-Object -First 1)
Info "PyInstaller: $PyInstallerVersion"

$GitCommit = "unknown"
$git = Get-Command git -ErrorAction SilentlyContinue
if ($git) {
    $head = & git -C $RepoRoot rev-parse --short HEAD 2>$null
    if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($head)) { $GitCommit = $head.Trim() }
}
$BuildTime = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$Platform = "Windows-$env:PROCESSOR_ARCHITECTURE"

# ---------------------------------------------------------------------------
# 3. Clean previous freeze output (artefact dirs only)
# ---------------------------------------------------------------------------
Info "Cleaning previous freeze output: packaging\dist-sidecar\, packaging\build-sidecar\"
foreach ($dir in @($DistRoot, $WorkRoot)) {
    if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

# ---------------------------------------------------------------------------
# 4. Freeze
# ---------------------------------------------------------------------------
Info "Freezing sidecar: PyInstaller packaging\yta_sidecar.spec (--onedir)"
$env:YTA_VERSION = $StampVersion
$env:YTA_GIT_COMMIT = $GitCommit
$env:YTA_BUILD_TIME = $BuildTime
$BackendDir = Join-Path $RepoRoot "backend"
if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) { $env:PYTHONPATH = $BackendDir }
else { $env:PYTHONPATH = "$BackendDir;$env:PYTHONPATH" }
& $PythonExe @PythonArgs -m PyInstaller $SpecFile --distpath $DistRoot --workpath $WorkRoot --noconfirm
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller freeze failed — see the output above" }

$ExeName = "$BundleName.exe"
if (-not (Test-Path (Join-Path $BundleDir $ExeName) -PathType Leaf)) {
    Fail "freeze reported success but no executable at $BundleDir\$ExeName"
}
Info "Bundle executable: $BundleDir\$ExeName"

# ---------------------------------------------------------------------------
# 4b. Completeness gate: modules the app loads BY STRING at runtime
#     (invisible to static analysis) must be inside the frozen PYZ.
#     Their absence is silent at build time and fatal at runtime —
#     fail loudly here instead. (See the PYTHONPATH note in step 3.)
# ---------------------------------------------------------------------------
$PyzToc = Join-Path $WorkRoot "$BundleName\PYZ-00.toc"
if (-not (Test-Path $PyzToc -PathType Leaf)) {
    Fail "completeness gate: $PyzToc not found — cannot verify the frozen module set"
}
$PyzTocText = Get-Content $PyzToc -Raw
$MissingModules = @()
foreach ($mod in @("app.main", "app.routers.health", "app.routers.system", "app.routers.ws")) {
    if ($PyzTocText -notmatch [regex]::Escape("'$mod'")) { $MissingModules += $mod }
}
if ($MissingModules.Count -gt 0) {
    Fail "completeness gate: frozen bundle is missing runtime-loaded module(s): $($MissingModules -join ' ') — the spec's collect_submodules(`"app`") collected nothing (backend\ must be importable at spec-exec time; these scripts set PYTHONPATH for exactly this). Do NOT ship this bundle."
}
Info "Completeness gate passed: runtime-loaded modules present in the frozen PYZ"

# ---------------------------------------------------------------------------
# 5. Hazard-H1 copy step (spec header): prompts/ BESIDE the executable.
# ---------------------------------------------------------------------------
$PromptsSrc = Join-Path $RepoRoot "prompts"
if (-not (Test-Path $PromptsSrc -PathType Container)) {
    Fail "repo-root prompts\ directory not found — the frozen sidecar cannot assemble agent prompts without it"
}
$PromptsDst = Join-Path $BundleDir "prompts"
if (Test-Path $PromptsDst) { Remove-Item $PromptsDst -Recurse -Force }
Copy-Item $PromptsSrc $PromptsDst -Recurse
Info "Staged prompts\ beside the executable (hazard H1; mirror also in _internal\prompts via the spec)"

# ---------------------------------------------------------------------------
# 6. Version stamping inside the bundle
# ---------------------------------------------------------------------------
Set-Content -Path (Join-Path $BundleDir "VERSION") -Value $StampVersion -Encoding UTF8
$BuildInfo = [ordered]@{
    product           = "youtube-agent-sidecar"
    bundle            = "yta_sidecar"
    version           = $StampVersion
    version_of_record = $RecordVersion
    git_commit        = $GitCommit
    build_time_utc    = $BuildTime
    platform          = $Platform
    pyinstaller       = "$PyInstallerVersion"
    spec              = "packaging/yta_sidecar.spec"
    executable        = $ExeName
    layout            = "pyinstaller --onedir"
}
$BuildInfo | ConvertTo-Json | Set-Content -Path (Join-Path $BundleDir "build-info.json") -Encoding UTF8
Info "Stamped version $StampVersion into $BundleDir\VERSION and build-info.json"

# ---------------------------------------------------------------------------
# 7. Stage for the Tauri shell (externalBin layout under src-tauri/binaries/)
# ---------------------------------------------------------------------------
$Triple = $env:YTA_TARGET_TRIPLE
if ([string]::IsNullOrWhiteSpace($Triple)) {
    if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { $Triple = "aarch64-pc-windows-msvc" }
    else { $Triple = "x86_64-pc-windows-msvc" }
}
$StagedTree = Join-Path $StageDir "agent-backend"
$StagedExe  = Join-Path $StageDir "agent-backend-$Triple.exe"
if (Test-Path $StagedTree) { Remove-Item $StagedTree -Recurse -Force }
if (Test-Path $StagedExe)  { Remove-Item $StagedExe -Force }
New-Item -ItemType Directory -Force -Path $StageDir | Out-Null
Copy-Item $BundleDir $StagedTree -Recurse
Copy-Item (Join-Path $BundleDir $ExeName) $StagedExe
Info "Staged for Tauri: $StagedExe (+ full tree at $StagedTree\)"

# ---------------------------------------------------------------------------
# 8. Summary
# ---------------------------------------------------------------------------
$Files = Get-ChildItem $BundleDir -Recurse -File
$TotalBytes = ($Files | Measure-Object -Property Length -Sum).Sum
if ($null -eq $TotalBytes) { $TotalBytes = 0 }
$TotalMB = [math]::Round($TotalBytes / 1MB, 1)

Write-Host ""
Info "SIDECAR FREEZE COMPLETE"
Write-Host "  Version (stamped) : $StampVersion"
Write-Host "  Bundle path       : $BundleDir"
Write-Host "  Executable        : $BundleDir\$ExeName"
Write-Host "  Bundle size       : $TotalBytes bytes ($TotalMB MB, $($Files.Count) files)"
Write-Host "  Tauri staging     : $StagedExe"
exit 0
