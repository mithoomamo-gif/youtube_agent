/**
 * Mission Control — Round 5 (Growth + Learning) read seams.
 *
 * Three Dashboard cards are fed from here. The Dashboard is
 * global-scope (all channels), while the Round 5 read models are
 * owner-scoped per channel — so the learning and drafts hooks below
 * aggregate across the user's connected channels, keeping every rule
 * labelled with the scope it came from (rules are never merged or
 * re-scored client-side; learning is never pooled across niches).
 *
 * Contracts (landed backend routers are the authority):
 *
 * - Views 28 Days — `GET /channels/{id}/analytics/overview`
 *   (LANDED: `backend/app/routers/analytics.py` over
 *   `app.analytics_read`). The value is the channel metrics'
 *   reporting-period views (`metrics.period.views` — the sync's
 *   period totals), summed across connected channels; freshness is
 *   the newest `last_synced_at`. Responses are parsed with the
 *   Analytics screen's own `parseAnalyticsOverviewWire`
 *   (`queries/analytics.ts`) so both surfaces read the wire
 *   identically. The landed read model carries NO previous-period
 *   total (its comparisons are per-video median baselines), so
 *   `changePct` stays null — the card says "no prior-period
 *   comparison yet" rather than inventing a delta. (The sync's
 *   period window is also not exposed on the wire; the figure is
 *   the analytics period figure the Analytics screen itself tiles.)
 *
 * - Learning highlights — `GET /channels/{id}/learning/rules?niche={key}`
 *   (LANDED: `backend/app/routers/learning.py`). The niche param is
 *   required (learning is never read unscoped); a channel's niche
 *   key is resolved from its `default_niche_profile_id` against
 *   `GET /niches`. Items are the learning consumption seam's
 *   normalised rules (`rule_id`, `kind`, `statement`,
 *   `evidence_count`, `confidence` 0–1, `effect_value`/`effect_unit`,
 *   `is_thin_evidence`); raw Learning Store rows (the shape
 *   `queries/growth.ts` documents) are tolerated too.
 *
 * - Comment drafts — `GET /channels/{id}/comment-drafts?status=proposed`
 *   (LANDED: `backend/app/routers/comments.py`; pending drafts are
 *   the ones in `proposed` status). If that path ever 404s, the
 *   comments-intelligence documented path
 *   (`/channels/{id}/comments/drafts?status=proposed`, see
 *   `queries/comments.ts`) is tried before declaring the seam
 *   missing.
 *
 * Honesty rules (roadmap §3.3 / §3.15):
 * - Analytics figures are labelled "Analytics updated [time]" — a
 *   sync timestamp, never implied to be real-time.
 * - A card only shows a number when a payload actually carries one.
 *   A 200 with no usable figure, an explicit `has_data: false`, or a
 *   404 (read model not landed) all normalise to an empty result;
 *   `endpointMissing` distinguishes "the seam isn't there yet" from
 *   "the seam answered with no data".
 *
 * Fetching follows the established pattern from `queries/mission.ts`
 * (`fetchUsageToday`): direct fetch with the `X-Local-Token` header
 * and the shared `{ error: { code, message } }` envelope → `ApiError`.
 * The wire is snake_case; every response is normalised here, once.
 */

import { useQuery } from "@tanstack/react-query";
import { ApiError } from "../types";
import { getBackendConnection } from "../tauri";
import { isOfflineError } from "./mission";
import {
  analyticsOverviewPath,
  parseAnalyticsOverviewWire,
  parseMetricTotals,
} from "./analytics";

export { isOfflineError };

// ── Wire helpers (same conventions as queries/mission.ts) ─────────────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function pick(record: Record<string, unknown>, keys: string[]): unknown {
  for (const key of keys) {
    if (record[key] !== undefined && record[key] !== null) return record[key];
  }
  return undefined;
}

/** Unwraps `{ items, … }` page envelopes and bare arrays alike. */
function toItems(raw: unknown): unknown[] {
  if (Array.isArray(raw)) return raw;
  if (isRecord(raw) && Array.isArray(raw.items)) return raw.items;
  return [];
}

// ── Transport (mirrors fetchUsageToday in queries/mission.ts) ─────────────

async function fetchJson(path: string): Promise<unknown> {
  const conn = await getBackendConnection();
  const url = `${conn.baseUrl.replace(/\/+$/, "")}${path}`;
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
    const envelope = isRecord(body) && isRecord(body.error) ? body.error : null;
    throw new ApiError({
      code: str(envelope?.code) ?? `HTTP_${res.status}`,
      message: str(envelope?.message) ?? `Request failed (${res.status})`,
      status: res.status,
      retryable: res.status === 429 || res.status >= 500,
    });
  }
  return body;
}

function isMissingEndpoint(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

// ── Channel scopes (shared by the per-channel seams) ──────────────────────

interface ChannelScope {
  channelId: string;
  channelTitle: string | null;
  /** Learning niche key for this channel, when resolvable. */
  nicheKey: string | null;
  nicheName: string | null;
}

/**
 * Connected channels + their learning niche keys, from the same
 * `GET /channels` / `GET /niches` lists the rest of the app reads.
 * A channel whose default niche cannot be resolved to a niche key
 * simply has no learning scope (the rules endpoint requires one).
 */
async function fetchChannelScopes(): Promise<ChannelScope[]> {
  const [channelsRaw, nichesRaw] = await Promise.all([
    fetchJson("/channels?limit=200"),
    fetchJson("/niches?limit=200").catch(() => null),
  ]);

  const keyByProfileId = new Map<string, string>();
  const nameByKey = new Map<string, string>();
  for (const item of toItems(nichesRaw)) {
    if (!isRecord(item)) continue;
    const key = str(pick(item, ["key"])) ?? str(pick(item, ["id"]));
    const id = str(pick(item, ["id"]));
    if (key && id) keyByProfileId.set(id, key);
    if (key) nameByKey.set(key, str(pick(item, ["name", "label"])) ?? key);
  }

  const scopes: ChannelScope[] = [];
  for (const item of toItems(channelsRaw)) {
    if (!isRecord(item)) continue;
    const channelId = str(pick(item, ["id"]));
    if (!channelId) continue;
    const profileId = str(
      pick(item, ["default_niche_profile_id", "defaultNicheProfileId"]),
    );
    const nicheKey =
      (profileId ? keyByProfileId.get(profileId) : undefined) ??
      (profileId && nameByKey.has(profileId) ? profileId : null);
    scopes.push({
      channelId,
      channelTitle: str(pick(item, ["title", "name"])),
      nicheKey,
      nicheName:
        (nicheKey ? nameByKey.get(nicheKey) : undefined) ??
        str(pick(item, ["default_niche_name", "defaultNicheName"])),
    });
  }
  return scopes;
}

// ── Views 28 Days — GET /channels/{id}/analytics/overview (landed) ──────

/**
 * Aggregated from each connected channel's analytics overview
 * (parsed by `parseAnalyticsOverviewWire`, the Analytics screen's
 * own parser):
 * - value: sum of `metrics.period.views` over channels whose
 *   overview carries a period figure (the documented `totals.views`
 *   shape is accepted as a fallback via `parseMetricTotals`).
 * - `lastSyncedMs`: the newest `last_synced_at` across channels.
 * - `changePct`: the landed read model exposes no previous-period
 *   channel total, so this is null unless a payload explicitly
 *   carries one (`previous_views_28d` / `views_prev_28d` beside the
 *   totals, or a `deltas.views` percent on a single-channel scope).
 * - `data_state` drives the empty copy: `not_connected` (no channel
 *   connected for analytics) vs `no_data` / never synced.
 */
export interface Views28Data {
  /** False → render the honest not-synced state, never a number. */
  hasData: boolean;
  /** True when every channel's overview route answered 404. */
  endpointMissing: boolean;
  /** No connected channels to read analytics for. */
  noScope: boolean;
  /** Every answered channel reported `data_state: not_connected`. */
  notConnected: boolean;
  views: number | null;
  previousViews: number | null;
  /** Percent change vs the previous 28 days; null when not derivable. */
  changePct: number | null;
  lastSyncedMs: number | null;
}

const EMPTY_VIEWS: Views28Data = {
  hasData: false,
  endpointMissing: false,
  noScope: false,
  notConnected: false,
  views: null,
  previousViews: null,
  changePct: null,
  lastSyncedMs: null,
};

function pctChange(current: number, previous: number): number | null {
  if (previous <= 0) return null;
  return ((current - previous) / previous) * 100;
}

export function useViews28Days() {
  return useQuery({
    queryKey: ["analytics", "overview", "dashboard"],
    queryFn: async (): Promise<Views28Data> => {
      const scopes = await fetchChannelScopes();
      if (scopes.length === 0) return { ...EMPTY_VIEWS, noScope: true };

      let answered = 0;
      let contributing = 0;
      let viewsTotal = 0;
      let previousTotal: number | null = null;
      let explicitDelta: number | null = null;
      let lastSyncedMs: number | null = null;
      let allNotConnected = true;

      for (const scope of scopes) {
        let raw: unknown;
        try {
          raw = await fetchJson(analyticsOverviewPath(scope.channelId));
        } catch (err) {
          if (isMissingEndpoint(err)) continue;
          throw err;
        }
        answered += 1;
        const overview = parseAnalyticsOverviewWire(raw);
        if (overview.dataState !== "not_connected") allNotConnected = false;
        if (
          overview.lastSyncedMs !== null &&
          (lastSyncedMs === null || overview.lastSyncedMs > lastSyncedMs)
        ) {
          lastSyncedMs = overview.lastSyncedMs;
        }

        const record = isRecord(raw) ? raw : {};
        let views = overview.metrics?.period?.views ?? null;
        if (views === null) {
          // Documented totals shape (queries/analytics.ts contract).
          views = parseMetricTotals(record.totals)?.views ?? null;
        }
        if (views === null) continue;
        contributing += 1;
        viewsTotal += views;

        const previous = num(
          pick(record, ["previous_views_28d", "views_prev_28d", "previousViews28d"]),
        );
        if (previous !== null) {
          previousTotal = (previousTotal ?? 0) + previous;
        }
        const deltas = isRecord(record.deltas) ? record.deltas : null;
        const delta = deltas
          ? num(pick(deltas, ["views", "views_pct", "views_delta_pct"]))
          : num(pick(record, ["views_delta_pct", "viewsDeltaPct"]));
        if (delta !== null) explicitDelta = delta;
      }

      if (answered === 0) return { ...EMPTY_VIEWS, endpointMissing: true };
      if (contributing === 0) {
        return { ...EMPTY_VIEWS, notConnected: allNotConnected, lastSyncedMs };
      }

      let changePct =
        previousTotal !== null ? pctChange(viewsTotal, previousTotal) : null;
      if (changePct === null && contributing === 1 && explicitDelta !== null) {
        changePct = explicitDelta;
      }

      return {
        hasData: true,
        endpointMissing: false,
        noScope: false,
        notConnected: false,
        views: viewsTotal,
        previousViews: previousTotal,
        changePct,
        lastSyncedMs,
      };
    },
    refetchInterval: 60_000,
    retry: 1,
  });
}

// ── Learning highlights — GET /channels/{id}/learning/rules?niche=… ──────

/**
 * One active learning rule. The landed endpoint returns the
 * consumption seam's normalised shape (`rule_id`, `statement`,
 * `kind`, `evidence_count`, `confidence`, `effect_value`,
 * `effect_unit`, `is_thin_evidence`); raw store rows (`id`, …,
 * `thin_evidence`) are tolerated. Confidence is 0–1 on the wire;
 * values > 1 are treated as an already-scaled percent.
 */
export interface LearningRuleData {
  id: string;
  text: string;
  type: string | null;
  /** Display label of the (channel, niche) scope the rule came from. */
  scope: string | null;
  /** 0–100, or null when the seam reports none. */
  confidencePct: number | null;
  evidenceCount: number | null;
}

export interface LearningHighlightsData {
  hasData: boolean;
  /** Every scope's rules endpoint answered 404 (router not landed). */
  endpointMissing: boolean;
  /** No connected channel with a resolvable niche to read rules for. */
  noScope: boolean;
  /** Top rules across scopes, highest confidence first, capped at 5. */
  rules: LearningRuleData[];
  /** Total active rules across scopes, when more than returned. */
  total: number | null;
}

const EMPTY_LEARNING: LearningHighlightsData = {
  hasData: false,
  endpointMissing: false,
  noScope: false,
  rules: [],
  total: null,
};

function normalizeRule(
  raw: unknown,
  index: number,
  scopeLabel: string | null,
): LearningRuleData | null {
  if (!isRecord(raw)) return null;
  const text = str(pick(raw, ["statement", "insight_text", "insightText", "rule", "summary", "text"]));
  if (!text) return null;
  const rawConfidence = num(pick(raw, ["confidence"]));
  const explicitEvidence = num(pick(raw, ["evidence_count", "evidenceCount"]));
  return {
    id: str(pick(raw, ["rule_id", "ruleId", "id"])) ?? `rule-${index}`,
    text,
    type: str(pick(raw, ["kind", "insight_type", "insightType", "type"])),
    scope: scopeLabel,
    confidencePct:
      rawConfidence === null
        ? null
        : Math.round(rawConfidence <= 1 ? rawConfidence * 100 : rawConfidence),
    evidenceCount: explicitEvidence,
  };
}

export function useLearningHighlights() {
  return useQuery({
    queryKey: ["learning", "rules", "active", "dashboard"],
    queryFn: async (): Promise<LearningHighlightsData> => {
      const scopes = await fetchChannelScopes();
      const scoped = scopes.filter((s) => s.nicheKey !== null);
      if (scoped.length === 0) return { ...EMPTY_LEARNING, noScope: true };

      const rules: LearningRuleData[] = [];
      let answered = 0;
      let totalCount = 0;
      for (const scope of scoped) {
        const path = `/channels/${encodeURIComponent(scope.channelId)}/learning/rules?niche=${encodeURIComponent(scope.nicheKey ?? "")}`;
        let raw: unknown;
        try {
          raw = await fetchJson(path);
        } catch (err) {
          if (isMissingEndpoint(err)) continue; // scope missing/router not landed
          throw err;
        }
        answered += 1;
        const scopeLabel =
          scope.nicheName ??
          (scopes.length > 1 ? scope.channelTitle : null);
        const items = toItems(raw);
        totalCount += num(isRecord(raw) ? pick(raw, ["count", "total"]) : null) ?? items.length;
        items.forEach((item, index) => {
          const rule = normalizeRule(item, rules.length + index, scopeLabel);
          if (rule) rules.push(rule);
        });
      }

      if (answered === 0) return { ...EMPTY_LEARNING, endpointMissing: true };

      rules.sort((a, b) => (b.confidencePct ?? -1) - (a.confidencePct ?? -1));
      return {
        hasData: rules.length > 0,
        endpointMissing: false,
        noScope: false,
        rules: rules.slice(0, 5),
        total: totalCount > rules.length ? totalCount : null,
      };
    },
    refetchInterval: 60_000,
    retry: 1,
  });
}

// ── Pending comment drafts — GET /channels/{id}/comment-drafts ────────────

export interface CommentDraftsData {
  hasData: boolean;
  /** Every channel's drafts endpoint answered 404 on both paths. */
  endpointMissing: boolean;
  /** No connected channels to count drafts for. */
  noScope: boolean;
  pendingCount: number | null;
  /** True when a page cursor means the count is a lower bound. */
  countIsLowerBound: boolean;
}

const EMPTY_DRAFTS: CommentDraftsData = {
  hasData: false,
  endpointMissing: false,
  noScope: false,
  pendingCount: null,
  countIsLowerBound: false,
};

/** Counts `proposed` drafts in one channel's list response. */
function countProposed(raw: unknown): { count: number; lowerBound: boolean } {
  const items = toItems(raw);
  let count = 0;
  for (const item of items) {
    if (!isRecord(item)) continue;
    const status = str(pick(item, ["status"]));
    // The endpoint filters by ?status=proposed server-side; rows are
    // still checked so an unfiltered response can never inflate the
    // count with approved/rejected drafts.
    if (status === null || status === "proposed" || status === "pending") {
      count += 1;
    }
  }
  const nextCursor = isRecord(raw) ? (raw.next_cursor ?? raw.nextCursor) : null;
  return { count, lowerBound: Boolean(nextCursor) };
}

async function fetchDraftsForChannel(channelId: string): Promise<unknown> {
  const base = `/channels/${encodeURIComponent(channelId)}`;
  try {
    return await fetchJson(`${base}/comment-drafts?status=proposed`);
  } catch (err) {
    if (!isMissingEndpoint(err)) throw err;
    // Fall back to the comments-intelligence documented path.
    return fetchJson(`${base}/comments/drafts?status=proposed`);
  }
}

export function usePendingCommentDrafts() {
  return useQuery({
    queryKey: ["comments", "drafts", "pending", "dashboard"],
    queryFn: async (): Promise<CommentDraftsData> => {
      const scopes = await fetchChannelScopes();
      if (scopes.length === 0) return { ...EMPTY_DRAFTS, noScope: true };

      let answered = 0;
      let pending = 0;
      let lowerBound = false;
      for (const scope of scopes) {
        let raw: unknown;
        try {
          raw = await fetchDraftsForChannel(scope.channelId);
        } catch (err) {
          if (isMissingEndpoint(err)) continue;
          throw err;
        }
        answered += 1;
        const result = countProposed(raw);
        pending += result.count;
        lowerBound = lowerBound || result.lowerBound;
      }

      if (answered === 0) return { ...EMPTY_DRAFTS, endpointMissing: true };
      return {
        hasData: true,
        endpointMissing: false,
        noScope: false,
        pendingCount: pending,
        countIsLowerBound: lowerBound,
      };
    },
    refetchInterval: 30_000,
    retry: 1,
  });
}
