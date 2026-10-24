//! sidecar — the Rust shell's supervisor for the Python backend.
//!
//! This module implements the sidecar lifecycle of roadmap §4.6 exactly.
//! The shell is the supervisor; the Python sidecar (`agent-backend`, a
//! Tauri `externalBin`) is a managed child:
//!
//! ```text
//! STOPPED → SPAWNING → STARTING → READY ⇄ DEGRADED → DRAINING → STOPPING
//!     → STOPPED, with CRASHED → (backoff, ≤3 restarts / 5 min) → SPAWNING
//!     and unrecoverable exits → FAILED (stable error state, UI Retry).
//! ```
//!
//! Key invariants (roadmap §4.5/§4.6):
//!
//! * a fresh 32-byte CSPRNG launch token (base64url, 43 chars) and a
//!   fresh `boot_id` are minted for EVERY (re)start — the old token dies
//!   with the old process, there is no renewal path;
//! * the token reaches the sidecar via the `YTA_TOKEN` ENVIRONMENT
//!   variable only — never argv (visible in process listings), never a
//!   file, never a URL. The PID file written for the orphan sweep
//!   contains pid/boot_id/exe/start-time and NO token, ever;
//! * exactly one supervisor thread owns the child: it alone spawns,
//!   probes, drains and kills it. Everything else (Tauri commands, the
//!   tray) talks to it through [`SupervisorControl`], an mpsc channel —
//!   or, for pause/stop, through the backend's own HTTP surface via
//!   `mini_http` (see [`control_pause_all`] / [`control_stop_all`]);
//! * on Windows the whole worker tree (sidecar + its FFmpeg children)
//!   sits in a Job Object owned by this process with
//!   `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`: closing the handle — which
//!   the OS does when this process exits by ANY path, including a shell
//!   crash — terminates every process in the job. No orphaned sidecar
//!   or FFmpeg can survive the shell. (See [`JobObject`].)
//!
//! Exit codes drive the restart logic (§4.6): 0 clean · 2 fatal config
//! (no restart) · 3 port-bind failure (respawn, fresh port, counts in
//! the restart budget) · 4 DB newer than binary (no restart; status
//! detail `database_newer_than_binary`, the UI routes to the updater) ·
//! 5 bad launch env (no restart) · anything else = crash → backoff
//! 1 s → 2 s → 5 s, max 3 automatic restarts in any 5-minute window,
//! then FAILED rather than a silent spin loop.

use std::collections::HashMap;
use std::fs;
use std::io::Write;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use rand::RngCore;
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

use crate::mini_http;
use crate::{AppState, BackendConnection, BackendStatus};

// ---------------------------------------------------------------------
// Tunables (roadmap §4.6 values, named once here).
// ---------------------------------------------------------------------

/// Name of the Tauri `externalBin` sidecar binary.
const SIDECAR_NAME: &str = "agent-backend";
/// Cold-start budget: spawn → READY must complete within 30 s.
const COLD_START_BUDGET: Duration = Duration::from_secs(30);
/// Steady-state `/health/ready` probe interval.
const STEADY_PROBE_INTERVAL: Duration = Duration::from_secs(10);
/// Graceful-shutdown budget before the Job Object / kill fallback.
const SHUTDOWN_BUDGET: Duration = Duration::from_secs(10);
/// Automatic-restart backoff ladder: 1 s → 2 s → 5 s.
const RESTART_BACKOFFS: [Duration; 3] = [
    Duration::from_secs(1),
    Duration::from_secs(2),
    Duration::from_secs(5),
];
/// Max automatic restarts inside [`RESTART_WINDOW`].
const RESTART_MAX_IN_WINDOW: usize = 3;
/// Sliding window for the automatic-restart budget.
const RESTART_WINDOW: Duration = Duration::from_secs(5 * 60);
/// Rotating sidecar log: 10 MB × 5 files under `<data dir>/logs`.
const LOG_MAX_BYTES: u64 = 10 * 1024 * 1024;
const LOG_KEEP: u32 = 5;

// ---------------------------------------------------------------------
// Supervisor control channel
// ---------------------------------------------------------------------

/// Messages understood by the supervisor thread. The supervisor owns the
/// child process; these messages are the ONLY way to ask it to act.
pub enum SupervisorMessage {
    /// Start the sidecar if it is not running (also used for the first
    /// spawn — `start_supervisor` sends it implicitly by booting the
    /// thread, which spawns before entering its loop).
    Start,
    /// Restart the sidecar: drain the current child if any, reset the
    /// automatic-restart budget (this is a human/UI action), spawn fresh
    /// (new port, new token, new boot_id). Wired to the UI failure
    /// screen's Retry button and the `retry_backend_start` command.
    Restart,
    /// Graceful shutdown (see `Supervisor::graceful_stop`). `ack` fires
    /// when the child is gone (or the budget elapsed and the force-kill
    /// path ran), so a synchronous caller — tray Quit, the
    /// `RunEvent::ExitRequested` hook — can hold the app's exit until
    /// the drain has actually finished.
    Shutdown { ack: Sender<()> },
    /// Ask the backend to pause claiming new jobs (persisted
    /// `execution.pause_all` flag). The HTTP call itself is made by
    /// [`apply_execution_flag`]; this message exists for channel-only
    /// callers such as the tray.
    PauseAll,
    /// Ask the backend to cancel in-flight jobs (persisted
    /// `execution.stop_all` flag). See [`apply_execution_flag`].
    StopAll,
}

/// Clone-free handle to the supervisor thread, stored in
/// [`crate::AppState`].
///
/// The channel is created with the control (not by the thread) so
/// commands issued during startup queue safely until the thread takes
/// the receiver in [`start_supervisor`]. One receiver exists, ever:
/// `take_receiver` yields it exactly once.
pub struct SupervisorControl {
    sender: Sender<SupervisorMessage>,
    receiver: Mutex<Option<Receiver<SupervisorMessage>>>,
}

impl SupervisorControl {
    pub fn new() -> Self {
        let (sender, receiver) = channel();
        SupervisorControl {
            sender,
            receiver: Mutex::new(Some(receiver)),
        }
    }

    /// Take the receiving end for the supervisor thread (first call only).
    fn take_receiver(&self) -> Option<Receiver<SupervisorMessage>> {
        self.receiver
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .take()
    }

    fn send(&self, message: SupervisorMessage) {
        // A dead supervisor thread is not worth panicking a command or
        // the tray over: the send simply goes nowhere.
        let _ = self.sender.send(message);
    }

    /// Ask the supervisor to (re)start the sidecar from a failure screen.
    pub fn request_restart(&self) {
        self.send(SupervisorMessage::Restart);
    }

    /// Ask the supervisor to start the sidecar if it is down.
    pub fn request_start(&self) {
        self.send(SupervisorMessage::Start);
    }

    /// Queue a shutdown and hand back the ack receiver (see
    /// [`SupervisorMessage::Shutdown`]).
    fn request_shutdown(&self) -> Receiver<()> {
        let (ack_tx, ack_rx) = channel();
        self.send(SupervisorMessage::Shutdown { ack: ack_tx });
        ack_rx
    }

    /// Queue a pause-all for channel-only callers.
    pub fn request_pause_all(&self) {
        self.send(SupervisorMessage::PauseAll);
    }

    /// Queue a stop-all for channel-only callers.
    pub fn request_stop_all(&self) {
        self.send(SupervisorMessage::StopAll);
    }
}

impl Default for SupervisorControl {
    fn default() -> Self {
        Self::new()
    }
}

// ---------------------------------------------------------------------
// Entry points called from lib.rs (Builder setup / run callback) and
// from the tray module.
// ---------------------------------------------------------------------

/// Boot the supervisor thread. Called FIRST in the Builder `setup()`
/// hook, after `AppState` has been managed: the thread performs the
/// orphan sweep, the first spawn and the readiness sequence, and from
/// then on owns the sidecar for the life of the process.
pub fn start_supervisor(app: &AppHandle) {
    let Some(receiver) = app
        .try_state::<AppState>()
        .and_then(|state| state.control.take_receiver())
    else {
        // Either AppState was not managed yet (a setup-order bug) or the
        // supervisor is already running. Never spawn a second one:
        // exactly one thread may own the sidecar.
        eprintln!("[shell] start_supervisor: no control receiver (already running?)");
        return;
    };
    let handle = app.clone();
    let spawn_result = std::thread::Builder::new()
        .name("sidecar-supervisor".to_string())
        .spawn(move || {
            let supervisor = Supervisor::new(handle, receiver);
            supervisor.run();
        });
    if let Err(e) = spawn_result {
        eprintln!("[shell] failed to spawn supervisor thread: {e}");
    }
}

/// The current backend status — THE read path for shell code (the
/// tray's status item uses it, the `get_backend_status` command serves
/// the same snapshot). Falls back to a plain `stopped` status when no
/// `AppState` is managed (which would be a setup-order bug, not a
/// runtime state worth inventing detail for).
pub fn status_snapshot(app: &AppHandle) -> BackendStatus {
    app.try_state::<AppState>()
        .map(|state| state.status_snapshot())
        .unwrap_or_else(BackendStatus::stopped)
}

/// Synchronous graceful shutdown for the tray's Quit item and for
/// `RunEvent::ExitRequested` in `lib.rs`.
///
/// "Synchronous" matters: the caller is about to exit the process, so
/// this holds it until the supervisor confirms the sidecar drained (its
/// 10 s budget, plus margin) — or until the status flips to `stopped`
/// some other way. If the supervisor is unreachable the wait simply
/// times out; even then the sidecar cannot outlive us on Windows,
/// because process exit closes the Job Object handle (see module docs).
/// `Err` is reserved for "the request could not even be delivered"
/// (no managed state); a drain that merely times out is still `Ok` —
/// the Job Object / parent-watchdog backstops own that case, and a
/// caller quitting the app must not be vetoed by it.
pub fn control_shutdown(app: &AppHandle) -> Result<(), String> {
    let Some(state) = app.try_state::<AppState>() else {
        return Err("backend state unavailable: AppState is not managed".to_string());
    };
    if state.status_snapshot().state == "stopped" {
        return Ok(()); // Already down (e.g. tray Quit already ran this).
    }
    let ack = state.control.request_shutdown();
    let deadline = Instant::now() + SHUTDOWN_BUDGET + Duration::from_secs(2);
    loop {
        match ack.recv_timeout(Duration::from_millis(100)) {
            Ok(()) => return Ok(()),
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => return Ok(()),
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                if state.status_snapshot().state == "stopped" || Instant::now() >= deadline {
                    return Ok(());
                }
            }
        }
    }
}

/// Pause all jobs (tray + `pause_all_jobs` command): the backend's real
/// switch is the persisted `execution.pause_all` flag — see
/// [`apply_execution_flag`] for the endpoint used and why.
pub fn control_pause_all(app: &AppHandle) -> Result<(), String> {
    apply_execution_flag(app, "pause_all")
}

/// Stop all jobs (tray + `stop_all_jobs` command): persisted
/// `execution.stop_all` flag — cancels in-flight jobs. See
/// [`apply_execution_flag`].
pub fn control_stop_all(app: &AppHandle) -> Result<(), String> {
    apply_execution_flag(app, "stop_all")
}

/// Set one persisted execution flag on the running backend.
///
/// GAP FOUND (backend, 2026-10-23): there is NO dedicated REST endpoint
/// for pause-all / stop-all. `backend/app/execution/flags.py` defines the
/// flags (`execution.pause_all` / `execution.stop_all`, JSON booleans in
/// the `settings` table; the JobRunner, the scheduler claimer and the
/// Style Lab claimer all re-read them) and the WebSocket
/// `client.command {pause_all}` door exists on paper, but
/// `app.state.ws_command_handler` is never wired in production, so WS
/// commands answer `command_unavailable`.
///
/// The one REAL, working HTTP surface is the generic settings endpoint:
/// `PUT /api/v1/settings` with body `{"values": {...}}`
/// (`backend/app/routers/settings.py::put_settings`, backed by the same
/// `settings` table the flag reader reads). We write BOTH key spellings
/// — canonical `execution.<flag>` (what `flags.set_flag` writes and what
/// `read_flags` prefers) and the bare `<flag>` (which `set_flag` also
/// writes "so any reader observes the same value") — with value `true`.
/// The runner re-reads the persisted flags every poll, so no separate
/// "apply" call is needed. Returns Err when the backend is not READY
/// (no published connection) or the backend rejects the write.
fn apply_execution_flag(app: &AppHandle, flag: &str) -> Result<(), String> {
    let connection = app
        .try_state::<AppState>()
        .and_then(|state| state.connection_snapshot())
        .ok_or_else(|| "backend is not ready: no connection to send the flag to".to_string())?;
    let url = format!("{}/api/v1/settings", connection.base_url);
    let body = serde_json::json!({
        "values": {
            format!("execution.{flag}"): true,
            flag: true,
        }
    })
    .to_string();
    let (status, response_body) = mini_http::put(
        &url,
        Some(&connection.token),
        "application/json",
        body.as_bytes(),
    )?;
    if (200..300).contains(&status) {
        Ok(())
    } else {
        Err(format!(
            "backend rejected {flag} (HTTP {status}): {response_body}"
        ))
    }
}

// ---------------------------------------------------------------------
// Launch secrets: token + boot_id (roadmap §4.5/§4.6)
// ---------------------------------------------------------------------

/// Mint the per-launch sidecar token: 32 bytes (256 bits) from the OS
/// CSPRNG (`rand::thread_rng`), base64url-encoded without padding →
/// exactly 43 characters. Called for EVERY (re)start; the token is
/// passed to the child via env only and dies with that child.
fn generate_launch_token() -> String {
    let mut bytes = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut bytes);
    base64url_encode(&bytes)
}

/// Mint a fresh boot_id: 128 random bits, hex, hyphenated in the UUID
/// layout (8-4-4-4-12). It identifies this sidecar PROCESS in logs, the
/// listening handshake, WS resync and the PID file — it is an identity,
/// not a credential, and carries no version/variant semantics.
fn generate_boot_id() -> String {
    let mut bytes = [0u8; 16];
    rand::thread_rng().fill_bytes(&mut bytes);
    let hex = hex_encode(&bytes);
    format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    )
}

fn hex_encode(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Hand-rolled base64url encoder (RFC 4648 §5, no padding) — the one
/// encoder the shell needs, not worth a crate. 32 input bytes produce
/// 10 full quartets (30 bytes) plus a 2-byte remainder → 40 + 3 = 43
/// characters.
fn base64url_encode(data: &[u8]) -> String {
    const ALPHABET: &[u8; 64] =
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity((data.len() + 2) / 3 * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[(n >> 18) as usize & 63] as char);
        out.push(ALPHABET[(n >> 12) as usize & 63] as char);
        if chunk.len() > 1 {
            out.push(ALPHABET[(n >> 6) as usize & 63] as char);
        }
        if chunk.len() > 2 {
            out.push(ALPHABET[n as usize & 63] as char);
        }
    }
    out
}

// ---------------------------------------------------------------------
// Data directory (must agree with the backend's canonical resolver)
// ---------------------------------------------------------------------

/// Resolve `<data dir>` exactly the way `backend/app/data_dir.py`
/// (`resolve_data_dir`) does, so the shell and the sidecar always agree
/// on where the DB, logs and PID file live:
///
/// 1. `YTA_DATA_DIR` env override, when set (the packaged runtime and
///    tests use it);
/// 2. `%APPDATA%\YouTubeAgent` — on Windows the shell computes it from
///    the std env `APPDATA` variable; the backend honours a present
///    `APPDATA` on every platform, and so do we;
/// 3. Windows fallback without APPDATA: `%USERPROFILE%\AppData\Roaming\YouTubeAgent`;
/// 4. dev default everywhere else: `~/.youtube-agent` (Rounds 1–5).
///
/// The resolved value is what we pass to the child as `YTA_DATA_DIR`.
pub fn data_dir() -> PathBuf {
    if let Ok(value) = std::env::var("YTA_DATA_DIR") {
        if !value.trim().is_empty() {
            return PathBuf::from(value);
        }
    }
    if let Ok(appdata) = std::env::var("APPDATA") {
        if !appdata.trim().is_empty() {
            return PathBuf::from(appdata).join("YouTubeAgent");
        }
    }
    if cfg!(windows) {
        if let Ok(profile) = std::env::var("USERPROFILE") {
            if !profile.trim().is_empty() {
                return PathBuf::from(profile)
                    .join("AppData")
                    .join("Roaming")
                    .join("YouTubeAgent");
            }
        }
    }
    if let Ok(home) = std::env::var("HOME") {
        if !home.trim().is_empty() {
            return PathBuf::from(home).join(".youtube-agent");
        }
    }
    PathBuf::from(".youtube-agent")
}

// ---------------------------------------------------------------------
// R6-013: the staged `--onedir` tree copy in the bundle resources
// ---------------------------------------------------------------------

/// The executable copy inside the staged PyInstaller `--onedir` tree,
/// when that copy is the one that can actually run.
///
/// `externalBin` ships the sidecar as a LONE exe next to the shell,
/// but a `--onedir` exe dies instantly (exit 255, "Failed to load
/// Python shared library") unless its `_internal/` sibling tree sits
/// beside the exe being run. `tauri.conf.json` therefore also maps
/// the staged tree (`binaries/agent-backend/`, staged by
/// `packaging/build_sidecar.*`) into the bundle resources as
/// `agent-backend/`, and the resolver in `app.path().resource_dir()`
/// points at that install location (on Windows the resource dir IS
/// the shell exe's directory).
///
/// Returns `Some(path)` only when BOTH hold:
///   * the `externalBin` exe's own directory has NO `_internal/`
///     (if it does, that layout is already self-contained and the
///     normal `sidecar()` resolution stays in charge), and
///   * a staged exe copy exists under `<resource dir>/agent-backend/`
///     WITH its `_internal/` beside it. The staged tree's exe keeps
///     its PyInstaller name (`yta_sidecar[.exe]`, per the freeze
///     spec); an `agent-backend[.exe]` copy is accepted first in case
///     staging ever renames it.
/// Every other case returns `None` and spawn falls back to the
/// existing `externalBin` path unchanged (dev runs included).
fn resource_sidecar_exe(app: &AppHandle) -> Option<PathBuf> {
    if let Ok(current) = std::env::current_exe() {
        if let Some(dir) = current.parent() {
            if dir.join("_internal").is_dir() {
                return None;
            }
        }
    }
    let resource_dir = app.path().resource_dir().ok()?;
    let tree = resource_dir.join("agent-backend");
    let exe_names: [&str; 2] = if cfg!(windows) {
        ["agent-backend.exe", "yta_sidecar.exe"]
    } else {
        ["agent-backend", "yta_sidecar"]
    };
    for name in exe_names {
        let candidate = tree.join(name);
        if candidate.is_file() && tree.join("_internal").is_dir() {
            return Some(candidate);
        }
    }
    None
}

// ---------------------------------------------------------------------
// Rotating sidecar log (roadmap §4.6: stdout/stderr → 10 MB × 5)
// ---------------------------------------------------------------------

/// Append-only log with simple size-based rotation: `sidecar.log` plus
/// archives `sidecar.log.1` (newest) … `sidecar.log.5` (oldest). The
/// supervisor tees every sidecar stdout/stderr line here, prefixed, and
/// adds its own `[shell]` lifecycle lines — one file answers "what did
/// the backend say before it died?" for the UI's View Logs action.
struct RotatingLog {
    path: PathBuf,
    size: u64,
}

impl RotatingLog {
    fn new(log_dir: &Path) -> Self {
        let _ = fs::create_dir_all(log_dir);
        let path = log_dir.join("sidecar.log");
        let size = fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
        RotatingLog { path, size }
    }

    fn write_line(&mut self, line: &str) {
        let needed = line.len() as u64 + 1;
        if self.size > 0 && self.size + needed > LOG_MAX_BYTES {
            self.rotate();
        }
        if let Ok(mut file) = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
        {
            if writeln!(file, "{line}").is_ok() {
                self.size += needed;
            }
        }
    }

    fn rotate(&mut self) {
        let oldest = self.archive_path(LOG_KEEP);
        let _ = fs::remove_file(&oldest);
        for i in (1..LOG_KEEP).rev() {
            let from = self.archive_path(i);
            let to = self.archive_path(i + 1);
            let _ = fs::rename(&from, &to);
        }
        let _ = fs::rename(&self.path, self.archive_path(1));
        self.size = 0;
    }

    fn archive_path(&self, index: u32) -> PathBuf {
        let mut name = self.path.as_os_str().to_owned();
        name.push(format!(".{index}"));
        PathBuf::from(name)
    }
}

// ---------------------------------------------------------------------
// PID file + orphan sweep (roadmap §4.6 spawn step 2)
// ---------------------------------------------------------------------

/// Contents of `<data dir>/backend.pid`. Written by US after every
/// successful spawn; read by the NEXT launch's orphan sweep. It holds
/// no token and no trusted port — deliberately nothing an attacker (or
/// a stale file) could use to reach the backend.
#[derive(Debug)]
struct PidRecord {
    pid: u32,
    boot_id: String,
    exe_path: String,
    /// Process start time, unix seconds (Windows FILETIME-derived; on
    /// Unix it is the `/proc/<pid>` start-time value instead — the field
    /// is only ever compared against a record written on the same
    /// platform/boot, so the units only need to agree with themselves).
    started_unix: u64,
}

fn pid_file_path(data_dir: &Path) -> PathBuf {
    data_dir.join("backend.pid")
}

/// Write our own child's record after spawn. Best-effort: a failure
/// here must never fail the spawn (the sweep is a safety net, not a
/// gate).
fn write_pid_file(data_dir: &Path, pid: u32, boot_id: &str) {
    let exe_path = process_exe_path(pid).unwrap_or_else(|| {
        // Fallback to the path the externalBin is expected at (next to
        // the shell executable); only used if the OS query failed.
        let exe_name = if cfg!(windows) {
            format!("{SIDECAR_NAME}.exe")
        } else {
            SIDECAR_NAME.to_string()
        };
        std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|dir| dir.join(&exe_name)))
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or(exe_name)
    });
    let started_unix = process_start_unix(pid).unwrap_or(0);
    let record = serde_json::json!({
        "pid": pid,
        "boot_id": boot_id,
        "exe_path": exe_path,
        "started_unix": started_unix,
    });
    let _ = fs::write(pid_file_path(data_dir), record.to_string());
}

/// Remove the PID file when its record belongs to `boot_id` (clean
/// exit paths); a record from another boot is left for the sweep.
fn remove_pid_file_if_ours(data_dir: &Path, boot_id: &str) {
    if let Ok(text) = fs::read_to_string(pid_file_path(data_dir)) {
        if let Some(record) = parse_pid_record(&text) {
            if record.boot_id == boot_id {
                let _ = fs::remove_file(pid_file_path(data_dir));
            }
        }
    }
}

/// Parse a PID record. Primary format is the JSON object we write; a
/// line format (`pid=…` / `pid: …`, one field per line) is accepted too,
/// per roadmap §4.6's "PID + boot_id + exe path + process start time".
fn parse_pid_record(text: &str) -> Option<PidRecord> {
    if let Ok(value) = serde_json::from_str::<serde_json::Value>(text) {
        let pid = value.get("pid")?.as_u64()?;
        return Some(PidRecord {
            pid: u32::try_from(pid).ok()?,
            boot_id: value
                .get("boot_id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            exe_path: value
                .get("exe_path")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            started_unix: value
                .get("started_unix")
                .and_then(|v| v.as_u64())
                .unwrap_or(0),
        });
    }
    // Line-format fallback.
    let mut record = PidRecord {
        pid: 0,
        boot_id: String::new(),
        exe_path: String::new(),
        started_unix: 0,
    };
    let mut seen_pid = false;
    for line in text.lines() {
        let Some((key, value)) = line.split_once(['=', ':']) else {
            continue;
        };
        let value = value.trim();
        match key.trim() {
            "pid" => {
                if let Ok(parsed) = value.parse::<u32>() {
                    record.pid = parsed;
                    seen_pid = true;
                }
            }
            "boot_id" => record.boot_id = value.to_string(),
            "exe_path" => record.exe_path = value.to_string(),
            "started_unix" => record.started_unix = value.parse().unwrap_or(0),
            _ => {}
        }
    }
    seen_pid.then_some(record)
}

/// Terminate a leftover sidecar from a previous launch — PID-reuse safe.
///
/// The leftover is killed ONLY when BOTH of these match the record in
/// the PID file:
///
/// * the exe path of the process currently holding that PID, and
/// * that process's start time.
///
/// A reused PID belongs to a different process (different exe, or same
/// exe image restarted later — different start time), so it is never
/// touched. As a third guard the recorded exe must actually be our
/// sidecar binary. Whatever happens, our stale record is then removed
/// so the next spawn writes a fresh one.
fn orphan_sweep(data_dir: &Path, log: &mut RotatingLog) {
    let path = pid_file_path(data_dir);
    let Ok(text) = fs::read_to_string(&path) else {
        return; // No record: the normal first-launch case.
    };
    let Some(record) = parse_pid_record(&text) else {
        log.write_line("[shell] orphan sweep: unparsable backend.pid, discarding it");
        let _ = fs::remove_file(&path);
        return;
    };
    if record.pid == 0 || record.pid == std::process::id() {
        let _ = fs::remove_file(&path);
        return;
    }
    let recorded_name = Path::new(&record.exe_path)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let looks_like_sidecar = recorded_name.starts_with(SIDECAR_NAME);
    let matches = looks_like_sidecar
        && record.started_unix != 0
        && process_exe_path(record.pid)
            .map(|live| exe_paths_equal(&live, &record.exe_path))
            .unwrap_or(false)
        && process_start_unix(record.pid) == Some(record.started_unix);
    if matches {
        if terminate_process(record.pid) {
            log.write_line(&format!(
                "[shell] orphan sweep: terminated leftover sidecar pid {} (boot {})",
                record.pid, record.boot_id
            ));
        } else {
            log.write_line(&format!(
                "[shell] orphan sweep: leftover pid {} matched but termination failed",
                record.pid
            ));
        }
    } else {
        log.write_line(&format!(
            "[shell] orphan sweep: stale backend.pid for pid {} (no live match), discarding",
            record.pid
        ));
    }
    let _ = fs::remove_file(&path);
}

/// Exe-path comparison: Windows paths are case-insensitive and both
/// separators occur in the wild; everywhere else the path is exact.
fn exe_paths_equal(a: &str, b: &str) -> bool {
    let normalize = |p: &str| {
        let replaced = p.replace('/', "\\");
        if cfg!(windows) {
            replaced.to_lowercase()
        } else {
            replaced
        }
    };
    normalize(a) == normalize(b)
}

// ---------------------------------------------------------------------
// Per-platform process queries (orphan sweep + PID record).
//
// Windows is the shipping platform and uses the real Win32 queries
// (windows-sys). Unix is best-effort via /proc (dev only). Anything
// else gets inert stubs: the sweep then simply never terminates, which
// is the safe failure direction.
// ---------------------------------------------------------------------

/// Full image path of process `pid`, if the OS will tell us.
#[cfg(windows)]
fn process_exe_path(pid: u32) -> Option<String> {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::Threading::{
        OpenProcess, QueryFullProcessImageNameW, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return None;
        }
        let mut buffer = vec![0u16; 1024];
        let mut len = buffer.len() as u32;
        // PROCESS_NAME_WIN32 == 0.
        let ok = QueryFullProcessImageNameW(
            handle,
            0,
            buffer.as_mut_ptr(),
            &mut len,
        );
        let _ = CloseHandle(handle);
        if ok == 0 {
            return None;
        }
        Some(String::from_utf16_lossy(
            &buffer[..(len as usize).min(buffer.len())],
        ))
    }
}

#[cfg(unix)]
fn process_exe_path(pid: u32) -> Option<String> {
    let target = fs::read_link(format!("/proc/{pid}/exe")).ok()?;
    let text = target.to_string_lossy().into_owned();
    // A deleted-but-running binary shows up with a " (deleted)" suffix.
    Some(text.strip_suffix(" (deleted)").unwrap_or(&text).to_string())
}

#[cfg(not(any(windows, unix)))]
fn process_exe_path(_pid: u32) -> Option<String> {
    None
}

/// Start time of process `pid` (see [`PidRecord::started_unix`] for the
/// platform units note). `None` when the process is gone or unreadable.
#[cfg(windows)]
fn process_start_unix(pid: u32) -> Option<u64> {
    use windows_sys::Win32::Foundation::{CloseHandle, FILETIME};
    use windows_sys::Win32::System::Threading::{
        GetProcessTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    /// 100-ns FILETIME ticks between 1601-01-01 and 1970-01-01.
    const FILETIME_UNIX_EPOCH_OFFSET: u64 = 116_444_736_000_000_000;
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return None;
        }
        let zeroed: FILETIME = std::mem::zeroed();
        let (mut creation, mut exit, mut kernel, mut user) =
            (zeroed, zeroed, zeroed, zeroed);
        let ok = GetProcessTimes(
            handle,
            &mut creation,
            &mut exit,
            &mut kernel,
            &mut user,
        );
        let _ = CloseHandle(handle);
        if ok == 0 {
            return None;
        }
        let ticks = ((creation.dwHighDateTime as u64) << 32)
            | (creation.dwLowDateTime as u64);
        ticks
            .checked_sub(FILETIME_UNIX_EPOCH_OFFSET)
            .map(|t| t / 10_000_000)
    }
}

#[cfg(unix)]
fn process_start_unix(pid: u32) -> Option<u64> {
    // /proc/<pid>/stat field 22 (starttime, clock ticks since boot).
    // Field 2 (comm) is parenthesised and may contain spaces/parens, so
    // count fields from the LAST ')': in the remainder, token 0 is
    // field 3 (state), making starttime token 19.
    let text = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let after_comm = text.rsplit_once(')')?.1;
    let fields: Vec<&str> = after_comm.split_whitespace().collect();
    fields.get(19)?.parse::<u64>().ok()
}

#[cfg(not(any(windows, unix)))]
fn process_start_unix(_pid: u32) -> Option<u64> {
    None
}

/// Force-terminate process `pid` (orphan sweep only — our own child is
/// always stopped through its `CommandChild` / the Job Object).
#[cfg(windows)]
fn terminate_process(pid: u32) -> bool {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::Threading::{
        OpenProcess, TerminateProcess, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE,
    };
    unsafe {
        let handle = OpenProcess(
            PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION,
            0,
            pid,
        );
        if handle.is_null() {
            return false;
        }
        // Exit code 1: "terminated by the shell's orphan sweep".
        let ok = TerminateProcess(handle, 1);
        let _ = CloseHandle(handle);
        ok != 0
    }
}

#[cfg(unix)]
fn terminate_process(pid: u32) -> bool {
    // Best-effort dev-platform path (no libc dependency in the shell):
    // delegate to /bin/kill with SIGTERM.
    std::process::Command::new("kill")
        .args(["-TERM", &pid.to_string()])
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

#[cfg(not(any(windows, unix)))]
fn terminate_process(_pid: u32) -> bool {
    false
}

// ---------------------------------------------------------------------
// Windows Job Object — the guaranteed tree kill (roadmap §4.6)
// ---------------------------------------------------------------------

/// Owner of the process Job Object (Windows) / documented no-op stub
/// (everywhere else).
///
/// Semantics that matter:
///
/// * Created once per supervisor (lazily on first assign), configured
///   with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Every spawned sidecar
///   is assigned right after spawn; processes a process IN a job
///   creates (the sidecar's FFmpeg children) join the job by default.
/// * While the shell lives, graceful paths run first (shutdown POST →
///   stdin fallback → child kill); the job is the LAST resort:
///   `close_kill` closes our handle, and the limit flag makes Windows
///   terminate every process still in the job.
/// * If the shell dies without running ANY cleanup (crash, kill),
///   process teardown closes the handle anyway — the tree still dies.
///   That is the entire point: no descendant can outlive the shell.
///
/// The handle is stored as a raw address (`usize`) purely so the
/// supervisor struct stays `Send` across threads; it is only ever used
/// on the supervisor thread.
struct JobObject {
    #[cfg(windows)]
    handle: Option<usize>,
}

impl JobObject {
    fn new() -> Self {
        JobObject {
            #[cfg(windows)]
            handle: None,
        }
    }

    /// Assign `pid` to the job (creating the job on first use).
    /// Failure is logged, never fatal: a sidecar that could not be
    /// assigned is still supervised normally; only the guaranteed-kill
    /// backstop is weakened.
    fn assign(&mut self, pid: u32) {
        #[cfg(windows)]
        {
            use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
            use windows_sys::Win32::System::Threading::{
                OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE,
            };
            let Some(job_bits) = self.ensure_created() else {
                return;
            };
            unsafe {
                let process = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid);
                if process.is_null() {
                    eprintln!("[shell] job object: OpenProcess({pid}) failed");
                    return;
                }
                let ok = windows_sys::Win32::System::JobObjects::AssignProcessToJobObject(
                    job_bits as HANDLE,
                    process,
                );
                let _ = CloseHandle(process);
                if ok == 0 {
                    eprintln!("[shell] job object: AssignProcessToJobObject({pid}) failed");
                }
            }
        }
        #[cfg(not(windows))]
        {
            // Non-Windows: no job objects. The supervisor's fallback is
            // CommandChild::kill on the direct child (see graceful_stop
            // and the health-probe restart path).
            let _ = pid;
        }
    }

    /// Close the job handle: with KILL_ON_JOB_CLOSE this terminates
    /// every process still assigned. Final-shutdown last resort ONLY —
    /// a supervisor that wants to spawn again afterwards can, since
    /// `assign` recreates the job lazily.
    fn close_kill(&mut self) {
        #[cfg(windows)]
        {
            use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
            if let Some(bits) = self.handle.take() {
                unsafe {
                    let _ = CloseHandle(bits as HANDLE);
                }
            }
        }
    }

    /// Create + configure the job on first use; returns the handle bits.
    #[cfg(windows)]
    fn ensure_created(&mut self) -> Option<usize> {
        use std::ffi::c_void;
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::JobObjects::{
            CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };
        if let Some(bits) = self.handle {
            return Some(bits);
        }
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                eprintln!("[shell] job object: CreateJobObjectW failed");
                return None;
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let ok = SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION as *const c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );
            if ok == 0 {
                eprintln!("[shell] job object: SetInformationJobObject failed");
                let _ = CloseHandle(job);
                return None;
            }
            let bits = job as usize;
            self.handle = Some(bits);
            Some(bits)
        }
    }
}

// ---------------------------------------------------------------------
// The supervisor thread
// ---------------------------------------------------------------------

/// Internal phase — richer than the public status string, which it maps
/// onto the contract states in `BackendStatus`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Phase {
    Stopped,
    Spawning,
    Starting,
    Ready,
    Degraded,
    AwaitRestart,
    Draining,
    Stopping,
    Failed,
}

/// A sidecar process that has spawned but not yet proven ready.
struct PendingBoot {
    port: u16,
    token: String,
    boot_id: String,
    started_at: Instant,
    next_poll_at: Instant,
    poll_interval: Duration,
    live_ok: bool,
    listening_ok: bool,
}

/// A sidecar process that reached READY. The supervisor keeps its own
/// copy of the coordinates (a clone is published into `AppState` for the
/// WebView); probes and the shutdown POST use this one.
struct CurrentBackend {
    port: u16,
    token: String,
    boot_id: String,
}

/// The supervisor. Owns the child, the Job Object, the restart budget
/// and the rotating log; runs on its own thread until the process exits.
struct Supervisor {
    app: AppHandle,
    control_rx: Receiver<SupervisorMessage>,
    data_dir: PathBuf,
    log: RotatingLog,
    job: JobObject,
    /// True when launched with `--headless` (Task Scheduler): the child
    /// then gets `YTA_DETACHED=1` and runs without the parent watchdog.
    headless: bool,
    /// Round 6: true while the NEXT spawn is the one-shot scheduled
    /// run — the process was launched with `--scheduled-run` (the
    /// Task Scheduler wake registered by
    /// `packaging/register_scheduler.ps1`).  That child is spawned
    /// with the flag in argv, never serves HTTP (no readiness
    /// polling, no cold-start budget), and its termination does NOT
    /// enter the crash/restart policy: `handle_terminated` clears
    /// this flag and hands over to the normal server sidecar, whose
    /// runner then executes the production jobs the one-shot tick
    /// enqueued.  Cleared on that first termination, so every later
    /// spawn in this process is a normal server spawn.
    scheduled_run: bool,
    /// Round 6: `--dry-run` accompanied `--scheduled-run` at launch;
    /// forwarded to the one-shot child ("list due schedules, change
    /// nothing").  Cleared together with `scheduled_run`.
    scheduled_dry_run: bool,
    /// Round 6: latched at launch when `--scheduled-run` was present:
    /// a Task Scheduler wake must never reveal the main window, even
    /// when the follow-on server sidecar reaches READY or FAILED.
    /// The app stays tray-resident; an interactive launch is a new
    /// process (or surfaces the window via the single-instance
    /// callback) and is unaffected.
    suppress_window_reveal: bool,
    phase: Phase,
    child: Option<CommandChild>,
    /// Std-channel mirror of the CURRENT child's plugin event receiver
    /// (a small async forwarder task feeds it). One channel per spawn:
    /// when a child is replaced, its channel is dropped with it, so a
    /// late `Terminated` from a previous generation can never be
    /// misread as belonging to the new one.
    events: Option<Receiver<CommandEvent>>,
    pending: Option<PendingBoot>,
    current: Option<CurrentBackend>,
    window_revealed: bool,
    restart_count: u32,
    restart_window: Vec<Instant>,
    restart_at: Option<Instant>,
    next_probe_at: Option<Instant>,
    probe_fail_streak: u32,
    last_exit_code: Option<i32>,
}

impl Supervisor {
    fn new(app: AppHandle, control_rx: Receiver<SupervisorMessage>) -> Self {
        let data_dir = data_dir();
        let _ = fs::create_dir_all(&data_dir);
        let log = RotatingLog::new(&data_dir.join("logs"));
        let argv: Vec<String> = std::env::args().collect();
        let headless = argv.iter().any(|arg| arg == "--headless");
        let scheduled_run = argv.iter().any(|arg| arg == "--scheduled-run");
        let scheduled_dry_run =
            scheduled_run && argv.iter().any(|arg| arg == "--dry-run");
        Supervisor {
            app,
            control_rx,
            data_dir,
            log,
            job: JobObject::new(),
            headless,
            scheduled_run,
            scheduled_dry_run,
            suppress_window_reveal: scheduled_run,
            phase: Phase::Stopped,
            child: None,
            events: None,
            pending: None,
            current: None,
            window_revealed: false,
            restart_count: 0,
            restart_window: Vec::new(),
            restart_at: None,
            next_probe_at: None,
            probe_fail_streak: 0,
            last_exit_code: None,
        }
    }

    /// Main loop: boot the first sidecar, then serve control messages,
    /// child events and timers on a 20 ms tick until process exit.
    fn run(mut self) {
        self.log
            .write_line("[shell] supervisor starting (roadmap 4.6 lifecycle)");
        self.spawn_sidecar();
        loop {
            while let Ok(message) = self.control_rx.try_recv() {
                self.handle_message(message);
            }
            self.drain_child_events();
            self.tick_timers();
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    // -- status plumbing ----------------------------------------------

    /// The single place statuses change: update `AppState`, emit
    /// `backend://status-changed`, note it in the log, and reveal the
    /// main window the first time we reach READY or FAILED.
    fn set_status(&mut self, state: &str, detail: Option<String>, exit_code: Option<i32>) {
        let boot_id = self
            .pending
            .as_ref()
            .map(|p| p.boot_id.clone())
            .or_else(|| self.current.as_ref().map(|c| c.boot_id.clone()));
        let status = BackendStatus {
            state: state.to_string(),
            detail,
            exit_code,
            boot_id,
            restart_count: self.restart_count,
        };
        if let Some(state_ref) = self.app.try_state::<AppState>() {
            state_ref.set_status(status.clone());
        }
        let _ = self.app.emit("backend://status-changed", status.clone());
        // Mirror the new status into the tray (status item + tooltip).
        // `refresh_status` is a silent no-op until the tray exists
        // (the supervisor boots before `tray::init`), and it reads the
        // status back through `status_snapshot` — already updated
        // above — so the tray can never show a stale state after a
        // transition.
        crate::tray::refresh_status(&self.app);
        self.log.write_line(&format!(
            "[shell] status={} detail={} exit_code={:?} restart_count={}",
            status.state,
            status.detail.as_deref().unwrap_or(""),
            status.exit_code,
            status.restart_count
        ));
        if (status.state == "ready" || status.state == "failed")
            && !self.window_revealed
            && !self.suppress_window_reveal
        {
            // Before this moment the frontend shows its starting (or
            // failure) screen behind a still-hidden window; revealing
            // earlier would flash an unusable dashboard (§4.6: the UI
            // must never show a working dashboard against a non-READY
            // backend — and the FAILED screen must be reachable).
            self.window_revealed = true;
            if let Some(window) = self.app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }
    }

    /// Publish (or clear) the WebView-visible connection. Publishing a
    /// connection also emits `backend://connection-changed`, which is
    /// the frontend's cue to re-fetch and resync by snapshot.
    fn publish_connection(&self, connection: Option<BackendConnection>, emit: bool) {
        if let Some(state_ref) = self.app.try_state::<AppState>() {
            state_ref.set_connection(connection.clone());
        }
        if emit {
            if let Some(connection) = connection {
                let _ = self.app.emit("backend://connection-changed", connection);
            }
        }
    }

    // -- control messages ----------------------------------------------

    fn handle_message(&mut self, message: SupervisorMessage) {
        match message {
            SupervisorMessage::Start => {
                if self.child.is_none()
                    && matches!(self.phase, Phase::Stopped | Phase::Failed)
                {
                    self.restart_window.clear();
                    self.spawn_sidecar();
                }
            }
            SupervisorMessage::Restart => {
                // A human asked (UI Retry / tray): the automatic-restart
                // budget starts over, then a fresh spawn — draining the
                // old child first if one is somehow still alive.
                self.restart_window.clear();
                self.restart_at = None;
                if self.child.is_some() {
                    self.graceful_stop(false);
                }
                self.spawn_sidecar();
            }
            SupervisorMessage::Shutdown { ack } => {
                if self.child.is_some() {
                    self.graceful_stop(true);
                } else {
                    self.phase = Phase::Stopped;
                    self.set_status("stopped", None, self.last_exit_code);
                }
                let _ = ack.send(());
            }
            SupervisorMessage::PauseAll => {
                let result = apply_execution_flag(&self.app, "pause_all");
                self.log.write_line(&format!(
                    "[shell] pause_all via settings flags: {result:?}"
                ));
            }
            SupervisorMessage::StopAll => {
                let result = apply_execution_flag(&self.app, "stop_all");
                self.log.write_line(&format!(
                    "[shell] stop_all via settings flags: {result:?}"
                ));
            }
        }
    }

    // -- spawn -----------------------------------------------------------

    /// One spawn attempt (roadmap §4.6 spawn sequence, in order):
    /// orphan sweep → ephemeral port → fresh token + boot_id → env-only
    /// launch contract → plugin-shell spawn → Job Object assignment →
    /// PID record → STARTING (readiness runs off timers from here).
    fn spawn_sidecar(&mut self) {
        if self.child.is_some() {
            return;
        }
        let _ = fs::create_dir_all(&self.data_dir);
        orphan_sweep(&self.data_dir, &mut self.log);

        // Port: bind 127.0.0.1:0, take the port, drop the listener. The
        // release→rebind race this opens is covered by exit code 3
        // (bind failure → respawn with a fresh port, in-budget).
        let port = match TcpListener::bind("127.0.0.1:0") {
            Ok(listener) => {
                let port = listener.local_addr().map(|addr| addr.port()).unwrap_or(0);
                drop(listener);
                port
            }
            Err(_) => 0,
        };
        if port == 0 {
            self.phase = Phase::Failed;
            self.set_status(
                "failed",
                Some("port_allocation_failed: could not bind 127.0.0.1:0".to_string()),
                None,
            );
            return;
        }

        let token = generate_launch_token();
        let boot_id = generate_boot_id();
        self.pending = Some(PendingBoot {
            port,
            token: token.clone(),
            boot_id: boot_id.clone(),
            started_at: Instant::now(),
            next_poll_at: Instant::now(),
            poll_interval: Duration::from_millis(100),
            live_ok: false,
            listening_ok: false,
        });
        self.phase = Phase::Spawning;
        self.set_status("spawning", None, None);

        // The environment is the ONLY configuration channel into the
        // sidecar. YTA_TOKEN goes here — env for this spawn call only,
        // never argv, never a file, never the parent process env.
        let mut env: HashMap<String, String> = HashMap::new();
        env.insert("YTA_PORT".to_string(), port.to_string());
        env.insert("YTA_TOKEN".to_string(), token);
        env.insert("YTA_BOOT_ID".to_string(), boot_id.clone());
        env.insert(
            "YTA_DATA_DIR".to_string(),
            self.data_dir.to_string_lossy().into_owned(),
        );
        env.insert("YTA_PARENT_PID".to_string(), std::process::id().to_string());
        env.insert("YTA_LOG_LEVEL".to_string(), "info".to_string());
        if self.headless || self.scheduled_run {
            // Task-Scheduler headless runs only: no parent watchdog.
            // A scheduled run (Round 6) is a Task Scheduler launch too,
            // so its one-shot child is detached the same way.
            env.insert("YTA_DETACHED".to_string(), "1".to_string());
        }

        // R6-013: prefer the staged `--onedir` tree copy from the
        // bundle resources when the lone `externalBin` exe has no
        // `_internal/` beside it (see `resource_sidecar_exe`); that
        // copy is spawned by path via the shell plugin's `command`,
        // which returns the same `process::Command` type the rest of
        // this spawn sequence already drives.
        let command = match resource_sidecar_exe(&self.app) {
            Some(exe) => {
                self.log.write_line(&format!(
                    "[shell] spawning staged onedir sidecar from resources: {}",
                    exe.display()
                ));
                self.app.shell().command(exe)
            }
            None => match self.app.shell().sidecar(SIDECAR_NAME) {
                Ok(command) => command,
                Err(e) => {
                    self.pending = None;
                    self.phase = Phase::Failed;
                    self.set_status(
                        "failed",
                        Some(format!("sidecar_unavailable: {e}")),
                        None,
                    );
                    return;
                }
            },
        };
        // Round 6: a scheduled-run spawn carries the flag in argv —
        // the sidecar's app.main dispatch runs the one-shot catch-up
        // (app.execution.scheduled_run) and exits instead of serving.
        // Unlike the token, these flags are not secrets, so argv is
        // the right channel for them.
        let mut command = command.envs(env);
        if self.scheduled_run {
            let mut args: Vec<&str> = vec!["--scheduled-run"];
            if self.scheduled_dry_run {
                args.push("--dry-run");
            }
            command = command.args(args);
        }
        match command.spawn() {
            Ok((mut rx, child)) => {
                let pid = child.pid();
                self.job.assign(pid);
                // Forward the plugin's async event receiver into a std
                // channel this thread can poll alongside its timers.
                let (forward_tx, forward_rx) = channel::<CommandEvent>();
                tauri::async_runtime::spawn(async move {
                    while let Some(event) = rx.recv().await {
                        if forward_tx.send(event).is_err() {
                            break;
                        }
                    }
                });
                write_pid_file(&self.data_dir, pid, &boot_id);
                self.child = Some(child);
                self.events = Some(forward_rx);
                self.phase = Phase::Starting;
                self.probe_fail_streak = 0;
                self.log.write_line(&format!(
                    "[shell] spawned {SIDECAR_NAME} pid {pid} on 127.0.0.1:{port} boot {boot_id}"
                ));
                self.set_status("starting", None, None);
            }
            Err(e) => {
                // A binary that cannot even start is a packaging/build
                // bug, not a crash loop: FAILED, no automatic restart.
                self.pending = None;
                self.phase = Phase::Failed;
                self.set_status("failed", Some(format!("spawn_failed: {e}")), None);
            }
        }
    }

    // -- child events ----------------------------------------------------

    /// Drain the current child's queued events: tee output to the
    /// rotating log, verify the structured listening line, and route
    /// termination through the exit-code policy.
    fn drain_child_events(&mut self) {
        let Some(events) = self.events.as_ref() else {
            return;
        };
        // Collect first, process after: processing may replace
        // `self.events` (termination / start failure), which must not
        // fight the borrow of the channel being drained.
        let mut collected: Vec<CommandEvent> = Vec::new();
        loop {
            match events.try_recv() {
                Ok(event) => collected.push(event),
                Err(std::sync::mpsc::TryRecvError::Empty) => break,
                Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                    collected.push(CommandEvent::Error(
                        "__shell_channel_disconnected__".to_string(),
                    ));
                    break;
                }
            }
        }
        for event in collected {
            match event {
                CommandEvent::Stdout(bytes) => {
                    let line = String::from_utf8_lossy(&bytes).into_owned();
                    self.log.write_line(&format!("[stdout] {line}"));
                    if self.check_listening_line(&line) {
                        return; // Start failed (mismatch): state replaced.
                    }
                }
                CommandEvent::Stderr(bytes) => {
                    let line = String::from_utf8_lossy(&bytes).into_owned();
                    self.log.write_line(&format!("[stderr] {line}"));
                }
                CommandEvent::Error(message) => {
                    if message == "__shell_channel_disconnected__" {
                        // The plugin's event task died without a
                        // Terminated: treat the child as crashed.
                        if self.child.is_some() {
                            self.log.write_line(
                                "[shell] sidecar event channel disconnected; treating as crash",
                            );
                            self.kill_child();
                            self.handle_terminated(None);
                        }
                        return;
                    }
                    self.log
                        .write_line(&format!("[shell] sidecar event error: {message}"));
                }
                CommandEvent::Terminated(payload) => {
                    self.handle_terminated(payload.code);
                    return;
                }
                _ => {}
            }
        }
    }

    /// Verify the sidecar's one structured stdout line (§4.6):
    /// `{"event":"listening","port":…,"boot_id":"…"}` must match the
    /// port and boot_id WE chose for this spawn. A mismatch means the
    /// process answering is not the process we think we spawned — the
    /// start fails rather than trusting it. Returns true when the start
    /// was failed by this line and the caller must stop processing.
    fn check_listening_line(&mut self, line: &str) -> bool {
        if !line.contains("\"listening\"") {
            return false;
        }
        let Ok(value) = serde_json::from_str::<serde_json::Value>(line.trim()) else {
            return false;
        };
        if value.get("event").and_then(|v| v.as_str()) != Some("listening") {
            return false;
        }
        let announced_port = value.get("port").and_then(|v| v.as_u64());
        let announced_boot = value
            .get("boot_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        // Decide under a short immutable borrow; act after it ends (the
        // mismatch path needs `&mut self` for `fail_start`).
        enum Verdict {
            /// A duplicate line after READY (or before any spawn).
            NoPending,
            Matched { port: u16, boot_id: String },
            Mismatched { detail: String },
        }
        let verdict = match self.pending.as_ref() {
            None => Verdict::NoPending,
            Some(pending)
                if announced_port == Some(pending.port as u64)
                    && announced_boot == pending.boot_id =>
            {
                Verdict::Matched {
                    port: pending.port,
                    boot_id: pending.boot_id.clone(),
                }
            }
            Some(pending) => Verdict::Mismatched {
                detail: format!(
                    "listening_mismatch: sidecar announced port {:?} boot {:?}, expected port {} boot {}",
                    announced_port, announced_boot, pending.port, pending.boot_id
                ),
            },
        };
        match verdict {
            Verdict::NoPending => false,
            Verdict::Matched { port, boot_id } => {
                if let Some(pending) = self.pending.as_mut() {
                    pending.listening_ok = true;
                }
                self.log.write_line(&format!(
                    "[shell] listening handshake verified (port {port}, boot {boot_id})"
                ));
                false
            }
            Verdict::Mismatched { detail } => {
                self.fail_start(detail);
                true
            }
        }
    }

    /// Fail an in-flight start (cold-start timeout / listening
    /// mismatch): kill the child, land in the stable FAILED state — the
    /// UI failure screen (Retry → `Restart`) takes it from here.
    fn fail_start(&mut self, detail: String) {
        let boot_id = self.pending.as_ref().map(|p| p.boot_id.clone());
        self.kill_child();
        self.pending = None;
        self.current = None;
        if let Some(boot_id) = boot_id {
            remove_pid_file_if_ours(&self.data_dir, &boot_id);
        }
        self.phase = Phase::Failed;
        self.set_status("failed", Some(detail), None);
    }

    /// Termination policy (roadmap §4.6 exit codes). Runs for any
    /// termination the supervisor did NOT itself orchestrate (graceful
    /// stops consume their own Terminated inside `graceful_stop`, and a
    /// killed child's channel is dropped before its Terminated can be
    /// misread here).
    fn handle_terminated(&mut self, code: Option<i32>) {
        let boot_id = self
            .pending
            .as_ref()
            .map(|p| p.boot_id.clone())
            .or_else(|| self.current.as_ref().map(|c| c.boot_id.clone()));
        self.child = None;
        self.events = None;
        self.pending = None;
        self.current = None;
        self.next_probe_at = None;
        self.publish_connection(None, false);
        if let Some(boot_id) = boot_id {
            remove_pid_file_if_ours(&self.data_dir, &boot_id);
        }
        self.last_exit_code = code;
        if self.scheduled_run {
            // Round 6: the one-shot scheduled run finished (any exit
            // code — its JSON report on stdout, already tee'd to the
            // rotating log, is the record of what ran).  It must NOT
            // enter the crash/restart policy below: clear the one-shot
            // flags and hand over to the normal server sidecar via
            // the existing restart timer, so its runner executes the
            // production jobs the tick enqueued.  The window stays
            // hidden (suppress_window_reveal was latched at launch).
            self.scheduled_run = false;
            self.scheduled_dry_run = false;
            self.log.write_line(&format!(
                "[shell] scheduled run finished (exit code {code:?}); starting normal sidecar"
            ));
            self.phase = Phase::AwaitRestart;
            self.restart_at = Some(Instant::now());
            self.set_status(
                "stopped",
                Some("scheduled_run_complete".to_string()),
                code,
            );
            return;
        }
        match code {
            // Clean exit while we were not shutting down is still a
            // dead backend: treat as crash (roadmap item: unexpected 0
            // == crash). When WE shut it down, graceful_stop consumed
            // the event itself, so this arm never sees that case.
            Some(0) => self.schedule_restart(
                "sidecar exited cleanly (code 0) but unexpectedly".to_string(),
                code,
            ),
            // 2 = fatal config / bind-scope: do not auto-restart.
            Some(2) => {
                self.phase = Phase::Failed;
                self.set_status(
                    "failed",
                    Some("fatal_config: sidecar exit code 2 (see diagnostics)".to_string()),
                    code,
                );
            }
            // 3 = port-bind failure: respawn with a FRESH port; counts
            // within the normal restart budget.
            Some(3) => self.schedule_restart(
                "port_bind_failed: sidecar exit code 3, respawning with a fresh port"
                    .to_string(),
                code,
            ),
            // 4 = DB schema newer than this binary (downgrade
            // protection): NO restart; the UI routes this exact detail
            // string to the updater.
            Some(4) => {
                self.phase = Phase::Failed;
                self.set_status("failed", Some("database_newer_than_binary".to_string()), code);
            }
            // 5 = missing/invalid launch environment: a build bug.
            Some(5) => {
                self.phase = Phase::Failed;
                self.set_status(
                    "failed",
                    Some("bad_launch_env: sidecar exit code 5".to_string()),
                    code,
                );
            }
            // Anything else (incl. signal death, code == None) = crash.
            other => self.schedule_restart(
                format!("sidecar crashed (exit code {other:?})"),
                other,
            ),
        }
    }

    /// Enter CRASHED and schedule an automatic restart honouring the
    /// budget: backoff 1 s → 2 s → 5 s, max 3 restarts in any 5-minute
    /// window; beyond that, the stable FAILED state — never a silent
    /// spin loop.
    fn schedule_restart(&mut self, detail: String, exit_code: Option<i32>) {
        let now = Instant::now();
        self.restart_window
            .retain(|at| now.duration_since(*at) < RESTART_WINDOW);
        if self.restart_window.len() >= RESTART_MAX_IN_WINDOW {
            self.phase = Phase::Failed;
            self.set_status(
                "failed",
                Some(format!(
                    "restart_budget_exhausted: {detail} ({} restarts in 5 minutes)",
                    RESTART_MAX_IN_WINDOW
                )),
                exit_code,
            );
            return;
        }
        let backoff = RESTART_BACKOFFS[self.restart_window.len()];
        self.restart_window.push(now);
        self.restart_count += 1;
        self.restart_at = Some(now + backoff);
        self.phase = Phase::AwaitRestart;
        self.set_status("crashed", Some(detail), exit_code);
    }

    /// Kill the current child through its handle and drop its event
    /// channel (used when WE decide the process must die: failed start,
    /// health-probe restart). Dropping the channel is what keeps the
    /// child's own Terminated from being re-processed as a fresh crash.
    fn kill_child(&mut self) {
        if let Some(child) = self.child.take() {
            let _ = child.kill();
        }
        self.events = None;
    }

    // -- timers: readiness, cold-start deadline, steady probes, restarts --

    fn tick_timers(&mut self) {
        let now = Instant::now();
        match self.phase {
            Phase::Starting => {
                // Round 6: the scheduled-run one-shot never serves
                // HTTP — there is no readiness to poll, and the 30 s
                // cold-start budget must not kill a long catch-up
                // tick.  Its Terminated event drives the transition
                // (see handle_terminated).
                if self.scheduled_run {
                    return;
                }
                let schedule = self
                    .pending
                    .as_ref()
                    .map(|p| (p.started_at, p.next_poll_at));
                if let Some((started_at, next_poll_at)) = schedule {
                    if now.duration_since(started_at) >= COLD_START_BUDGET {
                        self.fail_start(
                            "cold_start_timeout: sidecar not ready within 30s".to_string(),
                        );
                        return;
                    }
                    if now >= next_poll_at {
                        self.poll_readiness();
                    }
                }
            }
            Phase::Ready | Phase::Degraded => {
                if self.next_probe_at.map(|at| now >= at).unwrap_or(false) {
                    self.next_probe_at = Some(now + STEADY_PROBE_INTERVAL);
                    self.probe_steady();
                }
            }
            Phase::AwaitRestart => {
                if self.restart_at.map(|at| now >= at).unwrap_or(false) {
                    self.restart_at = None;
                    self.spawn_sidecar();
                }
            }
            _ => {}
        }
    }

    /// Readiness sequence (§4.6): poll `GET /health/live` (no token —
    /// it is the one token-exempt endpoint and returns only
    /// `{"status":"ok"}`) every 100 ms, backing off ×2 capped at 1 s;
    /// once live, poll `GET /health/ready` WITH the token until 200.
    /// READY additionally requires the verified listening line.
    fn poll_readiness(&mut self) {
        // Snapshot everything needed up front (owned values), so no
        // borrow of `self.pending` is live across the `&mut self` calls
        // below (`backoff_pending_poll`, `become_ready`).
        let Some((port, live_ok, token, listening_ok)) = self.pending.as_ref().map(|p| {
            (p.port, p.live_ok, p.token.clone(), p.listening_ok)
        }) else {
            return;
        };
        if !live_ok {
            let url = format!("http://127.0.0.1:{port}/health/live");
            match mini_http::get(&url, None) {
                Ok((200, _)) => {
                    if let Some(pending) = self.pending.as_mut() {
                        pending.live_ok = true;
                        pending.next_poll_at = Instant::now();
                    }
                }
                _ => self.backoff_pending_poll(),
            }
            return;
        }
        let url = format!("http://127.0.0.1:{port}/health/ready");
        match mini_http::get(&url, Some(&token)) {
            Ok((200, _)) => {
                if listening_ok {
                    self.become_ready();
                } else {
                    // The backend answers as ready but its listening
                    // line has not been verified yet (stdout is a
                    // separate pipe and can lag a few ms). Keep polling
                    // until the line lands or the 30 s budget ends the
                    // start as FAILED — readiness is only READY with
                    // the handshake matched.
                    self.backoff_pending_poll();
                }
            }
            _ => self.backoff_pending_poll(),
        }
    }

    fn backoff_pending_poll(&mut self) {
        if let Some(pending) = self.pending.as_mut() {
            let interval = pending.poll_interval;
            pending.next_poll_at = Instant::now() + interval;
            pending.poll_interval =
                std::cmp::min(interval * 2, Duration::from_secs(1));
        }
    }

    /// STARTING → READY: publish the connection (the WebView may call
    /// the backend from this moment), emit connection-changed, and arm
    /// the steady-state probe.
    fn become_ready(&mut self) {
        let Some(pending) = self.pending.take() else {
            return;
        };
        let connection = BackendConnection {
            base_url: format!("http://127.0.0.1:{}", pending.port),
            ws_url: format!("ws://127.0.0.1:{}", pending.port),
            token: pending.token.clone(),
        };
        self.current = Some(CurrentBackend {
            port: pending.port,
            token: pending.token,
            boot_id: pending.boot_id,
        });
        self.phase = Phase::Ready;
        self.probe_fail_streak = 0;
        self.next_probe_at = Some(Instant::now() + STEADY_PROBE_INTERVAL);
        self.publish_connection(Some(connection), true);
        self.set_status("ready", None, None);
    }

    /// Steady-state probe (§4.6): `GET /health/ready` every 10 s.
    /// 1 consecutive failure = nothing · 2 = DEGRADED · 3 = restart.
    /// A 200 while DEGRADED returns to READY and resets the streak.
    fn probe_steady(&mut self) {
        let Some((port, token)) = self
            .current
            .as_ref()
            .map(|c| (c.port, c.token.clone()))
        else {
            return;
        };
        let url = format!("http://127.0.0.1:{port}/health/ready");
        match mini_http::get(&url, Some(&token)) {
            Ok((200, _)) => {
                self.probe_fail_streak = 0;
                if self.phase == Phase::Degraded {
                    self.phase = Phase::Ready;
                    self.set_status(
                        "ready",
                        Some("health probe recovered".to_string()),
                        None,
                    );
                }
            }
            _ => {
                self.probe_fail_streak += 1;
                if self.probe_fail_streak >= 3 {
                    self.log.write_line(
                        "[shell] health probe failed 3 consecutive times: restarting sidecar",
                    );
                    self.kill_child();
                    let boot_id = self.current.take().map(|c| c.boot_id);
                    if let Some(boot_id) = boot_id {
                        remove_pid_file_if_ours(&self.data_dir, &boot_id);
                    }
                    self.publish_connection(None, false);
                    self.schedule_restart(
                        "health_probe_failed: 3 consecutive /health/ready failures"
                            .to_string(),
                        None,
                    );
                } else if self.probe_fail_streak == 2 && self.phase == Phase::Ready {
                    self.phase = Phase::Degraded;
                    self.set_status(
                        "degraded",
                        Some("health probe failing (2 consecutive failures)".to_string()),
                        None,
                    );
                }
            }
        }
    }

    // -- shutdown ---------------------------------------------------------

    /// The one shutdown sequence (§4.6), however it was initiated:
    ///
    /// 1. status → DRAINING; `POST /api/v1/system/shutdown` with the
    ///    token (the backend records the request and drains);
    /// 2. fallback: a `SHUTDOWN\n` line on the child's stdin;
    /// 3. status → STOPPING; wait up to the 10 s budget for exit,
    ///    still teeing output to the log;
    /// 4. still alive → last resort: for a FINAL shutdown, close the
    ///    Job Object (its KILL_ON_JOB_CLOSE terminates the whole tree,
    ///    FFmpeg included); then `CommandChild::kill`. For a restart
    ///    the job is kept (the next child joins it) and only the child
    ///    handle is killed.
    ///
    /// Ends in STOPPED with the connection cleared and the PID file
    /// removed. A forced kill is safe by design (60 s job leases expire,
    /// checkpoints persist) — it is a fallback, not the plan.
    fn graceful_stop(&mut self, final_shutdown: bool) {
        if self.child.is_none() {
            self.phase = Phase::Stopped;
            self.set_status("stopped", None, self.last_exit_code);
            return;
        }
        self.phase = Phase::Draining;
        self.set_status(
            "draining",
            Some("graceful shutdown requested".to_string()),
            None,
        );

        // (1) Polite HTTP drain. Works for a READY backend (published
        // coordinates) and for a still-STARTING one (pending boot).
        let target = self
            .current
            .as_ref()
            .map(|c| (c.port, c.token.clone()))
            .or_else(|| self.pending.as_ref().map(|p| (p.port, p.token.clone())));
        let mut posted = false;
        if let Some((port, token)) = target {
            let url = format!("http://127.0.0.1:{port}/api/v1/system/shutdown");
            if let Ok((status, _)) = mini_http::post(&url, Some(&token)) {
                posted = (200..300).contains(&status);
            }
        }
        // (2) Stdin fallback when the POST did not land.
        if !posted {
            if let Some(child) = self.child.as_mut() {
                let _ = child.write(b"SHUTDOWN\n");
            }
        }

        self.phase = Phase::Stopping;
        self.set_status("stopping", None, None);

        // (3) Wait out the budget, teeing output as it drains.
        let events = self.events.take();
        let mut exit_code: Option<i32> = None;
        let mut terminated = false;
        if let Some(events) = events.as_ref() {
            let deadline = Instant::now() + SHUTDOWN_BUDGET;
            while Instant::now() < deadline {
                match events.recv_timeout(Duration::from_millis(50)) {
                    Ok(CommandEvent::Stdout(bytes)) => {
                        let line = String::from_utf8_lossy(&bytes).into_owned();
                        self.log.write_line(&format!("[stdout] {line}"));
                    }
                    Ok(CommandEvent::Stderr(bytes)) => {
                        let line = String::from_utf8_lossy(&bytes).into_owned();
                        self.log.write_line(&format!("[stderr] {line}"));
                    }
                    Ok(CommandEvent::Terminated(payload)) => {
                        exit_code = payload.code;
                        terminated = true;
                        break;
                    }
                    Ok(CommandEvent::Error(message)) => {
                        self.log.write_line(&format!(
                            "[shell] sidecar event error during drain: {message}"
                        ));
                    }
                    Ok(_) => {}
                    Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
                    Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
                }
            }
        }

        // (4) Last resorts.
        if !terminated {
            if final_shutdown {
                self.job.close_kill();
            }
            if let Some(child) = self.child.take() {
                let _ = child.kill();
            }
            // Brief grace for the Terminated event so the exit code is
            // recorded honestly when it is knowable at all.
            if let Some(events) = events.as_ref() {
                let deadline = Instant::now() + Duration::from_secs(2);
                while Instant::now() < deadline {
                    match events.recv_timeout(Duration::from_millis(50)) {
                        Ok(CommandEvent::Terminated(payload)) => {
                            exit_code = payload.code;
                            break;
                        }
                        Ok(_) => {}
                        Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
                        Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
                    }
                }
            }
        }

        let boot_id = self
            .pending
            .as_ref()
            .map(|p| p.boot_id.clone())
            .or_else(|| self.current.as_ref().map(|c| c.boot_id.clone()));
        self.child = None;
        self.events = None;
        self.pending = None;
        self.current = None;
        self.next_probe_at = None;
        self.publish_connection(None, false);
        if let Some(boot_id) = boot_id {
            remove_pid_file_if_ours(&self.data_dir, &boot_id);
        }
        if exit_code.is_some() {
            self.last_exit_code = exit_code;
        }
        self.phase = Phase::Stopped;
        self.set_status("stopped", None, self.last_exit_code);
    }
}
