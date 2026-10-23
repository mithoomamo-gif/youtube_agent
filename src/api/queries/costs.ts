/**
 * Costs & Usage live queries (Round 2 integration).
 *
 * Endpoint → panel mapping:
 * - GET /usage         → provider meters + total recorded spend.
 *   Real wire shape (backend/app/routers/usage.py): items grouped BY
 *   PROVIDER ONLY — `{ provider, calls, tokens_in, tokens_out,
 *   characters, images, cost_micros, est_usd }`. The endpoint takes no
 *   date range and returns no per-project or per-day breakdown.
 * - GET /projects      → per-video totals (`total_cost_micros`, topic,
 *   status, target_minutes) for the project-cost table and the
 *   per-published-video / per-minute averages.
 * - GET /usage/today   → today's spend card (fetched directly;
 *   `ApiClient` exposes no method for this route).
 *
 * NOT exposed by the backend, so the screen says so instead of
 * inventing numbers: budget caps (no endpoint returns them — settings
 * is a generic key/value store with no cap keys), YouTube quota
 * buckets, per-category cost breakdowns per video, retry spend, and
 * any month-scoped or projected figures (the ledger has no time
 * series endpoint).
 */

import { useQuery } from "@tanstack/react-query";
import { getSharedApiClient } from "../client";
import { getBackendConnection } from "../tauri";
import { ApiError } from "../types";

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

// ── Usage rollups — GET /usage ────────────────────────────────────────────

export interface UsageRollup {
  provider: string;
  calls: number;
  tokensIn: number;
  tokensOut: number;
  characters: number;
  images: number;
  costMicros: number;
  estUsd: number;
}

function normalizeRollup(raw: unknown): UsageRollup {
  const r = isRecord(raw) ? raw : {};
  const costMicros = num(r.cost_micros) ?? num(r.costMicros) ?? 0;
  return {
    provider: str(r.provider) ?? "unknown",
    calls: num(r.calls) ?? 0,
    tokensIn: num(r.tokens_in) ?? num(r.tokensIn) ?? 0,
    tokensOut: num(r.tokens_out) ?? num(r.tokensOut) ?? 0,
    characters: num(r.characters) ?? 0,
    images: num(r.images) ?? 0,
    costMicros,
    estUsd: num(r.est_usd) ?? num(r.estUsd) ?? costMicros / 1_000_000,
  };
}

export function useUsageRollups() {
  return useQuery({
    queryKey: ["usage", "rollups"],
    queryFn: async (): Promise<UsageRollup[]> => {
      const client = await getApiClient();
      const raw = await client.getUsage();
      return (raw as unknown[]).map(normalizeRollup);
    },
    refetchInterval: 30_000,
    retry: 1,
  });
}

// ── Projects — GET /projects (per-video costs) ────────────────────────────

export interface ProjectCost {
  id: string;
  /** The video's working title — the backend stores it as `topic`. */
  title: string;
  status: string;
  targetMinutes: number | null;
  totalCostMicros: number;
  publishedAtMs: number | null;
  createdAtMs: number | null;
}

function normalizeProjectCost(raw: unknown): ProjectCost {
  const r = isRecord(raw) ? raw : {};
  return {
    id: str(r.id) ?? "",
    title: str(r.topic) ?? str(r.title) ?? str(r.name) ?? "Untitled project",
    status: str(r.status) ?? "draft",
    targetMinutes: num(r.target_minutes) ?? num(r.targetMinutes),
    totalCostMicros: num(r.total_cost_micros) ?? num(r.totalCostMicros) ?? 0,
    publishedAtMs: toMs(r.published_at ?? r.publishedAt),
    createdAtMs: toMs(r.created_at ?? r.createdAt),
  };
}

export function useProjectCosts() {
  return useQuery({
    queryKey: ["projects", "costs"],
    queryFn: async (): Promise<ProjectCost[]> => {
      const client = await getApiClient();
      const page = await client.listProjects({ limit: 200 });
      return (page.items as unknown[]).map(normalizeProjectCost);
    },
    refetchInterval: 30_000,
    retry: 1,
  });
}

// ── Usage today — GET /usage/today ────────────────────────────────────────

/**
 * Same wire shape and direct-fetch approach as the Mission Control
 * query (kept identical so the shared ["usage", "today"] cache entry
 * has one shape): `{ date, totals: { calls, …, cost_micros, est_usd } }`,
 * with `totals: {}` when nothing was recorded today.
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
