//! System-tray residency for the YouTube Agent shell.
//!
//! Roadmap references:
//!   * §4.6 / Part 4 — tray residency and the shutdown sequence. Closing
//!     the window is NOT shutdown (lib.rs hides the window instead); the
//!     only user-facing exits are this menu's "Quit", headless
//!     completion, and Windows logoff/shutdown.
//!   * Part 6 / §06 — the tray mirrors backend state and is the user's
//!     always-available control surface (Open / Pause All / STOP ALL /
//!     Quit) while renders and uploads keep running in the sidecar.
//!
//! This module owns NO product logic and NO sidecar protocol details:
//! every backend action goes through the narrow control surface in
//! `crate::sidecar` (`control_pause_all`, `control_stop_all`,
//! `control_shutdown`, `status_snapshot`), which is the one place that
//! knows about the loopback port, the per-launch token and the HTTP
//! calls. The tray never re-implements HTTP or token handling.
//!
//! Fixed contract with lib.rs:
//!   * `init(app: &tauri::AppHandle)` — called once from `.setup()`.
//!   * `refresh_status(app: &tauri::AppHandle)` — callable any time
//!     (lib.rs / sidecar.rs call it after every backend state change) to
//!     re-mirror `BackendStatus` into the disabled menu item + tooltip.

use tauri::{
    menu::{IsMenuItem, Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager,
};

use crate::sidecar;

/// Id of the one and only tray icon. `refresh_status` looks the icon up
/// by this id via `AppHandle::tray_by_id`, so no tray handle ever needs
/// to be stored in managed state (the fixed `AppState` has no slot for
/// it — and none is needed).
pub const TRAY_ID: &str = "main-tray";

// Menu item ids. The status item's id is never matched in the menu-event
// handler: it is created DISABLED, so Windows can never deliver a click
// for it. The id exists so the item is identifiable in diagnostics.
const ID_STATUS: &str = "tray-status";
const ID_OPEN: &str = "tray-open";
const ID_PAUSE_ALL: &str = "tray-pause-all";
const ID_STOP_ALL: &str = "tray-stop-all";
const ID_QUIT: &str = "tray-quit";

/// Label of the main window as declared in `tauri.conf.json`.
const MAIN_WINDOW: &str = "main";

// ---------------------------------------------------------------------------
// Status mirroring
// ---------------------------------------------------------------------------

/// Human label for the disabled status menu item, mirroring the current
/// `BackendStatus.state` (roadmap §4.6 state machine:
/// STOPPED → SPAWNING → STARTING → READY ⇄ DEGRADED → DRAINING →
/// STOPPING, CRASHED → RESTARTING).
///
/// The snapshot is taken through `sidecar::status_snapshot` — THE read
/// path for shell code (the `get_backend_status` command serves the
/// same snapshot to the WebView) — so this module never touches the
/// `AppState` locks itself. Published states (lib.rs `BackendStatus`
/// doc) are all lowercase: `stopped`, `spawning`, `starting`, `ready`,
/// `degraded`, `draining`, `stopping`, `crashed`, `failed`.
///
/// Matching is case-insensitive regardless, and unknown states are
/// shown verbatim (never silently rendered as "Ready"): a state this
/// build does not know about is exactly the state the user should be
/// able to read off the tray.
fn status_label(app: &AppHandle) -> String {
    let state = sidecar::status_snapshot(app).state;
    match state.trim().to_ascii_lowercase().as_str() {
        "ready" => "Backend: Ready".to_string(),
        "starting" | "spawning" => "Backend: Starting\u{2026}".to_string(),
        "restarting" => "Backend: Restarting\u{2026}".to_string(),
        "degraded" => "Backend: Degraded".to_string(),
        // CRASHED (roadmap spelling) and any explicit failure land here:
        // the tray cannot fix it, so it points at the app, which owns the
        // failure screen (View logs / Retry / Diagnostics).
        "failed" | "crashed" | "error" => "Backend: Failed \u{2014} see app".to_string(),
        "draining" | "stopping" => "Backend: Stopping\u{2026}".to_string(),
        "stopped" => "Backend: Stopped".to_string(),
        "paused" => "Backend: Paused".to_string(),
        "" => "Backend: Unknown".to_string(),
        other => format!("Backend: {other}"),
    }
}

/// Tray tooltip: `"YouTube Agent — <status>"`, e.g.
/// `"YouTube Agent — Ready"`. Derived from the menu label so the tooltip
/// and the status item can never disagree.
fn tooltip_for(label: &str) -> String {
    let status = label.strip_prefix("Backend: ").unwrap_or(label);
    format!("YouTube Agent \u{2014} {status}")
}

/// Build a fresh tray menu mirroring `label`.
///
/// A fresh menu is built on every refresh instead of mutating the old
/// one in place: Tauri 2 menu items are created on the main thread
/// through muda and a `MenuItem` handle would have to be kept in extra
/// managed/static state to be mutated later, while `AppState` is fixed
/// by contract and has no slot for it. Rebuilding + `TrayIcon::set_menu`
/// is the documented Tauri 2 way to change a tray menu's contents.
///
/// Windows note: replacing a tray menu is fully supported on Windows
/// (the shipping target). Tauri documents that on *Linux* a set menu
/// cannot be replaced — irrelevant here, noted only so nobody ports
/// this pattern blindly.
fn build_menu(app: &AppHandle, label: &str) -> Result<Menu, tauri::Error> {
    // DISABLED status line: `enabled = false`. It is a readout, not a
    // button — Windows greys it out and it can never be clicked.
    let status_item = MenuItem::with_id(app, ID_STATUS, label, false, None::<&str>)?;
    let open_item = MenuItem::with_id(app, ID_OPEN, "Open YouTube Agent", true, None::<&str>)?;
    let pause_item = MenuItem::with_id(app, ID_PAUSE_ALL, "Pause All", true, None::<&str>)?;
    // STOP ALL is deliberately an ordinary, immediately reachable menu
    // item (roadmap Part 4: no modal between the user and stopping).
    // Its handler calls `sidecar::control_stop_all`, which signals the
    // supervisor to stop dispatch and cancel running work — this is a
    // real backend stop, never a UI-only "stopped" paint.
    let stop_item = MenuItem::with_id(app, ID_STOP_ALL, "STOP ALL", true, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, ID_QUIT, "Quit", true, None::<&str>)?;
    let separator_top = PredefinedMenuItem::separator(app)?;
    let separator_bottom = PredefinedMenuItem::separator(app)?;

    Menu::with_items(
        app,
        &[
            &status_item as &dyn IsMenuItem,
            &separator_top as &dyn IsMenuItem,
            &open_item as &dyn IsMenuItem,
            &pause_item as &dyn IsMenuItem,
            &stop_item as &dyn IsMenuItem,
            &separator_bottom as &dyn IsMenuItem,
            &quit_item as &dyn IsMenuItem,
        ],
    )
}

// ---------------------------------------------------------------------------
// Window helpers
// ---------------------------------------------------------------------------

/// Show the main window: un-minimise (it may be minimised), show (it may
/// be hidden in the tray — window close hides instead of quitting, see
/// lib.rs) and focus it. Best-effort by design: a tray click must never
/// panic the shell, so individual window errors are swallowed.
fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

/// Left-click behaviour: toggle the main window. Visible and not
/// minimised → hide to tray; anything else (hidden, minimised, or state
/// unreadable) → show via [`show_main_window`]. The sidecar is never
/// touched: hiding the window is pure presentation, work keeps running.
fn toggle_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let visible = window.is_visible().unwrap_or(false);
        let minimized = window.is_minimized().unwrap_or(false);
        if visible && !minimized {
            let _ = window.hide();
        } else {
            show_main_window(app);
        }
    }
}

// ---------------------------------------------------------------------------
// Public surface (fixed contract)
// ---------------------------------------------------------------------------

/// Re-mirror the current backend status into the tray: tooltip text and
/// the disabled status menu item are rebuilt from a fresh
/// `sidecar::status_snapshot`.
///
/// Called on init, after every tray control action, lazily when the
/// pointer enters the tray icon (see `init`), and by lib.rs/sidecar.rs
/// whenever the backend state changes. Safe to call when the tray does
/// not exist (yet / any more): it silently does nothing. All errors are
/// swallowed for the same reason — a cosmetic refresh must never take
/// the shell down, and the next state change calls it again.
pub fn refresh_status(app: &AppHandle) {
    let Some(tray) = app.tray_by_id(TRAY_ID) else {
        return;
    };
    let label = status_label(app);
    let _ = tray.set_tooltip(Some(tooltip_for(&label)));
    if let Ok(menu) = build_menu(app, &label) {
        let _ = tray.set_menu(Some(menu));
    }
}

/// Create the tray icon, its menu and its event handlers. Called once
/// from lib.rs `.setup()`.
///
/// Errors are reported on stderr and otherwise swallowed: `init` has a
/// unit return by contract, and a shell that failed to create a tray
/// icon must still come up (the window + sidecar are the product; the
/// tray is a control surface for them). The bundler already fails the
/// build long before this runs if the icon assets are missing.
pub fn init(app: &AppHandle) {
    let label = status_label(app);

    let menu = match build_menu(app, &label) {
        Ok(menu) => menu,
        Err(err) => {
            eprintln!("youtube-agent: failed to build tray menu: {err}");
            return;
        }
    };

    // Icon: the app's default window icon, i.e. the icon Tauri embeds
    // from `tauri.conf.json > bundle.icon` — the Tauri 2-idiomatic
    // source. Deliberately NOT `include_bytes!("../icons/icon.png")`:
    // the generated icon files are not committed yet (see
    // icons/README.txt), so embedding the file directly would break the
    // build until the artwork lands, while the default window icon
    // resolves through the same bundle configuration either way.
    let mut builder = TrayIconBuilder::with_id(TRAY_ID)
        .menu(&menu)
        .tooltip(tooltip_for(&label))
        // Left click is OUR toggle (see the tray-icon event handler
        // below), so the menu must not also pop up on left click. On
        // Windows the menu still opens on right click, which is the
        // platform convention.
        .show_menu_on_left_click(false);
    if let Some(icon) = app.default_window_icon().cloned() {
        builder = builder.icon(icon);
    } else {
        eprintln!(
            "youtube-agent: no default window icon available; tray icon will use the platform default"
        );
    }

    let builder = builder
        // Menu actions. This handler is registered globally for the
        // app (Tauri 2 semantics), so it matches strictly on OUR ids
        // and ignores everything else.
        .on_menu_event(|app, event| match event.id.as_ref() {
            ID_OPEN => {
                show_main_window(app);
            }
            ID_PAUSE_ALL => {
                // Real backend action via the sidecar control surface:
                // the supervisor stops dispatching new work. If the
                // backend is unreachable the error is logged, not
                // hidden — but the tray itself must not panic.
                if let Err(err) = sidecar::control_pause_all(app) {
                    eprintln!("youtube-agent: Pause All failed: {err}");
                }
                refresh_status(app);
            }
            ID_STOP_ALL => {
                // STOP ALL (roadmap Part 4): signals the sidecar
                // supervisor through `control_stop_all` — dispatch
                // stops, cancel flags are set, unacknowledging workers
                // are terminated after the grace period. This is never
                // a UI-only state change.
                if let Err(err) = sidecar::control_stop_all(app) {
                    eprintln!("youtube-agent: STOP ALL failed: {err}");
                }
                refresh_status(app);
            }
            ID_QUIT => {
                // Shutdown contract (roadmap §4.6, one sequence, three
                // initiators — this is initiator #1, tray Quit):
                //
                //   1. FIRST drain the sidecar through the control
                //      surface. `sidecar::control_shutdown` is
                //      SYNCHRONOUS by design: it delivers the shutdown
                //      request to the supervisor (which performs the
                //      drain: POST /api/v1/system/shutdown, stdin
                //      fallback; refuse new jobs, checkpoint, finish
                //      the current FFmpeg segment, persist any
                //      resumable upload session, WAL-checkpoint, exit
                //      0) and holds this call until the supervisor
                //      confirms the sidecar stopped, or until its
                //      deadline — the 10 s drain budget + margin —
                //      expires. It therefore cannot hang the quit
                //      forever, and by the time it returns the drain
                //      is either complete or out of budget.
                //   2. THEN exit the shell. Calling `app.exit(0)` only
                //      after step 1 returns is what makes the ordering
                //      safe: exiting first could kill the process
                //      before the shutdown request was ever delivered,
                //      skipping the drain entirely. Whatever is still
                //      alive at exit is covered by the guaranteed last
                //      resort — the whole worker tree lives in the
                //      shell's Windows Job Object, so process exit
                //      closes the job and no descendant can outlive
                //      the shell (the sidecar's parent watchdog is the
                //      second backstop).
                //
                // `Err` from step 1 means only "the request could not
                // even be delivered" (no managed state) — a drain that
                // merely times out is `Ok`, owned by the backstops
                // above. Either way the error is logged but does NOT
                // veto the quit: the user asked to leave.
                //
                // `app.exit(0)` will surface as `RunEvent::ExitRequested`
                // in lib.rs, whose hook calls `control_shutdown` again
                // as the backstop for other exit routes; that second
                // call is a fast `Ok` no-op because the status is
                // already `stopped` by then (documented in sidecar.rs).
                if let Err(err) = sidecar::control_shutdown(app) {
                    eprintln!(
                        "youtube-agent: graceful shutdown request failed ({err}); \
                         exiting anyway — Job Object / parent watchdog will reap the sidecar"
                    );
                }
                app.exit(0);
            }
            _ => {}
        })
        // Tray-icon (pointer) events.
        .on_tray_icon_event(|tray, event| match event {
            // Left-click UP (release) toggles the window. Acting on Up
            // — not Down — matches the Windows shell convention and the
            // official Tauri 2 tray example; a full click also yields a
            // Down event first, which we ignore so the window toggles
            // exactly once per click.
            TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } => {
                toggle_main_window(tray.app_handle());
            }
            // Lazy status refresh. Tauri 2 has NO "menu will open"
            // event for tray menus (menu events fire only on item
            // activation), so the closest supported hook is the
            // pointer ENTERING the icon: on Windows the pointer must
            // enter the icon area before any right-click can open the
            // menu, so refreshing here means the menu and tooltip the
            // user is about to see are rebuilt from a fresh snapshot.
            // It is a best-effort top-up layered on top of the
            // authoritative refreshes (init, every state change via
            // lib.rs/sidecar.rs, every control action) — never the
            // only path.
            TrayIconEvent::Enter { .. } => {
                refresh_status(tray.app_handle());
            }
            _ => {}
        });

    if let Err(err) = builder.build(app) {
        eprintln!("youtube-agent: failed to create tray icon: {err}");
        return;
    }

    // Mirror the status once more now that the icon actually exists
    // (the label may have moved on while the menu was being built).
    refresh_status(app);
}
