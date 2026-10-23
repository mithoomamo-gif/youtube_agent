/**
 * Analytics screen read models — Round 5 (Growth + Learning), worker W3.
 *
 * REAL CONTRACT (landed by the analytics ingestion team during this
 * round; verified against `backend/app/routers/analytics.py` +
 * `backend/app/analytics_read.py` + `backend/tests/
 * test_analytics_read_api.py`). Routes are nested under the OWNING
 * channel, snake_case wire, `{ error: … }` envelope:
 *
 * - `GET /channels/{channelId}/analytics/overview` ->
 *   ```json
 *   {
 *     "channel_id": "…", "youtube_channel_id": "…",
 *     "data_state": "ok" | "no_data" | "not_connected",
 *     "last_synced_at": 1761177600000,           // epoch ms | null
 *     "sync": { "status": "…", "last_reason": "…|null",
 *               "last_attempt_at": …, "source": "…" },
 *     "metrics": null | {                        // latest channel snapshot
 *       "subscriber_count": …, "total_views": …, "total_videos": …,
 *       "ordinary_watch_hours": …, "qualified_watch_hours": …,
 *       "period": null | { "views": …, "watch_hours": …,
 *         "subs_delta": …, "likes": …, "comments_count": …, "shares": … },
 *       "snapshot_kind": "scheduled", "captured_at": …, "capture_day": "…"
 *     },
 *     "ypp_progress": null | { … }               // consumed by W4's hook
 *   }
 *   ```
 *   NOTE: the overview carries NO channel CTR / AVD and NO baseline —
 *   those live per-video (below). Tiles compose the overview's period
 *   figures with per-video aggregates; nothing is invented.
 * - `GET /channels/{channelId}/analytics/videos` ->
 *   `{ data_state, last_synced_at, sync, items: [...], next_cursor }`
 *   where each item is
 *   `{ youtube_video_id, project_id, title, published_at,
 *      data_state, latest_snapshot, comparison,
 *      comparison_unavailable_reason }`.
 *   `latest_snapshot` is `{ id, snapshot_kind, captured_at,
 *   capture_day, source, project_id, metrics }` with metrics keys
 *   `views, watch_hours, likes, comments_count, shares, ctr (0..1
 *   fraction, nullable), avg_view_duration_sec, avg_view_percentage,
 *   subs_gained, revenue_micros`.
 *   `comparison` (null + `comparison_unavailable_reason:
 *   "insufficient_baseline"` when the pool is too small) is
 *   `{ baseline_kind, baseline_video_count, baseline_median: {…same
 *   metric keys…}, deltas: { <metric>: { value, baseline, absolute,
 *   pct } } }` — the median of up to the channel's last-10 OTHER
 *   videos' latest snapshots of the same kind. This is the read
 *   model's last-10 baseline; the tile deltas below aggregate exactly
 *   these provided per-video baselines (documented at
 *   {@link aggregateChannelStats}).
 * - `GET /channels/{channelId}/analytics/videos/{videoId}/snapshots`
 *   -> `{ data_state, last_synced_at, youtube_video_id, items:
 *   [snapshot views, newest first], by_kind: { d3: [...], d7: [...],
 *   d28: [...], … }, next_cursor }` — the milestone table's source.
 *   A video that is not this channel's own answers 404
 *   `video_not_found`.
 * - `GET /channels/{channelId}/ypp-progress` — W4's flat alias;
 *   consumed by `src/api/queries/ypp.ts`, not here.
 *
 * RETENTION — honest gap in the landed backend: retention curves are
 * stored (`analytics_snapshots.retention_curve_json`, parsed by the
 * store as `retention_curve`) and section annotations are computed by
 * `backend/app/retention_sections/` (result `status: ok | unmapped |
 * no_data`, sections in seconds), but NO route serves either yet —
 * the snapshots endpoint's view shape omits the curve. This module
 * therefore (a) parses a `retention_curve` off any snapshot view the
 * moment the backend includes one, and (b) tries the documented
 * retention-analysis route contract
 * `GET …/analytics/videos/{videoId}/retention` (the
 * RetentionSectionResult shape) and degrades to the honest
 * `endpoint_missing` state while it 404s. Section labels are never
 * fabricated.
 *
 * Honesty rules (roadmap §3.15 + §7.8):
 * - `last_synced_at` is the ONLY source of the "Analytics updated …"
 *   freshness stamp (falling back to the newest snapshot
 *   `captured_at` in the analytics payloads — still a real sync time
 *   from the analytics data itself). No hard-coded times, ever.
 * - A snapshot column is "pending" only when the video is genuinely
 *   too young for it (published_at + N days is still in the future);
 *   an overdue-but-absent snapshot reads "Not captured".
 * - Baseline deltas render only when the read model provided a
 *   baseline for at least two videos — never a fake 0%.
 *
 * This module uses its own small fetch helper (same token header +
 * error envelope as `ApiClient`), mirroring `queries/ypp.ts`,
 * `queries/channels.ts` and `queries/styleLab.ts`; `client.ts` is
 * owned by another worker.
 */

import { useQueries, useQuery } from "@tanstack/react-query";
import { ApiError } from "../types";
import { getBackendConnection } from "../tauri";
import { isOfflineError } from "./mission";

export { isOfflineError };

// ── Paths ──────────────────────────────────────────────────────────────────

export function analyticsOverviewPath(channelId: string): string {
  return `/channels/${encodeURIComponent(channelId)}/analytics/overview`;
}

export function analyticsVideosPath(channelId: string): string {
  return `/channels/${encodeURIComponent(channelId)}/analytics/videos`;
}

export function videoSnapshotsPath(channelId: string, videoId: string): string {
  return `/channels/${encodeURIComponent(channelId)}/analytics/videos/${encodeURIComponent(videoId)}/snapshots`;
}

/** Documented retention-analysis contract (route not landed yet). */
export function videoRetentionPath(channelId: string, videoId: string): string {
  return `/channels/${encodeURIComponent(channelId)}/analytics/videos/${encodeURIComponent(videoId)}/retention`;
}

// ── Domain model ───────────────────────────────────────────────────────────

export interface AnalyticsMetricTotals {
  views: number | null;
  watchHours: number | null;
  /** Thumbnail CTR as a 0..1 fraction; null = not reported. */
  ctr: number | null;
  /** Why CTR is unavailable, when a payload reports one. */
  ctrUnavailableReason: string | null;
  avgViewDurationS: number | null;
  /** Average percentage viewed (0..100), when reported. */
  avgViewPercentage: number | null;
}

export interface ChannelPeriod {
  views: number | null;
  watchHours: number | null;
  subsDelta: number | null;
  likes: number | null;
  commentsCount: number | null;
  shares: number | null;
}

export interface ChannelMetrics {
  subscriberCount: number | null;
  totalViews: number | null;
  totalVideos: number | null;
  ordinaryWatchHours: number | null;
  qualifiedWatchHours: number | null;
  period: ChannelPeriod | null;
  capturedAtMs: number | null;
  snapshotKind: string | null;
}

export type AnalyticsDataState = "ok" | "no_data" | "not_connected";

export interface AnalyticsOverviewData {
  /** True when the route itself answered 404 (unknown channel). */
  endpointMissing: boolean;
  dataState: AnalyticsDataState | null;
  /** Epoch ms of the channel's last analytics sync; null = never. */
  lastSyncedMs: number | null;
  sync: { status: string | null; lastReason: string | null; lastAttemptAtMs: number | null } | null;
  metrics: ChannelMetrics | null;
}

export const SNAPSHOT_POINTS = ["d3", "d7", "d28"] as const;
export type SnapshotPoint = (typeof SNAPSHOT_POINTS)[number];

export const SNAPSHOT_POINT_DAYS: Record<SnapshotPoint, number> = {
  d3: 3,
  d7: 7,
  d28: 28,
};

export const SNAPSHOT_POINT_LABELS: Record<SnapshotPoint, string> = {
  d3: "D+3",
  d7: "D+7",
  d28: "D+28",
};

export interface RetentionPoint {
  elapsedRatio: number;
  watchRatio: number;
}

export interface VideoSnapshot extends AnalyticsMetricTotals {
  /** Wire snapshot kind (`d3` / `d7` / `d28` / `scheduled` / …). */
  kind: string | null;
  likes: number | null;
  comments: number | null;
  shares: number | null;
  subsGained: number | null;
  /** Epoch ms this snapshot was captured; null when unreported. */
  capturedAtMs: number | null;
  /** Retention curve stored on this snapshot, when served. */
  retentionCurve: RetentionPoint[];
}

/** Wire metric keys the read model compares against the baseline. */
export type BaselineMetricKey =
  | "views"
  | "watch_hours"
  | "likes"
  | "comments_count"
  | "shares"
  | "ctr"
  | "avg_view_duration_sec"
  | "subs_gained";

export interface BaselineDelta {
  value: number | null;
  baseline: number | null;
  absolute: number | null;
  pct: number | null;
}

export interface VideoComparison {
  /** Snapshot kind the baseline pool shares (e.g. "d7"). */
  baselineKind: string | null;
  baselineVideoCount: number | null;
  deltas: Partial<Record<BaselineMetricKey, BaselineDelta>>;
  unavailableReason: string | null;
}

export interface AnalyticsVideo {
  /** YouTube video id (the analytics key). */
  videoId: string;
  projectId: string | null;
  title: string | null;
  publishedAtMs: number | null;
  durationS: number | null;
  url: string | null;
  dataState: AnalyticsDataState | null;
  /** Latest snapshot of any kind, from the videos list payload. */
  latestSnapshot: VideoSnapshot | null;
  comparison: VideoComparison | null;
}

export interface AnalyticsVideosData {
  endpointMissing: boolean;
  dataState: AnalyticsDataState | null;
  videos: AnalyticsVideo[];
  lastSyncedMs: number | null;
}

export interface VideoSnapshotsData {
  endpointMissing: boolean;
  dataState: AnalyticsDataState | null;
  /** Newest snapshot per milestone point. */
  byPoint: Partial<Record<SnapshotPoint, VideoSnapshot>>;
  /** Every snapshot served for the video, newest first. */
  snapshots: VideoSnapshot[];
  lastSyncedMs: number | null;
}

export interface RetentionSection {
  index: number;
  title: string;
  /** Script-section kind when reported (hook / chapter / cta / …). */
  kind: string | null;
  startS: number;
  endS: number;
  startRatio: number | null;
  endRatio: number | null;
  entryRatio: number | null;
  exitRatio: number | null;
  /** entry − exit (watch-ratio points), when stats were computed. */
  dropOff: number | null;
  annotation: string | null;
}

export interface RetentionSteepestDrop {
  atS: number;
  atText: string | null;
  drop: number | null;
  sectionTitle: string | null;
}

export type RetentionStatus =
  | "ok"
  | "unmapped"
  | "no_data"
  | "endpoint_missing";

export interface RetentionData {
  status: RetentionStatus;
  /** True when the curve came from a snapshot row because the
   *  retention route itself has not landed (sections never faked). */
  fromSnapshotFallback: boolean;
  points: RetentionPoint[];
  sections: RetentionSection[];
  steepestDrop: RetentionSteepestDrop | null;
  annotations: string[];
  durationS: number | null;
  snapshotKind: string | null;
  capturedAtMs: number | null;
  timingSource: string | null;
  timingPrecision: string | null;
}

// ── Wire helpers ───────────────────────────────────────────────────────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

/** Backend timestamps arrive as epoch-ms numbers (DB) or ISO strings. */
function toMs(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value) && value > 0) {
    return value;
  }
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? null : parsed;
  }
  return null;
}

function pick(record: Record<string, unknown>, keys: string[]): unknown {
  for (const key of keys) {
    if (record[key] !== undefined && record[key] !== null) return record[key];
  }
  return undefined;
}

function toItems(raw: unknown): unknown[] {
  if (Array.isArray(raw)) return raw;
  if (isRecord(raw)) {
    if (Array.isArray(raw.items)) return raw.items;
    if (Array.isArray(raw.videos)) return raw.videos;
  }
  return [];
}

function dataStateOf(record: Record<string, unknown>): AnalyticsDataState | null {
  const raw = str(pick(record, ["data_state", "dataState"]));
  if (raw === "ok" || raw === "no_data" || raw === "not_connected") return raw;
  if (record.status === "not_connected" || record.connected === false) {
    return "not_connected";
  }
  return null;
}

// ── Metric parsing (canonical read-model keys; legacy mirrors tolerated) ──

function parseCtr(record: Record<string, unknown>): number | null {
  const bps = num(pick(record, ["ctr_bps", "ctrBps"]));
  if (bps !== null) return bps / 10_000;
  const raw = num(pick(record, ["ctr", "ctr_fraction", "ctrFraction"]));
  if (raw !== null) {
    // Canonical wire shape is a 0..1 fraction; a value above 1 can
    // only be a percent, so normalise rather than render nonsense.
    return raw > 1 ? raw / 100 : raw;
  }
  const pct = num(pick(record, ["ctr_pct", "ctr_percent", "ctrPct"]));
  return pct !== null ? pct / 100 : null;
}

function parseWatchHours(record: Record<string, unknown>): number | null {
  const hours = num(pick(record, ["watch_hours", "watchHours"]));
  if (hours !== null) return hours;
  const minutes = num(
    pick(record, ["watch_minutes", "watchTimeMinutes", "estimated_minutes_watched", "estimatedMinutesWatched"]),
  );
  if (minutes !== null) return minutes / 60;
  const x100 = num(pick(record, ["watch_hours_x100", "watchHoursX100"]));
  return x100 !== null ? x100 / 100 : null;
}

export function parseMetricTotals(raw: unknown): AnalyticsMetricTotals | null {
  if (!isRecord(raw)) return null;
  const pctRaw = num(pick(raw, ["avg_view_percentage", "avgViewPercentage", "average_view_percentage"]));
  const totals: AnalyticsMetricTotals = {
    views: num(pick(raw, ["views"])),
    watchHours: parseWatchHours(raw),
    ctr: parseCtr(raw),
    ctrUnavailableReason: str(pick(raw, ["ctr_unavailable_reason", "ctrUnavailableReason"])),
    avgViewDurationS: num(
      pick(raw, ["avg_view_duration_sec", "avg_view_duration_s", "average_view_duration", "averageViewDuration", "avgViewDurationS"]),
    ),
    avgViewPercentage: pctRaw !== null ? (pctRaw <= 1 ? pctRaw * 100 : pctRaw) : null,
  };
  const any =
    totals.views !== null ||
    totals.watchHours !== null ||
    totals.ctr !== null ||
    totals.avgViewDurationS !== null;
  return any ? totals : null;
}

/** Relative % change of `current` vs `baseline`; null when not honest
 *  to compute (missing value, or a zero baseline). */
export function deltaPct(current: number | null, baseline: number | null): number | null {
  if (current === null || baseline === null || baseline === 0) return null;
  return ((current - baseline) / baseline) * 100;
}

function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 1
    ? sorted[mid]
    : (sorted[mid - 1] + sorted[mid]) / 2;
}

// ── Overview parsing (real read-model shape) ───────────────────────────────

const EMPTY_OVERVIEW: AnalyticsOverviewData = {
  endpointMissing: false,
  dataState: null,
  lastSyncedMs: null,
  sync: null,
  metrics: null,
};

export function parseAnalyticsOverviewWire(raw: unknown): AnalyticsOverviewData {
  if (!isRecord(raw)) return EMPTY_OVERVIEW;
  const record = isRecord(raw.overview) ? raw.overview : raw;

  const syncRaw = isRecord(record.sync) ? record.sync : null;
  const sync = syncRaw
    ? {
        status: str(pick(syncRaw, ["status"])),
        lastReason: str(pick(syncRaw, ["last_reason", "lastReason"])),
        lastAttemptAtMs: toMs(pick(syncRaw, ["last_attempt_at", "lastAttemptAt"])),
      }
    : null;

  let metrics: ChannelMetrics | null = null;
  const metricsRaw = isRecord(record.metrics) ? record.metrics : null;
  if (metricsRaw) {
    const periodRaw = isRecord(metricsRaw.period) ? metricsRaw.period : null;
    metrics = {
      subscriberCount: num(pick(metricsRaw, ["subscriber_count", "subscribers"])),
      totalViews: num(pick(metricsRaw, ["total_views", "totalViews"])),
      totalVideos: num(pick(metricsRaw, ["total_videos", "totalVideos"])),
      ordinaryWatchHours: num(pick(metricsRaw, ["ordinary_watch_hours", "ordinaryWatchHours"])),
      qualifiedWatchHours: num(pick(metricsRaw, ["qualified_watch_hours", "qualifiedWatchHours"])),
      period: periodRaw
        ? {
            views: num(pick(periodRaw, ["views"])),
            watchHours: parseWatchHours(periodRaw),
            subsDelta: num(pick(periodRaw, ["subs_delta", "subsDelta"])),
            likes: num(pick(periodRaw, ["likes"])),
            commentsCount: num(pick(periodRaw, ["comments_count", "comments"])),
            shares: num(pick(periodRaw, ["shares"])),
          }
        : null,
      capturedAtMs: toMs(pick(metricsRaw, ["captured_at", "capturedAt"])),
      snapshotKind: str(pick(metricsRaw, ["snapshot_kind", "snapshotKind"])),
    };
  }

  return {
    endpointMissing: false,
    dataState: dataStateOf(record),
    lastSyncedMs: toMs(
      pick(record, ["last_synced_at", "lastSyncedAt", "last_synced", "synced_at", "as_of"]),
    ),
    sync,
    metrics,
  };
}

// ── Retention curve parsing (backend `parse_retention_curve` shapes) ──────

const ELAPSED_KEYS = ["elapsed_ratio", "elapsedVideoTimeRatio", "elapsed", "x"];
const WATCH_KEYS = ["watch_ratio", "audienceWatchRatio", "watch", "y"];

export function parseRetentionPoints(raw: unknown): RetentionPoint[] {
  let list: unknown = raw;
  if (typeof raw === "string") {
    try {
      list = JSON.parse(raw) as unknown;
    } catch {
      return [];
    }
  }
  if (isRecord(list) && Array.isArray(list.points)) list = list.points;
  if (!Array.isArray(list)) return [];
  const points: RetentionPoint[] = [];
  for (const entry of list) {
    let elapsed: number | null = null;
    let watch: number | null = null;
    // Pair form first: arrays also satisfy isRecord, so the record
    // branch would swallow them and drop every pair point.
    if (Array.isArray(entry) && entry.length >= 2) {
      elapsed = num(entry[0]);
      watch = num(entry[1]);
    } else if (isRecord(entry)) {
      elapsed = num(pick(entry, ELAPSED_KEYS));
      watch = num(pick(entry, WATCH_KEYS));
    }
    if (elapsed === null || watch === null) continue;
    if (elapsed < 0 || elapsed > 1 || watch < 0) continue;
    points.push({ elapsedRatio: elapsed, watchRatio: watch });
  }
  points.sort((a, b) => a.elapsedRatio - b.elapsedRatio);
  return points;
}

// ── Snapshot parsing (read-model snapshot view + store-row tolerance) ──────

function snapshotKindOf(raw: Record<string, unknown>): string | null {
  const kind = str(pick(raw, ["snapshot_kind", "snapshotKind", "kind", "point"]));
  return kind ? kind.toLowerCase().replace("+", "").replace(/\s+/g, "") : null;
}

function isSnapshotPoint(kind: string | null): kind is SnapshotPoint {
  return kind === "d3" || kind === "d7" || kind === "d28";
}

export function parseSnapshot(raw: unknown): VideoSnapshot | null {
  if (!isRecord(raw)) return null;
  // Read-model views nest the canonical metrics under `metrics`;
  // store-shaped rows may carry them flat. Same canonical keys.
  const metrics = isRecord(raw.metrics) ? raw.metrics : raw;
  const totals = parseMetricTotals(metrics) ?? parseMetricTotals(raw);
  const base: AnalyticsMetricTotals = totals ?? {
    views: null,
    watchHours: null,
    ctr: null,
    ctrUnavailableReason: null,
    avgViewDurationS: null,
    avgViewPercentage: null,
  };
  return {
    ...base,
    kind: snapshotKindOf(raw),
    likes: num(pick(metrics, ["likes"])) ?? num(pick(raw, ["likes"])),
    comments:
      num(pick(metrics, ["comments", "comments_count"])) ??
      num(pick(raw, ["comments", "comments_count"])),
    shares: num(pick(metrics, ["shares"])) ?? num(pick(raw, ["shares"])),
    subsGained:
      num(pick(metrics, ["subs_gained", "subs_delta", "subscribers_gained"])) ??
      num(pick(raw, ["subs_gained", "subs_delta"])),
    capturedAtMs: toMs(pick(raw, ["captured_at", "capturedAt", "snapshot_at", "snapshotAt", "created_at"])),
    retentionCurve: parseRetentionPoints(
      pick(raw, ["retention_curve", "retentionCurve"]) ??
        pick(metrics, ["retention_curve", "retentionCurve"]),
    ),
  };
}

// ── Videos parsing (real read-model shape) ─────────────────────────────────

const BASELINE_METRIC_KEYS: BaselineMetricKey[] = [
  "views",
  "watch_hours",
  "likes",
  "comments_count",
  "shares",
  "ctr",
  "avg_view_duration_sec",
  "subs_gained",
];

function parseComparison(
  raw: unknown,
  unavailableReason: string | null,
): VideoComparison | null {
  if (!isRecord(raw)) {
    return unavailableReason
      ? { baselineKind: null, baselineVideoCount: null, deltas: {}, unavailableReason }
      : null;
  }
  const deltasRaw = isRecord(raw.deltas) ? raw.deltas : {};
  const deltas: Partial<Record<BaselineMetricKey, BaselineDelta>> = {};
  for (const key of BASELINE_METRIC_KEYS) {
    const entry = deltasRaw[key];
    if (!isRecord(entry)) continue;
    deltas[key] = {
      value: num(entry.value),
      baseline: num(entry.baseline),
      absolute: num(entry.absolute),
      pct: num(entry.pct),
    };
  }
  return {
    baselineKind: str(pick(raw, ["baseline_kind", "baselineKind"])),
    baselineVideoCount: num(pick(raw, ["baseline_video_count", "baselineVideoCount"])),
    deltas,
    unavailableReason,
  };
}

function parseVideoItem(raw: Record<string, unknown>): AnalyticsVideo | null {
  const videoId = str(
    pick(raw, ["youtube_video_id", "youtubeVideoId", "video_id", "videoId", "external_video_id", "video_external_id"]),
  );
  if (!videoId) return null;
  const unavailableReason = str(
    pick(raw, ["comparison_unavailable_reason", "comparisonUnavailableReason"]),
  );
  return {
    videoId,
    projectId: str(pick(raw, ["project_id", "projectId"])),
    title: str(pick(raw, ["title"])),
    publishedAtMs: toMs(pick(raw, ["published_at", "publishedAt", "publish_date"])),
    durationS: num(pick(raw, ["duration_s", "durationS", "duration_sec", "duration_seconds"])),
    url: str(pick(raw, ["url", "video_url", "videoUrl"])),
    dataState: dataStateOf(raw),
    latestSnapshot: parseSnapshot(pick(raw, ["latest_snapshot", "latestSnapshot"])),
    comparison: parseComparison(pick(raw, ["comparison"]), unavailableReason),
  };
}

export function parseAnalyticsVideosWire(raw: unknown): AnalyticsVideosData {
  const root = isRecord(raw) ? raw : {};
  const videos: AnalyticsVideo[] = [];
  for (const item of toItems(raw)) {
    if (!isRecord(item)) continue;
    const video = parseVideoItem(item);
    if (video) videos.push(video);
  }
  return {
    endpointMissing: false,
    dataState: dataStateOf(root),
    videos,
    lastSyncedMs: toMs(pick(root, ["last_synced_at", "lastSyncedAt", "synced_at"])),
  };
}

// ── Per-video snapshots parsing (milestone endpoint) ───────────────────────

export function parseVideoSnapshotsWire(raw: unknown): VideoSnapshotsData {
  const root = isRecord(raw) ? raw : {};
  const byId = new Map<string, VideoSnapshot>();
  const add = (entry: unknown) => {
    const snap = parseSnapshot(entry);
    if (!snap) return;
    const key = `${snap.kind ?? "?"}:${snap.capturedAtMs ?? 0}:${snap.views ?? ""}`;
    if (!byId.has(key)) byId.set(key, snap);
  };
  for (const item of toItems(raw)) add(item);
  const byKind = isRecord(root.by_kind) ? root.by_kind : isRecord(root.byKind) ? root.byKind : null;
  if (byKind) {
    for (const entries of Object.values(byKind)) {
      if (Array.isArray(entries)) entries.forEach(add);
    }
  }
  const snapshots = [...byId.values()].sort(
    (a, b) => (b.capturedAtMs ?? 0) - (a.capturedAtMs ?? 0),
  );
  const byPoint: Partial<Record<SnapshotPoint, VideoSnapshot>> = {};
  for (const snap of snapshots) {
    if (isSnapshotPoint(snap.kind) && !byPoint[snap.kind]) {
      byPoint[snap.kind] = snap; // snapshots are newest-first
    }
  }
  return {
    endpointMissing: false,
    dataState: dataStateOf(root),
    byPoint,
    snapshots,
    lastSyncedMs: toMs(pick(root, ["last_synced_at", "lastSyncedAt", "synced_at"])),
  };
}

// ── Channel aggregates + tile model ────────────────────────────────────────

export interface ChannelStats {
  /** Videos with a latest snapshot. */
  videoCount: number;
  viewsTotal: number | null;
  watchHoursTotal: number | null;
  /** Exact aggregate AVD: Σ watch time / Σ views over the same videos. */
  avgViewDurationS: number | null;
  /** Median of per-video average-%-viewed, when reported. */
  avgViewPercentageMedian: number | null;
  /** Median per-video CTR (CTR has no honest sum/weighted aggregate
   *  without impressions, which the read model does not provide). */
  ctrMedian: number | null;
  /** Videos whose comparison provided a usable baseline pair. */
  baselinePairCount: number;
  deltas: {
    views: number | null;
    watchHours: number | null;
    ctr: number | null;
    avgViewDurationS: number | null;
  };
}

const NO_DELTAS = { views: null, watchHours: null, ctr: null, avgViewDurationS: null };

/**
 * Aggregate the per-video read model into channel figures.
 *
 * Delta method (the read model provides baselines PER VIDEO — the
 * median of up to the last-10 other videos' same-kind snapshots):
 * for views / watch hours, the channel delta compares the SUM of the
 * paired videos' actuals against the SUM of their provided baselines
 * ("how the channel did vs how these videos would have done at the
 * channel's typical level"). For AVD the same sums are taken through
 * watch-time/views on both sides. For CTR, medians are compared
 * (current median vs median of provided baselines). A delta is only
 * produced from ≥2 paired videos — otherwise it stays null and the
 * UI shows no delta at all.
 */
export function aggregateChannelStats(videos: AnalyticsVideo[]): ChannelStats {
  const withSnap = videos.filter((v) => v.latestSnapshot !== null);
  let viewsTotal: number | null = null;
  let watchHoursTotal: number | null = null;
  let avdViews = 0;
  let avdWatch = 0;
  let avdPairs = 0;
  const ctrs: number[] = [];
  const pcts: number[] = [];
  for (const video of withSnap) {
    const snap = video.latestSnapshot as VideoSnapshot;
    if (snap.views !== null) viewsTotal = (viewsTotal ?? 0) + snap.views;
    if (snap.watchHours !== null) {
      watchHoursTotal = (watchHoursTotal ?? 0) + snap.watchHours;
    }
    if (snap.views !== null && snap.watchHours !== null && snap.views > 0) {
      avdViews += snap.views;
      avdWatch += snap.watchHours;
      avdPairs += 1;
    }
    if (snap.ctr !== null) ctrs.push(snap.ctr);
    if (snap.avgViewPercentage !== null) pcts.push(snap.avgViewPercentage);
  }

  // Baseline pairs: videos whose comparison carries the metric's
  // provided baseline + actual.
  const pairs = videos.filter(
    (v) => v.comparison && Object.keys(v.comparison.deltas).length > 0,
  );
  const sumDelta = (
    key: BaselineMetricKey,
  ): { current: number; baseline: number; count: number } | null => {
    let current = 0;
    let baseline = 0;
    let count = 0;
    for (const video of pairs) {
      const delta = video.comparison?.deltas[key];
      if (delta && delta.value !== null && delta.baseline !== null) {
        current += delta.value;
        baseline += delta.baseline;
        count += 1;
      }
    }
    return count >= 2 ? { current, baseline, count } : null;
  };

  let pairCount = 0;
  let viewsDelta: number | null = null;
  let watchDelta: number | null = null;
  let avdDelta: number | null = null;
  let ctrDelta: number | null = null;

  const viewsPair = sumDelta("views");
  if (viewsPair) {
    viewsDelta = deltaPct(viewsPair.current, viewsPair.baseline);
    pairCount = Math.max(pairCount, viewsPair.count);
  }
  const watchPair = sumDelta("watch_hours");
  if (watchPair) {
    watchDelta = deltaPct(watchPair.current, watchPair.baseline);
    pairCount = Math.max(pairCount, watchPair.count);
  }
  // AVD delta: (Σwatch/Σviews) actual vs (Σwatch/Σviews) baseline,
  // over the videos paired on BOTH metrics.
  {
    let curW = 0;
    let curV = 0;
    let baseW = 0;
    let baseV = 0;
    let count = 0;
    for (const video of pairs) {
      const w = video.comparison?.deltas.watch_hours;
      const v = video.comparison?.deltas.views;
      if (
        w && v &&
        w.value !== null && w.baseline !== null &&
        v.value !== null && v.baseline !== null && v.value > 0 && v.baseline > 0
      ) {
        curW += w.value;
        curV += v.value;
        baseW += w.baseline;
        baseV += v.baseline;
        count += 1;
      }
    }
    if (count >= 2) {
      avdDelta = deltaPct((curW * 3600) / curV, (baseW * 3600) / baseV);
      pairCount = Math.max(pairCount, count);
    }
  }
  // CTR delta: median of actuals vs median of provided baselines.
  {
    const currents: number[] = [];
    const baselines: number[] = [];
    for (const video of pairs) {
      const delta = video.comparison?.deltas.ctr;
      if (delta && delta.value !== null && delta.baseline !== null) {
        currents.push(delta.value);
        baselines.push(delta.baseline);
      }
    }
    if (currents.length >= 2) {
      ctrDelta = deltaPct(median(currents), median(baselines));
      pairCount = Math.max(pairCount, currents.length);
    }
  }

  return {
    videoCount: withSnap.length,
    viewsTotal,
    watchHoursTotal,
    avgViewDurationS: avdPairs > 0 ? (avdWatch * 3600) / avdViews : null,
    avgViewPercentageMedian: median(pcts),
    ctrMedian: median(ctrs),
    baselinePairCount: pairCount,
    deltas:
      pairCount > 0
        ? { views: viewsDelta, watchHours: watchDelta, ctr: ctrDelta, avgViewDurationS: avdDelta }
        : { ...NO_DELTAS },
  };
}

export type TileSource = "period" | "videos" | "lifetime" | "median" | "aggregate";

export interface ChannelTile {
  value: number | null;
  deltaPct: number | null;
  source: TileSource | null;
}

export interface ChannelTiles {
  views: ChannelTile;
  watchHours: ChannelTile;
  ctr: ChannelTile;
  avgViewDuration: ChannelTile;
  avgViewPercentageMedian: number | null;
  baselinePairCount: number;
  hasAnyValue: boolean;
}

/**
 * Compose the four overview tiles from the two real read models:
 * the overview's period figures win for views / watch hours when the
 * sync shape provides them; otherwise the per-video aggregates are
 * used (labelled by source in the UI). CTR is the per-video median;
 * AVD is the exact aggregate. Deltas come only from
 * {@link aggregateChannelStats} (per-video last-10 baselines).
 */
export function buildChannelTiles(
  overview: AnalyticsOverviewData | undefined,
  videos: AnalyticsVideo[],
): ChannelTiles {
  const stats = aggregateChannelStats(videos);
  const period = overview?.metrics?.period ?? null;

  const views: ChannelTile =
    period?.views !== null && period?.views !== undefined
      ? { value: period.views, deltaPct: null, source: "period" }
      : stats.viewsTotal !== null
        ? { value: stats.viewsTotal, deltaPct: stats.deltas.views, source: "videos" }
        : overview?.metrics?.totalViews !== null && overview?.metrics?.totalViews !== undefined
          ? { value: overview.metrics.totalViews, deltaPct: null, source: "lifetime" }
          : { value: null, deltaPct: null, source: null };

  const watchHours: ChannelTile =
    period?.watchHours !== null && period?.watchHours !== undefined
      ? { value: period.watchHours, deltaPct: null, source: "period" }
      : stats.watchHoursTotal !== null
        ? { value: stats.watchHoursTotal, deltaPct: stats.deltas.watchHours, source: "videos" }
        : overview?.metrics?.ordinaryWatchHours !== null &&
            overview?.metrics?.ordinaryWatchHours !== undefined
          ? { value: overview.metrics.ordinaryWatchHours, deltaPct: null, source: "lifetime" }
          : { value: null, deltaPct: null, source: null };

  const ctr: ChannelTile =
    stats.ctrMedian !== null
      ? { value: stats.ctrMedian, deltaPct: stats.deltas.ctr, source: "median" }
      : { value: null, deltaPct: null, source: null };

  const avgViewDuration: ChannelTile =
    stats.avgViewDurationS !== null
      ? { value: stats.avgViewDurationS, deltaPct: stats.deltas.avgViewDurationS, source: "aggregate" }
      : { value: null, deltaPct: null, source: null };

  return {
    views,
    watchHours,
    ctr,
    avgViewDuration,
    avgViewPercentageMedian: stats.avgViewPercentageMedian,
    baselinePairCount: stats.baselinePairCount,
    hasAnyValue:
      views.value !== null ||
      watchHours.value !== null ||
      ctr.value !== null ||
      avgViewDuration.value !== null,
  };
}

// ── Snapshot cell state (D+3 / D+7 / D+28 honesty) ─────────────────────────

export type SnapshotCellState =
  | { kind: "present"; snapshot: VideoSnapshot }
  | { kind: "pending"; dueMs: number }
  | { kind: "missing" }
  | { kind: "unknown" };

/**
 * Derive one table cell. A snapshot that exists is shown; a video too
 * young for the milestone is `pending` (due date = published + N
 * days); a video old enough whose snapshot was never captured is
 * `missing`; without a publish date the state is honestly `unknown`.
 */
export function snapshotCellState(
  video: AnalyticsVideo,
  snapshots: Partial<Record<SnapshotPoint, VideoSnapshot>>,
  point: SnapshotPoint,
  nowMs: number,
): SnapshotCellState {
  const snapshot = snapshots[point];
  if (snapshot) return { kind: "present", snapshot };
  if (video.publishedAtMs === null) return { kind: "unknown" };
  const dueMs = video.publishedAtMs + SNAPSHOT_POINT_DAYS[point] * 86_400_000;
  return nowMs < dueMs ? { kind: "pending", dueMs } : { kind: "missing" };
}

/** The snapshot whose retention curve the chart should prefer. */
export function preferredRetentionSnapshot(
  snapshots: VideoSnapshot[],
): { kind: string | null; snapshot: VideoSnapshot } | null {
  for (const point of ["d7", "d28", "d3"] as const) {
    const snap = snapshots.find(
      (s) => s.kind === point && s.retentionCurve.length > 0,
    );
    if (snap) return { kind: point, snapshot: snap };
  }
  const any = snapshots.find((s) => s.retentionCurve.length > 0);
  return any ? { kind: any.kind, snapshot: any } : null;
}

// ── Retention parsing (RetentionSectionResult contract) ──────────────────

function parseRetentionSections(raw: unknown, durationS: number | null): RetentionSection[] {
  const list = Array.isArray(raw) ? raw : [];
  const sections: RetentionSection[] = [];
  list.forEach((entry, position) => {
    if (!isRecord(entry)) return;
    const startS = num(pick(entry, ["start_s", "startS", "start_sec", "start"]));
    const endS = num(pick(entry, ["end_s", "endS", "end_sec", "end"]));
    if (startS === null || endS === null || endS <= startS) return;
    const startRatio =
      num(pick(entry, ["start_ratio", "startRatio"])) ??
      (durationS ? startS / durationS : null);
    const endRatio =
      num(pick(entry, ["end_ratio", "endRatio"])) ??
      (durationS ? endS / durationS : null);
    sections.push({
      index: num(pick(entry, ["index"])) ?? position,
      title:
        str(pick(entry, ["title", "name", "label"])) ?? `Section ${position + 1}`,
      kind: str(pick(entry, ["kind", "type", "section_kind", "section_id"])),
      startS,
      endS,
      startRatio,
      endRatio,
      entryRatio: num(pick(entry, ["entry_ratio", "entryRatio"])),
      exitRatio: num(pick(entry, ["exit_ratio", "exitRatio"])),
      dropOff: num(pick(entry, ["drop_off", "dropOff"])),
      annotation: str(pick(entry, ["annotation"])),
    });
  });
  sections.sort((a, b) => a.startS - b.startS);
  return sections;
}

const EMPTY_RETENTION: RetentionData = {
  status: "no_data",
  fromSnapshotFallback: false,
  points: [],
  sections: [],
  steepestDrop: null,
  annotations: [],
  durationS: null,
  snapshotKind: null,
  capturedAtMs: null,
  timingSource: null,
  timingPrecision: null,
};

export function parseRetentionWire(raw: unknown): RetentionData {
  if (!isRecord(raw)) return EMPTY_RETENTION;
  const record = isRecord(raw.retention) ? raw.retention : raw;

  const durationS = num(pick(record, ["duration_s", "durationS", "duration_sec"]));
  const points = parseRetentionPoints(
    pick(record, ["points", "curve", "retention_curve", "retentionCurve", "retention_curve_json"]),
  );

  const sectionMap = isRecord(record.section_map)
    ? record.section_map
    : isRecord(record.sectionMap)
      ? record.sectionMap
      : null;
  const sections = parseRetentionSections(
    pick(record, ["sections"]) ?? (sectionMap ? sectionMap.sections : undefined),
    durationS ?? (sectionMap ? num(sectionMap.duration_s) : null),
  );

  const statusRaw = str(pick(record, ["status"]));
  const status: RetentionStatus =
    statusRaw === "ok" || statusRaw === "unmapped" || statusRaw === "no_data"
      ? statusRaw
      : points.length > 0
        ? sections.length > 0
          ? "ok"
          : "unmapped"
        : "no_data";

  let steepestDrop: RetentionSteepestDrop | null = null;
  const dropRaw = pick(record, ["steepest_drop", "steepestDrop"]);
  if (isRecord(dropRaw)) {
    const atS = num(pick(dropRaw, ["at_s", "atS", "at_sec"]));
    if (atS !== null) {
      steepestDrop = {
        atS,
        atText: str(pick(dropRaw, ["at_text", "atText"])),
        drop: num(pick(dropRaw, ["drop"])),
        sectionTitle: str(pick(dropRaw, ["section_title", "sectionTitle"])),
      };
    }
  }

  const annotationsRaw = pick(record, ["annotations"]);
  return {
    status,
    fromSnapshotFallback: false,
    points,
    sections,
    steepestDrop,
    annotations: Array.isArray(annotationsRaw)
      ? annotationsRaw.filter((a): a is string => typeof a === "string" && a.length > 0)
      : [],
    durationS,
    snapshotKind: str(pick(record, ["snapshot_kind", "snapshotKind", "snapshot_point", "snapshotPoint"])),
    capturedAtMs: toMs(pick(record, ["captured_at", "capturedAt", "snapshot_at", "created_at"])),
    timingSource:
      str(pick(record, ["timing_source", "timingSource"])) ??
      (sectionMap ? str(pick(sectionMap, ["source"])) : null),
    timingPrecision:
      str(pick(record, ["timing_precision", "timingPrecision"])) ??
      (sectionMap ? str(pick(sectionMap, ["precision"])) : null),
  };
}

/**
 * Retention state derived from a video's snapshots (the fallback
 * while the retention-analysis route is unlanded): a stored curve on
 * any snapshot renders `unmapped` — the backend's own honest state
 * for "curve present, no section map". No curve anywhere → no_data.
 */
export function retentionFromSnapshots(
  snapshots: VideoSnapshot[],
  durationS: number | null,
): RetentionData {
  const preferred = preferredRetentionSnapshot(snapshots);
  if (!preferred) return { ...EMPTY_RETENTION, durationS };
  return {
    ...EMPTY_RETENTION,
    status: "unmapped",
    fromSnapshotFallback: true,
    points: preferred.snapshot.retentionCurve,
    durationS,
    snapshotKind: preferred.kind,
    capturedAtMs: preferred.snapshot.capturedAtMs,
  };
}

// ── Transport (mirrors queries/ypp.ts) ─────────────────────────────────────

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

function isMissingEndpoint(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

// ── Enrichment join (existing read models: publications + projects) ───────

interface VideoEnrichment {
  title: string | null;
  publishedAtMs: number | null;
  projectId: string | null;
  url: string | null;
}

async function fetchEnrichment(): Promise<Map<string, VideoEnrichment>> {
  const byVideoId = new Map<string, VideoEnrichment>();
  const byProjectId = new Map<string, VideoEnrichment>();

  const fill = (
    map: Map<string, VideoEnrichment>,
    key: string | null,
    patch: Partial<VideoEnrichment>,
  ) => {
    if (!key) return;
    const current = map.get(key) ?? {
      title: null,
      publishedAtMs: null,
      projectId: null,
      url: null,
    };
    current.title ??= patch.title ?? null;
    current.publishedAtMs ??= patch.publishedAtMs ?? null;
    current.projectId ??= patch.projectId ?? null;
    current.url ??= patch.url ?? null;
    map.set(key, current);
  };

  // Publications: publish_records rows (title + youtube_video_id are
  // real columns; published_at is read tolerantly, created_at backs
  // it up). The analytics read model already joins publish records
  // itself, so this only fills gaps. A failure here must not sink
  // the analytics data itself.
  try {
    const raw = await request("/publications?limit=200");
    for (const item of toItems(raw)) {
      if (!isRecord(item)) continue;
      const patch: VideoEnrichment = {
        title: str(pick(item, ["title"])),
        publishedAtMs:
          toMs(pick(item, ["published_at", "publishedAt"])) ??
          toMs(pick(item, ["created_at", "createdAt"])),
        projectId: str(pick(item, ["project_id", "projectId"])),
        url: str(pick(item, ["video_url", "videoUrl", "url"])),
      };
      fill(byVideoId, str(pick(item, ["youtube_video_id", "youtubeVideoId", "video_id"])), patch);
      fill(byProjectId, patch.projectId, patch);
    }
  } catch {
    // Enrichment is best-effort; analytics rows render without it.
  }

  try {
    const raw = await request("/projects?limit=200");
    for (const item of toItems(raw)) {
      if (!isRecord(item)) continue;
      const patch: VideoEnrichment = {
        title: str(pick(item, ["title"])) ?? str(pick(item, ["topic"])),
        publishedAtMs: toMs(pick(item, ["published_at", "publishedAt"])),
        projectId: str(pick(item, ["id"])),
        url: null,
      };
      fill(byVideoId, str(pick(item, ["youtube_video_id", "youtubeVideoId"])), patch);
      fill(byProjectId, patch.projectId, patch);
    }
  } catch {
    // Same best-effort rule as publications.
  }

  const merged = new Map<string, VideoEnrichment>(byVideoId);
  for (const [key, value] of byProjectId) merged.set(`project:${key}`, value);
  return merged;
}

function enrichVideos(
  videos: AnalyticsVideo[],
  enrichment: Map<string, VideoEnrichment>,
): AnalyticsVideo[] {
  return videos.map((video) => {
    const byVideo = enrichment.get(video.videoId);
    const byProject = video.projectId
      ? enrichment.get(`project:${video.projectId}`)
      : undefined;
    const source = byVideo ?? byProject;
    if (!source) return video;
    return {
      ...video,
      title: video.title ?? source.title,
      publishedAtMs: video.publishedAtMs ?? source.publishedAtMs,
      projectId: video.projectId ?? source.projectId,
      url: video.url ?? source.url,
    };
  });
}

// ── Hooks ──────────────────────────────────────────────────────────────────

/**
 * Channel overview read model. Disabled without a connected channel —
 * a disconnected channel renders the screen's not-connected state,
 * not stale figures. 404 (unknown channel) → `endpointMissing` data,
 * not an error.
 */
export function useAnalyticsOverview(channelId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: ["analytics", "overview", channelId],
    enabled: channelId !== null && enabled,
    retry: 1,
    refetchInterval: 60_000,
    queryFn: async (): Promise<AnalyticsOverviewData> => {
      try {
        const raw = await request(analyticsOverviewPath(channelId ?? ""));
        return parseAnalyticsOverviewWire(raw);
      } catch (err) {
        if (isMissingEndpoint(err)) return { ...EMPTY_OVERVIEW, endpointMissing: true };
        throw err;
      }
    },
  });
}

/** Per-video latest snapshots + last-10 median comparisons. */
export function useAnalyticsVideos(channelId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: ["analytics", "videos", channelId],
    enabled: channelId !== null && enabled,
    retry: 1,
    refetchInterval: 60_000,
    queryFn: async (): Promise<AnalyticsVideosData> => {
      let parsed: AnalyticsVideosData;
      try {
        const raw = await request(analyticsVideosPath(channelId ?? ""));
        parsed = parseAnalyticsVideosWire(raw);
      } catch (err) {
        if (isMissingEndpoint(err)) {
          return { endpointMissing: true, dataState: null, videos: [], lastSyncedMs: null };
        }
        throw err;
      }
      const enrichment = await fetchEnrichment();
      return { ...parsed, videos: enrichVideos(parsed.videos, enrichment) };
    },
  });
}

async function fetchVideoSnapshots(
  channelId: string,
  videoId: string,
): Promise<VideoSnapshotsData> {
  try {
    const raw = await request(videoSnapshotsPath(channelId, videoId));
    return parseVideoSnapshotsWire(raw);
  } catch (err) {
    if (isMissingEndpoint(err)) {
      return {
        endpointMissing: true,
        dataState: null,
        byPoint: {},
        snapshots: [],
        lastSyncedMs: null,
      };
    }
    throw err;
  }
}

/** One video's milestone snapshots (the table's D+3/D+7/D+28 source). */
export function useVideoSnapshots(
  channelId: string | null,
  videoId: string | null,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["analytics", "video-snapshots", channelId, videoId],
    enabled: channelId !== null && videoId !== null && enabled,
    retry: 1,
    refetchInterval: 60_000,
    queryFn: () => fetchVideoSnapshots(channelId ?? "", videoId ?? ""),
  });
}

/** Milestone snapshots for up to `limit` videos at once (the table). */
export function useVideosSnapshots(
  channelId: string | null,
  videoIds: string[],
  enabled: boolean,
  limit = 25,
) {
  const ids = videoIds.slice(0, limit);
  const results = useQueries({
    queries: ids.map((videoId) => ({
      queryKey: ["analytics", "video-snapshots", channelId, videoId] as const,
      enabled: channelId !== null && enabled,
      retry: 1,
      refetchInterval: 60_000,
      queryFn: () => fetchVideoSnapshots(channelId ?? "", videoId),
    })),
  });
  const byVideoId = new Map<string, VideoSnapshotsData>();
  let anyPending = false;
  let firstError: unknown = null;
  results.forEach((result, index) => {
    if (result.data) byVideoId.set(ids[index], result.data);
    if (result.isPending) anyPending = true;
    if (result.isError && firstError === null) firstError = result.error;
  });
  return { byVideoId, anyPending, firstError, total: ids.length };
}

/**
 * Retention analysis for one video (documented contract; the route
 * has not landed yet). 404 → a `RetentionData` with status
 * `endpoint_missing`, NOT an error — the screen then falls back to
 * any curve stored on the video's snapshots
 * ({@link retentionFromSnapshots}).
 */
export function useRetentionAnalysis(
  channelId: string | null,
  videoId: string | null,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["analytics", "retention", channelId, videoId],
    enabled: channelId !== null && videoId !== null && enabled,
    retry: 1,
    queryFn: async (): Promise<RetentionData> => {
      try {
        const raw = await request(videoRetentionPath(channelId ?? "", videoId ?? ""));
        return parseRetentionWire(raw);
      } catch (err) {
        if (isMissingEndpoint(err)) {
          return { ...EMPTY_RETENTION, status: "endpoint_missing" };
        }
        throw err;
      }
    },
  });
}
