/**
 * Mission Control live queries (Round 2 integration).
 *
 * Endpoint → panel mapping:
 * - GET /queue/stats   → stat cards (running / queued jobs)
 * - GET /projects      → "Now Producing" + review / published counts
 * - GET /usage/today   → "Today's cost" stat card
 * - GET /usage/quota   → "YouTube Quota" panel (Round 3)
 * - GET /channels      → "Connected Channels" mini list (key ["channels"])
 *
 * The backend speaks snake_case on the wire while `src/api/types.ts`
 * describes the domain in camelCase, and `ApiClient` does not translate
 * project/channel rows — so every response is normalised here, once,
 * into the small shapes the screen renders. Both spellings are accepted
 * defensively; unknown fields are ignored, never invented.
 */

import { useQuery } from "@tanstack/react-query";
import { getSharedApiClient } from "../client";
import { getBackendConnection } from "../tauri";
import { ApiError } from "../types";
import type { QuotaUsage } from "../types";

// ── Client access ─────────────────────────────────────────────────────────

// Shared client, rebuilt automatically when the backend connection
// changes (restart = new port/token) — see getSharedApiClient.
function getApiClient() {
  return getSharedApiClient();
}

/** True when the failure means "backend unreachable", not a 4xx/5xx. */
export function isOfflineError(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.status === 0 || error.code === "NETWORK_ERROR")
  );
}

// ── Wire helpers ──────────────────────────────────────────────────────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

/** Backend timestamps arrive as ms-epoch numbers (DB) or ISO strings. */
function toMs(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? null : parsed;
  }
  return null;
}

// ── Queue stats — GET /queue/stats ────────────────────────────────────────

/**
 * Real wire shape (backend/app/routers/jobs.py `_stats`):
 * `{ by_status: { queued, leased, running, done, … }, running, queued }`.
 * `running` already includes leased jobs. The `pools` shape described by
 * the shared `QueueStats` type is tolerated as a fallback.
 */
export interface QueueStatsData {
  byStatus: Record<string, number>;
  running: number;
  queued: number;
}

function normalizeQueueStats(raw: unknown): QueueStatsData {
  const r = isRecord(raw) ? raw : {};
  if (isRecord(r.by_status)) {
    const byStatus: Record<string, number> = {};
    for (const [key, value] of Object.entries(r.by_status)) {
      const n = num(value);
      if (n !== null) byStatus[key] = n;
    }
    return {
      byStatus,
      running:
        num(r.running) ?? (byStatus.running ?? 0) + (byStatus.leased ?? 0),
      queued: num(r.queued) ?? byStatus.queued ?? 0,
    };
  }
  if (isRecord(r.pools)) {
    let running = 0;
    let queued = 0;
    for (const pool of Object.values(r.pools)) {
      if (isRecord(pool)) {
        running += num(pool.running) ?? 0;
        queued += num(pool.queued) ?? 0;
      }
    }
    return { byStatus: {}, running, queued };
  }
  return {
    byStatus: {},
    running: num(r.running) ?? 0,
    queued: num(r.queued) ?? 0,
  };
}

export function useQueueStats() {
  return useQuery({
    queryKey: ["queue", "stats"],
    queryFn: async (): Promise<QueueStatsData> => {
      const client = await getApiClient();
      return normalizeQueueStats(await client.queueStats());
    },
    refetchInterval: 20_000,
    retry: 1,
  });
}

// ── Projects — GET /projects ──────────────────────────────────────────────

/**
 * Project statuses per the backend schema (schema_ddl.py):
 * draft, in_production, awaiting_review, qa, ready, scheduled,
 * published, failed, blocked, cancelled.
 * "Now Producing" = actively being worked on (in_production + qa);
 * awaiting_review waits on a human, so it feeds its own stat card.
 */
export const IN_PRODUCTION_STATUSES: readonly string[] = ["in_production", "qa"];
export const AWAITING_REVIEW_STATUS = "awaiting_review";
export const PUBLISHED_STATUS = "published";

export interface MissionProject {
  id: string;
  /** The video's working title — the backend stores it as `topic`. */
  title: string;
  status: string;
  phase: string | null;
  targetMinutes: number | null;
  totalCostMicros: number;
  createdAtMs: number | null;
  publishedAtMs: number | null;
}

function normalizeProject(raw: unknown): MissionProject {
  const r = isRecord(raw) ? raw : {};
  return {
    id: str(r.id) ?? "",
    title:
      str(r.topic) ?? str(r.title) ?? str(r.name) ?? "Untitled project",
    status: str(r.status) ?? "draft",
    phase: str(r.current_phase) ?? str(r.currentPhase) ?? str(r.phase),
    targetMinutes: num(r.target_minutes) ?? num(r.targetMinutes),
    totalCostMicros: num(r.total_cost_micros) ?? num(r.totalCostMicros) ?? 0,
    createdAtMs: toMs(r.created_at ?? r.createdAt),
    publishedAtMs: toMs(r.published_at ?? r.publishedAt),
  };
}

export function useMissionProjects() {
  return useQuery({
    queryKey: ["projects", "mission"],
    queryFn: async (): Promise<MissionProject[]> => {
      const client = await getApiClient();
      const page = await client.listProjects({ limit: 200 });
      return (page.items as unknown[]).map(normalizeProject);
    },
    refetchInterval: 30_000,
    retry: 1,
  });
}

// ── Usage today — GET /usage/today ────────────────────────────────────────

/**
 * `ApiClient` has no method for /usage/today, so this file fetches it
 * directly, following the same protocol as client.ts: `X-Local-Token`
 * header, `{ error: { code, message } }` envelope → ApiError.
 *
 * Real wire shape (backend/app/routers/usage.py):
 * `{ date, totals: { calls, tokens_in, tokens_out, characters, images,
 * cost_micros, est_usd } }` — `totals` is `{}` when nothing was recorded.
 */
export interface UsageTodayData {
  date: string | null;
  hasData: boolean;
  calls: number;
  costMicros: number;
  estUsd: number;
}

function normalizeUsageToday(raw: unknown): UsageTodayData {
  const r = isRecord(raw) ? raw : {};
  const totals = isRecord(r.totals) ? r.totals : {};
  const costMicros = num(totals.cost_micros) ?? 0;
  return {
    date: str(r.date),
    hasData: Object.keys(totals).length > 0,
    calls: num(totals.calls) ?? 0,
    costMicros,
    estUsd: num(totals.est_usd) ?? costMicros / 1_000_000,
  };
}

async function fetchUsageToday(): Promise<UsageTodayData> {
  const conn = await getBackendConnection();
  const url = `${conn.baseUrl.replace(/\/+$/, "")}/usage/today`;
  let res: Response;
  try {
    res = await fetch(url, {
      headers: { "X-Local-Token": conn.token },
    });
  } catch (err) {
    throw new ApiError({
      code: "NETWORK_ERROR",
      message: err instanceof Error ? err.message : "Network request failed",
      status: 0,
      retryable: true,
    });
  }
  const text = typeof res.text === "function" ? await res.text() : "";
  let body: unknown;
  try {
    body = text ? (JSON.parse(text) as unknown) : undefined;
  } catch {
    body = undefined;
  }
  if (!res.ok) {
    const envelope =
      isRecord(body) && isRecord(body.error) ? body.error : null;
    throw new ApiError({
      code: str(envelope?.code) ?? `HTTP_${res.status}`,
      message: str(envelope?.message) ?? `Request failed (${res.status})`,
      status: res.status,
      retryable: res.status === 429 || res.status >= 500,
    });
  }
  return normalizeUsageToday(body);
}

export function useUsageToday() {
  return useQuery({
    queryKey: ["usage", "today"],
    queryFn: fetchUsageToday,
    refetchInterval: 30_000,
    retry: 1,
  });
}

// ── Quota — GET /usage/quota ──────────────────────────────────────────────

/**
 * Round 3 quota contract:
 * `{ buckets: { uploads: {used, limit: 100}, search: {used, limit: 100},
 *    shared: {used, limit: 10000} }, pacific_date, resets_at,
 *    seconds_until_reset }` — normalised by `ApiClient.getQuota`.
 * Keyed under ["usage", …] so `quota.*` WS events (liveSync maps the
 * quota namespace to ["usage"]) refresh it automatically.
 */
export function useQuota() {
  return useQuery({
    queryKey: ["usage", "quota"],
    queryFn: async (): Promise<QuotaUsage> => {
      const client = await getApiClient();
      return client.getQuota();
    },
    refetchInterval: 60_000,
    retry: 1,
  });
}

// ── Channels — GET /channels ──────────────────────────────────────────────

export interface ChannelMini {
  id: string;
  title: string;
  handle: string | null;
  /** Raw backend status, e.g. "active" | "paused" | "disconnected". */
  status: string | null;
}

/** Normalises one raw channel row (snake_case wire / camelCase types). */
export function toChannelMini(raw: unknown): ChannelMini {
  const r = isRecord(raw) ? raw : {};
  return {
    id: str(r.id) ?? "",
    title: str(r.title) ?? str(r.name) ?? "Untitled channel",
    handle: str(r.handle),
    status: str(r.status) ?? str(r.state),
  };
}

/**
 * Shares the ["channels"] key with the Channels screen and the wizard.
 * Their queryFns cache an ARRAY of raw channel rows, so this hook does
 * the same (the page's `items`, unmodified) — one cache shape for every
 * consumer; rows are normalised at render time via {@link toChannelMini}.
 */
export function useMissionChannels() {
  return useQuery({
    queryKey: ["channels"],
    queryFn: async (): Promise<unknown[]> => {
      const client = await getApiClient();
      const page = await client.listChannels();
      return page.items as unknown[];
    },
    refetchInterval: 30_000,
    retry: 1,
  });
}
