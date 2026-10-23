import { Lightbulb } from "lucide-react";
import { Badge } from "../ui/Badge";
import { Card } from "../ui/Card";
import { EmptyPanel } from "./cardStates";
import {
  isOfflineError,
  useLearningHighlights,
} from "../../api/queries/dashboardGrowth";

/**
 * "Learning highlights" card — the top active rules from the learning
 * store (`performance_insights`, roadmap §3.15 / Part 7.8: Agent 30 is
 * the single writer; consumers read ACTIVE insights).
 *
 * Data: `GET /channels/{id}/learning/rules?niche={key}` (landed
 * learning router) via `useLearningHighlights`, aggregated across
 * the user's channels with each rule's scope kept as a label. Each
 * rule shows its text, insight type, confidence and evidence count
 * when the seam provides them.
 *
 * Empty state: an honest "no learning rules yet" panel (or "not
 * available yet" when the read seam itself 404s) — never sample rules.
 */

const MAX_SHOWN = 3;

function titleCase(value: string): string {
  return value.length > 0
    ? value.charAt(0).toUpperCase() + value.slice(1).replace(/_/g, " ")
    : value;
}

export default function LearningHighlightsCard() {
  const learning = useLearningHighlights();

  return (
    <Card className="p-5">
      <div className="mb-4">
        <h2 className="flex items-center gap-2 text-base font-semibold">
          <Lightbulb className="h-4 w-4 text-accent2" />
          Learning highlights
        </h2>
        <p className="text-sm text-muted">
          Top active rules the agent has learned from your analytics
        </p>
      </div>

      {learning.isPending ? (
        <div className="space-y-3">
          {[0, 1, 2].map((i) => (
            <div
              key={i}
              className="rounded-xl border border-line bg-raised p-3"
            >
              <div className="h-3.5 w-3/4 animate-pulse rounded bg-panel" />
              <div className="mt-2.5 h-3 w-1/3 animate-pulse rounded bg-panel" />
            </div>
          ))}
        </div>
      ) : learning.isError ? (
        <EmptyPanel
          title={
            isOfflineError(learning.error)
              ? "Backend offline"
              : "Couldn't load learning rules"
          }
          text={
            isOfflineError(learning.error)
              ? "The local backend isn't responding. Learning highlights will appear here once it reconnects."
              : "The backend returned an error while loading learning rules. Try again in a moment."
          }
        />
      ) : !learning.data.hasData ? (
        <EmptyPanel
          title="No learning rules yet"
          text={
            learning.data.endpointMissing
              ? "The learning service isn't available yet. Rules appear here once it is connected and has analytics to learn from."
              : learning.data.noScope
                ? "Connect a channel with a default niche and rules the agent learns will appear here, scoped to that channel and niche."
                : "Rules appear here once published videos have enough analytics for the agent to learn from."
          }
        />
      ) : (
        <>
          <ul className="divide-y divide-line">
            {learning.data.rules.slice(0, MAX_SHOWN).map((rule) => (
              <li key={rule.id} className="py-3 first:pt-0 last:pb-0">
                <p className="text-sm text-ink">{rule.text}</p>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {rule.type ? (
                    <Badge tone="violet">{titleCase(rule.type)}</Badge>
                  ) : null}
                  {rule.scope ? (
                    <Badge tone="gray">{titleCase(rule.scope)}</Badge>
                  ) : null}
                  {rule.confidencePct !== null ? (
                    <span className="tnum font-mono text-xs text-ink2">
                      {rule.confidencePct}% confidence
                    </span>
                  ) : null}
                  {rule.evidenceCount !== null ? (
                    <span className="tnum font-mono text-xs text-muted">
                      Evidence ×{rule.evidenceCount}
                    </span>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
          {learning.data.total !== null ? (
            <p className="mt-3 border-t border-line pt-3 text-xs text-muted">
              +{learning.data.total - Math.min(MAX_SHOWN, learning.data.rules.length)}{" "}
              more active rules
            </p>
          ) : null}
        </>
      )}
    </Card>
  );
}
