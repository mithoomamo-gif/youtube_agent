/**
 * Tauri IPC bridge for backend connection + lifecycle (roadmap §4.5–4.7).
 *
 * The Rust shell owns the sidecar lifecycle and holds the per-launch
 * connection info in managed state. The WebView learns
 * `{ base_url, ws_url, token }` ONLY via the `get_backend_connection`
 * IPC command — never from the bundle, never from a URL, and the token
 * is never persisted (no localStorage / sessionStorage anywhere in
 * this app). The token lives in this module's memory only, for the
 * life of the page; a reload re-fetches it over IPC.
 *
 * Fixed shell contract (implemented by the Rust side):
 *   invoke("get_backend_connection") -> { base_url, ws_url, token } | null
 *                                       (null until the backend is READY)
 *   invoke("get_backend_status")     -> { state, detail, exit_code,
 *                                         boot_id, restart_count }
 *   invoke("retry_backend_start")    -> void
 *   invoke("pause_all_jobs") / invoke("stop_all_jobs") -> void
 *   invoke("open_logs_folder") / invoke("open_output_folder") -> void
 *   invoke("check_for_updates")      -> UpdateCheckResult (see below)
 *   invoke("install_update")         -> UpdateCheckResult
 *   invoke("autostart_get")          -> boolean
 *   invoke("autostart_set", { enabled }) -> void (may reject: string)
 *   events: "backend://status-changed"     (status payload)
 *           "backend://connection-changed" (connection payload)
 *           "updater://update-available"   ({ version, notes })
 *
 * The reactive bootstrap that consumes this module lives in
 * src/api/bootstrap.ts; this file is the raw IPC + connection holder.
 */

import type { BackendConnection } from "./types";

declare global {
  interface Window {
    /** Set by the Tauri runtime inside the desktop WebView only. */
    __TAURI_INTERNALS__?: unknown;
  }
}

/** True only inside the Tauri desktop WebView. */
export function isTauriRuntime(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.__TAURI_INTERNALS__ !== "undefined"
  );
}

// ── Wire shapes (snake_case, exactly as the shell sends them) ─────────────

interface RawBackendConnection {
  base_url?: string;
  ws_url?: string;
  token?: string;
  baseUrl?: string;
  wsUrl?: string;
}

export type BackendStatusState =
  | "stopped"
  | "spawning"
  | "starting"
  | "ready"
  | "degraded"
  | "draining"
  | "stopping"
  | "crashed"
  | "failed";

export interface BackendStatus {
  state: BackendStatusState;
  detail: string | null;
  exitCode: number | null;
  bootId: string | null;
  restartCount: number;
}

interface RawBackendStatus {
  state?: string;
  detail?: string | null;
  exit_code?: number | null;
  exitCode?: number | null;
  boot_id?: string | null;
  bootId?: string | null;
  restart_count?: number;
  restartCount?: number;
}

const BACKEND_STATES: ReadonlySet<string> = new Set([
  "stopped",
  "spawning",
  "starting",
  "ready",
  "degraded",
  "draining",
  "stopping",
  "crashed",
  "failed",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/**
 * Normalises a shell connection payload to the domain shape. The
 * current (pre-contract) Rust stub returns origin-only URLs
 * (`http://127.0.0.1:{port}` with no path); the frontend's contract is
 * that baseUrl carries `/api/v1` and wsUrl carries `/api/v1/ws`, so a
 * path-less URL is completed here instead of failing downstream.
 */
export function normalizeBackendConnection(raw: unknown): BackendConnection | null {
  // Tolerate a `{ connection: {...} }` wrapping, just in case.
  const candidate =
    isRecord(raw) && isRecord(raw.connection) ? raw.connection : raw;
  if (!isRecord(candidate)) return null;
  const r = candidate as RawBackendConnection;
  const rawBase = r.base_url ?? r.baseUrl;
  const rawWs = r.ws_url ?? r.wsUrl;
  const token = r.token;
  if (!rawBase || !rawWs || !token) return null;
  return {
    baseUrl: withPath(rawBase, "/api/v1"),
    wsUrl: withPath(rawWs, "/api/v1/ws"),
    token,
  };
}

function withPath(url: string, path: string): string {
  try {
    const parsed = new URL(url);
    if (parsed.pathname === "" || parsed.pathname === "/") {
      parsed.pathname = path;
    }
    return parsed.toString().replace(/\/+$/, "");
  } catch {
    return url.replace(/\/+$/, "");
  }
}

export function normalizeBackendStatus(raw: unknown): BackendStatus | null {
  const candidate =
    isRecord(raw) && isRecord(raw.status) ? raw.status : raw;
  if (!isRecord(candidate)) return null;
  const r = candidate as RawBackendStatus;
  const state = typeof r.state === "string" ? r.state.toLowerCase() : "";
  if (!BACKEND_STATES.has(state)) return null;
  const exit = r.exit_code ?? r.exitCode;
  const boot = r.boot_id ?? r.bootId;
  const restarts = r.restart_count ?? r.restartCount;
  return {
    state: state as BackendStatusState,
    detail: typeof r.detail === "string" && r.detail ? r.detail : null,
    exitCode: typeof exit === "number" ? exit : null,
    bootId: typeof boot === "string" && boot ? boot : null,
    restartCount: typeof restarts === "number" ? restarts : 0,
  };
}

// ── IPC helpers ───────────────────────────────────────────────────────────

async function tauriInvoke<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<T>(command, args);
}

/**
 * Fetches the connection straight from the shell (no caching).
 * Returns null when the shell reports "not ready yet", when the
 * payload is unusable, or when the command does not exist yet.
 * Throws only when not running under Tauri (a programming error).
 */
export async function fetchBackendConnectionFromShell(): Promise<BackendConnection | null> {
  if (!isTauriRuntime()) {
    throw new Error("fetchBackendConnectionFromShell called outside Tauri");
  }
  try {
    const raw = await tauriInvoke<RawBackendConnection | null>("get_backend_connection");
    return normalizeBackendConnection(raw);
  } catch (err) {
    console.warn("[api] get_backend_connection failed", err);
    return null;
  }
}

/**
 * Fetches the shell's lifecycle status. Returns null outside Tauri.
 * Throws when the command is missing/fails under Tauri so the
 * bootstrap can distinguish "legacy shell without the contract" from
 * a real status.
 */
export async function fetchBackendStatusFromShell(): Promise<BackendStatus | null> {
  if (!isTauriRuntime()) return null;
  const raw = await tauriInvoke<RawBackendStatus | null>("get_backend_status");
  return normalizeBackendStatus(raw);
}

/** Asks the shell to (re)start the backend after a failure. */
export async function retryBackendStart(): Promise<void> {
  await tauriInvoke<void>("retry_backend_start");
}

/** Opens the folder containing the backend log files. */
export async function openLogsFolder(): Promise<void> {
  await tauriInvoke<void>("open_logs_folder");
}

/** Opens the folder containing produced videos/outputs. */
export async function openOutputFolder(): Promise<void> {
  await tauriInvoke<void>("open_output_folder");
}

// ── Updater (src-tauri/src/updater.rs) + autostart (features.rs) ─────────
//
// Exact shell contract, read from the Rust side:
//   check_for_updates(app) -> UpdateCheckResult   (never installs)
//   install_update(app)    -> UpdateCheckResult   (re-checks, verifies the
//                             monotonic guard + signature, installs, then
//                             relaunches — on success it diverges and the
//                             promise may never settle)
//   autostart_get(app)     -> Result<bool, String>
//   autostart_set(app, enabled: bool) -> Result<(), String>
//   event "updater://update-available" -> { version: string,
//                                            notes: string | null }
//                             (background checks only; first at 30 s,
//                             then every 6 h; never auto-installs)
//
// UpdateCheckResult.state is exactly one of "up_to_date" | "available" |
// "error" (fail-closed: an "error" result is never permission to install).

export type UpdateCheckState = "up_to_date" | "available" | "error";

export interface UpdateCheckResult {
  state: UpdateCheckState;
  version: string | null;
  notes: string | null;
  detail: string | null;
}

/** Payload of the `updater://update-available` background event. */
export interface UpdateAvailablePayload {
  version: string;
  notes: string | null;
}

interface RawUpdateCheckResult {
  state?: string;
  version?: string | null;
  notes?: string | null;
  detail?: string | null;
}

const UPDATE_STATES: ReadonlySet<string> = new Set([
  "up_to_date",
  "available",
  "error",
]);

function updateError(detail: string): UpdateCheckResult {
  return { state: "error", version: null, notes: null, detail };
}

/**
 * Normalises a shell update payload to the domain shape. Anything
 * unrecognised becomes a fail-closed "error" result — mirroring the
 * Rust side, which never reports an unknown state as installable.
 */
export function normalizeUpdateCheckResult(raw: unknown): UpdateCheckResult {
  if (!isRecord(raw)) return updateError("invalid_update_response");
  const r = raw as RawUpdateCheckResult;
  const state = typeof r.state === "string" ? r.state : "";
  if (!UPDATE_STATES.has(state)) return updateError("invalid_update_response");
  return {
    state: state as UpdateCheckState,
    version: typeof r.version === "string" && r.version ? r.version : null,
    notes: typeof r.notes === "string" && r.notes ? r.notes : null,
    detail: typeof r.detail === "string" && r.detail ? r.detail : null,
  };
}

/** Normalises an `updater://update-available` event payload, or null. */
export function normalizeUpdateAvailablePayload(
  raw: unknown,
): UpdateAvailablePayload | null {
  if (!isRecord(raw)) return null;
  const version = (raw as { version?: unknown }).version;
  if (typeof version !== "string" || !version) return null;
  const notes = (raw as { notes?: unknown }).notes;
  return {
    version,
    notes: typeof notes === "string" && notes ? notes : null,
  };
}

/**
 * Checks for an app update now, without installing anything.
 * Outside Tauri — and on any IPC failure — this resolves to an honest
 * fail-closed "error" result instead of throwing, so callers can
 * render the outcome directly.
 */
export async function checkForUpdates(): Promise<UpdateCheckResult> {
  if (!isTauriRuntime()) {
    return updateError("updater_unavailable: not running in the desktop app");
  }
  try {
    const raw = await tauriInvoke<RawUpdateCheckResult>("check_for_updates");
    return normalizeUpdateCheckResult(raw);
  } catch (err) {
    return updateError(
      `check_failed: ${err instanceof Error ? err.message : String(err)}`,
    );
  }
}

/**
 * Downloads and installs the currently offered update, then relaunches
 * the app (the shell re-checks and re-verifies first; install follows
 * the shell's passive flow on Windows). Call ONLY when the job queue
 * is idle or the user has accepted installing — per the Rust command's
 * contract the idle gate lives in the UI. On success the app restarts,
 * so the returned promise may never settle.
 */
export async function installUpdate(): Promise<UpdateCheckResult> {
  if (!isTauriRuntime()) {
    return updateError("updater_unavailable: not running in the desktop app");
  }
  try {
    const raw = await tauriInvoke<RawUpdateCheckResult>("install_update");
    return normalizeUpdateCheckResult(raw);
  } catch (err) {
    return updateError(
      `install_failed: ${err instanceof Error ? err.message : String(err)}`,
    );
  }
}

/**
 * Whether "Start with Windows" is enabled for this user
 * (`autostart_get`). Returns null outside Tauri or when the shell
 * command fails — callers must render "unknown", never guess a state.
 */
export async function autostartGet(): Promise<boolean | null> {
  if (!isTauriRuntime()) return null;
  try {
    const value = await tauriInvoke<boolean>("autostart_get");
    return typeof value === "boolean" ? value : null;
  } catch (err) {
    console.warn("[api] autostart_get failed", err);
    return null;
  }
}

/**
 * Enables/disables "Start with Windows" (`autostart_set`). Rejects
 * with the shell's error string on failure; outside Tauri it rejects
 * immediately (a programming error to call it there — the Settings
 * card disables itself in that mode).
 */
export async function autostartSet(enabled: boolean): Promise<void> {
  if (!isTauriRuntime()) {
    throw new Error("autostartSet called outside Tauri");
  }
  await tauriInvoke<void>("autostart_set", { enabled });
}

/**
 * The running app's version, via the Tauri app API (`getVersion`,
 * allowed by the `core:default` capability). Returns null outside
 * Tauri or when the call fails — the UI then falls back to the
 * backend's reported app version, or says the version is unavailable.
 */
export async function getAppVersion(): Promise<string | null> {
  if (!isTauriRuntime()) return null;
  try {
    const { getVersion } = await import("@tauri-apps/api/app");
    const version = await getVersion();
    return typeof version === "string" && version ? version : null;
  } catch (err) {
    console.warn("[api] getVersion failed", err);
    return null;
  }
}

/** Pauses all running jobs via the shell (may reject with a string). */
export async function pauseAllJobs(): Promise<void> {
  await tauriInvoke<void>("pause_all_jobs");
}

/** Stops all running jobs via the shell (may reject with a string). */
export async function stopAllJobs(): Promise<void> {
  await tauriInvoke<void>("stop_all_jobs");
}

// ── Dev connection (plain-browser `vite dev` only) ────────────────────────

interface ViteDevEnv {
  VITE_YTA_PORT?: string;
  VITE_YTA_TOKEN?: string;
  VITE_YTA_HOST?: string;
}

/** Builds the dev connection from Vite env, or null when not configured. */
export function connectionFromViteEnv(): BackendConnection | null {
  const env = import.meta.env as unknown as ViteDevEnv;
  const rawPort = (env.VITE_YTA_PORT ?? "").trim();
  if (!rawPort) return null;
  const port = Number(rawPort);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    console.warn(
      `[api] VITE_YTA_PORT=${JSON.stringify(rawPort)} is not a valid port — ignoring dev env connection`,
    );
    return null;
  }
  const host = (env.VITE_YTA_HOST ?? "").trim() || "127.0.0.1";
  const token = (env.VITE_YTA_TOKEN ?? "").trim() || "dev-token";
  return {
    baseUrl: `http://${host}:${port}/api/v1`,
    wsUrl: `ws://${host}:${port}/api/v1/ws`,
    token,
  };
}

/**
 * True only for the plain-browser dev path: NOT under Tauri AND a
 * Vite dev build. See the release-invariant comment in
 * {@link loadBackendConnection} — this is the single gate for it.
 */
export function isDevFallbackAvailable(): boolean {
  return !isTauriRuntime() && import.meta.env.DEV;
}

// ── Connection holder (module memory only) ────────────────────────────────

/**
 * Clearly-unusable marker returned when no real connection exists.
 * Port 0 is never connectable and the empty token fails every auth
 * check, so any code path using it fails fast and honestly.
 */
export const UNUSABLE_CONNECTION: BackendConnection = {
  baseUrl: "http://127.0.0.1:0/api/v1",
  wsUrl: "ws://127.0.0.1:0/api/v1/ws",
  token: "",
};

/** True only for a connection that could actually reach a backend. */
export function isUsableConnection(conn: BackendConnection): boolean {
  if (!conn.token) return false;
  try {
    const port = Number(new URL(conn.baseUrl).port);
    return Number.isInteger(port) && port >= 1 && port <= 65535;
  } catch {
    return false;
  }
}

export function sameConnection(
  a: BackendConnection | null,
  b: BackendConnection | null,
): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return a.baseUrl === b.baseUrl && a.wsUrl === b.wsUrl && a.token === b.token;
}

let currentConnection: BackendConnection | null = null;
let cachedPromise: Promise<BackendConnection> | null = null;

/** Synchronous peek at the in-memory connection (null = none yet). */
export function getCurrentBackendConnection(): BackendConnection | null {
  return currentConnection;
}

/**
 * Installs (or clears) the in-memory connection. Called by the
 * bootstrap when the shell pushes `backend://connection-changed` or a
 * fresh status poll yields a connection. Returns true when the
 * connection actually changed (callers must resync by snapshot).
 */
export function setBackendConnection(conn: BackendConnection | null): boolean {
  const changed = !sameConnection(currentConnection, conn);
  currentConnection = conn && isUsableConnection(conn) ? conn : null;
  cachedPromise = currentConnection
    ? Promise.resolve(currentConnection)
    : null;
  return changed;
}

async function loadBackendConnection(): Promise<BackendConnection> {
  if (isTauriRuntime()) {
    // Desktop path: the shell is the ONLY source of connection info.
    // The dev-env fallback below must never run here — a release
    // WebView that fell back to env/default values could connect to
    // the wrong process with the wrong token.
    const conn = await fetchBackendConnectionFromShell();
    if (conn && isUsableConnection(conn)) {
      currentConnection = conn;
      return conn;
    }
    return { ...UNUSABLE_CONNECTION };
  }

  // ── RELEASE INVARIANT ──────────────────────────────────────────────
  // The Vite dev fallback below is reachable ONLY when both hold:
  //   1. the page is NOT running inside the Tauri WebView
  //      (`window.__TAURI_INTERNALS__` absent), and
  //   2. this is a Vite DEV build (`import.meta.env.DEV`).
  // Vite statically replaces `import.meta.env.DEV` with `false` in
  // production builds and dead-code-eliminates this branch, so the
  // release bundle physically cannot take the dev path — and inside
  // Tauri the branch above returns before this code is reached.
  if (import.meta.env.DEV) {
    const fromEnv = connectionFromViteEnv();
    if (fromEnv) {
      console.info(
        `[api] dev connection from Vite env → ${fromEnv.baseUrl} (see tools/DEV_UI.md)`,
      );
      currentConnection = fromEnv;
      return fromEnv;
    }
  }

  console.error(
    "[api] no backend connection available: not running in Tauri and no usable Vite dev env. " +
      "The UI will report the backend as down. For browser dev, start the sidecar with " +
      "tools/dev-ui.sh and pass the VITE_YTA_* env it prints.",
  );
  return { ...UNUSABLE_CONNECTION };
}

/**
 * Returns the backend connection for this launch. The underlying IPC
 * call happens once per page load; the promise is cached in module
 * scope and shared by every caller. An unusable result is NOT cached,
 * so a later call (after the shell signals readiness) re-fetches.
 */
export function getBackendConnection(): Promise<BackendConnection> {
  if (currentConnection) return Promise.resolve(currentConnection);
  if (!cachedPromise) {
    cachedPromise = loadBackendConnection().then((conn) => {
      if (!isUsableConnection(conn)) cachedPromise = null;
      return conn;
    });
  }
  return cachedPromise;
}

/**
 * Clears the cached connection and re-fetches it. Call this after the
 * shell signals a backend restart (new port/token/boot_id).
 */
export function refreshBackendConnection(): Promise<BackendConnection> {
  currentConnection = null;
  cachedPromise = null;
  return getBackendConnection();
}
