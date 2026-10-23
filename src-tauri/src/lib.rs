//! YouTube Agent — Tauri 2 shell (Rust, thin supervisor only).
//!
//! Per the master roadmap (Parts 4, 6 and 11; §4.5 launch-token
//! security, §4.6 sidecar lifecycle, §4.7 versioning) this crate
//! deliberately contains almost no product logic. Its jobs are:
//!
//!   * own the native window (custom title bar, tray-resident lifecycle),
//!   * guarantee a single running instance (never two Python sidecars),
//!   * spawn/supervise the Python sidecar on an ephemeral loopback port
//!     and hand its connection details to the WebView (`sidecar.rs`),
//!   * run the Tauri updater (shell + UI channel; the Python backend has
//!     its own separate update channel, verified by the shell).
//!
//! All pipeline/product logic lives in the Python sidecar (FastAPI +
//! LangGraph), which the WebView talks to directly over loopback HTTP/WS
//! once it has fetched the connection details via `get_backend_connection`.
//!
//! Composition of this crate:
//!
//!   * `sidecar`  — the supervisor thread and the sidecar state machine;
//!   * `mini_http` — the tiny blocking HTTP client the supervisor uses
//!     for health probes / shutdown / execution flags;
//!   * `tray`     — tray icon + menu (open, pause/stop all, quit);
//!   * `features` — native conveniences exposed as commands (open output
//!     / logs folders, file pickers, autostart toggle);
//!   * `updater`  — update check/install commands + startup check.
//!
//! Backend status is pushed to the frontend as Tauri events:
//! `backend://status-changed` (a [`BackendStatus`]) on every transition
//! and `backend://connection-changed` (a [`BackendConnection`]) on every
//! (re)start that reaches READY — a restart always means a new port, a
//! new token and a new boot_id, and the WebView resyncs by snapshot.

use std::sync::RwLock;

use serde::Serialize;
use tauri::{AppHandle, Manager, State, WindowEvent};

mod sidecar;
mod mini_http;
mod tray;
mod features;
mod updater;

/// Connection details for this launch's Python sidecar.
///
/// Returned to the WebView by the `get_backend_connection` command. The
/// per-launch `token` is generated fresh on every (re)start by the
/// supervisor (roadmap §4.5: 32 CSPRNG bytes, base64url), is held only
/// in managed Rust state and in WebView memory, and is NEVER baked into
/// the JS bundle and NEVER persisted to `localStorage` — a stolen bundle
/// or a stored WebView snapshot therefore contains no usable credential.
/// The sidecar requires this token on its HTTP and WebSocket endpoints.
#[derive(Debug, Clone, Serialize)]
pub struct BackendConnection {
    /// Base URL for REST calls, e.g. `http://127.0.0.1:54321`.
    pub base_url: String,
    /// Base URL for the progress WebSocket, e.g. `ws://127.0.0.1:54321`.
    pub ws_url: String,
    /// Unguessable per-launch bearer token (base64url of 32 random bytes).
    pub token: String,
}

/// Lifecycle status of the supervised sidecar, pushed to the frontend
/// with every transition (`backend://status-changed`) and readable on
/// demand via `get_backend_status`.
///
/// `state` is one of: `stopped`, `spawning`, `starting`, `ready`,
/// `degraded`, `draining`, `stopping`, `crashed`, `failed`. `failed` is
/// a stable error state (fatal exit code, exhausted restart budget,
/// cold-start timeout): the UI shows its failure screen — whose Retry
/// button calls `retry_backend_start` — instead of a dashboard.
#[derive(Debug, Clone, Serialize)]
pub struct BackendStatus {
    /// Machine-readable lifecycle state (see above).
    pub state: String,
    /// Human/machine-readable detail for the state (e.g. the exact
    /// string `database_newer_than_binary` for exit code 4, which the
    /// UI routes to the updater). Never contains the launch token.
    pub detail: Option<String>,
    /// Exit code of the last sidecar termination, when known.
    pub exit_code: Option<i32>,
    /// Boot id of the current (or most recent) sidecar process.
    pub boot_id: Option<String>,
    /// How many automatic restarts the supervisor has performed.
    pub restart_count: u32,
}

impl BackendStatus {
    /// The status before the supervisor thread has spawned anything.
    pub(crate) fn stopped() -> Self {
        BackendStatus {
            state: "stopped".to_string(),
            detail: None,
            exit_code: None,
            boot_id: None,
            restart_count: 0,
        }
    }
}

/// Managed Tauri state for the shell.
///
/// The connection and status are supervisor-owned facts mirrored here
/// behind locks: the supervisor thread (see `sidecar.rs`) is their only
/// writer, and the WebView-facing commands below are pure readers.
/// Before the sidecar first reaches READY there is NO connection —
/// `connection` is `None` and `get_backend_connection` returns null,
/// rather than the placeholder record earlier skeleton rounds used.
pub struct AppState {
    /// The current backend connection, `None` until READY (and cleared
    /// again whenever the sidecar dies or is drained).
    pub connection: RwLock<Option<BackendConnection>>,
    /// The latest lifecycle status published by the supervisor.
    pub status: RwLock<BackendStatus>,
    /// Control channel to the supervisor thread (start / restart /
    /// shutdown / pause-all / stop-all requests).
    pub control: sidecar::SupervisorControl,
}

impl AppState {
    fn new() -> Self {
        AppState {
            connection: RwLock::new(None),
            status: RwLock::new(BackendStatus::stopped()),
            control: sidecar::SupervisorControl::new(),
        }
    }

    /// Snapshot of the currently published connection (if any).
    pub(crate) fn connection_snapshot(&self) -> Option<BackendConnection> {
        self.connection
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone()
    }

    /// Publish or clear the connection (supervisor thread only).
    pub(crate) fn set_connection(&self, connection: Option<BackendConnection>) {
        *self
            .connection
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) = connection;
    }

    /// Snapshot of the latest lifecycle status.
    pub(crate) fn status_snapshot(&self) -> BackendStatus {
        self.status
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone()
    }

    /// Publish a new lifecycle status (supervisor thread only).
    pub(crate) fn set_status(&self, status: BackendStatus) {
        *self
            .status
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) = status;
    }
}

/// Hand this launch's sidecar connection to the WebView.
///
/// This command is the ONLY way the frontend learns the base URLs and
/// the per-launch token; see [`BackendConnection`] for why the token is
/// never baked into the bundle or stored in `localStorage`. Returns
/// `null` until the sidecar first reaches READY (and again whenever it
/// is down) — the frontend must treat null as "backend not ready",
/// never as an address to call.
#[tauri::command]
fn get_backend_connection(state: State<'_, AppState>) -> Option<BackendConnection> {
    state.connection_snapshot()
}

/// The sidecar's current lifecycle status (the same payload the
/// `backend://status-changed` event pushes on every transition).
#[tauri::command]
fn get_backend_status(state: State<'_, AppState>) -> BackendStatus {
    state.status_snapshot()
}

/// Retry starting the backend — the failure screen's Retry button.
/// Asks the supervisor for a full restart: the automatic-restart budget
/// is reset and a fresh sidecar is spawned with a new port, a new token
/// and a new boot_id.
#[tauri::command]
fn retry_backend_start(state: State<'_, AppState>) {
    state.control.request_restart();
}

/// Pause all jobs: the runner stops claiming new jobs; in-flight jobs
/// finish. Executed against the backend's persisted `execution.pause_all`
/// flag — see `sidecar::control_pause_all` for the endpoint used.
#[tauri::command]
fn pause_all_jobs(app: AppHandle) -> Result<(), String> {
    sidecar::control_pause_all(&app)
}

/// Stop all jobs: in-flight jobs are cancelled. Executed against the
/// backend's persisted `execution.stop_all` flag — see
/// `sidecar::control_stop_all` for the endpoint used.
#[tauri::command]
fn stop_all_jobs(app: AppHandle) -> Result<(), String> {
    sidecar::control_stop_all(&app)
}

/// Run the Tauri application. Called from `src/main.rs`.
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default()
        // Single instance FIRST: a second launch must never spawn a
        // second sidecar (roadmap §06/§36). Instead it surfaces the
        // existing main window — un-minimise, show (it may be hidden in
        // the tray) and focus it.
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            // Round 6: a second launch carrying `--scheduled-run` is a
            // Task Scheduler wake against the already-running instance,
            // not a user asking for the window — never surface the UI
            // for it.  Due schedules are covered by the running
            // sidecar's own scheduler loop / next catch-up.
            if args.iter().any(|arg| arg == "--scheduled-run") {
                return;
            }
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        // Shell/UI auto-update channel (roadmap §36, channel A). The
        // endpoint + minisign pubkey in `tauri.conf.json > plugins.updater`
        // are placeholders until the release pipeline exists; the signing
        // private key itself lives ONLY in CI secrets, never in this repo.
        .plugin(tauri_plugin_updater::Builder::new().build())
        // `process:allow-restart` — lets the UI relaunch the app to apply
        // a downloaded update (and for user-triggered restarts).
        .plugin(tauri_plugin_process::init())
        // Shell plugin: the supervisor (sidecar.rs) spawns the Python
        // backend through it as the `agent-backend` externalBin. The
        // WebView gets NO shell access (see capabilities/default.json);
        // this plugin serves Rust-side supervision only.
        .plugin(tauri_plugin_shell::init())
        // Native file/folder pickers for the features::* commands.
        .plugin(tauri_plugin_dialog::init())
        // Start-on-login for the features::autostart_* commands. The
        // `--autostart` marker arg distinguishes a login launch (start
        // in the tray) from an interactive one.
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            Some(vec!["--autostart"]),
        ))
        // Tray residency (roadmap §06): closing the window hides it to
        // the tray instead of quitting, so renders/uploads keep running
        // in the sidecar. Quitting lives in the tray menu (and must stop
        // the sidecar gracefully) — it is intentionally NOT what the
        // window's close button does.
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .setup(|app| {
            // Managed state first: everything below (and every command)
            // reads the connection/status/control triple from here.
            app.manage(AppState::new());

            // Boot order matters: the sidecar supervisor starts FIRST
            // (it owns a multi-second cold start), then the tray (its
            // menu reads live backend status), then the updater (its
            // startup check must not delay the first spawn).
            sidecar::start_supervisor(app.handle());
            tray::init(app.handle());
            updater::init(app.handle());

            // The main window is declared in `tauri.conf.json` with
            // `visible: false` so it never flashes an empty frame. It is
            // revealed by the supervisor only once the sidecar first
            // reaches READY — or FAILED, because the failure screen must
            // be visible; until then the frontend's starting screen is
            // what the user gets. See sidecar.rs `set_status`.
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_backend_connection,
            get_backend_status,
            retry_backend_start,
            pause_all_jobs,
            stop_all_jobs,
            features::open_output_folder,
            features::open_logs_folder,
            features::pick_files,
            features::pick_folder,
            features::autostart_get,
            features::autostart_set,
            updater::check_for_updates,
            updater::install_update
        ]);

    let app = builder
        .build(tauri::generate_context!())
        .expect("error while building YouTube Agent");

    app.run(|app_handle, event| {
        // ExitRequested is the OS/app-level quit path (tray Quit goes
        // through tray.rs, which drains first — this hook is the
        // backstop for every other exit route, e.g. Windows logoff).
        // Window close never reaches here: it is intercepted above and
        // hides to the tray instead.
        if let tauri::RunEvent::ExitRequested { .. } = event {
            let _ = sidecar::control_shutdown(app_handle);
        }
    });
}
