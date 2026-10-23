import { Link } from "react-router-dom";
import {
  ArrowRight,
  HelpCircle,
  MessagesSquare,
  PenLine,
} from "lucide-react";
import { Badge } from "../../components/ui/Badge";
import { Card } from "../../components/ui/Card";
import { ApiError } from "../../api/types";
import {
  useCommentDrafts,
  useCommentRollups,
} from "../../api/queries/comments";
import { SectionShell, fullDate } from "./shared";

/** True when the backend does not serve the route yet (HTTP 404). */
function isNotLanded(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

// ── Themes + questions ────────────────────────────────────────────────────

export function CommentInsightsSection({
  channelId,
}: {
  channelId: string | null;
}) {
  const query = useCommentRollups(channelId);
  const notLanded = isNotLanded(query.error);
  const rollup = query.data?.channel ?? null;
  const themes = rollup?.themes ?? [];
  const questions = rollup?.top_questions ?? [];
  const isEmpty =
    query.data !== undefined &&
    (rollup === null ||
      (themes.length === 0 &&
        questions.length === 0 &&
        (rollup.comment_count === null || rollup.comment_count === 0)));

  return (
    <>
      <SectionShell
        title="Comment themes & questions"
        subtitle="What viewers are talking about and asking, aggregated from this channel's own comments by the Comment Agent."
        isLoading={query.isLoading}
        isError={query.isError && !notLanded}
        errorMessage={
          query.error instanceof Error ? query.error.message : undefined
        }
        onRetry={() => void query.refetch()}
        available={!notLanded}
        unavailableNote="The comment-rollups read API has not landed in the engine yet. Themes and questions will appear here once it does — nothing is inferred from other channels in the meantime."
        isEmpty={isEmpty}
        emptyTitle="No comment analysis yet"
        emptyBody="No analysed comments for this channel yet. Once published videos collect comments and a sync + analysis run completes in the Comments screen, the recurring themes and the questions viewers ask most will be summarised here."
        emptyIcon={<MessagesSquare className="h-8 w-8" />}
      >
        {rollup ? (
          <div className="space-y-6">
            <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-muted">
              {rollup.comment_count !== null ? (
                <span>
                  {rollup.comment_count}{" "}
                  {rollup.comment_count === 1 ? "comment" : "comments"}{" "}
                  analysed
                </span>
              ) : null}
              <span>
                Analysis engine:{" "}
                <span className="font-semibold text-ink2">
                  {rollup.engine ?? "not reported by the backend"}
                </span>
              </span>
              {rollup.analysed_at ? (
                <span>As of {fullDate(rollup.analysed_at)}</span>
              ) : null}
            </div>

            {themes.length > 0 ? (
              <div>
                <h3 className="mb-2 text-sm font-semibold text-ink">
                  Recurring themes
                </h3>
                <ul className="space-y-2">
                  {themes.map((theme) => (
                    <li
                      key={theme.label}
                      className="flex items-center justify-between gap-3 rounded-xl border border-line bg-raised/40 px-4 py-2.5"
                    >
                      <span className="text-sm text-ink">{theme.label}</span>
                      <span className="shrink-0 text-xs text-muted">
                        {theme.count !== null
                          ? `${theme.count} ${theme.count === 1 ? "comment" : "comments"}`
                          : ""}
                        {theme.share !== null
                          ? `${theme.count !== null ? " · " : ""}${Math.round(theme.share * 100)}% of analysed`
                          : ""}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="text-sm text-muted">
                No recurring themes were identified in the latest
                analysis.
              </p>
            )}

            {questions.length > 0 ? (
              <div>
                <h3 className="mb-2 flex items-center gap-1.5 text-sm font-semibold text-ink">
                  <HelpCircle className="h-4 w-4" />
                  Top questions from viewers
                </h3>
                <ul className="space-y-2">
                  {questions.map((q, index) => (
                    <li
                      key={q.comment_id ?? `q-${index}`}
                      className="rounded-xl border border-line bg-raised/40 px-4 py-2.5"
                    >
                      <p className="text-sm text-ink">
                        &ldquo;{q.question}&rdquo;
                      </p>
                      <p className="mt-0.5 text-xs text-muted">
                        {[
                          q.author_name ? `Asked by ${q.author_name}` : null,
                          q.video_title ? `on “${q.video_title}”` : null,
                          q.count !== null && q.count > 1
                            ? `asked ${q.count} times`
                            : null,
                          q.like_count !== null && q.like_count > 0
                            ? `${q.like_count} ${q.like_count === 1 ? "like" : "likes"}`
                            : null,
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                      </p>
                    </li>
                  ))}
                </ul>
                <p className="mt-2 text-xs text-muted">
                  Questions are quoted verbatim from viewers&rsquo;
                  public comments.
                </p>
              </div>
            ) : (
              <p className="text-sm text-muted">
                No viewer questions were identified in the latest
                analysis.
              </p>
            )}
          </div>
        ) : null}
      </SectionShell>

      <DraftApprovalsCard channelId={channelId} />
    </>
  );
}

// ── Draft-approvals entry point (read-only list + path to Comments) ──────

/**
 * Entry point into the reply-draft approval flow owned by the
 * comments team. The pending list here is READ-ONLY; decisions
 * happen on the Comments screen (`/comments`), which is where the
 * approve / reject actions live. Per the backend's own boundary
 * stamp, approving a draft records a LOCAL decision only — posting
 * the reply to YouTube is not wired yet, and this card says so
 * rather than implying otherwise.
 */
function DraftApprovalsCard({ channelId }: { channelId: string | null }) {
  const query = useCommentDrafts(channelId);
  const notLanded = isNotLanded(query.error);
  const pending = (query.data ?? []).filter((d) => d.status === "proposed");

  return (
    <Card className="p-5">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold text-ink">
            Reply drafts awaiting approval
          </h2>
          <p className="mt-0.5 text-sm text-muted">
            Draft replies the Comment Agent has proposed for this
            channel.
          </p>
        </div>
        {!notLanded && !query.isLoading && !query.isError ? (
          <Badge tone={pending.length > 0 ? "amber" : "gray"}>
            {pending.length} pending
          </Badge>
        ) : null}
      </div>

      {query.isLoading ? (
        <div className="space-y-3" aria-label="Loading reply drafts">
          <div className="h-4 w-1/2 animate-pulse rounded bg-raised" />
          <div className="h-4 w-2/3 animate-pulse rounded bg-raised" />
        </div>
      ) : notLanded ? (
        <p className="text-sm text-muted">
          The comment-drafts read API has not landed in the engine
          yet, so pending drafts cannot be listed here.
        </p>
      ) : query.isError ? (
        <p className="text-sm text-muted">
          Could not load pending drafts
          {query.error instanceof Error
            ? ` — ${query.error.message}`
            : ""}
          .{" "}
          <button
            type="button"
            className="font-medium text-accent2 underline"
            onClick={() => void query.refetch()}
          >
            Retry
          </button>
        </p>
      ) : pending.length === 0 ? (
        <div className="flex flex-col items-center gap-2 py-6 text-center">
          <PenLine className="h-8 w-8 text-muted" />
          <p className="text-sm font-medium text-ink">
            No reply drafts are waiting
          </p>
          <p className="max-w-md text-sm text-muted">
            When the Comment Agent drafts a reply that needs a human
            decision — a question it cannot answer from the video
            itself, criticism, or anything on a sensitive niche — it
            will be listed here as pending.
          </p>
        </div>
      ) : (
        <>
          <ul className="space-y-3">
            {pending.slice(0, 5).map((draft) => (
              <li
                key={draft.id}
                className="rounded-xl border border-line bg-raised/40 p-4"
              >
                {draft.parent_text ? (
                  <p className="text-xs text-muted">
                    In reply to
                    {draft.parent_author
                      ? ` ${draft.parent_author}`
                      : ""}
                    : &ldquo;{draft.parent_text}&rdquo;
                  </p>
                ) : null}
                <p className="mt-1 text-sm text-ink">
                  &ldquo;{draft.draft_text}&rdquo;
                </p>
                <p className="mt-1 text-xs text-muted">
                  Drafted by the Comment Agent
                  {draft.created_at
                    ? ` · ${fullDate(draft.created_at)}`
                    : ""}
                  {draft.video_title ? ` · on “${draft.video_title}”` : ""}
                </p>
              </li>
            ))}
          </ul>
          {pending.length > 5 ? (
            <p className="mt-3 text-xs text-muted">
              …and {pending.length - 5} more pending{" "}
              {pending.length - 5 === 1 ? "draft" : "drafts"}.
            </p>
          ) : null}
        </>
      )}

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-3">
        <p className="max-w-xl text-xs text-muted">
          Approval happens on the Comments screen: approving a draft
          there records your decision — it does not post the reply,
          because posting to YouTube is not wired yet. This list is
          read-only.
        </p>
        <Link
          to="/comments"
          className="inline-flex items-center gap-1.5 text-sm font-medium text-accent2 underline"
        >
          Review drafts in Comments
          <ArrowRight className="h-4 w-4" />
        </Link>
      </div>
    </Card>
  );
}
