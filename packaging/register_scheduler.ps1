<#
.SYNOPSIS
    Registers (or removes) the per-user Windows Task Scheduler task that
    wakes YouTube Agent for scheduled video production.

.DESCRIPTION
    YouTube Agent deliberately does NOT run as a Windows Service
    (roadmap section 11.9): a Session-0 service could not reach the
    per-user DPAPI-protected YouTube OAuth tokens or the desktop/GPU
    session.  Instead, this script registers a Scheduled Task that runs
    as the CURRENT user, at the Limited (non-elevated) run level, and
    launches the installed app executable with the argument:

        --scheduled-run

    On launch with that argument the app performs one catch-up pass
    over its due schedules (catch_up_once: several missed slots collapse
    into a single production run), then the one-shot sidecar exits; the
    app itself is tray-resident once launched, so production proceeds in
    the user's own session.  See backend/app/execution/scheduled_run.py.

    Triggers:
      * At logon of the current user, so schedules missed while the
        machine was off are caught up as soon as the user signs in.
      * A repeating trigger (every -IntervalMinutes, default 30) so due
        schedules are also caught while the machine stays signed in but
        the app is not running.

    The task is registered in the root task folder under the current
    user's context.  No administrator rights are required, and the
    script never requests elevation.

    IDEMPOTENT: Register-ScheduledTask is called with -Force, so
    re-running this script REPLACES the existing task with the current
    definition instead of creating a duplicate.  Run it again after any
    install/upgrade or to change -IntervalMinutes.

.PARAMETER AppPath
    Full path to the installed app executable.  Defaults to the
    per-user install location used by the NSIS installer:
    %LOCALAPPDATA%\Programs\YouTubeAgent\youtube-agent.exe

.PARAMETER TaskName
    Name of the scheduled task.  Default: 'YouTube Agent - Scheduled Run'.

.PARAMETER IntervalMinutes
    Repetition interval of the repeating trigger, in minutes.
    Must be between 5 and 1440.  Default: 30.

.PARAMETER Unregister
    Switch: remove the task instead of registering it.  Idempotent -
    succeeds silently when the task does not exist.  The uninstaller's
    PREUNINSTALL hook performs the same removal.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File register_scheduler.ps1
    Registers the task with default settings.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File register_scheduler.ps1 -IntervalMinutes 15
    Registers the task with a 15-minute repeating trigger.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File register_scheduler.ps1 -Unregister
    Removes the task.

.NOTES
    Requires Windows PowerShell 5.1 or PowerShell 7+ on Windows
    (uses the built-in ScheduledTasks module).  The task is created
    with New-ScheduledTask* cmdlets and registered with
    Register-ScheduledTask -Force.

    STATIC REVIEW NOTE (honest): this script was authored and reviewed
    on a Linux build machine where PowerShell cannot execute.  It has
    been statically reviewed line by line against the ScheduledTasks
    module's documented cmdlets/parameters, but it has NEVER BEEN RUN.
    First execution must happen on a Windows machine (dev or CI
    windows-latest) before it is trusted in the installer.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$AppPath = (Join-Path $env:LOCALAPPDATA 'Programs\YouTubeAgent\youtube-agent.exe'),

    [Parameter(Mandatory = $false)]
    [string]$TaskName = 'YouTube Agent - Scheduled Run',

    [Parameter(Mandatory = $false)]
    [ValidateRange(5, 1440)]
    [int]$IntervalMinutes = 30,

    [Parameter(Mandatory = $false)]
    [switch]$Unregister
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Unregister path (-Unregister): remove the task if present, else no-op.
# ---------------------------------------------------------------------------
if ($Unregister) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Host "Scheduled task '$TaskName' is not registered; nothing to remove."
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Scheduled task '$TaskName' removed."
    exit 0
}

# ---------------------------------------------------------------------------
# Register path.
# ---------------------------------------------------------------------------

# The task must point at a real executable; fail loudly otherwise so a
# broken install never leaves a task that errors every 30 minutes.
if (-not (Test-Path -LiteralPath $AppPath -PathType Leaf)) {
    throw "YouTube Agent executable not found at '$AppPath'. Install the app first, or pass -AppPath with the correct location."
}

# Current user in DOMAIN\user form: the task runs ONLY as this user, in
# their interactive session (DPAPI secrets + GPU/desktop access), and
# with the Limited run level - never elevated.
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# Action: launch the app headless-catch-up entry point.  -Argument is a
# single fixed string; no user input is interpolated into it.
$action = New-ScheduledTaskAction `
    -Execute $AppPath `
    -Argument '--scheduled-run' `
    -WorkingDirectory (Split-Path -LiteralPath $AppPath -Parent)

# Triggers: at logon (catch up after the machine was off) plus a
# repeating trigger (catch due schedules while signed in).  The
# repetition duration is finite (10 years) because the ScheduledTasks
# API requires one; re-running this script or reinstalling the app
# refreshes it, and the logon trigger has no expiry.
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$repeatTrigger = New-ScheduledTaskTrigger `
    -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

# Settings:
#   StartWhenAvailable    - if the machine was asleep/off at a trigger
#                           time, run as soon as it is available again
#                           (this is what makes catch_up_once timely).
#   AllowStartIfOnBatteries / DontStopIfGoingOnBatteries
#                         - a laptop on battery must not skip or kill a
#                           scheduled production wake.
#   MultipleInstances IgnoreNew - overlapping wakes never double-launch;
#                           the app is single-instance regardless.
#   ExecutionTimeLimit    - TimeSpan.Zero, which Task Scheduler treats
#                           as NO limit: the wake hands over to the
#                           tray-resident app whose renders may run for
#                           hours, and the task must never kill one
#                           mid-render.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([System.TimeSpan]::Zero)

# Principal: current user, interactive logon only, Limited (no elevation).
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

$task = New-ScheduledTask `
    -Action $action `
    -Trigger $logonTrigger, $repeatTrigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Wakes YouTube Agent (--scheduled-run) so due video schedules are produced on time. Per-user task; not a Windows Service.'

# -Force makes registration idempotent: an existing task with this name
# is replaced, never duplicated.
Register-ScheduledTask `
    -TaskName $TaskName `
    -InputObject $task `
    -Force | Out-Null

Write-Host "Scheduled task '$TaskName' registered for user '$currentUser'."
Write-Host "  Action   : `"$AppPath`" --scheduled-run"
Write-Host "  Triggers : at logon + every $IntervalMinutes minutes"
Write-Host "Dry-run check: run `"$AppPath`" --scheduled-run --dry-run to list due schedules without running them."
