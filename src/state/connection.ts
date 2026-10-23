/**
 * Tiny zustand store for backend connection health.
 *
 * Mirrors the sidecar lifecycle states the UI renders against
 * (splash / live / amber "degraded" banner / backend-down). The
 * persistent connection pill reads from here — never a modal.
 *
 * Round 2: the status is DERIVED from two independent signals, never
 * asserted by a single caller:
 *
 *   REST reachable?  (health monitor below: /health/ready, falling
 *                     back to /api/v1/system/info — one success = up)
 *   WS socket state  (fed by src/state/liveSync.ts from WsClient)
 *
 *   REST ok  + WS open                  → "ready"
 *   REST ok  + WS down / reconnecting   → "degraded"
 *   REST down + WS open                 → "degraded" (half-alive)
 *   both failing                        → "down"
 *   nothing checked yet                 → "connecting" (initial)
 *
 * The boot_id of the connected backend is recorded here too, from
 * whichever source reports it first: the monitor's system/info poll
 * or the WS `system.hello_ack` (via liveSync).
 */

import { useEffect } from "react";
import { create } from "zustand";
import { ApiClient } from "../api/client";
import { getBackendConnection, isUsableConnection } from "../api/tauri";

export type ConnectionStatus = "unknown" | "connecting" | "ready" | "degraded" | "down";

/** WebSocket link state as reported by the WsClient (liveSync). */
export type WsLinkState = "unknown" | "connecting" | "open" | "closed";

export interface SystemInfoPatch {
  bootId?: string | null;
  backendVersion?: string | null;
  appVersion?: string | null;
}

export interface ConnectionState {
  /** Derived from `restOk` + `wsState` — see the mapping table above. */
  status: ConnectionStatus;
  /** boot_id of the currently connected backend process, if known. */
  bootId: string | null;
  /** Last REST probe result: true = up, false = down, null = unchecked. */
  restOk: boolean | null;
  /** WebSocket link state, fed by liveSync. */
  wsState: WsLinkState;
  /** Versions from the last successful /system/info, if any. */
  backendVersion: string | null;
  appVersion: string | null;
  /** Epoch ms of the last completed REST probe. */
  lastCheckedAt: number | null;
  /** Legacy direct setter (kept for compatibility); prefer the signal setters. */
  setStatus: (status: ConnectionStatus) => void;
  setBootId: (bootId: string | null) => void;
  setRestOk: (restOk: boolean | null) => void;
  setWsState: (wsState: WsLinkState) => void;
  setSystemInfo: (info: SystemInfoPatch) => void;
}

function deriveStatus(restOk: boolean | null, wsState: WsLinkState): ConnectionStatus {
  if (restOk === true && wsState === "open") return "ready";
  if (restOk === true) return "degraded";
  if (restOk === false && wsState === "open") return "degraded";
  if (restOk === false) return "down";
  // REST not checked yet: a live socket alone is not proof of "ready".
  if (wsState === "open") return "degraded";
  return "connecting";
}

export const useConnectionStore = create<ConnectionState>()((set) => ({
  status: "connecting",
  bootId: null,
  restOk: null,
  wsState: "unknown",
  backendVersion: null,
  appVersion: null,
  lastCheckedAt: null,
  setStatus: (status) => set({ status }),
  setBootId: (bootId) => set({ bootId }),
  setRestOk: (restOk) =>
    set((s) => ({
      restOk,
      lastCheckedAt: restOk === null ? s.lastCheckedAt : Date.now(),
      status: deriveStatus(restOk, s.wsState),
    })),
  setWsState: (wsState) =>
    set((s) => ({ wsState, status: deriveStatus(s.restOk, wsState) })),
  setSystemInfo: (info) =>
    set((s) => ({
      bootId: info.bootId !== undefined ? info.bootId : s.bootId,
      backendVersion:
        info.backendVersion !== undefined ? info.backendVersion : s.backendVersion,
      appVersion: info.appVersion !== undefined ? info.appVersion : s.appVersion,
    })),
}));

/** Hook-free snapshot getter for non-React code (clients, handlers). */
export function getConnectionState(): { status: ConnectionStatus; bootId: string | null } {
  const { status, bootId } = useConnectionStore.getState();
  return { status, bootId };
}

// ── Health monitor ────────────────────────────────────────────────────────

/** REST probe cadence for the connection monitor. */
export const CONNECTION_POLL_MS = 10_000;

/**
 * One REST reachability probe. The health endpoint lives on the root
 * origin (NOT under /api/v1) — ApiClient.healthReady() already knows
 * that. /health/ready can 503 while the process is up (DB down), so a
 * failed probe falls back to /api/v1/system/info: either success
 * means the backend is reachable. A successful info call also records
 * bootId + versions in the store.
 */
export async function checkBackendOnce(): Promise<void> {
  const store = useConnectionStore.getState();

  let conn;
  try {
    conn = await getBackendConnection();
  } catch {
    store.setRestOk(false);
    return;
  }
  if (!isUsableConnection(conn)) {
    // No IPC and no dev env — nothing to probe; honestly down.
    store.setRestOk(false);
    return;
  }

  const client = new ApiClient(conn);
  let restUp = false;
  try {
    await client.healthReady();
    restUp = true;
  } catch {
    restUp = false; // fall through to the system/info fallback
  }

  if (!restUp) {
    try {
      const info = await client.systemInfo();
      restUp = true;
      store.setSystemInfo({
        bootId: info.bootId || null,
        backendVersion: info.backendVersion || null,
        appVersion: info.appVersion || null,
      });
    } catch {
      store.setRestOk(false);
      return;
    }
  } else {
    // Health proved REST is up; record boot/version best-effort.
    try {
      const info = await client.systemInfo();
      store.setSystemInfo({
        bootId: info.bootId || null,
        backendVersion: info.backendVersion || null,
        appVersion: info.appVersion || null,
      });
    } catch {
      // system/info failed but health succeeded — REST is still up.
    }
  }
  store.setRestOk(true);
}

let monitorTimer: ReturnType<typeof setInterval> | null = null;
let monitorInFlight = false;

/**
 * Starts the singleton health monitor (immediate probe, then every
 * ~10s). Returns a stop function; starting twice is a no-op that
 * returns the same stop function.
 */
export function startConnectionMonitor(intervalMs: number = CONNECTION_POLL_MS): () => void {
  if (monitorTimer !== null) return stopConnectionMonitor;
  const tick = () => {
    if (monitorInFlight) return;
    monitorInFlight = true;
    void checkBackendOnce().finally(() => {
      monitorInFlight = false;
    });
  };
  tick();
  monitorTimer = setInterval(tick, intervalMs);
  return stopConnectionMonitor;
}

export function stopConnectionMonitor(): void {
  if (monitorTimer !== null) {
    clearInterval(monitorTimer);
    monitorTimer = null;
  }
}

/** React convenience wrapper around {@link startConnectionMonitor}. */
export function useConnectionMonitor(): void {
  useEffect(() => startConnectionMonitor(), []);
}
