import { Eye } from "lucide-react";
import { Card } from "../ui/Card";
import { StatSkeleton, StatErrorCard } from "./cardStates";
import {
  isOfflineError,
  useViews28Days,
} from "../../api/queries/dashboardGrowth";

/**
 * "Views 28 Days" KPI card (roadmap §3.3 card 5, §3.15).
 *
 * Data: `GET /channels/{id}/analytics/overview` (landed analytics
 * read model) via `useViews28Days` — reporting-period views summed
 * across connected channels, and the last synced time. % change vs
 * the previous 28 days is shown only when a payload actually
 * carries a previous-period figure (the landed read model does not
 * expose one yet — the card then says so instead of inventing a
 * delta). The freshness line follows the roadmap wording exactly:
 * "Analytics updated [time]" — analytics is pull-based with 48–72h
 * source latency, never presented as live.
 *
 * Empty state: when the seam returns no usable figure (nothing synced
 * yet, or the read model isn't there yet) the card shows "—" and says
 * so; it never invents a number.
 */

function formatCount(value: number): string {
  return Math.round(value).toLocaleString("en-US");
}

function formatSynced(ms: number): string {
  return new Date(ms).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export default function Views28DaysCard() {
  const views = useViews28Days();
  const icon = <Eye className="h-5 w-5" />;

  if (views.isPending) return <StatSkeleton icon={icon} />;

  if (views.isError) {
    return (
      <StatErrorCard
        label="Views 28 Days"
        icon={icon}
        offline={isOfflineError(views.error)}
        onRetry={() => void views.refetch()}
      />
    );
  }

  const data = views.data;

  if (!data.hasData || data.views === null) {
    return (
      <Card className="p-5">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="text-xs font-medium uppercase tracking-wide text-muted">
              Views 28 Days
            </p>
            <p className="tnum mt-2 font-mono text-3xl font-bold text-ink">—</p>
            <p className="mt-1.5 text-xs text-ink2">
              {data.endpointMissing
                ? "Analytics isn't available yet"
                : data.noScope || data.notConnected
                  ? "No channel connected"
                  : "Not synced yet"}
            </p>
            <p className="mt-1 text-xs leading-relaxed text-muted">
              {data.endpointMissing
                ? "Views appear here once the analytics read model is connected."
                : data.noScope || data.notConnected
                  ? "Connect a channel and views from its analytics will appear here — never estimated."
                  : "Views appear here after the first analytics sync."}
            </p>
          </div>
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-accent/15 text-accent2">
            {icon}
          </div>
        </div>
      </Card>
    );
  }

  const change = data.changePct;
  return (
    <Card className="p-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-wide text-muted">
            Views 28 Days
          </p>
          <p className="tnum mt-2 font-mono text-3xl font-bold text-ink">
            {formatCount(data.views)}
          </p>
          {change !== null ? (
            <p
              className={`mt-1.5 text-xs font-medium ${change >= 0 ? "text-ok" : "text-danger"}`}
            >
              {change >= 0 ? "+" : ""}
              {change.toFixed(1)}% vs prior 28 days
            </p>
          ) : (
            <p className="mt-1.5 text-xs text-ink2">
              No prior-period comparison yet
            </p>
          )}
          <p className="mt-1 text-xs text-muted">
            {data.lastSyncedMs !== null
              ? `Analytics updated ${formatSynced(data.lastSyncedMs)}`
              : "Analytics updated — sync time not reported"}
          </p>
        </div>
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-accent/15 text-accent2">
          {icon}
        </div>
      </div>
    </Card>
  );
}
