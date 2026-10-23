//! Thin native-feature commands for the WebView (Round 6, features.rs).
//!
//! Every command here is a *bridge*, not a feature: it translates one
//! frontend intent into one operating-system / plugin call and returns
//! the plain result. All product logic lives in the Python sidecar.
//!
//! Why these exist as Rust commands at all (security posture, roadmap
//! Part 11): the WebView is deliberately granted NO dialog, filesystem,
//! shell or autostart capability permissions in
//! `capabilities/default.json`. File pickers and autostart are reached
//! exclusively through the *Rust-side* plugin APIs
//! (`tauri_plugin_dialog::DialogExt`, `tauri_plugin_autostart::ManagerExt`)
//! from these commands, so the plugins' own JS guest bindings stay
//! unreachable from web content and the capability file does not grow.
//! If you are adding a command here, keep that rule: Rust-side plugin
//! API only, never a new WebView permission.
//!
//! Fixed contract with lib.rs — exactly these commands are registered:
//! `open_output_folder`, `open_logs_folder`, `pick_files`,
//! `pick_folder`, `autostart_get`, `autostart_set`.

use std::path::PathBuf;

#[cfg(windows)]
use std::process::Command;

use tauri::AppHandle;
use tauri_plugin_autostart::ManagerExt;
use tauri_plugin_dialog::{DialogExt, FilePath};

// ---------------------------------------------------------------------------
// Folder resolution
//
// These MUST match the paths the rest of the shell already uses:
//   * Data dir: the same directory handed to the sidecar as
//     `YTA_DATA_DIR` (roadmap §4.6 env contract), resolved by the
//     sidecar's canonical resolver (`sidecar::data_dir`: `YTA_DATA_DIR`
//     override, else `%APPDATA%\YouTubeAgent`, else the dev default).
//     Logs live in its `logs` subdirectory.
//   * Default output dir: `%USERPROFILE%\Videos\YouTubeAgent` — the
//     per-user Videos library, i.e. where a Windows user expects to
//     find finished videos in Explorer.
// Both are resolved from the environment at call time (never cached,
// never hardcoded to a drive letter): `APPDATA`/`USERPROFILE` follow
// roaming-profile and folder-redirection policies on real machines.
// ---------------------------------------------------------------------------

/// The app data dir (`YTA_DATA_DIR`) — delegated to the sidecar
/// supervisor's canonical resolver (`sidecar::data_dir`), so the logs
/// folder opened here is always the folder the supervisor actually
/// writes to, including when the `YTA_DATA_DIR` environment override
/// is set (packaged runtime / tests). That resolver falls back through
/// `%APPDATA%\YouTubeAgent` and the dev default; it never fails, so
/// this helper no longer needs an error case.
fn data_dir() -> Result<PathBuf, String> {
    Ok(crate::sidecar::data_dir())
}

/// `%USERPROFILE%\Videos\YouTubeAgent` — the default output folder.
fn output_dir() -> Result<PathBuf, String> {
    match std::env::var_os("USERPROFILE") {
        Some(profile) if !profile.is_empty() => {
            Ok(PathBuf::from(profile).join("Videos").join("YouTubeAgent"))
        }
        _ => Err(
            "USERPROFILE is not set, so the default output folder cannot be resolved"
                .to_string(),
        ),
    }
}

/// Create `path` if missing, then open it in Windows Explorer.
///
/// Windows behaviour worth knowing:
///   * `explorer.exe` is spawned, never waited on. Explorer famously
///     exits with code 1 even when it successfully opened the folder,
///     so its exit status carries no information — only a failure to
///     *spawn* it is an error here.
///   * The path is passed as a single `Command` argument (no shell
///     string is ever built), so spaces/Unicode in the profile path
///     are safe and there is no command-injection surface.
///   * `explorer` reuses an existing Explorer window for the folder
///     when one is open, which is the behaviour users expect.
///
/// Non-Windows fallback: a plain error. The shipping target is Windows
/// only; on a dev machine (macOS/Linux) the caller gets an honest
/// message naming the folder instead of a silent no-op or a wrong-OS
/// opener (`open`/`xdg-open` are deliberately NOT substituted — the
/// contract for this command is "open in Explorer").
fn open_in_explorer(path: PathBuf) -> Result<(), String> {
    std::fs::create_dir_all(&path).map_err(|err| {
        format!("could not create folder {}: {err}", path.display())
    })?;

    // Explicit `return` in the Windows arm: two consecutive cfg'd blocks
    // would otherwise parse the first as a statement (not the tail
    // expression) once the other is stripped.
    #[cfg(windows)]
    {
        return Command::new("explorer")
            .arg(&path)
            .spawn()
            .map(|_child| ())
            .map_err(|err| {
                format!("could not launch Explorer for {}: {err}", path.display())
            });
    }

    #[cfg(not(windows))]
    {
        Err(format!(
            "opening folders is only supported on Windows (folder: {})",
            path.display()
        ))
    }
}

/// Open the default output folder (`%USERPROFILE%\Videos\YouTubeAgent`)
/// in Explorer, creating it first if it does not exist yet (e.g. the
/// user clicks "open output folder" before the first render finished).
#[tauri::command]
pub fn open_output_folder() -> Result<(), String> {
    open_in_explorer(output_dir()?)
}

/// Open the logs folder (`%APPDATA%\YouTubeAgent\logs`) in Explorer,
/// creating it first if missing (same reasoning as the output folder:
/// "View logs" on the failure screen must work even if the sidecar died
/// before it could create its log directory).
#[tauri::command]
pub fn open_logs_folder() -> Result<(), String> {
    open_in_explorer(data_dir()?.join("logs"))
}

// ---------------------------------------------------------------------------
// File / folder picking (tauri-plugin-dialog, Rust side only)
//
// These two commands are the ONLY dialog paths in the app — see the
// module doc: the WebView holds no dialog permissions.
//
// Threading: the plugin's `blocking_pick_*` APIs block the calling
// thread until the user closes the dialog and must NOT run on the main
// thread (they would deadlock against the event loop that has to paint
// the dialog). Tauri commands do not guarantee a non-main thread for a
// plain `async fn`, so the blocking call is explicitly moved onto a
// blocking-worker thread with `tauri::async_runtime::spawn_blocking`
// and awaited — the idiomatic Tauri 2 pattern for blocking plugin APIs
// inside commands.
// ---------------------------------------------------------------------------

/// Convert a plugin `FilePath` into a plain filesystem path string.
///
/// On desktop the dialog returns `FilePath::Path` (a real `PathBuf`).
/// The `Url` variant exists for mobile content URIs; should it ever
/// appear on Windows it is a `file://` URL, which is converted back to
/// its path rather than handing a URL string to callers that expect a
/// filesystem path (the sidecar opens these paths as files).
fn file_path_to_string(path: FilePath) -> String {
    match path {
        FilePath::Path(path_buf) => path_buf.to_string_lossy().into_owned(),
        FilePath::Url(url) => match url.to_file_path() {
            Ok(path_buf) => path_buf.to_string_lossy().into_owned(),
            Err(_) => url.to_string(),
        },
    }
}

/// Native multi-file picker. Returns the picked filesystem paths, or
/// `None` if the user cancelled/closed the dialog (cancellation is a
/// normal outcome, not an error). `Err` is reserved for the picker
/// machinery itself failing (e.g. the blocking worker panicked).
#[tauri::command]
pub async fn pick_files(app: AppHandle) -> Result<Option<Vec<String>>, String> {
    let picked = tauri::async_runtime::spawn_blocking(move || {
        app.dialog().file().blocking_pick_files()
    })
    .await
    .map_err(|err| format!("file picker failed: {err}"))?;

    Ok(picked.map(|paths| paths.into_iter().map(file_path_to_string).collect()))
}

/// Native single-folder picker. Returns the picked folder's filesystem
/// path, or `None` if the user cancelled/closed the dialog.
#[tauri::command]
pub async fn pick_folder(app: AppHandle) -> Result<Option<String>, String> {
    let picked = tauri::async_runtime::spawn_blocking(move || {
        app.dialog().file().blocking_pick_folder()
    })
    .await
    .map_err(|err| format!("folder picker failed: {err}"))?;

    Ok(picked.map(file_path_to_string))
}

// ---------------------------------------------------------------------------
// Autostart (tauri-plugin-autostart, Rust side only)
//
// Roadmap §11.9: autostart is a SEPARATE, OPT-IN, per-user registry Run
// entry. On Windows the plugin writes a value under
// `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` — current-user
// hive, so NO administrator rights are needed (and none are ever
// requested), and it can be undone by the user in Task Manager's
// Startup tab as well as here.
//
// Default is OFF, and nothing in this module ever changes that
// implicitly: `autostart_set` runs only when the frontend calls it in
// response to the user flipping the "Start with Windows" setting. Do
// not call `autostart_set(true)` from setup/first-run code.
// ---------------------------------------------------------------------------

/// Whether "Start with Windows" is currently enabled for this user.
#[tauri::command]
pub fn autostart_get(app: AppHandle) -> Result<bool, String> {
    app.autolaunch()
        .is_enabled()
        .map_err(|err| format!("could not read autostart state: {err}"))
}

/// Enable or disable "Start with Windows" for this user (HKCU Run key,
/// no admin). Idempotent at the plugin level: enabling an already
/// enabled entry / disabling an absent one is not an error the user
/// needs to see, and the plugin reports it as success.
#[tauri::command]
pub fn autostart_set(app: AppHandle, enabled: bool) -> Result<(), String> {
    let autolaunch = app.autolaunch();
    let result = if enabled {
        autolaunch.enable()
    } else {
        autolaunch.disable()
    };
    result.map_err(|err| format!("could not change autostart state: {err}"))
}
