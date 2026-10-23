/**
 * React Query hook for YPP (YouTube Partner Program) progress —
 * Round 5, worker W4.
 *
 * CONTRACT STATUS (checked 2026-10-23 against the concurrent Round 5
 * tree): the analytics team's STORE has landed —
 * `backend/app/analytics/store.py`, table `channel_metric_snapshots` —
 * with exactly the field names this parser reads: `subscribers`,
 * `watch_hours` (ordinary), `qualified_watch_hours` (nullable; NULL =
 * qualification data unavailable, never derived from ordinary hours)
 * and `captured_at` (epoch ms). No HTTP route exposes those rows yet
 * (no analytics router is registered in `backend/app/routers/api.py`),
 * so this module targets the contract below, which follows the repo's
 * established conventions (routes nested under the owning channel,
 * snake_case wire, `{ error: … }` envelope). When the route lands,
 * only {@link yppProgressPath} should need to change; the parser also
 * tolerates the store's list shape (`list_channel_metrics` returns
 * snapshots oldest-first — the latest row wins) and the plausible
 * field spellings.
 *
 * Expected contract:
 *
 * - `GET /channels/{channelId}/ypp-progress` -> one snapshot row:
 *   ```json
 *   {
 *     "channel_id": "…",
 *     "subscribers": 812,
 *     "qualified_watch_hours": 2910.5,
 *     "watch_hours": 3402.0,
 *     "captured_at": 1792800000000
 *   }
 *   ```
 * - `404` when the channel has no analytics snapshot yet → mapped to
 *   `null` data ("no analytics yet"), NOT an error.
 *
 * HARD ROADMAP RULE (MASTER_ROADMAP line 366): qualification is never
 * estimated from ordinary watch time. Enforced in this parser:
 *
 * - `qualifiedWatchHours` is read ONLY from `qualified_watch_hours` /
 *   `qualified_public_watch_hours`. Ordinary-watch fields
 *   (`ordinary_watch_hours`, `watch_hours`, `total_watch_hours`) are
 *   read into the separate `ordinaryWatchHours` field and are NEVER
 *   copied across.
 * - If the payload explicitly says `qualification_data_available:
 *   false`, any qualified figure present is discarded (null) — the
 *   explicit unavailability flag wins over a stray number.
 * - No defaults are invented: a missing field stays null, and the
 *   component renders its honest unavailable state.
 *
 * This module uses its own small fetch helper (same token header +
 * error envelope as `ApiClient`), mirroring `queries/channels.ts` and
 * `queries/styleLab.ts`; `client.ts` is owned by another worker.
 */

import { useQuery } from "@tanstack/react-query";
import { ApiError } from "../types";
import { getBackendConnection } from "../tauri";
import type { YppProgressData } from "../../components/analytics/yppProgressModel";

/** Path template for the analytics read model (see header contract). */
export function yppProgressPath(channelId: string): string {
  return `/channels/${encodeURIComponent(channelId)}/ypp-progress`;
}

// ── Wire parsing ───────────────────────────────────────────────────────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function firstNumber(
  record: Record<string, unknown>,
  keys: string[],
): number | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value) && value >= 0) {
      return value;
    }
  }
  return null;
}

function firstTimestamp(
  record: Record<string, unknown>,
  keys: string[],
): string | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value;
    if (typeof value === "number" && Number.isFinite(value) && value > 0) {
      // Backend DB convention elsewhere: epoch milliseconds.
      return new Date(value).toISOString();
    }
  }
  return null;
}

/**
 * Parse a wire payload into {@link YppProgressData}. Exported (pure)
 * so the mapping — above all the qualification rule — is testable
 * without a network. Returns null when the payload carries no usable
 * figure at all.
 */
export function parseYppProgressWire(raw: unknown): YppProgressData | null {
  // Tolerate a snapshot LIST (bare array or `{ items: [...] }`): the
  // analytics store returns snapshots oldest-first, so the LAST row
  // is the current figure.
  if (Array.isArray(raw)) {
    return raw.length > 0 ? parseYppProgressWire(raw[raw.length - 1]) : null;
  }
  if (!isRecord(raw)) return null;
  if (Array.isArray(raw.items)) {
    return raw.items.length > 0
      ? parseYppProgressWire(raw.items[raw.items.length - 1])
      : null;
  }
  // Tolerate a one-level envelope (`{ ypp_progress: { … } }`).
  const record = isRecord(raw.ypp_progress) ? raw.ypp_progress : raw;

  const subscriberCount = firstNumber(record, [
    "subscriber_count",
    "subscribers",
    "subscriberCount",
  ]);

  // QUALIFICATION RULE (see header): qualified hours come only from
  // the qualification fields, and an explicit unavailable flag voids
  // any number that happens to be present.
  let qualifiedWatchHours = firstNumber(record, [
    "qualified_watch_hours",
    "qualified_public_watch_hours",
    "qualifiedWatchHours",
  ]);
  if (record.qualification_data_available === false) {
    qualifiedWatchHours = null;
  }

  // Ordinary watch time — a separate display-only figure. It is never
  // assigned to `qualifiedWatchHours` on any path through this parser.
  const ordinaryWatchHours = firstNumber(record, [
    "ordinary_watch_hours",
    "watch_hours",
    "total_watch_hours",
    "ordinaryWatchHours",
  ]);

  const asOf = firstTimestamp(record, [
    "as_of",
    "synced_at",
    "snapshot_at",
    // The analytics store's field: epoch milliseconds.
    "captured_at",
  ]);

  if (
    subscriberCount === null &&
    qualifiedWatchHours === null &&
    ordinaryWatchHours === null
  ) {
    return null;
  }
  return { subscriberCount, qualifiedWatchHours, ordinaryWatchHours, asOf };
}

// ── Minimal request helper (mirrors ApiClient's envelope handling) ───────

async function request(path: string): Promise<unknown> {
  const conn = await getBackendConnection();
  const baseUrl = conn.baseUrl.replace(/\/+$/, "");

  let res: Response;
  try {
    res = await fetch(`${baseUrl}${path}`, {
      method: "GET",
      headers: { "X-Local-Token": conn.token },
    });
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") throw err;
    throw new ApiError({
      code: "NETWORK_ERROR",
      message: err instanceof Error ? err.message : "Network request failed",
      status: 0,
      retryable: true,
    });
  }

  if (res.status === 204) return undefined;

  const text = await res.text();
  let parsed: unknown;
  if (text) {
    try {
      parsed = JSON.parse(text) as unknown;
    } catch {
      parsed = text;
    }
  }

  if (!res.ok) {
    if (isRecord(parsed) && isRecord(parsed.error)) {
      const err = parsed.error;
      throw new ApiError({
        code: typeof err.code === "string" ? err.code : `HTTP_${res.status}`,
        message:
          typeof err.message === "string"
            ? err.message
            : `Request failed (${res.status})`,
        status: res.status,
        retryable:
          typeof err.retryable === "boolean"
            ? err.retryable
            : res.status === 429 || res.status >= 500,
        requestId: typeof err.request_id === "string" ? err.request_id : undefined,
        details: err.details,
      });
    }
    throw new ApiError({
      code: `HTTP_${res.status}`,
      message: `Request failed (${res.status})`,
      status: res.status,
      retryable: res.status === 429 || res.status >= 500,
    });
  }

  return parsed;
}

// ── Hook ───────────────────────────────────────────────────────────────────

/**
 * Fetch YPP progress for one channel.
 *
 * - Disabled unless a channel id is given AND the channel is
 *   connected — a disconnected channel must render the component's
 *   not-connected state, not stale figures.
 * - HTTP 404 → `data: null` (no analytics yet), not an error.
 * - React Query keeps the last good data across a failed refetch, so
 *   the component can keep showing real figures with their as-of
 *   label instead of blanking out.
 */
export function useYppProgress(
  channelId: string | null,
  channelConnected: boolean,
) {
  return useQuery({
    queryKey: ["analytics", "ypp-progress", channelId],
    enabled: channelId !== null && channelConnected,
    retry: 1,
    queryFn: async (): Promise<YppProgressData | null> => {
      try {
        const raw = await request(yppProgressPath(channelId ?? ""));
        return parseYppProgressWire(raw);
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) return null;
        throw err;
      }
    },
  });
}
