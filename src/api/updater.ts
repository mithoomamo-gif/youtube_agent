/**
 * Updater store + background-event lifecycle (roadmap §11.4).
 *
 * The Rust shell (src-tauri/src/updater.rs) runs the background check
 * policy — first check 30 s after launch, then every 6 h — and emits
 * `updater://update-available` ({ version, notes }) when a check finds
 * a valid, strictly-newer update. Background checks NEVER install.
 *
 * This module is the frontend half, mirroring src/api/bootstrap.ts:
 *
 *   1. subscribes to `updater://update-available` via the Tauri event
 *      API (only under Tauri — in a plain browser / Vite dev build it
 *      never subscribes, so no fake "update available" state can ever
 *      appear there),
 *   2. holds the pending-update state in a zustand store (the app's
 *      shell-level state mechanism — no new state library),
 *   3. owns the foreground check / install actions used by the update
 *      banner and the Settings "Updates" card, keeping their status
 *      text honest: available / up_to_date / error are rendered
 *      exactly as the shell reports them (fail-closed — an "error"
 *      result is never presented as permission to install),
 *   4. tracks the banner dismissal in memory only: dismissing hides
 *      the banner for THIS session; a relaunch (or a newer offer)
 *      shows it again.
 *
 * Install contract (§11.4, from the Rust `install_update` command):
 * the idle gate lives in the UI — install must only be invoked when
 * the user asks for it; on Windows the shell's configured
 * `installMode: "passive"` shows install progress without needing
 * interaction here, and a successful install relaunches the app.
 */

import { create } from "zustand";
import {
  checkForUpdates,
  getAppVersion,
  installUpdate,
  isTauriRuntime,
  normalizeUpdateAvailablePayload,
  type UpdateAvailablePayload,
  type UpdateCheckResult,
} from "./tauri";

/** Foreground check status, as shown on the Settings Updates card. */
export type UpdateCheckStatus =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "done"; result: UpdateCheckResult; checkedAt: number }
  | { kind: "installing" }
  | { kind: "install_done"; result: UpdateCheckResult };

export interface UpdaterState {
  /** Update offered by a background event or a foreground check. */
  pendingUpdate: UpdateAvailablePayload | null;
  /** Version the banner was dismissed for (session-only, in memory). */
  dismissedVersion: string | null;
  /** Foreground check / install status for the Settings card. */
  checkStatus: UpdateCheckStatus;
  /** Running app version (Tauri app API; null = unavailable). */
  currentVersion: string | null;
  currentVersionLoaded: boolean;
}

export const useUpdaterStore = create<UpdaterState>()(() => ({
  pendingUpdate: null,
  dismissedVersion: null,
  checkStatus: { kind: "idle" },
  currentVersion: null,
  currentVersionLoaded: false,
}));

/** True when the banner should be visible for the pending update. */
export function isUpdateBannerVisible(state: UpdaterState): boolean {
  return (
    state.pendingUpdate !== null &&
    state.dismissedVersion !== state.pendingUpdate.version
  );
}

// ── Actions ───────────────────────────────────────────────────────────────

function applyCheckResult(result: UpdateCheckResult): void {
  if (result.state === "available" && result.version) {
    useUpdaterStore.setState({
      pendingUpdate: { version: result.version, notes: result.notes },
    });
  } else if (result.state === "up_to_date") {
    // A foreground "no update" verdict supersedes a stale offer.
    useUpdaterStore.setState({ pendingUpdate: null });
  }
  // "error" deliberately leaves any pending offer untouched: the
  // check failed, it did not prove the offer went away.
}

/** Runs a foreground update check (Settings card / banner refresh). */
export async function checkForUpdatesNow(): Promise<UpdateCheckResult> {
  useUpdaterStore.setState({ checkStatus: { kind: "checking" } });
  const result = await checkForUpdates();
  applyCheckResult(result);
  useUpdaterStore.setState({
    checkStatus: { kind: "done", result, checkedAt: Date.now() },
  });
  return result;
}

/**
 * Installs the pending update via the shell's `install_update`
 * command. On success the app relaunches, so this may never resolve;
 * a resolved "error" result is surfaced honestly on the card/banner.
 */
export async function installPendingUpdate(): Promise<void> {
  useUpdaterStore.setState({ checkStatus: { kind: "installing" } });
  const result = await installUpdate();
  if (result.state === "up_to_date") {
    useUpdaterStore.setState({ pendingUpdate: null });
  }
  useUpdaterStore.setState({
    checkStatus: { kind: "install_done", result },
  });
}

/** Hides the banner for this session only (nothing is persisted). */
export function dismissUpdateBanner(): void {
  const { pendingUpdate } = useUpdaterStore.getState();
  if (pendingUpdate) {
    useUpdaterStore.setState({ dismissedVersion: pendingUpdate.version });
  }
}

/** Loads the running app version once (Tauri app API; no-op outside). */
export async function loadCurrentVersion(): Promise<void> {
  if (useUpdaterStore.getState().currentVersionLoaded) return;
  const version = await getAppVersion();
  useUpdaterStore.setState({
    currentVersion: version,
    currentVersionLoaded: true,
  });
}

// ── Lifecycle (mirrors startBackendBootstrap in bootstrap.ts) ────────────

let started = false;
let unlisten: (() => void) | null = null;

/**
 * Starts listening for `updater://update-available` (singleton; safe
 * under React StrictMode's double-effect). Returns the stop function.
 * Outside Tauri this is a no-op: no subscription, no pending state —
 * the banner can never show in a plain-browser/dev build.
 */
export function startUpdaterListener(): () => void {
  if (started) return stopUpdaterListener;
  started = true;

  if (!isTauriRuntime()) return stopUpdaterListener;

  void (async () => {
    try {
      const { listen } = await import("@tauri-apps/api/event");
      const un = await listen<unknown>(
        "updater://update-available",
        (event) => {
          const payload = normalizeUpdateAvailablePayload(event.payload);
          if (payload) {
            useUpdaterStore.setState({ pendingUpdate: payload });
          }
        },
      );
      if (!started) {
        un();
        return;
      }
      unlisten = un;
    } catch (err) {
      // Event subscription is an optimisation; the Settings card's
      // foreground check rediscovers any update on demand.
      console.warn("[updater] event subscription failed", err);
    }
  })();

  void loadCurrentVersion();

  return stopUpdaterListener;
}

export function stopUpdaterListener(): void {
  started = false;
  if (unlisten) {
    try {
      unlisten();
    } catch {
      // Listener already gone — teardown stays idempotent.
    }
    unlisten = null;
  }
}
