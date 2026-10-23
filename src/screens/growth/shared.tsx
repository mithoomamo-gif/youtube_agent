import type { ReactNode } from "react";
import { AlertTriangle, Info } from "lucide-react";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";

// ── Formatters (same conventions as the Library screen) ───────────────────

export function toDate(value: number | string | null | undefined): Date | null {
  if (value === null || value === undefined) return null;
  const d = typeof value === "number" ? new Date(value) : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function fullDate(value: number | string | null | undefined): string {
  const d = toDate(value);
  return d ? d.toLocaleString() : "—";
}

export function humanize(value: string | null | undefined): string {
  if (!value) return "—";
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

/** Confidence is stored 0–1 on every Round 5 seam; render as a %. */
export function confidencePct(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  return `${Math.round(value * 100)}%`;
}

// ── Section shell ─────────────────────────────────────────────────────────

interface SectionShellProps {
  title: string;
  subtitle?: string;
  isLoading: boolean;
  isError: boolean;
  errorMessage?: string;
  onRetry?: () => void;
  /** false = the backend seam has not landed yet (HTTP 404). */
  available: boolean;
  unavailableNote: string;
  isEmpty: boolean;
  emptyTitle: string;
  emptyBody: string;
  emptyIcon: ReactNode;
  children: ReactNode;
}

/**
 * One honest state machine for every Growth surface, in priority
 * order: loading skeleton -> seam-not-landed notice -> error with
 * retry -> honest empty state -> content. No state ever renders
 * placeholder data.
 */
export function SectionShell({
  title,
  subtitle,
  isLoading,
  isError,
  errorMessage,
  onRetry,
  available,
  unavailableNote,
  isEmpty,
  emptyTitle,
  emptyBody,
  emptyIcon,
  children,
}: SectionShellProps) {
  return (
    <Card className="p-5">
      <div className="mb-4">
        <h2 className="text-base font-semibold text-ink">{title}</h2>
        {subtitle ? (
          <p className="mt-0.5 text-sm text-muted">{subtitle}</p>
        ) : null}
      </div>

      {isLoading ? (
        <div className="space-y-3" aria-label={`Loading ${title}`}>
          <div className="h-4 w-2/3 animate-pulse rounded bg-raised" />
          <div className="h-4 w-1/2 animate-pulse rounded bg-raised" />
          <div className="h-4 w-3/5 animate-pulse rounded bg-raised" />
        </div>
      ) : !available ? (
        <div className="flex gap-3 rounded-xl border border-line bg-raised/50 p-4">
          <Info className="mt-0.5 h-5 w-5 shrink-0 text-muted" />
          <div>
            <p className="text-sm font-medium text-ink">
              This surface is not connected yet
            </p>
            <p className="mt-1 text-sm text-muted">{unavailableNote}</p>
          </div>
        </div>
      ) : isError ? (
        <div className="flex gap-3 rounded-xl border border-danger/30 bg-danger/10 p-4">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-danger" />
          <div>
            <p className="text-sm font-medium text-ink">
              Could not load this section
            </p>
            <p className="mt-1 text-sm text-muted">
              {errorMessage ?? "The engine returned an error."}
            </p>
            {onRetry ? (
              <Button
                variant="outline"
                size="sm"
                className="mt-3"
                onClick={onRetry}
              >
                Retry
              </Button>
            ) : null}
          </div>
        </div>
      ) : isEmpty ? (
        <div className="flex flex-col items-center gap-2 py-8 text-center">
          <div className="text-muted">{emptyIcon}</div>
          <p className="text-sm font-medium text-ink">{emptyTitle}</p>
          <p className="max-w-md text-sm text-muted">{emptyBody}</p>
        </div>
      ) : (
        children
      )}
    </Card>
  );
}
