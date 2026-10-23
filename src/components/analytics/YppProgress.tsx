/**
 * YppProgress — YouTube Partner Program progress (Round 5, worker W4).
 *
 * Self-contained component; the Analytics screen (W3) mounts it via
 * props only:
 *
 * ```tsx
 * <YppProgress
 *   channelConnected={channelConnected}
 *   data={yppData}            // YppProgressData | null, from useYppProgress
 *   loading={…}
 *   loadFailed={…}
 *   onRetry={…}
 * />
 * ```
 *
 * or via the wired wrapper {@link YppProgressForChannel}, which runs
 * `useYppProgress(channelId, channelConnected)` itself.
 *
 * All decisions (which state, which bars, which numbers) come from the
 * pure shaping module `yppProgressModel.ts`. The HARD ROADMAP RULE —
 * never estimate qualification from ordinary watch time — is enforced
 * there and in the wire parser (`src/api/queries/ypp.ts`): when
 * qualification data is unavailable this component receives NO number
 * for the watch-hours bars and renders the explicit "qualification
 * data unavailable" treatment instead of a bar. Ordinary watch hours,
 * when reported, appear on their own line, labelled as NOT the
 * qualification figure.
 *
 * States rendered (see `shapeYppProgress` for triggers):
 * `not_connected`, `loading`, `load_error`, `no_analytics`, and
 * `ready` — with a per-bar unavailable treatment inside `ready` when
 * the subscriber count or the qualification figure is not reported.
 */

import {
  AlertTriangle,
  CalendarClock,
  CheckCircle2,
  Clock,
  Hourglass,
  Link2Off,
  RefreshCw,
  Users,
} from "lucide-react";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { ProgressBar } from "../ui/ProgressBar";
import {
  formatFigure,
  formatHours,
  shapeYppProgress,
  YPP_CURRENT_BAR_DEADLINE_LABEL,
  YPP_UPCOMING_EFFECTIVE_LABEL,
  type YppBarModel,
  type YppProgressData,
} from "./yppProgressModel";
import { useYppProgress } from "../../api/queries/ypp";

export interface YppProgressProps {
  /** False when there is no channel or it is disconnected/needs reauth. */
  channelConnected: boolean;
  /** Figures from the analytics read model; null = nothing reported yet. */
  data: YppProgressData | null;
  loading?: boolean;
  loadFailed?: boolean;
  /** Shown in the header when provided (the owning channel's title). */
  channelTitle?: string;
  onRetry?: () => void;
  className?: string;
}

function formatAsOf(asOf: string | null): string | null {
  if (!asOf) return null;
  const date = new Date(asOf);
  if (Number.isNaN(date.getTime())) return asOf;
  return date.toLocaleString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** The unavailable treatment for a single bar: NO bar, NO percentage —
 *  an explicit dashed notice instead, so nothing can be misread as 0%. */
function UnavailableBar({ bar, note }: { bar: YppBarModel; note: string }) {
  return (
    <div>
      <BarHeading bar={bar} />
      <div className="flex items-start gap-2 rounded-lg border border-dashed border-line bg-raised/60 px-3 py-2.5">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warn" />
        <div>
          <p className="text-sm font-medium text-ink2">{note}</p>
          <p className="mt-0.5 text-xs text-muted">
            No figure is shown rather than an estimate.
          </p>
        </div>
      </div>
    </div>
  );
}

function BarHeading({ bar }: { bar: YppBarModel }) {
  return (
    <div className="mb-1.5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-sm font-medium text-ink">{bar.label}</p>
        {bar.met ? <Badge tone="green">Target met</Badge> : null}
      </div>
      {/* The target is labelled explicitly on every bar (roadmap rule). */}
      <p className="text-xs text-muted">{bar.targetLabel}</p>
    </div>
  );
}

function AvailableBar({
  bar,
  format,
  unit,
  tone,
}: {
  bar: YppBarModel;
  format: (value: number) => string;
  unit: string;
  tone: "violet" | "amber";
}) {
  if (!bar.available || bar.current === null || bar.pct === null) return null;
  return (
    <div>
      <BarHeading bar={bar} />
      <div className="flex items-center gap-3">
        <ProgressBar
          value={bar.pct}
          tone={bar.met ? "green" : tone}
          className="flex-1"
        />
        <span className="font-mono text-sm text-ink2 tnum">
          {format(bar.current)} / {format(bar.target)} {unit}
        </span>
      </div>
      <p className="mt-1 text-xs text-muted">
        {bar.met
          ? "Target reached on reported figures."
          : `${format(bar.remaining ?? 0)} ${unit} to go · ${Math.floor(bar.pct)}% of target`}
      </p>
    </div>
  );
}

/** Ordinary watch time, on its own line, never inside a bar. */
function OrdinaryWatchHoursLine({ hours }: { hours: number }) {
  return (
    <p className="flex items-start gap-2 text-xs text-muted">
      <Clock className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span>
        All watch time (ordinary, last 12 months):{" "}
        <span className="font-mono text-ink2 tnum">{formatHours(hours)}</span>{" "}
        hours — shown for context only. This is{" "}
        <span className="font-semibold text-ink2">not</span> the qualification
        figure and is never used to estimate eligibility.
      </span>
    </p>
  );
}

function StateNotice({
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
        <p className="mt-1 text-sm text-muted">{body}</p>
        {onRetry ? (
          <Button
            variant="ghost"
            size="sm"
            className="mt-2"
            onClick={onRetry}
          >
            <RefreshCw className="mr-1 h-3.5 w-3.5" />
            Retry
          </Button>
        ) : null}
      </div>
    </div>
  );
}

export function YppProgress({
  channelConnected,
  data,
  loading = false,
  loadFailed = false,
  channelTitle,
  onRetry,
  className = "",
}: YppProgressProps) {
  const vm = shapeYppProgress({ channelConnected, loading, loadFailed, data });
  const asOfLabel = formatAsOf(vm.asOf);

  return (
    <Card className={`p-5 ${className}`}>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-base font-semibold text-ink">
          YouTube Partner Program
          {channelTitle ? (
            <span className="font-normal text-muted"> — {channelTitle}</span>
          ) : null}
        </h2>
        {vm.state === "ready" ? (
          <span className="text-xs text-muted">
            {asOfLabel
              ? `Data as of ${asOfLabel}`
              : "As-of time not reported by Analytics"}
          </span>
        ) : null}
      </div>

      {vm.state === "not_connected" ? (
        <StateNotice
          icon={<Link2Off className="h-5 w-5" />}
          title="Channel not connected"
          body="Connect a YouTube channel to see its Partner Program progress. No progress is shown for a disconnected channel."
        />
      ) : null}

      {vm.state === "loading" ? (
        <div aria-busy="true" aria-label="Loading Partner Program progress">
          <div className="space-y-4">
            {[0, 1].map((i) => (
              <div key={i}>
                <div className="mb-1.5 h-4 w-40 animate-pulse rounded bg-raised" />
                <div className="h-2 w-full animate-pulse rounded-full bg-raised" />
              </div>
            ))}
          </div>
          <p className="mt-3 text-xs text-muted">
            Loading Partner Program progress…
          </p>
        </div>
      ) : null}

      {vm.state === "load_error" ? (
        <StateNotice
          icon={<AlertTriangle className="h-5 w-5 text-warn" />}
          title="Couldn't load Partner Program progress"
          body="The analytics request failed, so no figures are shown. This is a loading problem, not a zero — your channel's actual progress is unchanged."
          onRetry={onRetry}
        />
      ) : null}

      {vm.state === "no_analytics" ? (
        <StateNotice
          icon={<Hourglass className="h-5 w-5" />}
          title="No analytics yet"
          body="This channel has no Analytics figures yet. YouTube Analytics reports lag 48–72 hours, and Partner Program progress appears here after the first sync — nothing is estimated in the meantime."
        />
      ) : null}

      {vm.state === "ready" ? (
        <div className="space-y-5">
          {vm.currentThresholdsMet ? (
            <div className="flex items-start gap-2 rounded-lg bg-ok/10 px-3 py-2.5">
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-ok" />
              <p className="text-sm text-ink">
                Current thresholds met on reported figures. YouTube still
                reviews the channel before accepting it into the programme.
              </p>
            </div>
          ) : null}

          {/* Subscribers — target 1,000, labelled on the bar. */}
          {vm.subscribersBar.available ? (
            <AvailableBar
              bar={vm.subscribersBar}
              format={formatFigure}
              unit="subscribers"
              tone="violet"
            />
          ) : (
            <UnavailableBar
              bar={vm.subscribersBar}
              note="Subscriber count not reported yet"
            />
          )}

          {/* Current application bar — QUALIFIED hours toward 4,000.
              Filled only from Analytics qualification data; otherwise
              the explicit unavailable treatment renders instead. */}
          {vm.currentHoursBar.available ? (
            <AvailableBar
              bar={vm.currentHoursBar}
              format={formatHours}
              unit="qualified hours"
              tone="violet"
            />
          ) : (
            <UnavailableBar
              bar={vm.currentHoursBar}
              note="Qualification data unavailable"
            />
          )}

          {!vm.qualificationDataAvailable ? (
            <p className="text-xs text-muted">
              YouTube Analytics has not reported qualified public watch
              hours for this channel, so eligibility progress cannot be
              shown. It is never estimated from ordinary watch time.
            </p>
          ) : null}

          {vm.ordinaryWatchHours !== null ? (
            <OrdinaryWatchHoursLine hours={vm.ordinaryWatchHours} />
          ) : null}

          {/* Upcoming bar — 8,000 hours from 1 Feb 2027 for NEW
              channels. Same qualified numerator, larger target, kept
              visually distinct (amber, dashed frame, Upcoming badge)
              and never a replacement for the current bar above. */}
          <div className="rounded-xl border border-dashed border-warn/50 bg-warn/5 p-4">
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <CalendarClock className="h-4 w-4 text-warn" />
              <p className="text-sm font-semibold text-ink">
                Upcoming — from {YPP_UPCOMING_EFFECTIVE_LABEL}
              </p>
              <Badge tone="amber">New channels</Badge>
            </div>
            <p className="mb-3 text-xs text-muted">
              From {YPP_UPCOMING_EFFECTIVE_LABEL}, new channels need 1,000
              subscribers + 8,000 qualified watch hours. Applications
              received by {YPP_CURRENT_BAR_DEADLINE_LABEL} are judged on
              the current 4,000-hour bar above.
            </p>
            {vm.upcomingHoursBar.available ? (
              <AvailableBar
                bar={vm.upcomingHoursBar}
                format={formatHours}
                unit="qualified hours"
                tone="amber"
              />
            ) : (
              <UnavailableBar
                bar={vm.upcomingHoursBar}
                note="Qualification data unavailable"
              />
            )}
          </div>

          <p className="flex items-center gap-1.5 text-xs text-muted">
            <Users className="h-3.5 w-3.5" />
            Thresholds are labelled as reported by YouTube Analytics;
            meeting them does not guarantee acceptance.
          </p>
        </div>
      ) : null}
    </Card>
  );
}

/**
 * Wired variant for W3: runs the analytics query itself. Renders the
 * exact same states — the query's 404-to-null mapping means a channel
 * with no snapshot yet lands in `no_analytics`, and while the
 * endpoint is absent the component stays in that honest state rather
 * than showing invented numbers.
 */
export function YppProgressForChannel({
  channelId,
  channelConnected,
  channelTitle,
  className,
}: {
  channelId: string | null;
  channelConnected: boolean;
  channelTitle?: string;
  className?: string;
}) {
  const query = useYppProgress(channelId, channelConnected);
  return (
    <YppProgress
      channelConnected={channelConnected}
      channelTitle={channelTitle}
      data={query.data ?? null}
      loading={channelConnected && channelId !== null && query.isPending}
      loadFailed={query.isError}
      onRetry={() => {
        void query.refetch();
      }}
      className={className}
    />
  );
}
