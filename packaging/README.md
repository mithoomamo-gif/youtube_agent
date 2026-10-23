# Packaging — Windows Task Scheduler integration (Round 6)

Scheduled production on Windows uses **Windows Task Scheduler — never a
Windows Service** (locked roadmap decision, §11.9): a Session-0 service
could not reach the per-user DPAPI-protected YouTube OAuth tokens or the
desktop/GPU session. The scheduler wakes the *app*; the app already owns
scheduling semantics (`SchedulerTick`, `catch_up_once`).

## Files

| File | Purpose |
|---|---|
| `register_scheduler.ps1` | Registers/removes the per-user scheduled task. Registration is the default; `-Unregister` removes the task (same script, documented in its comment-based help). |

## The task

* **Name:** `YouTube Agent - Scheduled Run` (root task folder)
* **Runs as:** the current user only, `LogonType Interactive`,
  `RunLevel Limited` — **no elevation, ever**
* **Action:** `"%LOCALAPPDATA%\Programs\YouTubeAgent\youtube-agent.exe" --scheduled-run`
  (override with `-AppPath`)
* **Triggers:** at logon of the current user, plus a repeating trigger
  every 30 minutes (`-IntervalMinutes`, 5–1440)
* **Settings:** `StartWhenAvailable` (missed wakes run when the machine
  returns), runs on battery / not stopped on battery, `MultipleInstances
  IgnoreNew`, no execution time limit (a long render must never be
  killed by the wake's own task)
* **Idempotent:** re-running the script replaces the task
  (`Register-ScheduledTask -Force`); it never duplicates. Re-run after
  install/upgrade. The uninstaller's PREUNINSTALL hook removes the task
  (equivalent to `-Unregister`).

```powershell
# Register (defaults)
powershell -NoProfile -ExecutionPolicy Bypass -File register_scheduler.ps1
# Register with a 15-minute repeat
powershell -NoProfile -ExecutionPolicy Bypass -File register_scheduler.ps1 -IntervalMinutes 15
# Remove
powershell -NoProfile -ExecutionPolicy Bypass -File register_scheduler.ps1 -Unregister
```

## How a wake flows

```
Task Scheduler (logon / every 30 min)
  -> youtube-agent.exe --scheduled-run            (Tauri shell)
       lib.rs: a single-instance wake against a running app returns
         early — the window is never surfaced for a scheduled launch
       sidecar.rs Supervisor (args parsed next to the existing
         --headless handling):
         * suppress_window_reveal latched for the whole launch
         * first spawn is the ONE-SHOT child: argv gets
           --scheduled-run (+ --dry-run when present), env gets
           YTA_DETACHED=1 (Task Scheduler launch => no parent watchdog)
         * no HTTP readiness polling / cold-start budget for the
           one-shot — it never serves; its Terminated event drives on
  -> sidecar process: app.main.run() sees --scheduled-run before any
     server startup and dispatches to app.execution.scheduled_run.main()
       1. reads persisted flags (app.execution.flags)
          stop_all or pause_all set -> report "blocked", exit 0, nothing runs
       2. lists due schedules (same selection as SchedulerTick.tick)
       3. runs ONE real SchedulerTick.tick() — catch_up_once: missed
          slots collapse into one project for the latest slot; the
          production DAG is enqueued in the job queue
       4. prints a JSON report on stdout, exits 0
  -> Supervisor: the one-shot's termination does NOT enter the
     crash/restart policy; the flags are cleared and the NORMAL server
     sidecar is spawned via the existing restart timer — still without
     revealing the window.  Its JobRunner executes the enqueued DAGs
     while the app sits in the tray.
```

Materialising + enqueueing is exactly what the Schedules UI "Run now"
does too (a single `SchedulerTick.tick()`); execution is the resident
JobRunner's job in both cases.

## Dry-run

```
youtube-agent.exe --scheduled-run --dry-run
```

(or `python -m app.execution.scheduled_run --scheduled-run --dry-run`
against the sidecar) lists every due schedule with its catch-up
arithmetic (`missed_slots`, `missed_collapsed`, `slot_at_ms`) and
changes nothing. `--dry-run` without `--scheduled-run` is a usage error
(exit 64).

## Verification status — honest gaps

* **`register_scheduler.ps1` has never been executed.** It was authored
  on a Linux build machine (no PowerShell available) and **statically
  reviewed line by line** against the ScheduledTasks module's documented
  cmdlets/parameters (action/trigger/settings/principal constructors,
  `Register-ScheduledTask -Force`, `Unregister-ScheduledTask
  -Confirm:$false`), including quoting and line-continuation checks.
  First real execution must happen on Windows (dev machine or
  `windows-latest` CI) before the installer trusts it.
* **The Rust shell changes are likewise uncompiled on this machine**
  (no Rust toolchain; shell build is deferred to Windows/CI per
  `docs/DEV_SETUP.md`). They are small, follow the supervisor's own
  existing patterns (argv parsing beside `--headless`, the restart
  timer for the hand-over), and were statically reviewed (structure /
  brace balance / field-construction completeness), but `cargo check`
  has never run on them. The Python side (`app.main` dispatch +
  `app.execution.scheduled_run`) is implemented and fully tested now.
* The Python scheduled-run path is covered by
  `backend/tests/test_scheduled_run.py` (16 tests: dispatch, dry-run,
  flags, mocked runner, real-tick catch-up end-to-end).
