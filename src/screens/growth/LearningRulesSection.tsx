import { BookOpenCheck, Lightbulb, Shapes } from "lucide-react";
import { Badge } from "../../components/ui/Badge";
import { Card } from "../../components/ui/Card";
import { ProgressBar } from "../../components/ui/ProgressBar";
import { useLearningRules } from "../../api/queries/growth";
import type { LearningRuleWire } from "../../api/queries/growth";
import { SectionShell, confidencePct, fullDate, humanize } from "./shared";

const KIND_LABELS: Record<string, string> = {
  topic_affinity: "Topic affinity",
  hook_pattern: "Hook pattern",
  duration_band: "Duration",
  title_pattern: "Title pattern",
  thumbnail_style: "Thumbnail style",
  publish_window: "Publish window",
};

function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? humanize(kind);
}

/** Effect estimate, only when the store recorded one. */
function effectLabel(rule: LearningRuleWire): string | null {
  if (rule.effect_value === null) return null;
  const value = Number.isInteger(rule.effect_value)
    ? String(rule.effect_value)
    : rule.effect_value.toFixed(1);
  return rule.effect_unit
    ? `Estimated effect: ${value} ${rule.effect_unit}`
    : `Estimated effect: ${value}`;
}

function RuleRow({ rule }: { rule: LearningRuleWire }) {
  const effect = effectLabel(rule);
  return (
    <li className="rounded-xl border border-line bg-raised/40 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <p className="min-w-0 flex-1 text-sm font-medium text-ink">
          {rule.statement}
        </p>
        <div className="flex shrink-0 flex-wrap items-center gap-1.5">
          <Badge tone="violet">{kindLabel(rule.kind)}</Badge>
          {rule.version !== null ? (
            <Badge tone="gray">v{rule.version}</Badge>
          ) : null}
          {rule.thin_evidence ? (
            <Badge tone="amber">Thin evidence</Badge>
          ) : null}
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-muted">
        <span>
          Evidence:{" "}
          <span className="font-semibold text-ink2">
            {rule.evidence_count}{" "}
            {rule.evidence_count === 1 ? "video" : "videos"}
          </span>
        </span>
        {rule.confidence !== null ? (
          <span className="flex items-center gap-2">
            Confidence:{" "}
            <span className="font-semibold text-ink2">
              {confidencePct(rule.confidence)}
            </span>
            <span className="inline-block w-20 align-middle">
              <ProgressBar
                value={Math.round(rule.confidence * 100)}
                tone={rule.thin_evidence ? "amber" : "violet"}
              />
            </span>
          </span>
        ) : (
          <span>Confidence: not recorded</span>
        )}
        {effect ? <span>{effect}</span> : null}
        {rule.created_at ? (
          <span>Recorded {fullDate(rule.created_at)}</span>
        ) : null}
      </div>

      {rule.thin_evidence ? (
        <p className="mt-2 text-xs text-warn">
          Below the minimum-evidence threshold — treat this as a
          suggestion, not a settled rule.
        </p>
      ) : null}
    </li>
  );
}

export function LearningRulesSection({
  channelId,
  niche,
  nicheLabel,
}: {
  channelId: string | null;
  /** Learning scope key for the channel's niche; null = unscoped. */
  niche: string | null;
  nicheLabel: string | null;
}) {
  const query = useLearningRules(channelId, niche);
  const result = query.data;
  const rules = result?.available ? result.data : [];

  // The backend REQUIRES a niche scope (learning is never pooled
  // across niches). Without a default niche on the channel there is
  // no honest scope to query — say so instead of guessing one.
  if (niche === null) {
    return (
      <Card className="p-5">
        <div className="mb-4">
          <h2 className="text-base font-semibold text-ink">
            What the agent has learned
          </h2>
          <p className="mt-0.5 text-sm text-muted">
            Active learning rules are scoped to one channel and one
            niche.
          </p>
        </div>
        <div className="flex flex-col items-center gap-2 py-8 text-center">
          <Shapes className="h-8 w-8 text-muted" />
          <p className="text-sm font-medium text-ink">
            No niche scope for this channel
          </p>
          <p className="max-w-md text-sm text-muted">
            This channel has no default niche profile set, so its
            learning scope can&rsquo;t be determined. Set a default
            niche on the Channels screen and this channel&rsquo;s
            rules will appear here — they are never read across
            niches.
          </p>
        </div>
      </Card>
    );
  }

  return (
    <SectionShell
      title="What the agent has learned"
      subtitle={`Active learning rules for this channel's ${nicheLabel ?? niche} niche — every rule is an evidence-backed statement, versioned by the Learning Store.`}
      isLoading={query.isLoading}
      isError={query.isError}
      errorMessage={
        query.error instanceof Error ? query.error.message : undefined
      }
      onRetry={() => void query.refetch()}
      available={result ? result.available : true}
      unavailableNote="The learning read API is not answering for this channel yet, so no rules can be shown. Nothing is estimated in the meantime — this panel reads straight from the Learning Store once the endpoint responds."
      isEmpty={rules.length === 0}
      emptyTitle="No active learning rules yet"
      emptyBody="Rules appear here after published videos are graded at D+7 / D+28 and a pattern clears the minimum-evidence bar. Until then the agent has learned nothing about this channel — and it will not pretend otherwise."
      emptyIcon={<Lightbulb className="h-8 w-8" />}
    >
      <ul className="space-y-3">
        {rules.map((rule) => (
          <RuleRow key={rule.id} rule={rule} />
        ))}
      </ul>
      <p className="mt-4 flex items-center gap-1.5 text-xs text-muted">
        <BookOpenCheck className="h-3.5 w-3.5" />
        Rules are scoped to one channel and one niche — learning is
        never pooled across channels or niches.
      </p>
    </SectionShell>
  );
}
