import type { ReactNode } from "react";
import { Card } from "../ui/Card";

/**
 * Shared loading / error states for the Round 5 Dashboard cards,
 * mirroring the design language Mission Control already uses for its
 * stat cards (same skeleton blocks, same "—" + Retry error card).
 */

export function StatSkeleton({ icon }: { icon: ReactNode }) {
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

export function StatErrorCard({
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

/** Dashed honest-empty panel, same shape as Mission Control's panels. */
export function EmptyPanel({ title, text }: { title: string; text: string }) {
  return (
    <div className="rounded-xl border border-dashed border-line bg-raised px-4 py-6 text-center">
      <p className="text-sm font-semibold">{title}</p>
      <p className="mt-1 text-xs leading-relaxed text-ink2">{text}</p>
    </div>
  );
}
