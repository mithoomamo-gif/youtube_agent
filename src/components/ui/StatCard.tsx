import type { ReactNode } from "react";
import { Card } from "./Card";

export function StatCard({
  label,
  value,
  sub,
  icon,
}: {
  label: string;
  value: string;
  sub?: string;
  icon?: ReactNode;
}) {
  return (
    <Card className="p-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-wide text-muted">
            {label}
          </p>
          <p className="tnum mt-2 font-mono text-3xl font-bold text-ink">
            {value}
          </p>
          {sub ? <p className="mt-1.5 text-xs text-ink2">{sub}</p> : null}
        </div>
        {icon ? (
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-accent/15 text-accent2">
            {icon}
          </div>
        ) : null}
      </div>
    </Card>
  );
}
