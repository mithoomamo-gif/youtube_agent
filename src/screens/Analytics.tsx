/**
 * Analytics screen (Round 5, worker W3) — roadmap §3.15.
 *
 * Everything on this screen comes from the analytics read models via
 * `src/api/queries/analytics.ts` (real contract:
 * `backend/app/routers/analytics.py` + `app.analytics_read`):
 *
 * - Per-channel overview tiles (views, watch hours, CTR, average view
 *   duration). The overview endpoint itself carries channel period /
 *   lifetime figures; CTR, AVD and the last-10 baseline deltas are
 *   per-video in the read model, so the tiles compose them exactly as
 *   documented in `buildChannelTiles` / `aggregateChannelStats`.
 *   When a baseline is missing, NO delta is rendered — never a
 *   fake 0%.
 * - The freshness stamp reads "Analytics updated [time]" and is
 *   driven by the real last-sync time in the analytics data
 *   (`last_synced_at`, falling back to the newest snapshot capture).
 * - Per-video milestone table at D+3 / D+7 / D+28, from the per-video
 *   snapshots endpoint. A video too young for a milestone shows an
 *   honest "Pending — due [date]" cell; an overdue-but-uncaptured
 *   milestone shows "Not captured". Each video also shows its
 *   read-model comparison vs the last-10 median baseline when one
 *   was provided.
 * - A retention curve per selected video, annotated with the
 *   script-section boundaries (hook / body sections / CTA) from the
 *   video's production section map. HONEST GAP in the landed
 *   backend: no route serves retention curves or section annotations
 *   yet, so until the retention-analysis route lands this panel shows
 *   its not-available state (a curve stored on a snapshot, if one is
 *   ever served, renders without labels — never fabricated ones).
 *
 * The YPP progress component is W4's (`components/analytics/
 * YppProgress.tsx`); it is mounted below the overview tiles via its
 * wired wrapper. The Growth view is W6's separate file — not here.
 * The old screen's hard-coded "learned rules" panel is gone: learning
 * rules are the learning team's surface (dashboard highlights +
 * Growth view), and fake rules must never render as real ones.
 *
 * Every panel degrades to an honest not-connected / not-landed /
 * no-data state — there are no placeholder numbers anywhere.
 */

import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Clock,
  Eye,
  Hourglass,
  Link2Off,
  MousePointerClick,
  RefreshCw,
  Timer,
} from "lucide-react";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { YppProgressForChannel } from "../components/analytics/YppProgress";
import { RetentionChart, formatClock } from "../components/analytics/RetentionChart";
import { useChannels } from "../api/queries/channels";
import type { ChannelWire } from "../api/queries/channels";
import {
  buildChannelTiles,
  isOfflineError,
  retentionFromSnapshots,
  snapshotCellState,
  SNAPSHOT_POINTS,
  SNAPSHOT_POINT_LABELS,
  useAnalyticsOverview,
  useAnalyticsVideos,
  useRetentionAnalysis,
  useVideoSnapshots,
  useVideosSnapshots,
} from "../api/queries/analytics";
import type {
  AnalyticsVideo,
  ChannelTile,
  ChannelTiles,
  SnapshotPoint,
  VideoSnapshot,
  VideoSnapshotsData,
} from "../api/queries/analytics";

// ── Small shared pieces (selector styling mirrors the Channels screen) ─────

const inputClass =
  "w-full rounded-lg border border-line bg-raised px-3 py-2 text-sm text-ink outline-none focus:border-accent placeholder:text-muted";
const labelClass = "mb-1 block text-xs font-medium text-ink2";

/** Connected for analytics purposes: listed and not in a
 *  disconnected / re-auth state (mirrors the Channels screen's
 *  status vocabulary). Paused / at-risk channels keep their grant. */
function channelConnected(channel: ChannelWire | null): boolean {
  if (!channel) return false;
  const raw = (channel.status ?? channel.state ?? "").toLowerCase();
  return ![
    "disconnected",
    "needs_reauth",
    "token-expired",
    "token_expired",
    "expired",
  ].includes(raw);
}

function channelTitle(channel: ChannelWire): string {
  return channel.title || channel.handle || channel.id;
}

// ── Formatting ─────────────────────────────────────────────────────────────

function formatCount(value: number): string {
  return Math.round(value).toLocaleString("en-US");
}

function formatHours(value: number): string {
  return value.toLocaleString("en-US", {
    minimumFractionDigits: value < 10 ? 1 : 0,
    maximumFractionDigits: 1,
  });
}

function formatCtr(fraction: number): string {
  return `${(fraction * 100).toFixed(1)}%`;
}

function formatDateTime(ms: number): string {
  return new Date(ms).toLocaleString("en-GB", {
    day: "numeric",
    month: "short",
    hour: "numeric",
    minute: "2-digit",
  });
}

function formatDate(ms: number): string {
  return new Date(ms).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
  });
}

// ── Panels ─────────────────────────────────────────────────────────────────

function StatePanel({
  icon,
  title,
  body,
  onRetry,
}: {
  icon: React.ReactNode;
  title: string;
  body: string;
  onRetry?: () => void;
}) {
  return (
    <div className="flex items-start gap-3">
      <span className="mt-0.5 text-muted">{icon}</span>
      <div>
        <p className="text-sm font-medium text-ink">{title}</p>
        <p className="mt-1 max-w-2xl text-sm leading-relaxed text-muted">{body}</p>
        {onRetry ? (
          <Button variant="ghost" size="sm" className="mt-2" onClick={onRetry}>
            <RefreshCw className="mr-1 h-3.5 w-3.5" />
            Retry
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function ErrorPanel({
  title,
  error,
  onRetry,
}: {
  title: string;
  error: unknown;
  onRetry: () => void;
}) {
  return (
    <StatePanel
      icon={<AlertTriangle className="h-5 w-5 text-warn" />}
      title={title}
      body={
        isOfflineError(error)
          ? "The backend isn't reachable, so nothing is shown — a connection problem is not a zero. Check that the app backend is running, then retry."
          : "The analytics request failed, so no figures are shown. This is a loading problem, not a zero — the channel's actual performance is unchanged."
      }
      onRetry={onRetry}
    />
  );
}

// ── Overview tiles ─────────────────────────────────────────────────────────

function MetricTile({
  label,
  value,
  icon,
  tile,
  valueSub,
  nullSub,
}: {
  label: string;
  value: string;
  icon: React.ReactNode;
  tile: ChannelTile;
  valueSub: string;
  nullSub: string;
}) {
  return (
    <Card className="p-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-wide text-muted">
            {label}
          </p>
          <p className="tnum mt-2 font-mono text-3xl font-bold text-ink">{value}</p>
          {tile.deltaPct !== null ? (
            <p
              className={`mt-1.5 text-xs font-medium ${tile.deltaPct >= 0 ? "text-ok" : "text-danger"}`}
            >
              {tile.deltaPct >= 0 ? "+" : ""}
              {tile.deltaPct.toFixed(1)}% vs last-10 baseline
            </p>
          ) : (
            <p className="mt-1.5 text-xs text-ink2">
              {tile.value !== null ? valueSub : nullSub}
            </p>
          )}
        </div>
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-accent/15 text-accent2">
          {icon}
        </div>
      </div>
    </Card>
  );
}

function sourceSub(tile: ChannelTile, trackedVideos: number): string {
  switch (tile.source) {
    case "period":
      return "Latest reported period";
    case "videos":
      return `Across ${trackedVideos} tracked video${trackedVideos === 1 ? "" : "s"}`;
    case "lifetime":
      return "Lifetime total";
    case "median":
      return "Median across this channel's videos";
    case "aggregate":
      return "Across this channel's tracked videos";
    default:
      return "";
  }
}

function OverviewTiles({
  tiles,
  trackedVideos,
}: {
  tiles: ChannelTiles;
  trackedVideos: number;
}) {
  const anyDelta =
    tiles.views.deltaPct !== null ||
    tiles.watchHours.deltaPct !== null ||
    tiles.ctr.deltaPct !== null ||
    tiles.avgViewDuration.deltaPct !== null;
  return (
    <div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <MetricTile
          label="Views"
          value={tiles.views.value !== null ? formatCount(tiles.views.value) : "—"}
          icon={<Eye className="h-5 w-5" />}
          tile={tiles.views}
          valueSub={sourceSub(tiles.views, trackedVideos)}
          nullSub="Not reported yet"
        />
        <MetricTile
          label="Watch hours"
          value={
            tiles.watchHours.value !== null ? formatHours(tiles.watchHours.value) : "—"
          }
          icon={<Clock className="h-5 w-5" />}
          tile={tiles.watchHours}
          valueSub={sourceSub(tiles.watchHours, trackedVideos)}
          nullSub="Not reported yet"
        />
        <MetricTile
          label="CTR"
          value={tiles.ctr.value !== null ? formatCtr(tiles.ctr.value) : "—"}
          icon={<MousePointerClick className="h-5 w-5" />}
          tile={tiles.ctr}
          valueSub={sourceSub(tiles.ctr, trackedVideos)}
          nullSub="Unavailable from Analytics"
        />
        <MetricTile
          label="Avg. view duration"
          value={
            tiles.avgViewDuration.value !== null
              ? formatClock(tiles.avgViewDuration.value)
              : "—"
          }
          icon={<Timer className="h-5 w-5" />}
          tile={tiles.avgViewDuration}
          valueSub={
            tiles.avgViewPercentageMedian !== null
              ? `${tiles.avgViewPercentageMedian.toFixed(0)}% of the video watched on average (median)`
              : sourceSub(tiles.avgViewDuration, trackedVideos)
          }
          nullSub="Not reported yet"
        />
      </div>
      {anyDelta ? (
        <p className="mt-2 text-xs leading-relaxed text-muted">
          Deltas vs the last-10 baseline: the read model compares each
          video with the median of up to 10 of the channel's other
          videos; the tile delta aggregates those provided baselines
          over {tiles.baselinePairCount} videos. Baselines are
          channel-relative — a growing channel's raw numbers are never
          read as creative improvement on their own.
        </p>
      ) : null}
      {tiles.ctr.value === null ? (
        <p className="mt-2 text-xs leading-relaxed text-muted">
          CTR is shown only when Analytics reports it per video — it is
          never estimated. Per-video CTR, when present, is in the table
          below.
        </p>
      ) : null}
    </div>
  );
}

// ── Per-video milestone table ──────────────────────────────────────────────

function SnapshotCell({
  video,
  snapshots,
  point,
  nowMs,
}: {
  video: AnalyticsVideo;
  snapshots: VideoSnapshotsData | undefined;
  point: SnapshotPoint;
  nowMs: number;
}) {
  const state = snapshotCellState(video, snapshots?.byPoint ?? {}, point, nowMs);
  if (state.kind === "present") {
    const snap: VideoSnapshot = state.snapshot;
    const parts: string[] = [];
    if (snap.avgViewDurationS !== null) parts.push(`AVD ${formatClock(snap.avgViewDurationS)}`);
    if (snap.ctr !== null) parts.push(`CTR ${formatCtr(snap.ctr)}`);
    if (snap.watchHours !== null) parts.push(`${formatHours(snap.watchHours)} watch h`);
    return (
      <div
        title={
          snap.capturedAtMs !== null
            ? `Captured ${formatDateTime(snap.capturedAtMs)}`
            : undefined
        }
      >
        <p className="font-mono text-sm text-ink tnum">
          {snap.views !== null ? formatCount(snap.views) : "—"}
        </p>
        {parts.length > 0 ? (
          <p className="mt-0.5 text-[11px] leading-snug text-muted">{parts.join(" · ")}</p>
        ) : null}
      </div>
    );
  }
  if (state.kind === "pending") {
    return (
      <div>
        <Badge tone="gray">Pending</Badge>
        <p className="mt-1 text-[11px] text-muted">
          Due {formatDate(state.dueMs)}
        </p>
      </div>
    );
  }
  if (state.kind === "missing") {
    return (
      <p className="text-xs text-muted" title="This milestone was due but no snapshot has been captured yet.">
        Not captured
      </p>
    );
  }
  return <p className="text-xs text-muted">—</p>;
}

/** The read model's per-video comparison, in one honest line. */
function ComparisonLine({ video }: { video: AnalyticsVideo }) {
  const comparison = video.comparison;
  if (!comparison) return null;
  const viewsPct = comparison.deltas.views?.pct ?? null;
  if (viewsPct !== null) {
    return (
      <p className="mt-0.5 text-[11px] text-muted">
        Views{" "}
        <span className={viewsPct >= 0 ? "text-ok" : "text-danger"}>
          {viewsPct >= 0 ? "+" : ""}
          {viewsPct.toFixed(1)}%
        </span>{" "}
        vs last-10 median
        {comparison.baselineKind ? ` (${comparison.baselineKind.toUpperCase()} snapshots)` : ""}
      </p>
    );
  }
  if (comparison.unavailableReason === "insufficient_baseline") {
    return (
      <p className="mt-0.5 text-[11px] text-muted">
        No baseline yet — not enough comparable videos on this channel.
      </p>
    );
  }
  return null;
}

// ── Screen ─────────────────────────────────────────────────────────────────

export default function Analytics() {
  const channelsQuery = useChannels();
  const channels = channelsQuery.data ?? [];

  const [selectedChannelId, setSelectedChannelId] = useState<string | null>(null);
  useEffect(() => {
    if (channels.length === 0) return;
    if (!selectedChannelId || !channels.some((c) => c.id === selectedChannelId)) {
      setSelectedChannelId(channels[0].id);
    }
  }, [channels, selectedChannelId]);

  const selectedChannel =
    channels.find((c) => c.id === selectedChannelId) ?? channels[0] ?? null;
  const connected = channelConnected(selectedChannel);
  const channelId = selectedChannel?.id ?? null;

  const overview = useAnalyticsOverview(channelId, connected);
  const videosQuery = useAnalyticsVideos(channelId, connected);

  const videos = useMemo(
    () => videosQuery.data?.videos ?? [],
    [videosQuery.data],
  );
  const videoIds = useMemo(() => videos.map((v) => v.videoId), [videos]);
  const snapshotsMap = useVideosSnapshots(channelId, videoIds, connected);

  const [selectedVideoId, setSelectedVideoId] = useState<string | null>(null);
  useEffect(() => {
    if (videos.length === 0) return;
    if (!selectedVideoId || !videos.some((v) => v.videoId === selectedVideoId)) {
      setSelectedVideoId(videos[0].videoId);
    }
  }, [videos, selectedVideoId]);

  const selectedVideo =
    videos.find((v) => v.videoId === selectedVideoId) ?? videos[0] ?? null;

  // Selected video's snapshots: the shared per-video query (same cache
  // key the table's batch uses, so this never double-fetches).
  const selectedSnapshots = useVideoSnapshots(
    channelId,
    selectedVideo?.videoId ?? null,
    connected,
  );
  const analysis = useRetentionAnalysis(
    channelId,
    selectedVideo?.videoId ?? null,
    connected,
  );

  const retentionData = useMemo(() => {
    if (analysis.data && analysis.data.status !== "endpoint_missing") {
      return {
        ...analysis.data,
        durationS: analysis.data.durationS ?? selectedVideo?.durationS ?? null,
      };
    }
    if (selectedSnapshots.data) {
      return retentionFromSnapshots(
        selectedSnapshots.data.snapshots,
        selectedVideo?.durationS ?? null,
      );
    }
    return analysis.data ?? null; // endpoint_missing placeholder or still loading
  }, [analysis.data, selectedSnapshots.data, selectedVideo]);

  const tiles = useMemo(
    () => buildChannelTiles(overview.data, videos),
    [overview.data, videos],
  );
  const trackedVideos = useMemo(
    () => videos.filter((v) => v.latestSnapshot !== null).length,
    [videos],
  );

  // Freshness: the read model's own last-sync time wins; otherwise the
  // newest snapshot capture across the analytics payloads. Both are
  // real sync times from the analytics data — never a hard-coded time.
  const lastSyncedMs = useMemo(() => {
    let newest: number | null = overview.data?.lastSyncedMs ?? null;
    const consider = (ms: number | null | undefined) => {
      if (typeof ms === "number" && (newest === null || ms > newest)) newest = ms;
    };
    consider(videosQuery.data?.lastSyncedMs);
    consider(overview.data?.metrics?.capturedAtMs);
    for (const video of videos) consider(video.latestSnapshot?.capturedAtMs);
    for (const entry of snapshotsMap.byVideoId.values()) {
      consider(entry.lastSyncedMs);
      for (const snap of entry.snapshots) consider(snap.capturedAtMs);
    }
    return newest;
  }, [overview.data, videosQuery.data, videos, snapshotsMap.byVideoId]);

  const nowMs = Date.now();

  // Overview section state, in precedence order. Data wins over a
  // sibling query's error: if either read model produced figures,
  // they are shown.
  const overviewState: "tiles" | "loading" | "error" | "missing" | "not_connected" | "no_data" =
    !connected
      ? "not_connected"
      : tiles.hasAnyValue
        ? "tiles"
        : overview.isPending || videosQuery.isPending
          ? "loading"
          : overview.data?.dataState === "not_connected" ||
              videosQuery.data?.dataState === "not_connected"
            ? "not_connected"
            : overview.isError && videosQuery.isError
              ? "error"
              : overview.data?.endpointMissing && videosQuery.data?.endpointMissing
                ? "missing"
                : "no_data";

  return (
    <div className="bg-app min-h-full p-6 text-ink">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-ink">Analytics</h1>
          <p className="mt-1 text-sm text-muted">
            {lastSyncedMs !== null
              ? `Analytics updated ${formatDateTime(lastSyncedMs)}`
              : "Analytics updated — not synced yet"}
            {" · "}YouTube Analytics reports lag 48–72h, so figures are
            snapshots, not live numbers.
          </p>
        </div>
        {channels.length > 0 && selectedChannel ? (
          <div className="w-full sm:w-64">
            <label className={labelClass} htmlFor="analytics-channel">
              Channel
            </label>
            <select
              id="analytics-channel"
              className={inputClass}
              value={selectedChannel.id}
              onChange={(e) => setSelectedChannelId(e.target.value)}
            >
              {channels.map((c) => (
                <option key={c.id} value={c.id}>
                  {channelTitle(c)}
                </option>
              ))}
            </select>
          </div>
        ) : null}
      </header>

      {/* ── Channel-level states ─────────────────────────────────── */}
      {channelsQuery.isPending ? (
        <Card className="p-5">
          <div aria-busy="true" aria-label="Loading channels">
            <div className="h-4 w-48 animate-pulse rounded bg-raised" />
            <div className="mt-3 h-4 w-72 animate-pulse rounded bg-raised" />
            <p className="mt-3 text-xs text-muted">Loading channels…</p>
          </div>
        </Card>
      ) : channelsQuery.isError ? (
        <Card className="p-5">
          <ErrorPanel
            title="Couldn't load channels"
            error={channelsQuery.error}
            onRetry={() => void channelsQuery.refetch()}
          />
        </Card>
      ) : channels.length === 0 ? (
        <Card className="p-5">
          <StatePanel
            icon={<Link2Off className="h-5 w-5" />}
            title="No channel connected"
            body="Connect a YouTube channel on the Channels screen first. Analytics is owner-only data pulled with that channel's own grant — nothing can be shown, or estimated, without one."
          />
        </Card>
      ) : (
        <>
          {/* ── Overview ─────────────────────────────────────────── */}
          {overviewState === "not_connected" ? (
            <Card className="p-5">
              <StatePanel
                icon={<Link2Off className="h-5 w-5" />}
                title="Channel not connected"
                body={`“${selectedChannel ? channelTitle(selectedChannel) : "This channel"}” is disconnected or needs re-authentication, so its analytics can't be pulled. Reconnect it on the Channels screen — no figures are shown for a disconnected channel.`}
              />
            </Card>
          ) : overviewState === "loading" ? (
            <div
              className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4"
              aria-busy="true"
              aria-label="Loading analytics overview"
            >
              {[0, 1, 2, 3].map((i) => (
                <Card key={i} className="p-5">
                  <div className="h-3 w-20 animate-pulse rounded bg-raised" />
                  <div className="mt-3 h-8 w-24 animate-pulse rounded bg-raised" />
                  <div className="mt-2 h-3 w-28 animate-pulse rounded bg-raised" />
                </Card>
              ))}
            </div>
          ) : overviewState === "error" ? (
            <Card className="p-5">
              <ErrorPanel
                title="Couldn't load analytics"
                error={overview.error ?? videosQuery.error}
                onRetry={() => {
                  void overview.refetch();
                  void videosQuery.refetch();
                }}
              />
            </Card>
          ) : overviewState === "missing" ? (
            <Card className="p-5">
              <StatePanel
                icon={<Hourglass className="h-5 w-5" />}
                title="Analytics isn't available yet"
                body="The analytics read model isn't connected yet. Views, watch hours, CTR and view duration for this channel will appear here after it lands and the first sync completes — nothing is estimated in the meantime."
              />
            </Card>
          ) : overviewState === "no_data" ? (
            <Card className="p-5">
              <StatePanel
                icon={<Hourglass className="h-5 w-5" />}
                title="No analytics synced yet"
                body={`This channel has no analytics figures yet. YouTube Analytics reports lag 48–72 hours; figures appear here after the first sync — nothing is estimated in the meantime.${
                  overview.data?.sync?.lastReason
                    ? ` Last sync note: ${overview.data.sync.lastReason}.`
                    : ""
                }`}
              />
            </Card>
          ) : (
            <OverviewTiles tiles={tiles} trackedVideos={trackedVideos} />
          )}

          {/* ── YPP progress — W4's component, mounted here ──────── */}
          <div className="mt-6">
            <YppProgressForChannel
              channelId={channelId}
              channelConnected={connected}
              channelTitle={selectedChannel ? channelTitle(selectedChannel) : undefined}
            />
          </div>

          {/* ── Per-video milestones ─────────────────────────────── */}
          <Card className="mt-6 p-5">
            <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="text-base font-semibold text-ink">Videos</h2>
              {videos.length > 0 ? (
                <span className="text-xs text-muted">
                  {videos.length} video{videos.length === 1 ? "" : "s"}
                  {snapshotsMap.total < videos.length
                    ? ` · snapshots shown for the newest ${snapshotsMap.total}`
                    : ""}
                </span>
              ) : null}
            </div>
            <p className="mb-4 text-xs leading-relaxed text-muted">
              Milestone snapshots are captured at D+3, D+7 and D+28 after
              publish — the points learning is allowed to act on. Select
              a video to see its retention curve below.
            </p>

            {!connected ? (
              <p className="text-sm text-muted">
                Connect the channel to see per-video snapshots.
              </p>
            ) : videosQuery.isPending ? (
              <div aria-busy="true" aria-label="Loading videos">
                <div className="h-4 w-full animate-pulse rounded bg-raised" />
                <div className="mt-2 h-4 w-5/6 animate-pulse rounded bg-raised" />
                <div className="mt-2 h-4 w-4/6 animate-pulse rounded bg-raised" />
              </div>
            ) : videosQuery.isError ? (
              <ErrorPanel
                title="Couldn't load per-video analytics"
                error={videosQuery.error}
                onRetry={() => void videosQuery.refetch()}
              />
            ) : videosQuery.data?.endpointMissing ? (
              <StatePanel
                icon={<Hourglass className="h-5 w-5" />}
                title="Per-video analytics isn't available yet"
                body="The analytics read model isn't connected yet, so no milestone snapshots can be shown. They will appear here once it lands — no rows are invented in the meantime."
              />
            ) : videos.length === 0 ? (
              <StatePanel
                icon={<Hourglass className="h-5 w-5" />}
                title="No videos with analytics yet"
                body="No published videos on this channel have analytics snapshots yet. The first snapshot for a video is captured at D+3 after publish."
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-line text-left text-xs text-muted">
                      <th className="pb-2 pr-3 font-medium">Video</th>
                      <th className="pb-2 pr-3 font-medium">Published</th>
                      {SNAPSHOT_POINTS.map((point) => (
                        <th key={point} className="pb-2 pr-3 font-medium">
                          {SNAPSHOT_POINT_LABELS[point]}
                          <span className="block font-normal">views</span>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {videos.map((video) => {
                      const isSelected = selectedVideo?.videoId === video.videoId;
                      return (
                        <tr
                          key={video.videoId}
                          onClick={() => setSelectedVideoId(video.videoId)}
                          aria-selected={isSelected}
                          className={`cursor-pointer border-b border-line last:border-0 ${
                            isSelected ? "bg-accent/10" : "hover:bg-raised/50"
                          }`}
                        >
                          <td className="py-3 pr-3">
                            <p className="max-w-md truncate font-medium text-ink">
                              {video.title ?? "Untitled video"}
                            </p>
                            <p className="mt-0.5 font-mono text-[11px] text-muted">
                              {video.videoId}
                              {video.durationS !== null
                                ? ` · ${formatClock(video.durationS)}`
                                : ""}
                            </p>
                            <ComparisonLine video={video} />
                          </td>
                          <td className="py-3 pr-3 whitespace-nowrap text-ink2">
                            {video.publishedAtMs !== null
                              ? formatDate(video.publishedAtMs)
                              : "—"}
                          </td>
                          {SNAPSHOT_POINTS.map((point) => (
                            <td key={point} className="py-3 pr-3 align-top">
                              <SnapshotCell
                                video={video}
                                snapshots={snapshotsMap.byVideoId.get(video.videoId)}
                                point={point}
                                nowMs={nowMs}
                              />
                            </td>
                          ))}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          {/* ── Retention ────────────────────────────────────────── */}
          <Card className="mt-6 p-5">
            <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="text-base font-semibold text-ink">
                  Retention
                  {selectedVideo?.title ? (
                    <span className="font-normal text-muted"> — {selectedVideo.title}</span>
                  ) : null}
                </h2>
                <p className="mt-1 max-w-2xl text-xs leading-relaxed text-muted">
                  The curve is annotated with the script's section
                  boundaries from the video's production data, so a
                  drop-off can be read against the exact section it
                  happened in.
                </p>
              </div>
              {videos.length > 1 ? (
                <div className="w-full sm:w-72">
                  <label className={labelClass} htmlFor="analytics-retention-video">
                    Video
                  </label>
                  <select
                    id="analytics-retention-video"
                    className={inputClass}
                    value={selectedVideo?.videoId ?? ""}
                    onChange={(e) => setSelectedVideoId(e.target.value)}
                  >
                    {videos.map((v) => (
                      <option key={v.videoId} value={v.videoId}>
                        {v.title ?? v.videoId}
                      </option>
                    ))}
                  </select>
                </div>
              ) : null}
            </div>

            {!connected ? (
              <p className="text-sm text-muted">
                Connect the channel to see retention curves.
              </p>
            ) : !selectedVideo ? (
              <p className="text-sm text-muted">
                {videosQuery.data?.endpointMissing
                  ? "Retention curves appear here once the analytics read model is connected."
                  : "No published video to show a retention curve for yet."}
              </p>
            ) : analysis.isPending || selectedSnapshots.isPending ? (
              <div aria-busy="true" aria-label="Loading retention curve">
                <div className="h-56 w-full animate-pulse rounded bg-raised" />
                <p className="mt-3 text-xs text-muted">Loading retention curve…</p>
              </div>
            ) : analysis.isError ? (
              <ErrorPanel
                title="Couldn't load the retention curve"
                error={analysis.error}
                onRetry={() => void analysis.refetch()}
              />
            ) : !retentionData || retentionData.status === "endpoint_missing" ? (
              <StatePanel
                icon={<Hourglass className="h-5 w-5" />}
                title="Retention isn't available yet"
                body="The analytics read API doesn't serve retention curves yet, and this video's snapshots carry no stored curve. When the retention analysis lands, the curve will appear here annotated with the script's sections — nothing is drawn from estimates."
              />
            ) : retentionData.status === "no_data" || retentionData.points.length === 0 ? (
              <StatePanel
                icon={<Hourglass className="h-5 w-5" />}
                title="No retention curve captured yet"
                body="This video has no retention curve yet. The first curve is pulled at D+3 after publish (Analytics data itself lags 48–72h), and section annotations appear with it."
              />
            ) : (
              <div>
                {retentionData.fromSnapshotFallback ? (
                  <p className="mb-3 rounded-lg bg-raised/60 px-3 py-2 text-xs leading-relaxed text-ink2">
                    Showing the curve stored on this video's
                    {retentionData.snapshotKind
                      ? ` ${SNAPSHOT_POINT_LABELS[retentionData.snapshotKind as SnapshotPoint] ?? retentionData.snapshotKind.toUpperCase()}`
                      : ""}{" "}
                    snapshot. Script-section annotations aren't
                    available for this video yet, so the curve is shown
                    without section labels — labels are never guessed.
                  </p>
                ) : retentionData.status === "unmapped" ? (
                  <p className="mb-3 rounded-lg bg-raised/60 px-3 py-2 text-xs leading-relaxed text-ink2">
                    No script section map is stored for this video, so
                    the curve is shown without section labels — labels
                    are never guessed.
                  </p>
                ) : (
                  <p className="mb-3 text-xs text-muted">
                    Section boundaries from the production section map
                    {retentionData.timingSource
                      ? ` (${retentionData.timingSource.replace(/_/g, " ")})`
                      : ""}
                    {retentionData.snapshotKind
                      ? ` · snapshot ${retentionData.snapshotKind.toUpperCase()}`
                      : ""}
                    {retentionData.capturedAtMs !== null
                      ? ` · captured ${formatDateTime(retentionData.capturedAtMs)}`
                      : ""}
                    .
                  </p>
                )}

                <RetentionChart
                  points={retentionData.points}
                  sections={retentionData.sections}
                  durationS={retentionData.durationS ?? selectedVideo.durationS}
                  steepestDrop={retentionData.steepestDrop}
                />

                {retentionData.steepestDrop ? (
                  <p className="mt-2 text-sm text-ink2">
                    Steepest drop at{" "}
                    <span className="font-mono text-ink tnum">
                      {retentionData.steepestDrop.atText ??
                        formatClock(retentionData.steepestDrop.atS)}
                    </span>
                    {retentionData.steepestDrop.sectionTitle
                      ? ` — inside “${retentionData.steepestDrop.sectionTitle}”`
                      : ""}
                    {retentionData.steepestDrop.drop !== null
                      ? ` (${Math.round(retentionData.steepestDrop.drop * 100)} pts of the audience)`
                      : ""}
                    .
                  </p>
                ) : null}

                {retentionData.sections.length > 0 ? (
                  <ul className="mt-4 divide-y divide-line">
                    {retentionData.sections.map((section) => (
                      <li
                        key={`${section.index}-${section.startS}`}
                        className="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-2.5"
                      >
                        <span className="min-w-40 text-sm font-medium text-ink">
                          {section.title}
                        </span>
                        <span className="font-mono text-xs text-muted tnum">
                          {formatClock(section.startS)}–{formatClock(section.endS)}
                        </span>
                        {section.entryRatio !== null && section.exitRatio !== null ? (
                          <span className="font-mono text-xs text-ink2 tnum">
                            {Math.round(section.entryRatio * 100)}% →{" "}
                            {Math.round(section.exitRatio * 100)}% watching
                          </span>
                        ) : null}
                        {section.dropOff !== null && section.dropOff > 0 ? (
                          <Badge tone="amber">
                            −{Math.round(section.dropOff * 100)} pts in this section
                          </Badge>
                        ) : section.dropOff !== null && section.dropOff < 0 ? (
                          <Badge tone="green">Audience grew in this section</Badge>
                        ) : null}
                        {section.annotation ? (
                          <span className="w-full text-xs leading-relaxed text-muted">
                            {section.annotation}
                          </span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                ) : null}

                {retentionData.annotations.length > 0 ? (
                  <ul className="mt-3 space-y-1">
                    {retentionData.annotations.map((note, i) => (
                      <li key={i} className="text-xs leading-relaxed text-ink2">
                        • {note}
                      </li>
                    ))}
                  </ul>
                ) : null}

                {retentionData.timingPrecision ? (
                  <p className="mt-3 text-[11px] text-muted">
                    Boundary precision: {retentionData.timingPrecision}
                  </p>
                ) : null}
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
