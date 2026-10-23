import type { ReactNode } from "react";
import {
  AlertTriangle,
  DollarSign,
  Film,
  Info,
  Timer,
  Wallet,
  WifiOff,
} from "lucide-react";
import { Card } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { ProgressBar } from "../components/ui/ProgressBar";
import { StatCard } from "../components/ui/StatCard";
import {
  isOfflineError,
  useProjectCosts,
  useUsageRollups,
  useUsageToday,
} from "../api/queries/costs";
import type { UsageRollup } from "../api/queries/costs";

// ── Formatting ────────────────────────────────────────────────────────────

function formatUsdValue(usd: number): string {
  return `$${usd.toFixed(2)}`;
}

function formatCount(n: number): string {
  return n.toLocaleString("en-US");
}

const PROVIDER_NAMES: Record<string, string> = {
  gemini: "Gemini",
  openai: "OpenAI",
  anthropic: "Anthropic",
  elevenlabs: "ElevenLabs",
  pexels: "Pexels",
  pixabay: "Pixabay",
  azure_tts: "Azure TTS",
  google_tts: "Google TTS",
  youtube: "YouTube",
};

function providerName(provider: string): string {
  return (
    PROVIDER_NAMES[provider.toLowerCase()] ??
    (provider.length > 0
      ? provider.charAt(0).toUpperCase() + provider.slice(1)
      : provider)
  );
}

function providerDetail(rollup: UsageRollup): string {
  const parts: string[] = [];
  if (rollup.tokensIn > 0 || rollup.tokensOut > 0) {
    parts.push(
      `${formatCount(rollup.tokensIn)} tokens in · ${formatCount(rollup.tokensOut)} tokens out`,
    );
  }
  if (rollup.characters > 0) {
    parts.push(`${formatCount(rollup.characters)} characters`);
  }
  if (rollup.images > 0) {
    parts.push(`${formatCount(rollup.images)} images`);
  }
  parts.push(`${formatCount(rollup.calls)} ${rollup.calls === 1 ? "call" : "calls"}`);
  return parts.join(" · ");
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
    <div className="mb-6 flex items-center gap-3 rounded-2xl border border-warn/50 bg-warn/10 px-4 py-3">
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

export default function Costs() {
  const usage = useUsageRollups();
  const projects = useProjectCosts();
  const today = useUsageToday();

  const rollups = [...(usage.data ?? [])].sort((a, b) => b.estUsd - a.estUsd);
  const totalSpendUsd = rollups.reduce((sum, r) => sum + r.estUsd, 0);

  const projectList = projects.data ?? [];
  const publishedProjects = projectList.filter(
    (p) => p.status === "published",
  );
  const publishedCostMicros = publishedProjects.reduce(
    (sum, p) => sum + p.totalCostMicros,
    0,
  );
  const publishedMinutes = publishedProjects.reduce(
    (sum, p) => sum + (p.targetMinutes ?? 0),
    0,
  );
  const allProjectsCostMicros = projectList.reduce(
    (sum, p) => sum + p.totalCostMicros,
    0,
  );
  const costedProjects = [...projectList]
    .sort((a, b) => b.totalCostMicros - a.totalCostMicros)
    .slice(0, 6);

  const anyOffline =
    (usage.isError && isOfflineError(usage.error)) ||
    (projects.isError && isOfflineError(projects.error)) ||
    (today.isError && isOfflineError(today.error));

  return (
    <div className="bg-app min-h-full p-6 text-ink">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold text-ink">Costs &amp; Usage</h1>
        <p className="mt-1 text-sm text-muted">
          All recorded usage from the backend ledger · All amounts in USD
        </p>
      </header>

      {anyOffline ? <OfflineBanner /> : null}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {usage.isPending ? (
          <StatSkeleton icon={<Wallet className="h-5 w-5" />} />
        ) : usage.isError ? (
          <StatErrorCard
            label="Total recorded spend"
            icon={<Wallet className="h-5 w-5" />}
            offline={isOfflineError(usage.error)}
            onRetry={() => void usage.refetch()}
          />
        ) : (
          <StatCard
            label="Total recorded spend"
            value={formatUsdValue(totalSpendUsd)}
            sub={
              rollups.length > 0
                ? `Across ${rollups.length} ${rollups.length === 1 ? "provider" : "providers"} · all-time ledger`
                : "No usage recorded yet"
            }
            icon={<Wallet className="h-5 w-5" />}
          />
        )}

        {projects.isPending ? (
          <StatSkeleton icon={<Film className="h-5 w-5" />} />
        ) : projects.isError ? (
          <StatErrorCard
            label="Cost per published video"
            icon={<Film className="h-5 w-5" />}
            offline={isOfflineError(projects.error)}
            onRetry={() => void projects.refetch()}
          />
        ) : publishedProjects.length > 0 ? (
          <StatCard
            label="Cost per published video"
            value={formatUsdValue(
              publishedCostMicros / 1_000_000 / publishedProjects.length,
            )}
            sub={`${publishedProjects.length} ${publishedProjects.length === 1 ? "video" : "videos"} published`}
            icon={<Film className="h-5 w-5" />}
          />
        ) : projectList.length > 0 ? (
          <StatCard
            label="Avg cost per project"
            value={formatUsdValue(
              allProjectsCostMicros / 1_000_000 / projectList.length,
            )}
            sub={`No published videos yet · ${projectList.length} ${projectList.length === 1 ? "project" : "projects"}`}
            icon={<Film className="h-5 w-5" />}
          />
        ) : (
          <StatCard
            label="Cost per published video"
            value="—"
            sub="No projects yet"
            icon={<Film className="h-5 w-5" />}
          />
        )}

        {projects.isPending ? (
          <StatSkeleton icon={<Timer className="h-5 w-5" />} />
        ) : projects.isError ? (
          <StatErrorCard
            label="Cost per published minute"
            icon={<Timer className="h-5 w-5" />}
            offline={isOfflineError(projects.error)}
            onRetry={() => void projects.refetch()}
          />
        ) : publishedProjects.length > 0 && publishedMinutes > 0 ? (
          <StatCard
            label="Cost per published minute"
            value={formatUsdValue(
              publishedCostMicros / 1_000_000 / publishedMinutes,
            )}
            sub={`${formatCount(publishedMinutes)} target minutes published`}
            icon={<Timer className="h-5 w-5" />}
          />
        ) : (
          <StatCard
            label="Cost per published minute"
            value="—"
            sub="Not enough published data yet"
            icon={<Timer className="h-5 w-5" />}
          />
        )}

        {today.isPending ? (
          <StatSkeleton icon={<DollarSign className="h-5 w-5" />} />
        ) : today.isError ? (
          <StatErrorCard
            label="Spend today"
            icon={<DollarSign className="h-5 w-5" />}
            offline={isOfflineError(today.error)}
            onRetry={() => void today.refetch()}
          />
        ) : (
          <StatCard
            label="Spend today"
            value={formatUsdValue(today.data.estUsd)}
            sub={
              today.data.hasData
                ? `${today.data.calls} API ${today.data.calls === 1 ? "call" : "calls"} today`
                : "No usage recorded today"
            }
            icon={<DollarSign className="h-5 w-5" />}
          />
        )}
      </div>

      <div className="mt-6 grid grid-cols-1 gap-6 xl:grid-cols-2">
        <Card className="p-5">
          <h2 className="mb-4 text-base font-semibold text-ink">
            Provider meters
          </h2>
          {usage.isPending ? (
            <div className="divide-y divide-line">
              {[0, 1, 2].map((i) => (
                <div key={i} className="flex items-center justify-between py-3">
                  <div>
                    <div className="h-4 w-24 animate-pulse rounded bg-raised" />
                    <div className="mt-2 h-3 w-48 animate-pulse rounded bg-raised" />
                  </div>
                  <div className="h-4 w-14 animate-pulse rounded bg-raised" />
                </div>
              ))}
            </div>
          ) : usage.isError ? (
            <PanelState
              offline={isOfflineError(usage.error)}
              title={
                isOfflineError(usage.error)
                  ? "Backend offline"
                  : "Couldn't load usage"
              }
              text={
                isOfflineError(usage.error)
                  ? "The local backend isn't responding. Provider usage will appear here once it reconnects."
                  : "The backend returned an error while loading usage. Try again in a moment."
              }
              onRetry={() => void usage.refetch()}
            />
          ) : rollups.length === 0 ? (
            <div className="rounded-xl border border-dashed border-line bg-raised px-4 py-8 text-center">
              <p className="text-sm font-semibold">No usage recorded yet</p>
              <p className="mt-1 text-xs leading-relaxed text-ink2">
                Provider calls are written to the usage ledger as production
                runs. They'll appear here once the first video is produced.
              </p>
            </div>
          ) : (
            <div className="divide-y divide-line">
              {rollups.map((rollup) => (
                <div
                  key={rollup.provider}
                  className="flex items-center justify-between py-3 first:pt-0 last:pb-0"
                >
                  <div>
                    <p className="text-sm font-medium text-ink">
                      {providerName(rollup.provider)}
                    </p>
                    <p className="mt-0.5 font-mono text-xs text-muted tnum">
                      {providerDetail(rollup)}
                    </p>
                  </div>
                  <span className="font-mono text-sm text-ink tnum">
                    {formatUsdValue(rollup.estUsd)}
                  </span>
                </div>
              ))}

              <div className="py-3 last:pb-0">
                <div className="flex items-center justify-between">
                  <p className="text-sm font-medium text-ink">YouTube quota</p>
                  <span className="text-xs text-muted">not exposed yet</span>
                </div>
                <p className="mt-1.5 text-xs leading-relaxed text-muted">
                  The backend's usage endpoints return costs only — no quota
                  buckets — so upload / search / shared-unit meters can't be
                  shown yet.
                </p>
              </div>
            </div>
          )}
        </Card>

        <Card className="p-5">
          <h2 className="mb-4 text-base font-semibold text-ink">Budget caps</h2>
          <div className="flex gap-3 rounded-xl border border-dashed border-line bg-raised p-4">
            <Info className="mt-0.5 h-5 w-5 shrink-0 text-muted" />
            <div>
              <p className="text-sm font-semibold">
                Caps aren't exposed by the backend yet
              </p>
              <p className="mt-1 text-xs leading-relaxed text-ink2">
                No endpoint returns per-video, daily, or monthly budget caps,
                so this panel can't show spend against a cap without
                inventing numbers. The real spend figures below come from the
                usage ledger.
              </p>
            </div>
          </div>
          <div className="mt-5 space-y-3">
            <div className="flex items-center justify-between text-sm">
              <span className="text-ink2">Total recorded spend</span>
              <span className="tnum font-mono text-ink">
                {usage.isPending
                  ? "…"
                  : usage.isError
                    ? "—"
                    : formatUsdValue(totalSpendUsd)}
              </span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-ink2">Spend today</span>
              <span className="tnum font-mono text-ink">
                {today.isPending
                  ? "…"
                  : today.isError
                    ? "—"
                    : formatUsdValue(today.data.estUsd)}
              </span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-ink2">Providers tracked</span>
              <span className="tnum font-mono text-ink">
                {usage.isPending ? "…" : usage.isError ? "—" : rollups.length}
              </span>
            </div>
          </div>
          <p className="mt-5 border-t border-line pt-3 text-xs text-muted">
            Once the backend exposes budget caps, this panel will show spend
            against each cap with its warning thresholds.
          </p>
        </Card>
      </div>

      <Card className="mt-6 p-5">
        <h2 className="text-base font-semibold text-ink">Project costs</h2>
        <p className="mb-4 mt-1 text-sm text-muted">
          Per-video totals from the projects ledger, highest first. A
          per-category breakdown (script · voice · stock · render) and retry
          spend aren't exposed by the backend yet, so the bar shows each
          video's share of total project spend instead.
        </p>
        {projects.isPending ? (
          <div className="space-y-3">
            {[0, 1, 2].map((i) => (
              <div key={i} className="flex items-center gap-4">
                <div className="h-4 w-1/3 animate-pulse rounded bg-raised" />
                <div className="h-2.5 flex-1 animate-pulse rounded-full bg-raised" />
                <div className="h-4 w-14 animate-pulse rounded bg-raised" />
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
                ? "The local backend isn't responding. Project costs will appear here once it reconnects."
                : "The backend returned an error while loading projects. Try again in a moment."
            }
            onRetry={() => void projects.refetch()}
          />
        ) : costedProjects.length === 0 ? (
          <div className="rounded-xl border border-dashed border-line bg-raised px-4 py-8 text-center">
            <p className="text-sm font-semibold">No projects yet</p>
            <p className="mt-1 text-xs leading-relaxed text-ink2">
              Video costs will appear here once projects exist and production
              starts recording spend against them.
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-muted">
                  <th className="pb-2 pr-3 font-medium">Title</th>
                  <th className="pb-2 pr-3 font-medium">Share of spend</th>
                  <th className="pb-2 text-right font-medium">Total</th>
                </tr>
              </thead>
              <tbody>
                {costedProjects.map((project) => {
                  const share =
                    allProjectsCostMicros > 0
                      ? (project.totalCostMicros / allProjectsCostMicros) *
                        100
                      : 0;
                  return (
                    <tr
                      key={project.id}
                      className="border-b border-line last:border-0"
                    >
                      <td className="py-3 pr-3 text-ink">
                        {project.title}
                        <span className="mt-0.5 block text-[11px] text-muted">
                          {project.status.replace(/_/g, " ")}
                          {project.targetMinutes !== null
                            ? ` · ${project.targetMinutes} min target`
                            : ""}
                        </span>
                      </td>
                      <td className="w-56 py-3 pr-3">
                        <ProgressBar value={share} />
                        <p className="tnum mt-1 text-[11px] text-muted">
                          {share.toFixed(1)}% of total project spend
                        </p>
                      </td>
                      <td className="py-3 text-right font-mono text-ink tnum">
                        {formatUsdValue(project.totalCostMicros / 1_000_000)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
