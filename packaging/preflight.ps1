<#
.SYNOPSIS
    YouTube Agent release preflight (Windows PowerShell).

.DESCRIPTION
    Mirror of packaging/preflight.sh — the two scripts implement the SAME
    checks with the same PASS/FAIL semantics. Self-contained: no network
    access, no secrets are read or printed. Every check prints PASS or
    FAIL with a one-line reason. The script exits 0 only if EVERY check
    passes; any FAIL exits non-zero, because a failed check is a genuine
    release blocker, not a warning.

.PARAMETER Version
    Optional. The version being released (e.g. 0.1.0). Defaults to the
    version in src-tauri/tauri.conf.json (the release-facing source of
    truth for this project).

.PARAMETER Root
    Optional. Repository root. Defaults to the parent directory of this
    script, or to $env:YTA_PREFLIGHT_ROOT when set (used for fixture /
    positive-control runs).

.EXAMPLE
    .\packaging\preflight.ps1
    .\packaging\preflight.ps1 0.1.0
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Version,

    [string]$Root
)

$ErrorActionPreference = 'Stop'

if (-not $Root) {
    if ($env:YTA_PREFLIGHT_ROOT) {
        $Root = $env:YTA_PREFLIGHT_ROOT
    } else {
        $Root = Split-Path -Parent $PSScriptRoot
    }
}

$TauriConfPath       = Join-Path $Root 'src-tauri/tauri.conf.json'
$CargoTomlPath       = Join-Path $Root 'src-tauri/Cargo.toml'
$PackageJsonPath     = Join-Path $Root 'package.json'
$BackendInitPath     = Join-Path $Root 'backend/app/__init__.py'
$BackendPyprojectPath = Join-Path $Root 'backend/pyproject.toml'
$MigrationsDir       = Join-Path $Root 'backend/migrations/versions'
$ChangelogPath       = Join-Path $Root 'CHANGELOG.md'

$script:PassCount = 0
$script:FailCount = 0

function Report-Pass([string]$Name, [string]$Reason) {
    $script:PassCount++
    Write-Host "[PASS] $Name — $Reason"
}

function Report-Fail([string]$Name, [string]$Reason) {
    $script:FailCount++
    Write-Host "[FAIL] $Name — $Reason"
}

# ---------------------------------------------------------------------------
# Config extraction (tauri.conf.json via ConvertFrom-Json).
# ---------------------------------------------------------------------------
$ConfigError  = ''
$TauriResolveError = ''
$TauriConfig  = $null
$TauriVersion = ''
$IconList     = @()
$TargetList   = @()
$NsisInstallMode = ''
$UpdaterPubkey = '__MISSING__'
$EndpointList = @()

if (-not (Test-Path $TauriConfPath)) {
    $ConfigError = "src-tauri/tauri.conf.json not found under $Root"
} else {
    try {
        $TauriConfig = Get-Content -Raw -Path $TauriConfPath | ConvertFrom-Json
        $TauriVersion = [string]$TauriConfig.version
        if ($TauriConfig.bundle) {
            if ($TauriConfig.bundle.icon)    { $IconList   = @($TauriConfig.bundle.icon) }
            if ($TauriConfig.bundle.targets) { $TargetList = @($TauriConfig.bundle.targets) }
            if ($TauriConfig.bundle.windows -and $TauriConfig.bundle.windows.nsis) {
                $NsisInstallMode = [string]$TauriConfig.bundle.windows.nsis.installMode
            }
        }
        if ($TauriConfig.plugins -and $TauriConfig.plugins.updater) {
            $updater = $TauriConfig.plugins.updater
            if ($null -ne $updater.PSObject.Properties['pubkey']) {
                $UpdaterPubkey = [string]$updater.pubkey
            }
            if ($updater.endpoints) { $EndpointList = @($updater.endpoints) }
        }
    } catch {
        $ConfigError = "could not parse src-tauri/tauri.conf.json: $($_.Exception.Message)"
    }
}

# Resolve the Tauri version indirection: tauri.conf.json's version may be
# a path reference (e.g. "../package.json", ending in .json) instead of a
# literal semver. When it is, read the referenced file (relative to
# src-tauri/) and use ITS version as the tauri version everywhere below
# (default target version + check a). A missing/unparseable referenced
# file fails check (a) with the resolve reason.
if (-not $ConfigError -and $TauriVersion -and $TauriVersion.EndsWith('.json')) {
    $refPath = Join-Path (Join-Path $Root 'src-tauri') $TauriVersion
    if (-not (Test-Path $refPath)) {
        $TauriResolveError = "tauri.conf.json version references '$TauriVersion' but the referenced file was not found: $refPath"
    } else {
        try {
            $resolvedVersion = [string](Get-Content -Raw -Path $refPath | ConvertFrom-Json).version
            if (-not $resolvedVersion) {
                $TauriResolveError = "tauri.conf.json version references '$TauriVersion' but no version could be parsed from $refPath"
            } else {
                $TauriVersion = $resolvedVersion
            }
        } catch {
            $TauriResolveError = "tauri.conf.json version references '$TauriVersion' but the referenced file could not be parsed: $($_.Exception.Message)"
        }
    }
}

$TargetVersion = $Version
if (-not $TargetVersion) { $TargetVersion = $TauriVersion }

function Get-FirstMatchValue([string]$Path, [string]$Pattern) {
    if (-not (Test-Path $Path)) { return '' }
    foreach ($line in Get-Content -Path $Path) {
        if ($line -match $Pattern) { return $Matches[1] }
    }
    return ''
}

Write-Host 'YouTube Agent — release preflight'
Write-Host "Root: $Root"
Write-Host "Target version: $(if ($TargetVersion) { $TargetVersion } else { '<unknown>' })"
Write-Host '----------------------------------------------------------------'

# ---------------------------------------------------------------------------
# a. Version consistency:
#    tauri.conf.json (release-facing source of truth) == Cargo.toml
#    == package.json == backend version sources (app/__init__.py
#    __version__ and backend/pyproject.toml, both of which exist here).
# ---------------------------------------------------------------------------
function Check-Versions {
    $name = 'a. Version consistency'
    if ($ConfigError) { Report-Fail $name $ConfigError; return }
    if ($TauriResolveError) { Report-Fail $name $TauriResolveError; return }

    $cargoVersion   = Get-FirstMatchValue $CargoTomlPath '^\s*version\s*=\s*"([^"]*)"'
    $packageVersion = ''
    if (Test-Path $PackageJsonPath) {
        try { $packageVersion = [string](Get-Content -Raw $PackageJsonPath | ConvertFrom-Json).version }
        catch { $packageVersion = '' }
    }
    $backendInitVersion    = Get-FirstMatchValue $BackendInitPath '^\s*__version__\s*=\s*"([^"]*)"'
    $backendProjectVersion = Get-FirstMatchValue $BackendPyprojectPath '^\s*version\s*=\s*"([^"]*)"'

    if (-not $TauriVersion) { Report-Fail $name 'tauri.conf.json has no version'; return }

    $problems = ''
    if ($cargoVersion   -ne $TauriVersion) { $problems += " Cargo.toml='$cargoVersion'" }
    if ($packageVersion -ne $TauriVersion) { $problems += " package.json='$packageVersion'" }
    if ((Test-Path $BackendInitPath) -and $backendInitVersion -ne $TauriVersion) {
        $problems += " backend/app/__init__.py __version__='$backendInitVersion'"
    }
    if ((Test-Path $BackendPyprojectPath) -and $backendProjectVersion -ne $TauriVersion) {
        $problems += " backend/pyproject.toml='$backendProjectVersion'"
    }

    if ($problems) {
        Report-Fail $name "tauri.conf.json='$TauriVersion' but mismatched:$problems"
    } else {
        Report-Pass $name "tauri.conf.json, Cargo.toml, package.json and backend version sources all = $TauriVersion"
    }
}

# ---------------------------------------------------------------------------
# b. Icon set present: every file listed in bundle.icon exists (relative
#    to src-tauri/) and is non-empty.
# ---------------------------------------------------------------------------
function Check-Icons {
    $name = 'b. Icon set present'
    if ($ConfigError) { Report-Fail $name $ConfigError; return }
    if ($IconList.Count -eq 0) { Report-Fail $name 'tauri.conf.json bundle.icon is empty or missing'; return }

    $missing = ''
    foreach ($icon in $IconList) {
        $iconPath = Join-Path (Join-Path $Root 'src-tauri') $icon
        if (-not (Test-Path $iconPath) -or (Get-Item $iconPath).Length -eq 0) {
            $missing += " $icon"
        }
    }
    if ($missing) {
        Report-Fail $name "missing or empty icon file(s) referenced by bundle.icon:$missing (see src-tauri/icons/README.txt)"
    } else {
        Report-Pass $name 'every bundle.icon file exists and is non-empty'
    }
}

# ---------------------------------------------------------------------------
# c. Updater: pubkey present and non-empty; every endpoint is https://
#    and not a placeholder host (example.invalid / example.com / .org /
#    .net, or localhost).
# ---------------------------------------------------------------------------
function Test-PlaceholderHost([string]$HostName) {
    $placeholderHosts = @('example.invalid', 'example.com', 'example.org', 'example.net', 'localhost', '127.0.0.1')
    if ($placeholderHosts -contains $HostName) { return $true }
    foreach ($suffix in @('.example.invalid', '.example.com', '.example.org', '.example.net')) {
        if ($HostName.EndsWith($suffix)) { return $true }
    }
    return $false
}

function Check-Updater {
    $name = 'c. Updater pubkey + endpoints'
    if ($ConfigError) { Report-Fail $name $ConfigError; return }

    $problems = ''
    if ($UpdaterPubkey -eq '__MISSING__') {
        $problems += ' plugins.updater.pubkey is missing;'
    } elseif (-not $UpdaterPubkey) {
        $problems += ' plugins.updater.pubkey is empty (no signing key configured — updates cannot be verified);'
    }

    if ($EndpointList.Count -eq 0) {
        $problems += ' plugins.updater.endpoints is empty;'
    } else {
        foreach ($endpoint in $EndpointList) {
            if ($endpoint -notlike 'https://*') {
                $problems += " endpoint '$endpoint' is not https://;"
                continue
            }
            try { $hostName = ([Uri]$endpoint).Host } catch { $hostName = '' }
            if (Test-PlaceholderHost $hostName) {
                $problems += " endpoint '$endpoint' uses placeholder host '$hostName';"
            }
        }
    }

    if ($problems) {
        Report-Fail $name $problems.Trim()
    } else {
        Report-Pass $name 'pubkey is non-empty and all updater endpoints are https:// on non-placeholder hosts'
    }
}

# ---------------------------------------------------------------------------
# d. Migration chain single head: parse revision / down_revision from
#    every file in backend/migrations/versions. Exactly one head (a
#    revision no other revision points to), no duplicate revisions, no
#    dangling down_revision, exactly one root (down_revision = None).
# ---------------------------------------------------------------------------
function Check-Migrations {
    $name = 'd. Migration chain single head'
    if (-not (Test-Path $MigrationsDir)) {
        Report-Fail $name "migrations directory not found: $MigrationsDir"
        return
    }

    $revisions = @()
    $downs = @()      # plain down_revision values
    $rootCount = 0

    foreach ($file in Get-ChildItem -Path $MigrationsDir -Filter '*.py') {
        if ($file.Name -eq '__init__.py') { continue }
        $text = Get-Content -Raw -Path $file.FullName

        $revMatch = [regex]::Match($text, '(?m)^\s*revision\s*(?::[^=\r\n]*)?=\s*"([^"]+)"')
        if (-not $revMatch.Success) {
            Report-Fail $name "could not parse a revision in $($file.Name)"
            return
        }
        $revisions += $revMatch.Groups[1].Value

        if ($text -match '(?m)^\s*down_revision\s*(?::[^=\r\n]*)?=\s*None') {
            $rootCount++
        } else {
            $downMatch = [regex]::Match($text, '(?m)^\s*down_revision\s*(?::[^=\r\n]*)?=\s*"([^"]+)"')
            if (-not $downMatch.Success) {
                Report-Fail $name "could not parse down_revision in $($file.Name)"
                return
            }
            $downs += $downMatch.Groups[1].Value
        }
    }

    if ($revisions.Count -eq 0) { Report-Fail $name 'no migration files found'; return }

    $duplicates = $revisions | Group-Object | Where-Object { $_.Count -gt 1 } | ForEach-Object { $_.Name }
    if ($duplicates) {
        Report-Fail $name "duplicate revision(s): $($duplicates -join ' ')"
        return
    }

    foreach ($down in $downs) {
        if ($revisions -notcontains $down) {
            Report-Fail $name "down_revision '$down' does not match any revision (broken chain)"
            return
        }
    }

    if ($rootCount -ne 1) {
        Report-Fail $name "expected exactly 1 root migration (down_revision = None), found $rootCount"
        return
    }

    $heads = @($revisions | Where-Object { $downs -notcontains $_ })
    if ($heads.Count -ne 1) {
        Report-Fail $name "expected exactly 1 migration head, found $($heads.Count): $($heads -join ' ')"
    } else {
        Report-Pass $name "single migration head: $($heads[0]) (chain of $($revisions.Count) revisions, 1 root, no duplicates or dangling links)"
    }
}

# ---------------------------------------------------------------------------
# e. Changelog entry exists for the target version.
# ---------------------------------------------------------------------------
function Check-Changelog {
    $name = 'e. Changelog entry'
    if (-not $TargetVersion) {
        Report-Fail $name 'no target version (tauri.conf.json version missing and no version argument given)'
        return
    }
    if (-not (Test-Path $ChangelogPath)) {
        Report-Fail $name 'CHANGELOG.md not found at repo root'
        return
    }
    $escaped = [regex]::Escape($TargetVersion)
    $pattern = "(?m)^##\s+\[?$escaped(\]|[^\d.])"
    $patternEol = "(?m)^##\s+\[?$escaped\]?\s*$"
    $text = Get-Content -Raw -Path $ChangelogPath
    if ($text -match $pattern -or $text -match $patternEol) {
        Report-Pass $name "CHANGELOG.md has a heading for version $TargetVersion"
    } else {
        Report-Fail $name "CHANGELOG.md has no '## [$TargetVersion]' (or '## $TargetVersion') heading"
    }
}

# ---------------------------------------------------------------------------
# f. NSIS target + per-user install mode still configured (guard against
#    silent config drift in tauri.conf.json).
# ---------------------------------------------------------------------------
function Check-Nsis {
    $name = 'f. NSIS target + per-user install mode'
    if ($ConfigError) { Report-Fail $name $ConfigError; return }

    if ($TargetList -notcontains 'nsis') {
        Report-Fail $name "bundle.targets does not include 'nsis' (found: $($TargetList -join ', '))"
    } elseif ($NsisInstallMode -ne 'currentUser') {
        Report-Fail $name "bundle.windows.nsis.installMode is '$NsisInstallMode', expected 'currentUser'"
    } else {
        Report-Pass $name "bundle.targets includes 'nsis' and bundle.windows.nsis.installMode = 'currentUser'"
    }
}

Check-Versions
Check-Icons
Check-Updater
Check-Migrations
Check-Changelog
Check-Nsis

Write-Host '----------------------------------------------------------------'
Write-Host "Summary: $script:PassCount passed, $script:FailCount failed"
if ($script:FailCount -gt 0) {
    Write-Host 'Preflight FAILED — resolve the FAIL item(s) above before releasing.'
    exit 1
}
Write-Host "Preflight PASSED — all checks green for version $TargetVersion."
exit 0
