/**
 * Backend bootstrap + lifecycle store (roadmap §4.5–4.7).
 *
 * Single owner of "can the UI show the dashboard?" for the desktop
 * app. It:
 *
 *   1. detects Tauri (`window.__TAURI_INTERNALS__`),
 *   2. invokes `get_backend_status` + `get_backend_connection`,
 *   3. subscribes to `backend://status-changed` and
 *      `backend://connection-changed` (plus a slow safety-net poll —
 *      a missed event must never wedge the app on a splash screen),
 *   4. exposes the reactive {@link useBackendBootstrapStore} consumed
 *      by the BackendGate component,
 *   5. on every connection CHANGE: installs the new connection in the
 *      in-memory holder (src/api/tauri.ts), then resyncs by SNAPSHOT
 *      — all React Query data is invalidated/refetched, never patched
 *      with deltas from the previous backend process (§4.6),
 *   6. once connected, performs the §4.7 version handshake
 *      (`GET /api/v1/system/info`): if the backend's `min_ui_api`
 *      exceeds the UI's API major, the store flips to `out_of_sync`
 *      and the gate blocks with the components-out-of-sync screen.
 *
 * Phase mapping (shell state -> UI phase):
 *   spawning / starting            -> "starting"
 *   draining / stopping            -> "starting" (gate renders the
 *                                     "Shutting down…" interstitial
 *                                     from `shellState`)
 *   ready + usable connection      -> "ready" (after handshake)
 *   degraded + usable connection   -> "degraded" (app usable + banner)
 *   crashed / failed / stopped     -> "failed"
 *   ready/degraded, no connection  -> "starting" (waiting for the
 *                                     connection-changed push)
 *   plain-browser Vite dev         -> "dev" (existing dev path)
 *
 * A handshake that fails or arrives in an unexpected shape degrades
 * gracefully: it is logged, `handshake` becomes "unavailable", and
 * the app continues — only an explicit `min_ui_api` above the UI's
 * API major blocks.
 */

import type { QueryClient } from "@tanstack/react-query";
import { create } from "zustand";
import { ApiClient } from "./client";
import {
  connectionFromViteEnv,
  fetchBackendConnectionFromShell,
  fetchBackendStatusFromShell,
  getCurrentBackendConnection,
  isDevFallbackAvailable,
  isTauriRuntime,
  isUsableConnection,
  normalizeBackendConnection,
  normalizeBackendStatus,
  setBackendConnection,
  type BackendStatus,
  type BackendStatusState,
} from "./tauri";
import type { BackendConnection, SystemInfo } from "./types";

/** The API major this UI speaks (`/api/v1`, WS envelope v1). */
export const UI_API_MAJOR = 1;

export type BackendPhase = "starting" | "ready" | "degraded" | "failed" | "dev";

export type HandshakeState = "pending" | "ok" | "out_of_sync" | "unavailable";

export interface BackendBootstrapState {
  phase: BackendPhase;
  /** Raw shell lifecycle state, when known (null in dev/legacy mode). */
  shellState: BackendStatusState | null;
  /** Plain-language detail from the shell status, if any. */
  detail: string | null;
  exitCode: number | null;
  bootId: string | null;
  restartCount: number;
  /** True when the shell has no `get_backend_status` (pre-contract). */
  legacyShell: boolean;
  /** Increments on every accepted connection change (WS restarts on it). */
  connectionVersion: number;
  handshake: HandshakeState;
  /** Raw min_ui_api reported by the backend, when it reported one. */
  minUiApi: number | null;
  systemInfo: SystemInfo | null;
}

const initialState: Omit<
  BackendBootstrapState,
  "phase"
> = {
  shellState: null,
  detail: null,
  exitCode: null,
  bootId: null,
  restartCount: 0,
  legacyShell: false,
  connectionVersion: 0,
  handshake: "pending",
  minUiApi: null,
  systemInfo: null,
};

export const useBackendBootstrapStore = create<BackendBootstrapState>()(() => ({
  phase: "starting",
  ...initialState,
}));

// ── Query resync registration ─────────────────────────────────────────────

let registeredQueryClient: QueryClient | null = null;

/** Called by the BackendGate so connection changes can resync queries. */
export function registerBootstrapQueryClient(client: QueryClient | null): void {
  registeredQueryClient = client;
}

function resyncBySnapshot(): void {
  if (!registeredQueryClient) return;
  // Snapshot resync (§4.6): throw away the previous backend's view of
  // the world wholesale — invalidate everything, refetch what is on
  // screen. Never patch deltas across a restart.
  void registeredQueryClient.invalidateQueries();
  void registeredQueryClient.refetchQueries({ type: "active" });
}

// ── Handshake (§4.7) ─────────────────────────────────────────────────────

let handshakeSeq = 0;

async function runHandshake(conn: BackendConnection): Promise<void> {
  const seq = ++handshakeSeq;
  useBackendBootstrapStore.setState({ handshake: "pending", minUiApi: null });
  try {
    const info = await new ApiClient(conn).systemInfo();
    if (seq !== handshakeSeq) return; // superseded by a newer connection
    const hasShape =
      typeof info.apiVersion === "number" && info.apiVersion > 0;
    if (!hasShape && info.minUiApi === undefined) {
      // system/info answered but not in the §4.7 shape — log + continue.
      console.warn(
        "[bootstrap] /system/info did not carry the expected version fields; continuing without a version floor check",
        info,
      );
      useBackendBootstrapStore.setState({
        handshake: "unavailable",
        systemInfo: info,
      });
      return;
    }
    if (typeof info.minUiApi === "number" && info.minUiApi > UI_API_MAJOR) {
      console.error(
        `[bootstrap] backend requires UI API >= ${info.minUiApi}, this UI speaks v${UI_API_MAJOR} — components out of sync`,
      );
      useBackendBootstrapStore.setState({
        handshake: "out_of_sync",
        minUiApi: info.minUiApi,
        systemInfo: info,
        bootId: info.bootId || useBackendBootstrapStore.getState().bootId,
      });
      return;
    }
    useBackendBootstrapStore.setState({
      handshake: "ok",
      minUiApi: typeof info.minUiApi === "number" ? info.minUiApi : null,
      systemInfo: info,
      bootId: info.bootId || useBackendBootstrapStore.getState().bootId,
    });
  } catch (err) {
    if (seq !== handshakeSeq) return;
    // No usable system/info at all (404 on an old backend, network
    // blip): degrade gracefully — log and continue unblocked.
    console.warn(
      "[bootstrap] version handshake failed; continuing without it",
      err,
    );
    useBackendBootstrapStore.setState({ handshake: "unavailable" });
  }
}

// ── Applying shell updates ────────────────────────────────────────────────

function applyConnection(conn: BackendConnection | null): void {
  const usable = conn && isUsableConnection(conn) ? conn : null;
  const changed = setBackendConnection(usable);
  if (!usable) {
    handshakeSeq += 1; // any in-flight handshake belongs to a dead conn
    useBackendBootstrapStore.setState({ handshake: "pending" });
    return;
  }
  if (changed) {
    useBackendBootstrapStore.setState((s) => ({
      connectionVersion: s.connectionVersion + 1,
    }));
    resyncBySnapshot();
    void runHandshake(usable);
  }
}

function phaseFor(
  status: Pick<BackendStatus, "state" | "detail" | "exitCode"> | null,
  hasConnection: boolean,
): BackendPhase {
  switch (status?.state) {
    case "spawning":
    case "starting":
    case "draining":
    case "stopping":
      return "starting";
    case "ready":
      return hasConnection ? "ready" : "starting";
    case "degraded":
      return hasConnection ? "degraded" : "starting";
    case "crashed":
    case "failed":
      return "failed";
    case "stopped":
      // The shell's pre-spawn initial status is also "stopped" (no
      // detail, no exit code) — that is a launch still in progress,
      // not a failure. A stop WITH a detail/exit code is terminal.
      return status.detail || status.exitCode !== null ? "failed" : "starting";
    default:
      return hasConnection ? "ready" : "starting";
  }
}

function applyStatus(status: BackendStatus | null): void {
  if (!status) return;
  const hasConnection = getCurrentBackendConnection() !== null;
  useBackendBootstrapStore.setState({
    shellState: status.state,
    detail: status.detail,
    exitCode: status.exitCode,
    bootId: status.bootId ?? useBackendBootstrapStore.getState().bootId,
    restartCount: status.restartCount,
    phase: phaseFor(status, hasConnection),
  });
}

function rederivePhase(): void {
  const s = useBackendBootstrapStore.getState();
  if (s.legacyShell || s.phase === "dev") return;
  useBackendBootstrapStore.setState({
    phase: phaseFor(
      { state: s.shellState as BackendStatusState, detail: s.detail, exitCode: s.exitCode },
      getCurrentBackendConnection() !== null,
    ),
  });
}

// ── Shell refresh cycle ───────────────────────────────────────────────────

let refreshInFlight = false;

async function refreshFromShell(): Promise<void> {
  if (refreshInFlight || !isTauriRuntime()) return;
  refreshInFlight = true;
  try {
    let status: BackendStatus | null = null;
    let statusSupported = true;
    try {
      status = await fetchBackendStatusFromShell();
    } catch (err) {
      // Pre-contract shell: get_backend_status does not exist yet.
      // Fall back to connection-only bootstrapping (a usable
      // connection is treated as READY) and say so in the store.
      statusSupported = false;
      console.warn(
        "[bootstrap] get_backend_status unavailable — legacy shell, connection-only bootstrap",
        err,
      );
    }

    const conn = await fetchBackendConnectionFromShell().catch(() => null);
    applyConnection(conn);
    if (statusSupported) {
      applyStatus(status);
      rederivePhase();
    } else {
      useBackendBootstrapStore.setState((s) => ({
        legacyShell: true,
        phase:
          s.phase === "failed"
            ? "failed"
            : getCurrentBackendConnection()
              ? "ready"
              : "starting",
      }));
    }
  } finally {
    refreshInFlight = false;
  }
}

/** Re-runs the status+connection fetch now (e.g. after Retry). */
export function refreshBootstrapNow(): Promise<void> {
  return refreshFromShell();
}

/** Marks the start of a user-triggered retry (gate shows "starting"). */
export function noteBackendRetry(): void {
  useBackendBootstrapStore.setState({
    phase: "starting",
    shellState: "spawning",
    detail: null,
    exitCode: null,
    handshake: "pending",
  });
}

// ── Lifecycle ─────────────────────────────────────────────────────────────

const POLL_MS = 2_500;

let started = false;
let pollTimer: ReturnType<typeof setInterval> | null = null;
let unlisteners: Array<() => void> = [];

function startDevBootstrap(): void {
  const conn = connectionFromViteEnv();
  if (conn && isUsableConnection(conn)) {
    setBackendConnection(conn);
    useBackendBootstrapStore.setState((s) => ({
      phase: "dev",
      connectionVersion: s.connectionVersion + 1,
    }));
    void runHandshake(conn);
  } else {
    console.error(
      "[bootstrap] browser dev mode but VITE_YTA_PORT is unset/invalid — no backend connection",
    );
    useBackendBootstrapStore.setState({
      phase: "failed",
      detail:
        "No dev backend is configured. Start the sidecar with tools/dev-ui.sh and pass the VITE_YTA_* values it prints.",
    });
  }
}

/**
 * Starts the bootstrap (singleton; safe under React StrictMode's
 * double-effect: the returned stop function fully tears down, and a
 * later start re-subscribes). Returns the stop function.
 */
export function startBackendBootstrap(): () => void {
  if (started) return stopBackendBootstrap;
  started = true;

  if (!isTauriRuntime()) {
    // Plain-browser path — reachable in a bundle only for Vite dev
    // builds (see the release-invariant comment in src/api/tauri.ts).
    if (isDevFallbackAvailable()) {
      startDevBootstrap();
    } else {
      useBackendBootstrapStore.setState({
        phase: "failed",
        detail:
          "The backend engine is only reachable from inside the YouTube Agent desktop app.",
      });
    }
    return stopBackendBootstrap;
  }

  void (async () => {
    try {
      const { listen } = await import("@tauri-apps/api/event");
      const unStatus = await listen<unknown>(
        "backend://status-changed",
        (event) => {
          const status = normalizeBackendStatus(event.payload);
          if (status) {
            applyStatus(status);
            // A READY push may arrive before the connection push;
            // fetch the connection to close the gap.
            if (status.state === "ready" || status.state === "degraded") {
              void refreshFromShell();
            } else {
              rederivePhase();
            }
          }
        },
      );
      const unConn = await listen<unknown>(
        "backend://connection-changed",
        (event) => {
          applyConnection(normalizeBackendConnection(event.payload));
          rederivePhase();
        },
      );
      if (!started) {
        unStatus();
        unConn();
        return;
      }
      unlisteners = [unStatus, unConn];
    } catch (err) {
      // Event subscription is an optimisation; the poll below is the
      // safety net and keeps the app correct without it.
      console.warn("[bootstrap] Tauri event subscription failed; polling only", err);
    }
  })();

  void refreshFromShell();
  pollTimer = setInterval(() => {
    void refreshFromShell();
  }, POLL_MS);

  return stopBackendBootstrap;
}

export function stopBackendBootstrap(): void {
  started = false;
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  for (const unlisten of unlisteners) {
    try {
      unlisten();
    } catch {
      // Listener already gone — teardown stays idempotent.
    }
  }
  unlisteners = [];
}
