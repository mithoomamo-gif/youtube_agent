; ============================================================================
; YouTube Agent — NSIS installer hooks (roadmap §11.2 / §11.7)
; ============================================================================
; Wiring: Tauri's NSIS bundler includes this file when tauri.conf.json sets
;   bundle > windows > nsis > installerHooks  to  "windows/hooks.nsh"
; (that config key is owned by the shell-config worker; this file only
; defines the four hook macros the Tauri template calls:
;   NSIS_HOOK_PREINSTALL / NSIS_HOOK_POSTINSTALL
;   NSIS_HOOK_PREUNINSTALL / NSIS_HOOK_POSTUNINSTALL
;
; ---------------------------------------------------------------------------
; USER-DATA GUARANTEE (§11.7) — read before editing anything below:
;
;   * The user's videos are NEVER deleted. The output folder
;     (%USERPROFILE%\Videos\YouTubeAgent, or wherever the user moved it)
;     is user content, not app data. There is NO code path in this file —
;     under NO option, prompt answer, or failure mode — that deletes,
;     moves, or even writes to the output folder. It is named here only
;     to state that guarantee; no instruction below references it.
;   * Shared runtimes are NEVER removed by uninstall: the WebView2
;     runtime and the VC++ Redistributable are shared system components
;     other software depends on, so they are left installed (§11.7).
;   * App data (%APPDATA%\YouTubeAgent — settings, channel connections,
;     the database, logs) is kept by default. It is deleted ONLY when
;     the user explicitly says so: either the POSTUNINSTALL prompt
;     answered Yes, or the in-app "Remove all my data on uninstall"
;     opt-in marker (see POSTUNINSTALL). Default answer is always NO.
;   * No hook fails the install silently. A dependency problem shows a
;     plain-language message and writes an install-log line; it never
;     aborts the install (§11.2: "No hook may fail the install
;     silently").
;
; Style choices (deliberate, to minimise include/compile risk — this
; file cannot be compiled on the Linux build VM, so it uses only the
; most conservative NSIS dialect):
;   * Plain StrCmp / Goto flow everywhere. LogicLib (${If} ...) is NOT
;     used, so no extra !include is required from the Tauri template.
;   * Only core NSIS instructions plus the stock nsExec plugin (it
;     ships with NSIS itself, not with Tauri) for capturing command
;     results. No nsProcess / FindProcess plugin: that DLL is not
;     bundled by Tauri, so process detection uses tasklist instead.
;   * Every path is quoted. Every external command's result is either
;     checked or explicitly ignored with a comment saying why.
;   * "IfSilent" (core instruction) guards every prompt: silent
;     installs/uninstalls (/S — smoke tests, Store/IT deployment, and
;     the Tauri updater's passive mode) never block on a dialog.
;
; Process names (§11.2 on-disk layout, %LOCALAPPDATA%\Programs\YouTubeAgent):
;   youtube-agent.exe  — the Tauri shell (tray app)
;   agent-backend.exe  — the Python sidecar (installed name of the
;                        externalBin binary; the staged build input is
;                        agent-backend-x86_64-pc-windows-msvc.exe, see
;                        src-tauri/binaries/README.md)
; ============================================================================


; ============================================================================
; NSIS_HOOK_PREINSTALL — upgrade hygiene (§11.2)
; ----------------------------------------------------------------------------
; If the previous version is still running (tray app and/or sidecar),
; ask before closing it, then close gracefully. Files are NEVER yanked
; from under a running render silently: a render can be 20 minutes of
; work, and replacing binaries mid-flight corrupts the upgrade.
;
; Running-process check: tasklist filtered by image name, piped to
; find; the pipeline's exit code (find's) is what nsExec reports back.
; Known limits of this pattern (accepted, and commented per §11.2):
;   * It matches the image NAME only — a renamed copy of the exe is
;     not detected, and an unrelated process with the exact same name
;     would be a false positive. Both names are distinctive enough
;     for this to be the pattern the Tauri/NSIS ecosystem commonly
;     uses, and the user confirms via the prompt before anything is
;     closed interactively.
;   * It sees the current session — right for a per-user install:
;     the app runs in the same session as the installer.
;   * If the check itself fails (nsExec returns "error"), we treat
;     the app as not running and continue, but say so in the log:
;     blocking an install on a broken probe would be worse, and the
;     file copy that follows still fails loudly in the (unlikely)
;     event a binary really is locked.
; In silent mode there is no prompt: the Tauri updater closes the app
; itself before launching the installer, and a silent caller has
; already decided — so we close the app directly and log it.
; ============================================================================
!macro NSIS_HOOK_PREINSTALL
  ; --- probe: is the shell running? -----------------------------------
  nsExec::ExecToStack 'cmd /C tasklist /FI "IMAGENAME eq youtube-agent.exe" /NH | find /I "youtube-agent.exe" > NUL'
  Pop $R0 ; exit code: "0" = found, "1" = not found, "error" = probe failed
  Pop $R1 ; captured output (unused — the exit code is the signal)
  StrCmp $R0 "error" yta_pre_probe_failed yta_pre_shell_checked
  yta_pre_probe_failed:
    DetailPrint "YouTube Agent: could not check whether the app is running (probe failed) — continuing with the install."
    Goto yta_pre_probe_done
  yta_pre_shell_checked:
    StrCmp $R0 "0" yta_pre_app_running yta_pre_probe_sidecar
  yta_pre_probe_sidecar:
    ; --- probe: is the sidecar running? -------------------------------
    nsExec::ExecToStack 'cmd /C tasklist /FI "IMAGENAME eq agent-backend.exe" /NH | find /I "agent-backend.exe" > NUL'
    Pop $R0
    Pop $R1
    StrCmp $R0 "0" yta_pre_app_running yta_pre_probe_done
    Goto yta_pre_probe_done

  yta_pre_app_running:
    IfSilent yta_pre_close_it
    MessageBox MB_YESNO|MB_ICONQUESTION "YouTube Agent is still running.$\n$\nIt needs to close before the update can be installed. Any video that is rendering right now will be stopped.$\n$\nClose YouTube Agent and continue?" IDYES yta_pre_close_it
    ; User said No: stop the install cleanly rather than pulling files
    ; from under a running app. Nothing has been changed yet, so
    ; aborting here is safe and honest.
    MessageBox MB_OK|MB_ICONINFORMATION "Setup was cancelled. YouTube Agent was left running and nothing was changed.$\n$\nYou can close YouTube Agent from its tray icon and run this installer again."
    Abort
  yta_pre_close_it:
    DetailPrint "YouTube Agent: closing the running app before installing..."
    ; Graceful first: taskkill WITHOUT /F asks the shell to close
    ; (WM_CLOSE), which lets the shell stop its sidecar cleanly.
    nsExec::ExecToLog 'taskkill /IM "youtube-agent.exe"'
    nsExec::ExecToLog 'taskkill /IM "agent-backend.exe"'
    Sleep 3000 ; give a render a moment to wind down and files to close
    ; Fallback only: force whatever is still alive. A console sidecar
    ; often cannot honour a graceful close request, which is exactly
    ; what this fallback exists for.
    nsExec::ExecToLog 'taskkill /F /IM "youtube-agent.exe"'
    nsExec::ExecToLog 'taskkill /F /IM "agent-backend.exe"'
    Sleep 1000
    ; Final check: if something STILL will not close, do not yank its
    ; files — tell the user plainly and stop (interactive), or log and
    ; stop (silent). Aborting leaves the old install untouched.
    nsExec::ExecToStack 'cmd /C tasklist /FI "IMAGENAME eq youtube-agent.exe" /NH | find /I "youtube-agent.exe" > NUL'
    Pop $R0
    Pop $R1
    StrCmp $R0 "0" yta_pre_still_running yta_pre_probe_done
    Goto yta_pre_probe_done
  yta_pre_still_running:
    IfSilent yta_pre_still_running_silent
    MessageBox MB_OK|MB_ICONEXCLAMATION "YouTube Agent could not be closed automatically.$\n$\nPlease close it from the tray icon (right-click > Exit), then run this installer again. Nothing was changed."
    Abort
  yta_pre_still_running_silent:
    DetailPrint "YouTube Agent: the running app could not be closed during a silent install — install stopped, nothing was changed."
    Abort
  yta_pre_probe_done:
!macroend


; ============================================================================
; NSIS_HOOK_POSTINSTALL — VC++ Redistributable remediation (§11.2)
; ----------------------------------------------------------------------------
; The Python sidecar and FFmpeg need the Visual C++ 2015-2022 x64
; runtime. If it is missing AND the release staged vc_redist.x64.exe
; into $INSTDIR\resources, run it passively (no forced reboot), then
; delete the staged copy so it does not sit in the install dir.
; If it is missing and nothing was staged, we say so in plain language
; and log it — per the roadmap, a dependency failure must NOT abort
; the install and must NOT pass silently either.
;
; Presence check: the runtime's own registry value
;   HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64
;     Installed = 1 (DWORD)
; plus the WOW6432Node fallback path (what a 32-bit view of the
; registry sees on 64-bit Windows), so the check works regardless of
; which registry view this installer process runs with.
; ============================================================================
!macro NSIS_HOOK_POSTINSTALL
  StrCpy $R2 "0" ; $R2 = 1 once the runtime is known to be present
  ReadRegDWORD $R0 HKLM "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" "Installed"
  StrCmp $R0 "1" yta_post_vc_present yta_post_vc_check_wow
  yta_post_vc_check_wow:
    ReadRegDWORD $R0 HKLM "SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" "Installed"
    StrCmp $R0 "1" yta_post_vc_present yta_post_vc_missing
  yta_post_vc_present:
    StrCpy $R2 "1"
    DetailPrint "YouTube Agent: Visual C++ runtime already installed — nothing to do."
    Goto yta_post_vc_done
  yta_post_vc_missing:
    DetailPrint "YouTube Agent: Visual C++ runtime not found — checking for the bundled installer..."
    IfFileExists "$INSTDIR\resources\vc_redist.x64.exe" yta_post_vc_staged yta_post_vc_not_staged
  yta_post_vc_staged:
    DetailPrint "YouTube Agent: installing the Visual C++ runtime (this can take a minute)..."
    ExecWait '"$INSTDIR\resources\vc_redist.x64.exe" /passive /norestart' $R1
    ; Exit codes: 0 = installed, 3010 = installed but a restart is
    ; recommended (still a success — we never force a reboot).
    ; Anything else is logged and reported, never fatal.
    StrCmp $R1 "0" yta_post_vc_ran_ok yta_post_vc_check_3010
    yta_post_vc_check_3010:
      StrCmp $R1 "3010" yta_post_vc_ran_ok yta_post_vc_ran_failed
    yta_post_vc_ran_ok:
      DetailPrint "YouTube Agent: Visual C++ runtime installed (exit code $R1)."
      Goto yta_post_vc_cleanup
    yta_post_vc_ran_failed:
      DetailPrint "YouTube Agent: WARNING — the Visual C++ runtime installer returned exit code $R1."
      ; Install-log line (file) as well as the on-screen detail log:
      FileOpen $R3 "$INSTDIR\install.log" a
      FileSeek $R3 0 END
      FileWrite $R3 "YouTube Agent setup: VC++ Redistributable install failed with exit code $R1. Some features may not work until the Microsoft Visual C++ 2015-2022 Redistributable (x64) is installed.$\r$\n"
      FileClose $R3
      ClearErrors
      IfSilent yta_post_vc_cleanup
      MessageBox MB_OK|MB_ICONEXCLAMATION "YouTube Agent was installed, but a part it needs — the Microsoft Visual C++ runtime — could not be installed (error $R1).$\n$\nSome features may not work until it is installed. You can get it free from Microsoft by searching for 'Visual C++ Redistributable 2015-2022 x64'."
      Goto yta_post_vc_cleanup
    yta_post_vc_cleanup:
      ; The staged installer is a one-time remedy, not payload:
      ; always remove it afterwards, success or failure.
      Delete "$INSTDIR\resources\vc_redist.x64.exe"
      Goto yta_post_vc_done
  yta_post_vc_not_staged:
    DetailPrint "YouTube Agent: WARNING — Visual C++ runtime missing and no bundled installer was staged."
    FileOpen $R3 "$INSTDIR\install.log" a
    FileSeek $R3 0 END
    FileWrite $R3 "YouTube Agent setup: VC++ Redistributable is missing and no vc_redist.x64.exe was bundled with this installer. Some features may not work until the Microsoft Visual C++ 2015-2022 Redistributable (x64) is installed.$\r$\n"
    FileClose $R3
    ClearErrors
    IfSilent yta_post_vc_done
    MessageBox MB_OK|MB_ICONEXCLAMATION "YouTube Agent was installed, but a part it needs — the Microsoft Visual C++ runtime — is not on this PC and was not included with this installer.$\n$\nSome features may not work until it is installed. You can get it free from Microsoft by searching for 'Visual C++ Redistributable 2015-2022 x64'."
  yta_post_vc_done:
!macroend


; ============================================================================
; NSIS_HOOK_PREUNINSTALL — teardown BEFORE any file is deleted (§11.7)
; ----------------------------------------------------------------------------
; Order matters: processes are stopped and every registration that
; points at our executables is removed first, while the paths still
; resolve. A scheduled task or autostart entry left behind would keep
; "launching" a deleted program after every reboot.
; Everything here is best-effort by design: a missing task or a
; process that is not running is the normal case, never an error
; worth stopping an uninstall for — so results are logged, not gated.
; ============================================================================
!macro NSIS_HOOK_PREUNINSTALL
  DetailPrint "YouTube Agent: stopping the app before removing it..."
  ; 1) Stop the shell and sidecar — graceful first, forced as fallback
  ;    (same reasoning as PREINSTALL: no orphaned sidecar may survive,
  ;    and the shell gets the first chance to shut it down cleanly).
  nsExec::ExecToLog 'taskkill /IM "youtube-agent.exe"'
  nsExec::ExecToLog 'taskkill /IM "agent-backend.exe"'
  Sleep 2000
  nsExec::ExecToLog 'taskkill /F /IM "youtube-agent.exe"'
  nsExec::ExecToLog 'taskkill /F /IM "agent-backend.exe"'
  Sleep 500

  ; 2) Remove the autostart registration (roadmap §11.9: the opt-in
  ;    HKCU Run entry named "YouTube Agent").
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "YouTube Agent"
  DetailPrint "YouTube Agent: autostart entry removed (if it was enabled)."

  ; 3) Remove the Task Scheduler entry. The task the app registers
  ;    is "YouTube Agent - Scheduled Run" in the ROOT task folder
  ;    (packaging/register_scheduler.ps1 registers it with no
  ;    -TaskPath; §11.9: it launches the installed app with
  ;    --scheduled-run). It is deleted by its exact name first...
  nsExec::ExecToLog 'schtasks /Delete /TN "YouTube Agent - Scheduled Run" /F'
  ;    ...then a best-effort PowerShell sweep removes the same task
  ;    by the same exact name, in case schtasks could not. PowerShell
  ;    ships with every supported Windows; if it is unavailable or
  ;    blocked, the sweep simply does nothing and the exact-name
  ;    delete above has already covered the registered task.
  nsExec::ExecToLog "powershell.exe -NoProfile -Command $\"Get-ScheduledTask -TaskName 'YouTube Agent - Scheduled Run' -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$$false -ErrorAction SilentlyContinue$\""
  DetailPrint "YouTube Agent: scheduled tasks removed."
!macroend


; ============================================================================
; NSIS_HOOK_POSTUNINSTALL — the data prompt (§11.7)
; ----------------------------------------------------------------------------
; Binaries, shortcuts and registrations are already gone at this
; point; what remains is the user's data in %APPDATA%\YouTubeAgent
; (settings, channel connections, database, logs). It is KEPT by
; default — a routine uninstall/repair must not nuke a channel setup.
;
; It is deleted only on an explicit Yes, which can arrive two ways:
;   1. The prompt below (interactive uninstalls only). It defaults to
;      NO (MB_DEFBUTTON2): pressing Enter, closing the dialog, or any
;      doubt at all keeps the data.
;   2. The in-app opt-in: Settings > "Remove all my data on
;      uninstall" writes %APPDATA%\YouTubeAgent\uninstall-purge.marker
;      (backend: app/uninstall_policy.py — the marker filename is a
;      contract, do not rename it here without the backend team).
;      That earlier, deliberate opt-in is honoured without asking
;      again — including in silent uninstalls, where the prompt can
;      never appear. No marker + silent = data kept, always.
;
; On a Yes, the Credential Manager secrets for this app are removed
; too (§11.7: API keys / OAuth refresh tokens never outlive an
; explicit full uninstall, and are never deleted silently either) —
; best-effort via cmdkey against the app's credential service name
; "YouTubeAgent" (backend: app/secrets_store.py). Secrets stored as
; DPAPI-protected files inside the data folder disappear with the
; folder itself.
;
; Reminder of the header guarantee: this hook — like every hook —
; never touches the output folder or the shared runtimes, on either
; answer.
; ============================================================================
!macro NSIS_HOOK_POSTUNINSTALL
  IfFileExists "$APPDATA\YouTubeAgent\uninstall-purge.marker" yta_postun_purge yta_postun_ask
  yta_postun_ask:
    IfSilent yta_postun_keep ; silent + no marker = keep, always
    MessageBox MB_YESNO|MB_DEFBUTTON2|MB_ICONQUESTION "Also delete settings, channel connections and history?$\n$\nIf you choose No, your YouTube Agent settings, connected channels and history are kept, so reinstalling picks up right where you left off.$\n$\nYour rendered videos are never deleted by uninstalling, whichever you choose." IDYES yta_postun_purge
    Goto yta_postun_keep
  yta_postun_purge:
    DetailPrint "YouTube Agent: removing settings, channel connections and history as requested..."
    nsExec::ExecToLog 'cmdkey /delete:"YouTubeAgent"'
    RMDir /r "$APPDATA\YouTubeAgent"
    Goto yta_postun_done
  yta_postun_keep:
    DetailPrint "YouTube Agent: your settings and history were kept in $APPDATA\YouTubeAgent — reinstalling will find them again."
  yta_postun_done:
!macroend

; End of hooks. (No LogicLib include is required by anything above.)
