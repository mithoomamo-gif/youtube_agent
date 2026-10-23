import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  AlertTriangle,
  ClipboardList,
  DollarSign,
  Play,
  Upload,
  Video,
  MonitorPlay,
  WifiOff,
} from "lucide-react";
import { Card } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { Badge } from "../components/ui/Badge";
import { ProgressBar } from "../components/ui/ProgressBar";
import { StatCard } from "../components/ui/StatCard";
import Views28DaysCard from "../components/dashboard/Views28DaysCard";
import LearningHighlightsCard from "../components/dashboard/LearningHighlightsCard";
import PendingCommentDraftsCard from "../components/dashboard/PendingCommentDraftsCard";
import {
  AWAITING_REVIEW_STATUS,
  IN_PRODUCTION_STATUSES,
  PUBLISHED_STATUS,
  isOfflineError,
  toChannelMini,
  useMissionChannels,
  useMissionProjects,
  useQueueStats,
  useQuota,
  useUsageToday,
} from "../api/queries/mission";
import type { MissionProject } from "../api/queries/mission";

type BadgeTone = "violet" | "green" | "amber" | "red" | "gray";

// ── Formatting ────────────────────────────────────────────────────────────

function formatUsd(micros: number): string {
  return `$${(micros / 1_000_000).toFixed(2)}`;
}

function formatUsdValue(usd: number): string {
  return `$${usd.toFixed(2)}`;
}

function titleCase(value: string): string {
  return value.length > 0
    ? value.charAt(0).toUpperCase() + value.slice(1).replace(/_/g, " ")
    : value;
}

function formatDate(ms: number): string {
  return new Date(ms).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

// ── Shared panel states (existing design language) ────────────────────────

function StatSkeleton({ icon }: { icon: ReactNode }) {
  return (
    <Card className="p-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="h-3 w-24 animate-pulse rounded bg-raised" />
          <div className="mt-3 h-8 w-20 animate-pulse rounded bg-raised" />
          <div className="mt-2.5 h-3 w-32 animate-pulse rounded bg-raised" />
        </div>
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-accent/15 text-accent2">
          {icon}
        </div>
      </div>
    </Card>
  );
}

function StatErrorCard({
  label,
  icon,
  offline,
  onRetry,
}: {
  label: string;
  icon: ReactNode;
  offline: boolean;
  onRetry: () => void;
}) {
  return (
    <Card className="p-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-wide text-muted">
            {label}
          </p>
          <p className="tnum mt-2 font-mono text-3xl font-bold text-ink">—</p>
          <p className="mt-1.5 flex items-center gap-2 text-xs text-ink2">
            {offline ? "Backend offline" : "Couldn't load"}
            <button
              type="button"
              onClick={onRetry}
              className="font-medium text-accent2 hover:text-ink"
            >
              Retry
            </button>
          </p>
        </div>
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-accent/15 text-accent2">
          {icon}
        </div>
      </div>
    </Card>
  );
}

function PanelState({
  offline,
  title,
  text,
  onRetry,
}: {
  offline: boolean;
  title: string;
  text: string;
  onRetry: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-line bg-raised px-4 py-8 text-center">
      {offline ? (
        <WifiOff className="h-6 w-6 text-warn" />
      ) : (
        <AlertTriangle className="h-6 w-6 text-warn" />
      )}
      <p className="mt-3 text-sm font-semibold">{title}</p>
      <p className="mt-1 max-w-sm text-xs leading-relaxed text-ink2">{text}</p>
      <div className="mt-4">
        <Button variant="outline" size="sm" onClick={onRetry}>
          Retry
        </Button>
      </div>
    </div>
  );
}

function OfflineBanner() {
  return (
    <div className="flex items-center gap-3 rounded-2xl border border-warn/50 bg-warn/10 px-4 py-3">
      <WifiOff className="h-5 w-5 shrink-0 text-warn" />
      <p className="text-sm text-ink">
        <span className="font-semibold">Backend offline.</span>{" "}
        <span className="text-ink2">
          The local backend isn't responding, so live numbers are hidden
          until it reconnects.
        </span>
      </p>
    </div>
  );
}

// ── Screen ────────────────────────────────────────────────────────────────

export default function MissionControl() {
  const queue = useQueueStats();
  const projects = useMissionProjects();
  const usageToday = useUsageToday();
  const quota = useQuota();
  const channels = useMissionChannels();

  const projectList = projects.data ?? [];
  const producing = projectList.filter((p) =>
    IN_PRODUCTION_STATUSES.includes(p.status),
  );
  const awaitingReview = projectList.filter(
    (p) => p.status === AWAITING_REVIEW_STATUS,
  ).length;
  const published = projectList.filter((p) => p.status === PUBLISHED_STATUS);
  const publishedDated = published.filter(
    (p) => (p.publishedAtMs ?? p.createdAtMs) !== null,
  );
  const weekAgo = Date.now() - 7 * 24 * 60 * 60 * 1000;
  const publishedThisWeek = publishedDated.filter(
    (p) => (p.publishedAtMs ?? p.createdAtMs ?? 0) >= weekAgo,
  );
  const lastPublishedMs =
    publishedDated.length > 0
      ? Math.max(
          ...publishedDated.map((p) => p.publishedAtMs ?? p.createdAtMs ?? 0),
        )
      : null;
  const publishedWeekKnown =
    published.length > 0 && publishedDated.length === published.length;

  const channelList = (channels.data ?? []).map(toChannelMini);

  const anyOffline =
    (queue.isError && isOfflineError(queue.error)) ||
    (projects.isError && isOfflineError(projects.error)) ||
    (usageToday.isError && isOfflineError(usageToday.error)) ||
    (quota.isError && isOfflineError(quota.error)) ||
    (channels.isError && isOfflineError(channels.error));

  return (
    <div className="min-h-full space-y-5 bg-app p-6 text-ink">
      <header>
        <h1 className="text-2xl font-bold tracking-tight">Mission Control</h1>
        <p className="mt-1 text-sm text-ink2">
          Overview of production pipeline and system status
        </p>
      </header>

      {anyOffline ? <OfflineBanner /> : null}

      <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-5">
        {queue.isPending ? (
          <StatSkeleton icon={<Video className="h-5 w-5" />} />
        ) : queue.isError ? (
          <StatErrorCard
            label="In Production"
            icon={<Video className="h-5 w-5" />}
            offline={isOfflineError(queue.error)}
            onRetry={() => void queue.refetch()}
          />
        ) : (
          <StatCard
              label="In Production"
              value={`${queue.data.running} ${queue.data.running === 1 ? "job" : "jobs"}`}
              sub={`${queue.data.queued} queued`}
              icon={<Video className="h-5 w-5" />}
            />
        )}

        {projects.isPending ? (
          <StatSkeleton icon={<ClipboardList className="h-5 w-5" />} />
        ) : projects.isError ? (
          <StatErrorCard
            label="Awaiting Review"
            icon={<ClipboardList className="h-5 w-5" />}
            offline={isOfflineError(projects.error)}
            onRetry={() => void projects.refetch()}
          />
        ) : (
          <StatCard
            label="Awaiting Review"
            value={`${awaitingReview}`}
            sub={
              awaitingReview > 0 ? "Needs human review" : "Nothing waiting"
            }
            icon={<ClipboardList className="h-5 w-5" />}
          />
        )}

        {projects.isPending ? (
          <StatSkeleton icon={<Upload className="h-5 w-5" />} />
        ) : projects.isError ? (
          <StatErrorCard
            label="Published"
            icon={<Upload className="h-5 w-5" />}
            offline={isOfflineError(projects.error)}
            onRetry={() => void projects.refetch()}
          />
        ) : published.length === 0 ? (
          <StatCard
            label="Published this week"
            value="0"
            sub="Nothing published yet"
            icon={<Upload className="h-5 w-5" />}
          />
        ) : publishedWeekKnown ? (
          <StatCard
            label="Published this week"
            value={`${publishedThisWeek.length}`}
            sub={
              lastPublishedMs !== null
                ? `Last: ${formatDate(lastPublishedMs)}`
                : undefined
            }
            icon={<Upload className="h-5 w-5" />}
          />
        ) : (
          <StatCard
            label="Published"
            value={`${published.length}`}
            sub="All-time published · weekly split unavailable"
            icon={<Upload className="h-5 w-5" />}
          />
        )}

        {usageToday.isPending ? (
          <StatSkeleton icon={<DollarSign className="h-5 w-5" />} />
        ) : usageToday.isError ? (
          <StatErrorCard
            label="Today's cost"
            icon={<DollarSign className="h-5 w-5" />}
            offline={isOfflineError(usageToday.error)}
            onRetry={() => void usageToday.refetch()}
          />
        ) : (
          <StatCard
            label="Today's cost"
            value={formatUsdValue(usageToday.data.estUsd)}
            sub={
              usageToday.data.hasData
                ? `${usageToday.data.calls} API ${usageToday.data.calls === 1 ? "call" : "calls"} today`
                : "No usage recorded today"
            }
            icon={<DollarSign className="h-5 w-5" />}
          />
        )}

        <Views28DaysCard />
      </section>

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-[2fr_1fr]">
        <LearningHighlightsCard />
        <PendingCommentDraftsCard />
      </section>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[2fr_1fr]">
        <Card className="p-5">
          <div className="mb-4">
            <h2 className="text-base font-semibold">Now Producing</h2>
            <p className="text-sm text-muted">Active jobs in pipeline</p>
          </div>

          {projects.isPending ? (
            <div className="space-y-4">
              {[0, 1].map((i) => (
                <div
                  key={i}
                  className="flex gap-4 rounded-xl border border-line bg-raised p-3"
                >
                  <div className="h-24 w-40 shrink-0 animate-pulse rounded-lg bg-panel" />
                  <div className="min-w-0 flex-1 py-1">
                    <div className="h-4 w-2/3 animate-pulse rounded bg-panel" />
                    <div className="mt-3 h-3 w-1/2 animate-pulse rounded bg-panel" />
                    <div className="mt-2 h-3 w-1/3 animate-pulse rounded bg-panel" />
                  </div>
                </div>
              ))}
            </div>
          ) : projects.isError ? (
            <PanelState
              offline={isOfflineError(projects.error)}
              title={
                isOfflineError(projects.error)
                  ? "Backend offline"
                  : "Couldn't load projects"
              }
              text={
                isOfflineError(projects.error)
                  ? "The local backend isn't responding. Production status will appear here once it reconnects."
                  : "The backend returned an error while loading projects. Try again in a moment."
              }
              onRetry={() => void projects.refetch()}
            />
          ) : producing.length === 0 ? (
            <div className="rounded-xl border border-dashed border-line bg-raised px-4 py-8 text-center">
              <p className="text-sm font-semibold">Nothing in production</p>
              <p className="mt-1 text-xs leading-relaxed text-ink2">
                No videos are being produced right now. Start a new video and
                it will appear here while it moves through the pipeline.
              </p>
            </div>
          ) : (
            <div className="space-y-4">
              {producing.map((project, index) => (
                <ProducingRow
                  key={project.id}
                  project={project}
                  highlighted={index === 0}
                />
              ))}
            </div>
          )}
        </Card>

        <div className="space-y-5">
          <Card className="p-5">
            <h2 className="mb-4 text-base font-semibold">Connected Channels</h2>
            {channels.isPending ? (
              <div className="space-y-3">
                {[0, 1].map((i) => (
                  <div
                    key={i}
                    className="flex items-center gap-3 rounded-xl border border-line bg-raised p-3"
                  >
                    <div className="h-9 w-9 shrink-0 animate-pulse rounded-lg bg-panel" />
                    <div className="min-w-0 flex-1">
                      <div className="h-3.5 w-2/3 animate-pulse rounded bg-panel" />
                      <div className="mt-2 h-3 w-1/2 animate-pulse rounded bg-panel" />
                    </div>
                  </div>
                ))}
              </div>
            ) : channels.isError ? (
              <PanelState
                offline={isOfflineError(channels.error)}
                title={
                  isOfflineError(channels.error)
                    ? "Backend offline"
                    : "Couldn't load channels"
                }
                text={
                  isOfflineError(channels.error)
                    ? "The local backend isn't responding. Connected channels will appear here once it reconnects."
                    : "The backend returned an error while loading channels. Try again in a moment."
                }
                onRetry={() => void channels.refetch()}
              />
            ) : channelList.length === 0 ? (
              <div className="rounded-xl border border-dashed border-line bg-raised px-4 py-6 text-center">
                <p className="text-sm font-semibold">No channels connected</p>
                <p className="mt-1 text-xs leading-relaxed text-ink2">
                  Connect a channel from the Channels screen and it will show
                  up here.
                </p>
              </div>
            ) : (
              <div className="space-y-3">
                {channelList.map((channel) => (
                  <div
                    key={channel.id}
                    className="flex items-center gap-3 rounded-xl border border-line bg-raised p-3"
                  >
                    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-danger text-white">
                      <MonitorPlay className="h-5 w-5" />
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-semibold">
                        {channel.title}
                      </p>
                      <p className="text-xs text-ink2">
                        {channel.handle ? `${channel.handle} · ` : ""}
                        <ChannelStatus status={channel.status} />
                      </p>
                    </div>
                    <Button variant="ghost" size="sm">
                      Switch
                    </Button>
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card className="p-5">
            <h2 className="text-base font-semibold">YouTube Quota</h2>
            <p className="mb-4 text-sm text-muted">
              Daily quota usage · resets at midnight Pacific
            </p>
            {quota.isPending ? (
              <div className="space-y-4">
                {[0, 1, 2].map((i) => (
                  <div key={i}>
                    <div className="h-3 w-24 animate-pulse rounded bg-raised" />
                    <div className="mt-2 h-2 w-full animate-pulse rounded-full bg-raised" />
                  </div>
                ))}
              </div>
            ) : quota.isError ? (
              <PanelState
                offline={isOfflineError(quota.error)}
                title={
                  isOfflineError(quota.error)
                    ? "Backend offline"
                    : "Couldn't load quota"
                }
                text={
                  isOfflineError(quota.error)
                    ? "The local backend isn't responding. Quota usage will appear here once it reconnects."
                    : "The backend returned an error while loading YouTube quota usage. Try again in a moment."
                }
                onRetry={() => void quota.refetch()}
              />
            ) : (
              <div className="space-y-4">
                <QuotaRow
                  label="Uploads"
                  used={quota.data.buckets.uploads.used}
                  limit={quota.data.buckets.uploads.limit}
                />
                <QuotaRow
                  label="Search"
                  used={quota.data.buckets.search.used}
                  limit={quota.data.buckets.search.limit}
                />
                <QuotaRow
                  label="Shared units"
                  used={quota.data.buckets.shared.used}
                  limit={quota.data.buckets.shared.limit}
                />
                <QuotaResetCountdown
                  resetsAt={quota.data.resetsAt}
                  secondsUntilReset={quota.data.secondsUntilReset}
                  pacificDate={quota.data.pacificDate}
                />
              </div>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

// ── Pieces ────────────────────────────────────────────────────────────────

function formatCount(value: number): string {
  return value.toLocaleString("en-US");
}

function QuotaRow({
  label,
  used,
  limit,
}: {
  label: string;
  used: number;
  limit: number;
}) {
  const pct = limit > 0 ? (used / limit) * 100 : 0;
  const tone = pct >= 90 ? "red" : pct >= 70 ? "amber" : "violet";
  return (
    <div>
      <div className="mb-1.5 flex items-baseline justify-between gap-2">
        <p className="text-sm font-medium">{label}</p>
        <p className="tnum font-mono text-xs text-ink2">
          {formatCount(used)} / {formatCount(limit)}
          <span className="text-muted"> · {Math.round(pct)}%</span>
        </p>
      </div>
      <ProgressBar value={pct} tone={tone} />
    </div>
  );
}

/**
 * Live countdown to the Pacific-midnight quota reset. The deadline
 * comes from the backend's `resets_at` when it parses, otherwise from
 * `seconds_until_reset` anchored at fetch time; the clock ticks every
 * 30s so the figure stays honest between the hook's 60s refetches.
 */
function QuotaResetCountdown({
  resetsAt,
  secondsUntilReset,
  pacificDate,
}: {
  resetsAt?: string;
  secondsUntilReset?: number;
  pacificDate?: string;
}) {
  const [fetchedAt] = useState(() => Date.now());
  const deadlineMs = useMemo(() => {
    if (resetsAt) {
      const parsed = Date.parse(resetsAt);
      if (!Number.isNaN(parsed)) return parsed;
    }
    if (typeof secondsUntilReset === "number") {
      return fetchedAt + secondsUntilReset * 1000;
    }
    return null;
  }, [resetsAt, secondsUntilReset, fetchedAt]);

  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);

  let countdown: string | null = null;
  if (deadlineMs !== null) {
    const remainingSec = Math.max(0, Math.round((deadlineMs - nowMs) / 1000));
    if (remainingSec <= 0) {
      countdown = "Reset due — fresh buckets arrive shortly";
    } else {
      const hours = Math.floor(remainingSec / 3600);
      const minutes = Math.floor((remainingSec % 3600) / 60);
      countdown =
        hours > 0
          ? `Resets in ${hours}h ${minutes}m`
          : minutes > 0
            ? `Resets in ${minutes}m`
            : "Resets in under a minute";
    }
  }

  if (!countdown && !pacificDate) return null;
  return (
    <p className="border-t border-line pt-3 text-xs text-muted">
      {countdown}
      {countdown && pacificDate ? " · " : ""}
      {pacificDate ? `Pacific date: ${pacificDate}` : ""}
    </p>
  );
}

function ChannelStatus({ status }: { status: string | null }) {
  if (!status) return <span className="text-ink2">Status unknown</span>;
  if (status === "active") return <span className="text-ok">● Online</span>;
  if (status === "paused") return <span className="text-warn">● Paused</span>;
  if (status === "disconnected")
    return <span className="text-danger">● Disconnected</span>;
  if (status === "at_risk") return <span className="text-warn">● At risk</span>;
  return <span className="text-ink2">● {titleCase(status)}</span>;
}

function ProducingRow({
  project,
  highlighted,
}: {
  project: MissionProject;
  highlighted: boolean;
}) {
  const stageLabel = project.phase
    ? titleCase(project.phase)
    : titleCase(project.status);
  const stageTone: BadgeTone = project.phase ? "violet" : "gray";
  const metaParts: string[] = [];
  metaParts.push(`Status: ${titleCase(project.status)}`);
  if (project.targetMinutes !== null) {
    metaParts.push(`Target ${project.targetMinutes} min`);
  }
  metaParts.push(`Cost so far ${formatUsd(project.totalCostMicros)}`);
  if (project.createdAtMs !== null) {
    metaParts.push(`Created ${formatDate(project.createdAtMs)}`);
  }

  return (
    <div className="flex gap-4 rounded-xl border border-line bg-raised p-3">
      <div
        className={`relative flex h-24 w-40 shrink-0 items-center justify-center overflow-hidden rounded-lg ${highlighted ? "accent-gradient" : "bg-panel"}`}
      >
        <span className="flex h-10 w-10 items-center justify-center rounded-full bg-black/40 text-white">
          <Play className="h-5 w-5" />
        </span>
        {project.targetMinutes !== null ? (
          <span className="tnum absolute right-2 bottom-2 rounded bg-black/70 px-1.5 py-0.5 font-mono text-[11px] text-white">
            {project.targetMinutes} min
          </span>
        ) : null}
      </div>

      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="truncate text-sm font-semibold">{project.title}</h3>
          <Badge tone={stageTone}>{stageLabel}</Badge>
        </div>
        <p className="mt-3 text-sm text-ink2">
          {project.phase
            ? `Currently in the ${titleCase(project.phase)} phase of the pipeline.`
            : "In the production pipeline."}
        </p>
        <p className="mt-2 text-xs text-muted">{metaParts.join(" · ")}</p>
      </div>
    </div>
  );
}
