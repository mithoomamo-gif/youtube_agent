//! Update Channel A — Tauri updater policy (roadmap §11.4).
//!
//! This module is the shell side of the shell+UI update channel. It is
//! deliberately thin: signature verification, download and install are
//! the `tauri-plugin-updater` plugin's job, driven by the configuration
//! in `tauri.conf.json > plugins.updater` (`endpoints`, `pubkey`,
//! `windows.installMode`). What lives here is *policy*:
//!
//!   * when checks happen (first check 30 s after launch, then every
//!     6 hours while the app is tray-resident — §11.4),
//!   * how a check result is reported to the WebView
//!     ([`UpdateCheckResult`], plus the `updater://update-available`
//!     event for background checks),
//!   * the monotonic-version guard (never accept an offered version
//!     that is not strictly newer than the running app — fail closed),
//!   * and the rule that background checks NEVER install anything:
//!     install is user/idle-triggered only (see [`install_update`]).
//!
//! Signature verification is the plugin's job, via the minisign pubkey
//! embedded in the binary from `tauri.conf.json`. If that pubkey is
//! empty (placeholder state) or does not match the key that signed an
//! artefact, the plugin's verification fails and this module reports
//! `state: "error"` — every update fails closed, by design. No code
//! path in this file bypasses, downgrades or ignores a verification,
//! network, or manifest-parse failure. Key custody for the signing
//! keypair is documented in `docs/UPDATER_KEY_RUNBOOK.md`; the private
//! key lives only in CI secrets and never in this repo.
//!
//! Wiring (owned by `lib.rs`, fixed contract — not this file):
//! `mod updater;`, `updater::init(app.handle())` in `setup`, and the
//! commands `updater::check_for_updates` / `updater::install_update`
//! registered in the invoke handler.

use std::cmp::Ordering;
use std::time::Duration;

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_updater::UpdaterExt;

/// Event emitted to the WebView when a *background* check finds an
/// update. Payload: [`UpdateAvailablePayload`]. A foreground call to
/// [`check_for_updates`] returns the same information directly instead
/// of emitting.
pub const UPDATE_AVAILABLE_EVENT: &str = "updater://update-available";

/// Delay before the first background check after launch (§11.4:
/// "check on launch" — deferred briefly so startup, sidecar spawn and
/// the first paint are never competing with an update check).
const FIRST_CHECK_DELAY: Duration = Duration::from_secs(30);

/// Interval between background checks while tray-resident (§11.4).
const CHECK_INTERVAL: Duration = Duration::from_secs(6 * 60 * 60);

/// Serialisable result of an update check / install attempt.
///
/// `state` is exactly one of:
///   * `"up_to_date"` — no newer, valid update is available;
///   * `"available"`  — a newer, signature-verifiable update exists
///     (`version` / `notes` describe it). Nothing has been installed;
///   * `"error"`      — the check/install failed or was refused
///     (`detail` says why; e.g. `"non_monotonic_version"`). Fail-closed:
///     an `"error"` result must never be treated as permission to
///     install anything.
#[derive(Debug, Clone, Serialize)]
pub struct UpdateCheckResult {
    pub state: String,
    pub version: Option<String>,
    pub notes: Option<String>,
    pub detail: Option<String>,
}

impl UpdateCheckResult {
    fn up_to_date() -> Self {
        Self {
            state: "up_to_date".to_string(),
            version: None,
            notes: None,
            detail: None,
        }
    }

    fn available(version: String, notes: Option<String>) -> Self {
        Self {
            state: "available".to_string(),
            version: Some(version),
            notes,
            detail: None,
        }
    }

    fn error(detail: impl Into<String>) -> Self {
        Self {
            state: "error".to_string(),
            version: None,
            notes: None,
            detail: Some(detail.into()),
        }
    }
}

/// Payload for [`UPDATE_AVAILABLE_EVENT`].
#[derive(Debug, Clone, Serialize)]
struct UpdateAvailablePayload {
    version: String,
    notes: Option<String>,
}

// ---------------------------------------------------------------------------
// Monotonic version guard
// ---------------------------------------------------------------------------

/// Parse a dotted-numeric version (`"1"`, `"1.4"`, `"1.4.2"`, `"v1.4.2"`)
/// into its numeric components.
///
/// No `semver` crate is a dependency of this crate (and `Cargo.toml` is
/// not this module's to change), so this is a small, strict parser: an
/// optional leading `v`/`V` is stripped, the remainder is split on `.`,
/// and each component contributes its leading run of ASCII digits (so a
/// suffix such as `2-beta` contributes `2`). A component with no leading
/// digit at all, or an empty version string, makes the whole parse fail
/// (`None`) — callers treat an unparseable version as an error, never
/// as "newer".
fn parse_dotted_version(raw: &str) -> Option<Vec<u64>> {
    let trimmed = raw.trim();
    let trimmed = trimmed
        .strip_prefix('v')
        .or_else(|| trimmed.strip_prefix('V'))
        .unwrap_or(trimmed);
    if trimmed.is_empty() {
        return None;
    }
    let mut parts = Vec::new();
    for component in trimmed.split('.') {
        let digits: String = component.chars().take_while(|c| c.is_ascii_digit()).collect();
        if digits.is_empty() {
            return None;
        }
        parts.push(digits.parse::<u64>().ok()?);
    }
    Some(parts)
}

/// Compare two dotted-numeric versions, component by component, with
/// missing trailing components treated as `0` (`1.4` == `1.4.0`).
/// Returns `None` if either side is unparseable.
fn compare_versions(a: &str, b: &str) -> Option<Ordering> {
    let a_parts = parse_dotted_version(a)?;
    let b_parts = parse_dotted_version(b)?;
    let len = a_parts.len().max(b_parts.len());
    for i in 0..len {
        let av = a_parts.get(i).copied().unwrap_or(0);
        let bv = b_parts.get(i).copied().unwrap_or(0);
        match av.cmp(&bv) {
            Ordering::Equal => continue,
            other => return Some(other),
        }
    }
    Some(Ordering::Equal)
}

/// The monotonic guard (§11.4 / §11.6): an offered version is acceptable
/// only if it is *strictly greater* than the currently running version.
/// Equal, older, or unparseable offered versions are all refused —
/// fail-closed. (The one sanctioned exception in the roadmap, a signed
/// emergency downgrade via dynamic manifest in §11.6, is a server-side
/// emergency procedure, not something this client enables by default;
/// a permanently downgrade-friendly client is a security liability.)
fn is_strictly_newer(offered: &str, current: &str) -> bool {
    matches!(compare_versions(offered, current), Some(Ordering::Greater))
}

/// The running app's version, as the monotonic guard compares against.
fn current_version(app: &AppHandle) -> String {
    app.package_info().version.to_string()
}

// ---------------------------------------------------------------------------
// Check
// ---------------------------------------------------------------------------

/// Perform one update check and classify the outcome.
///
/// Shared by the background policy task and the `check_for_updates`
/// command so both enforce exactly the same rules — in particular the
/// monotonic guard runs *before* anything is reported as "available",
/// and any plugin error (network, manifest parse, signature) becomes
/// `state: "error"` with the failure in `detail`. This function never
/// downloads or installs anything.
async fn perform_check(app: &AppHandle) -> UpdateCheckResult {
    let updater = match app.updater() {
        Ok(updater) => updater,
        Err(e) => return UpdateCheckResult::error(format!("updater_unavailable: {e}")),
    };

    match updater.check().await {
        Ok(Some(update)) => {
            let current = current_version(app);
            if !is_strictly_newer(&update.version, &current) {
                // Refuse equal/older/unparseable offers. The plugin
                // itself normally only returns newer versions; this
                // guard is defence-in-depth against a compromised or
                // misconfigured endpoint serving a rollback (§11.6).
                return UpdateCheckResult::error("non_monotonic_version");
            }
            UpdateCheckResult::available(update.version.clone(), update.body.clone())
        }
        Ok(None) => UpdateCheckResult::up_to_date(),
        Err(e) => UpdateCheckResult::error(format!("check_failed: {e}")),
    }
}

/// Command: check for an update now, without installing anything.
///
/// Returns an [`UpdateCheckResult`]; see its docs for the state
/// contract. The frontend typically calls this from the Settings /
/// dashboard "Check for updates" affordance and from the update card
/// shown after a background [`UPDATE_AVAILABLE_EVENT`].
#[tauri::command]
pub async fn check_for_updates(app: AppHandle) -> UpdateCheckResult {
    perform_check(&app).await
}

// ---------------------------------------------------------------------------
// Install
// ---------------------------------------------------------------------------

/// Command: download and install the currently offered update, then
/// relaunch the app.
///
/// CONTRACT — the idle gate lives in the UI, this command stays thin:
/// per §11.4 an update must **never** kill work in flight (a render, a
/// TTS job, an upload). The frontend must only invoke this command
/// when the job queue is idle, or after the user accepts the
/// non-modal prompt "Update ready — will install when the current
/// video finishes" and the queue has since drained. This command
/// trusts that gate; it does not (and, as a thin shell command, cannot
/// reliably) second-guess the sidecar's queue state.
///
/// CONTRACT — post-relaunch health check / Rollback is frontend-owned:
/// after the relaunch below, the shell brings the sidecar up as usual
/// and the frontend health-checks it (`/health` with the per-launch
/// token). A failed post-update health check must surface a
/// **Rollback** action to the user (§11.4) instead of a dead app;
/// the rollback mechanics themselves (forward-fix repackage /
/// retained previous release, §11.6) are a release-pipeline concern,
/// not this command's.
///
/// Safety properties enforced here, in order:
///   1. Re-check (never install on a stale earlier result);
///   2. Re-verify the monotonic guard against the fresh offer;
///   3. `download_and_install` — the plugin verifies the artefact's
///      minisign signature against the embedded pubkey as part of
///      this call; a signature failure errors out and nothing is
///      installed;
///   4. Only on success, relaunch via the process plugin
///      (`AppHandle::restart`, the Tauri 2 idiomatic equivalent gated
///      by the `process:allow-restart` capability).
/// Any failure before step 4 returns `state: "error"` and the running
/// app is left completely untouched.
#[tauri::command]
pub async fn install_update(app: AppHandle) -> UpdateCheckResult {
    let updater = match app.updater() {
        Ok(updater) => updater,
        Err(e) => return UpdateCheckResult::error(format!("updater_unavailable: {e}")),
    };

    let update = match updater.check().await {
        Ok(Some(update)) => update,
        Ok(None) => return UpdateCheckResult::up_to_date(),
        Err(e) => return UpdateCheckResult::error(format!("check_failed: {e}")),
    };

    let current = current_version(&app);
    if !is_strictly_newer(&update.version, &current) {
        return UpdateCheckResult::error("non_monotonic_version");
    }

    // No progress UI is wired in the shell: on Windows the configured
    // `installMode: "passive"` shows install progress without needing
    // user interaction (§11.4), so both plugin callbacks are no-ops.
    if let Err(e) = update
        .download_and_install(|_chunk_length, _content_length| {}, || {})
        .await
    {
        return UpdateCheckResult::error(format!("install_failed: {e}"));
    }

    // The update is on disk; relaunch so the new binary runs.
    // `AppHandle::restart` (the process-plugin relaunch) diverges —
    // it terminates this process and never returns on success — and
    // its `!` type satisfies this command's return type.
    app.restart()
}

// ---------------------------------------------------------------------------
// Background policy task
// ---------------------------------------------------------------------------

/// Start the background update policy (§11.4): first check
/// [`FIRST_CHECK_DELAY`] after launch, then every [`CHECK_INTERVAL`]
/// for the life of the process (the app is tray-resident, so this
/// keeps running while the window is hidden).
///
/// Called once from `lib.rs` `setup` as `updater::init(app.handle())`.
///
/// A background check that finds a valid, strictly-newer update emits
/// [`UPDATE_AVAILABLE_EVENT`] (`{ version, notes }`) to the WebView;
/// the frontend turns that into the non-modal update card described
/// in §11.4. Background checks **never** download or install on their
/// own — installation happens only via [`install_update`], invoked by
/// the frontend once the user/idle conditions are met. Check failures
/// (offline machine, placeholder endpoint, bad signature) are silent
/// in the background by design: they are retried on the next interval
/// and are only surfaced when the user runs a foreground check.
pub fn init(app: &AppHandle) {
    let handle = app.clone();
    // A plain OS thread + `block_on` is used instead of an async task
    // so this module needs no direct `tokio` dependency (none is
    // declared in `Cargo.toml`): the cadence is two sleeps and one
    // awaited check per cycle, nothing latency-sensitive.
    std::thread::spawn(move || {
        std::thread::sleep(FIRST_CHECK_DELAY);
        loop {
            let result = tauri::async_runtime::block_on(perform_check(&handle));
            if result.state == "available" {
                let payload = UpdateAvailablePayload {
                    version: result.version.clone().unwrap_or_default(),
                    notes: result.notes.clone(),
                };
                // Best-effort: if no WebView is listening yet, the
                // next foreground check will rediscover the update.
                let _ = handle.emit(UPDATE_AVAILABLE_EVENT, payload);
            }
            std::thread::sleep(CHECK_INTERVAL);
        }
    });
}
