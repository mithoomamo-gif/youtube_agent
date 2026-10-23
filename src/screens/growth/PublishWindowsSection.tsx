import { CalendarClock, Lock } from "lucide-react";
import { Badge } from "../../components/ui/Badge";
import { usePublishWindows } from "../../api/queries/growth";
import type { PublishWindowWire } from "../../api/queries/growth";
import { SectionShell, confidencePct } from "./shared";

/**
 * Humanise the seam's reason CODES. The landed contract returns
 * `reason: "not_enough_data"` (a code, not prose) when a channel is
 * not eligible; unknown codes fall back to a generic honest line.
 */
const REASON_TEXT: Record<string, string> = {
  not_enough_data:
    "There is not enough learned evidence yet: a publish-window suggestion needs at least one active publish-window rule learned from this channel's own videos.",
  insufficient_data:
    "There is not enough learned evidence yet: a publish-window suggestion needs at least one active publish-window rule learned from this channel's own videos.",
};

function windowWhen(window: PublishWindowWire): string | null {
  const days = window.days.join(", ");
  const times =
    window.start_local && window.end_local
      ? `${window.start_local}–${window.end_local}`
      : (window.start_local ?? window.end_local);
  const parts = [days, times].filter((p) => p && p.length > 0);
  return parts.length > 0 ? parts.join(" · ") : null;
}

function WindowRow({
  window,
  actionable,
}: {
  window: PublishWindowWire;
  actionable: boolean;
}) {
  const when = windowWhen(window);
  const heading = window.label ?? window.statement ?? "Suggested window";
  return (
    <li
      className={`rounded-xl border border-line p-4 ${
        actionable ? "bg-raised/40" : "bg-raised/20 opacity-70"
      }`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium text-ink">{heading}</p>
        <div className="flex shrink-0 flex-wrap items-center gap-1.5">
          {window.niche ? <Badge tone="gray">{window.niche}</Badge> : null}
          {window.thin_evidence ? (
            <Badge tone="amber">Thin evidence</Badge>
          ) : null}
          {actionable ? (
            <Badge tone="green">Eligible suggestion</Badge>
          ) : (
            <Badge tone="gray">Not actionable</Badge>
          )}
        </div>
      </div>
      {window.label && window.statement ? (
        <p className="mt-1 text-sm text-muted">{window.statement}</p>
      ) : null}
      {when ? <p className="mt-1 text-sm text-muted">{when}</p> : null}
      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted">
        {window.evidence_count !== null ? (
          <span>
            Evidence:{" "}
            <span className="font-semibold text-ink2">
              {window.evidence_count}{" "}
              {window.evidence_count === 1 ? "video" : "videos"}
            </span>
          </span>
        ) : null}
        {window.confidence !== null ? (
          <span>
            Confidence:{" "}
            <span className="font-semibold text-ink2">
              {confidencePct(window.confidence)}
            </span>
          </span>
        ) : null}
      </div>
    </li>
  );
}

export function PublishWindowsSection({
  channelId,
}: {
  channelId: string | null;
}) {
  const query = usePublishWindows(channelId);
  const result = query.data;
  const data = result?.available ? result.data : null;
  const windows = data?.windows ?? [];
  const eligible = data?.eligible === true;

  // The eligibility facts the seam reported, stated plainly.
  const facts =
    data && data.active_rule_count !== null
      ? `Active publish-window rules: ${data.active_rule_count}` +
        (data.min_active_rules !== null
          ? ` of ${data.min_active_rules} required`
          : "") +
        (data.candidate_rule_count !== null
          ? ` · candidate rules still gathering evidence: ${data.candidate_rule_count}`
          : "")
      : null;

  // Empty = nothing to suggest AND no eligibility verdict to report.
  const isEmpty =
    data !== null && windows.length === 0 && data.eligible === null;

  return (
    <SectionShell
      title="Publish-window suggestions"
      subtitle="Best-time windows learned from this channel's own published videos. Suggestions only — a window is never applied to a schedule silently."
      isLoading={query.isLoading}
      isError={query.isError}
      errorMessage={
        query.error instanceof Error ? query.error.message : undefined
      }
      onRetry={() => void query.refetch()}
      available={result ? result.available : true}
      unavailableNote="The publish-window read API is not answering for this channel yet. Suggested windows will appear here once it does — no window is guessed from other channels in the meantime."
      isEmpty={isEmpty}
      emptyTitle="No publish windows yet"
      emptyBody="No publish-window suggestion has been computed for this channel, and the engine reported no eligibility verdict. Windows are learned from this channel's own videos — never borrowed from another channel."
      emptyIcon={<CalendarClock className="h-8 w-8" />}
    >
      {data ? (
        <div className="space-y-4">
          {data.eligible === false ? (
            <div className="flex gap-3 rounded-xl border border-warn/30 bg-warn/10 p-4">
              <Lock className="mt-0.5 h-5 w-5 shrink-0 text-warn" />
              <div>
                <p className="text-sm font-medium text-ink">
                  Not eligible for window suggestions yet
                </p>
                <p className="mt-1 text-sm text-muted">
                  {data.reason_code
                    ? (REASON_TEXT[data.reason_code] ??
                      "This channel has not met the eligibility requirements for publish-window suggestions.")
                    : "This channel has not met the eligibility requirements for publish-window suggestions."}
                </p>
                {facts ? (
                  <p className="mt-1 text-xs text-muted">{facts}</p>
                ) : null}
              </div>
            </div>
          ) : data.eligible === null ? (
            <div className="flex gap-3 rounded-xl border border-line bg-raised/50 p-4">
              <Lock className="mt-0.5 h-5 w-5 shrink-0 text-muted" />
              <div>
                <p className="text-sm font-medium text-ink">
                  Eligibility not reported
                </p>
                <p className="mt-1 text-sm text-muted">
                  The engine did not report whether this channel is
                  eligible, so any windows below are shown for
                  information only — not as actionable suggestions.
                </p>
                {facts ? (
                  <p className="mt-1 text-xs text-muted">{facts}</p>
                ) : null}
              </div>
            </div>
          ) : facts ? (
            <p className="text-xs text-muted">{facts}</p>
          ) : null}

          {windows.length > 0 ? (
            <ul className="space-y-3">
              {windows.map((window, index) => (
                <WindowRow
                  key={`${window.label ?? window.statement ?? "window"}-${index}`}
                  window={window}
                  actionable={eligible}
                />
              ))}
            </ul>
          ) : data.eligible !== false ? (
            <p className="text-sm text-muted">
              No specific window has been learned for this channel
              yet.
            </p>
          ) : null}
        </div>
      ) : null}
    </SectionShell>
  );
}
