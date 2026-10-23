import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Check,
  Flag,
  HelpCircle,
  Link2,
  MessagesSquare,
  RefreshCw,
  Sparkles,
  ThumbsDown,
  ThumbsUp,
  X,
} from "lucide-react";
import { Card } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { Badge } from "../components/ui/Badge";
import { ApiError } from "../api/types";
import { useChannels } from "../api/queries/channels";
import type { ChannelWire } from "../api/queries/channels";
import {
  useApproveCommentDraft,
  useCommentDrafts,
  useCommentRollups,
  useCommentVideos,
  useDiscardCommentDraft,
  useGenerateCommentDraft,
  useModerationDecision,
  useModerationQueue,
  useRejectCommentDraft,
  useRunCommentAnalysis,
  useSyncComments,
} from "../api/queries/comments";
import type {
  CommentDraftWire,
  CommentRollupWire,
  CommentVideoWire,
  ModerationFlagWire,
  SentimentSharesWire,
  TopQuestionWire,
} from "../api/queries/comments";

type BadgeTone = "violet" | "green" | "amber" | "red" | "gray";

// ── Comments Intelligence (Round 5, Agent 28) ─────────────────────────────
//
// Placement decision: Comments is its OWN screen (`/comments`), listed
// in the sidebar immediately after Analytics. The Growth/Analytics area
// is where it belongs conceptually (Phase 7 — Growth), but Analytics.tsx
// is a single static report page with no tab/section system, so a
// separate route next to it fits the app's flat one-screen-per-route
// navigation cleanly and keeps this view deep-linkable.
//
// Honesty rules for this screen:
// - No sample or fake comments anywhere: every row comes from the API,
//   and each data area has explicit loading / error / empty states.
// - A channel the backend reports as disconnected gets a "Connect a
//   channel" empty state and NO comment queries are fired for it.
// - Every rollup shows its analysis provenance: the LLM model id the
//   backend reported, the exact label "Deterministic analysis" for the
//   deterministic engine, or "engine not reported" — never invented.
// - Draft replies can be approved/rejected/discarded (local
//   decisions; discarded is distinct from rejected). There is
//   NO post/publish button: an approved draft says so, in words.

const inputClass =
  "w-full rounded-lg border border-line bg-raised px-3 py-2 text-sm text-ink outline-none focus:border-accent placeholder:text-muted";
const labelClass = "mb-1 block text-xs font-medium text-ink2";

function humanize(value: string): string {
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function errorText(err: unknown): string {
  if (err instanceof ApiError && err.code === "NETWORK_ERROR") {
    return "The backend isn't reachable. Check that the app backend is running, then retry.";
  }
  return err instanceof Error ? err.message : "Something went wrong.";
}

/** Backend timestamps: epoch-ms numbers or ISO strings (both tolerated). */
function formatTimestamp(value: string | number | null): string | null {
  if (value === null || value === "") return null;
  let ms: number;
  if (typeof value === "number") {
    ms = value;
  } else if (/^\d+$/.test(value.trim())) {
    ms = Number(value.trim());
  } else {
    ms = Date.parse(value);
  }
  if (!Number.isFinite(ms)) return null;
  return new Date(ms).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

/**
 * The provenance label for a rollup/draft. An LLM model id is shown
 * verbatim; a deterministic marker becomes the exact label
 * "Deterministic analysis"; a missing engine is reported as missing.
 */
function engineLabel(engine: string | null): string {
  if (!engine) return "Analysis engine not reported by the backend";
  if (engine.toLowerCase().includes("deterministic")) return "Deterministic analysis";
  if (engine === "llm") return "LLM (model id not reported)";
  return engine;
}

function Provenance({ engine, analysedAt }: { engine: string | null; analysedAt?: string | number | null }) {
  const when = formatTimestamp(analysedAt ?? null);
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <Badge tone="gray">Analysed by: {engineLabel(engine)}</Badge>
      {when && <span className="text-xs text-muted">{when}</span>}
    </span>
  );
}

// ── Sentiment band ─────────────────────────────────────────────────────────

const BAND_SEGMENTS: Array<{
  key: keyof SentimentSharesWire;
  label: string;
  barClass: string;
}> = [
  { key: "positive", label: "Positive", barClass: "bg-ok" },
  { key: "neutral", label: "Neutral", barClass: "bg-muted" },
  { key: "negative", label: "Negative", barClass: "bg-danger" },
  { key: "questions", label: "Questions", barClass: "accent-gradient" },
];

function SentimentBand({ sentiment }: { sentiment: SentimentSharesWire }) {
  return (
    <div>
      <div
        className="flex h-3 w-full overflow-hidden rounded-full bg-raised"
        role="img"
        aria-label={`Sentiment: ${BAND_SEGMENTS.map(
          (s) => `${s.label} ${Math.round(sentiment[s.key] * 100)}%`,
        ).join(", ")}`}
      >
        {BAND_SEGMENTS.map((seg) => {
          const pct = Math.max(0, Math.min(100, sentiment[seg.key] * 100));
          if (pct <= 0) return null;
          return (
            <div
              key={seg.key}
              className={`h-full ${seg.barClass}`}
              style={{ width: `${pct}%` }}
              title={`${seg.label}: ${Math.round(pct)}%`}
            />
          );
        })}
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
        {BAND_SEGMENTS.map((seg) => (
          <span key={seg.key} className="inline-flex items-center gap-1.5 text-xs text-ink2">
            <span className={`h-2 w-2 rounded-full ${seg.barClass}`} />
            {seg.label}
            <span className="font-mono text-ink tnum">
              {Math.round(sentiment[seg.key] * 100)}%
            </span>
          </span>
        ))}
      </div>
    </div>
  );
}

// ── Sync status chip ───────────────────────────────────────────────────────

function syncChip(video: CommentVideoWire): { label: string; tone: BadgeTone } {
  if (video.sync_status_raw) {
    return { label: humanize(video.sync_status_raw), tone: "gray" };
  }
  switch (video.sync_status) {
    case "synced": {
      const when = formatTimestamp(video.last_synced_at);
      return {
        label: `Synced${video.comment_count !== null ? ` · ${video.comment_count} comments` : ""}${when ? ` · ${when}` : ""}`,
        tone: "green",
      };
    }
    case "never_synced":
      return { label: "Never synced", tone: "gray" };
    case "not_connected":
      return { label: "Not connected", tone: "red" };
    case "comments_disabled":
      return { label: "Comments disabled on this video", tone: "amber" };
    case "syncing":
      return { label: "Syncing…", tone: "violet" };
    case "sync_failed":
    case "error":
      return { label: "Sync failed", tone: "red" };
    default:
      return { label: humanize(video.sync_status), tone: "gray" };
  }
}

// ── Shared section states ──────────────────────────────────────────────────

function SectionLoading({ text }: { text: string }) {
  return <p className="mt-3 text-sm text-muted">{text}</p>;
}

function SectionError({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry: () => void;
}) {
  return (
    <div className="mt-3 flex flex-wrap items-center gap-2">
      <p className="text-sm text-danger">{errorText(error)}</p>
      <Button variant="outline" size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}

function EmptyState({ children }: { children: React.ReactNode }) {
  return (
    <p className="mt-3 rounded-lg border border-dashed border-line px-4 py-5 text-center text-sm text-ink2">
      {children}
    </p>
  );
}

// ── Videos & sync ──────────────────────────────────────────────────────────

function VideosSyncSection({ channelId }: { channelId: string }) {
  const videosQuery = useCommentVideos(channelId);
  const sync = useSyncComments(channelId);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const videos = videosQuery.data ?? [];

  const runSync = (videoId?: string) => {
    setNotice(null);
    setError(null);
    sync.mutate(videoId, {
      onSuccess: (result) => {
        setNotice(
          result.synced_count !== null
            ? `Sync finished — ${result.synced_count} comments now stored for this channel.`
            : "Sync finished — the list below shows the latest per-video status.",
        );
      },
      onError: (err) => setError(errorText(err)),
    });
  };

  return (
    <Card className="mt-6 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="flex items-center gap-2 text-base font-semibold text-ink">
            <RefreshCw className="h-5 w-5 text-accent2" />
            Videos &amp; comment sync
          </h2>
          <p className="mt-1 max-w-2xl text-sm leading-relaxed text-ink2">
            Comments are fetched from YouTube per video. A video only
            appears in the analysis below after it has been synced.
          </p>
        </div>
        <Button
          disabled={sync.isPending || videosQuery.isLoading}
          onClick={() => runSync(undefined)}
        >
          <RefreshCw className="mr-1.5 h-4 w-4" />
          {sync.isPending ? "Syncing…" : "Sync comments"}
        </Button>
      </div>

      {notice && <p className="mt-3 text-xs text-ink2">{notice}</p>}
      {error && <p className="mt-3 text-xs text-danger">{error}</p>}

      {videosQuery.isLoading && <SectionLoading text="Loading videos…" />}
      {videosQuery.isError && (
        <SectionError
          error={videosQuery.error}
          onRetry={() => void videosQuery.refetch()}
        />
      )}
      {!videosQuery.isLoading && !videosQuery.isError && videos.length === 0 && (
        <EmptyState>
          No videos found for this channel yet — once the channel has
          published videos, they will show up here for syncing.
        </EmptyState>
      )}
      {videos.length > 0 && (
        <ul className="mt-4 divide-y divide-line">
          {videos.map((video) => {
            const chip = syncChip(video);
            const canSync =
              video.sync_status === "never_synced" ||
              video.sync_status === "sync_failed" ||
              video.sync_status === "error";
            return (
              <li
                key={video.video_id || video.id}
                className="flex flex-wrap items-center gap-3 py-3 first:pt-0 last:pb-0"
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-ink">
                    {video.title ?? "Untitled video"}
                  </p>
                  <p className="mt-0.5 font-mono text-xs text-muted">
                    {video.video_id}
                  </p>
                  {video.error_reason && (
                    <p className="mt-0.5 text-xs text-danger">
                      Last sync failed: {video.error_reason}
                    </p>
                  )}
                </div>
                <Badge tone={chip.tone}>{chip.label}</Badge>
                {canSync && (
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={sync.isPending}
                    onClick={() => runSync(video.video_id)}
                  >
                    Sync
                  </Button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

// ── Rollup body (shared by the channel rollup and per-video rollups) ──────

function TopicChips({
  topics,
  tone,
}: {
  topics: CommentRollupWire["themes"];
  tone: BadgeTone;
}) {
  if (topics.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {topics.map((topic) => (
        <Badge key={topic.label} tone={tone}>
          {topic.label}
          {topic.count !== null
            ? ` · ${topic.count}`
            : topic.share !== null
              ? ` · ${Math.round(topic.share * 100)}%`
              : ""}
        </Badge>
      ))}
    </div>
  );
}

function RollupBody({ rollup, compact = false }: { rollup: CommentRollupWire; compact?: boolean }) {
  const hasTopics =
    rollup.themes.length > 0 ||
    rollup.praise_topics.length > 0 ||
    rollup.complaint_topics.length > 0;
  return (
    <div className={compact ? "mt-3 space-y-4" : "mt-4 space-y-5"}>
      <div className="flex flex-wrap items-center gap-2">
        <Provenance engine={rollup.engine} analysedAt={rollup.analysed_at} />
        {rollup.comment_count !== null && (
          <span className="text-xs text-muted">
            Based on {rollup.comment_count} comments
          </span>
        )}
      </div>

      {rollup.sentiment ? (
        <div>
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">
            Sentiment
          </p>
          <SentimentBand sentiment={rollup.sentiment} />
        </div>
      ) : (
        <p className="text-sm text-muted">
          Sentiment was not reported for this rollup.
        </p>
      )}

      {rollup.themes.length > 0 && (
        <div>
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">
            Themes
          </p>
          <TopicChips topics={rollup.themes} tone="violet" />
        </div>
      )}

      {(rollup.praise_topics.length > 0 || rollup.complaint_topics.length > 0) && (
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <p className="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted">
              <ThumbsUp className="h-3.5 w-3.5 text-ok" />
              Praised
            </p>
            {rollup.praise_topics.length > 0 ? (
              <TopicChips topics={rollup.praise_topics} tone="green" />
            ) : (
              <p className="text-sm text-muted">No praise topics reported.</p>
            )}
          </div>
          <div>
            <p className="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted">
              <ThumbsDown className="h-3.5 w-3.5 text-danger" />
              Complaints
            </p>
            {rollup.complaint_topics.length > 0 ? (
              <TopicChips topics={rollup.complaint_topics} tone="red" />
            ) : (
              <p className="text-sm text-muted">No complaint topics reported.</p>
            )}
          </div>
        </div>
      )}

      {!rollup.sentiment && !hasTopics && (
        <p className="text-sm text-muted">
          This rollup carries no sentiment or topic detail.
        </p>
      )}
    </div>
  );
}

// ── Top questions (these feed topic planning — shown prominently) ─────────

function mergeQuestions(rollups: CommentRollupWire[]): TopQuestionWire[] {
  const channel = rollups.find((r) => r.scope === "channel");
  if (channel && channel.top_questions.length > 0) return channel.top_questions;
  const seen = new Map<string, TopQuestionWire>();
  for (const rollup of rollups) {
    for (const q of rollup.top_questions) {
      const key = q.question.trim().toLowerCase();
      if (!seen.has(key)) {
        seen.set(key, {
          ...q,
          video_title: q.video_title ?? rollup.video_title,
        });
      }
    }
  }
  return [...seen.values()]
    .sort((a, b) => (b.count ?? 0) - (a.count ?? 0))
    .slice(0, 10);
}

function TopQuestionsCard({
  rollups,
  channelId,
}: {
  rollups: CommentRollupWire[];
  channelId: string;
}) {
  const generateDraft = useGenerateCommentDraft(channelId);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pendingCommentId, setPendingCommentId] = useState<string | null>(null);

  const questions = mergeQuestions(rollups);
  if (questions.length === 0) return null;

  const handleDraftReply = (q: TopQuestionWire) => {
    if (!q.comment_id) return;
    setNotice(null);
    setError(null);
    setPendingCommentId(q.comment_id);
    generateDraft.mutate(q.comment_id, {
      onSuccess: () =>
        setNotice(
          "Draft reply proposed — review and approve it under “Draft replies” below. Nothing is posted automatically.",
        ),
      onError: (err) => setError(errorText(err)),
      onSettled: () => setPendingCommentId(null),
    });
  };

  return (
    <Card className="mt-6 border-accent/40 p-5">
      <h2 className="flex items-center gap-2 text-base font-semibold text-ink">
        <HelpCircle className="h-5 w-5 text-accent2" />
        Top questions from viewers
      </h2>
      <p className="mt-1 text-sm leading-relaxed text-ink2">
        The questions your audience asks most. These feed topic planning —
        a question asked again and again is a future video. You can also
        draft a reply to a question right here.
      </p>
      {notice && <p className="mt-3 text-xs text-ink2">{notice}</p>}
      {error && <p className="mt-3 text-xs text-danger">{error}</p>}
      <ol className="mt-4 space-y-3">
        {questions.map((q, index) => (
          <li key={`${q.question}-${index}`} className="flex items-start gap-3">
            <span className="accent-gradient flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-bold text-white">
              {index + 1}
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-sm text-ink">{q.question}</p>
              <p className="mt-0.5 flex flex-wrap gap-x-3 text-xs text-muted">
                {q.count !== null && <span>Asked {q.count}×</span>}
                {q.count === null && q.like_count !== null && q.like_count > 0 && (
                  <span>♥ {q.like_count}</span>
                )}
                {q.video_title && <span>on “{q.video_title}”</span>}
                {q.author_name && <span>first by {q.author_name}</span>}
              </p>
            </div>
            {q.comment_id && (
              <Button
                variant="ghost"
                size="sm"
                disabled={generateDraft.isPending}
                onClick={() => handleDraftReply(q)}
              >
                {pendingCommentId === q.comment_id ? "Drafting…" : "Draft reply"}
              </Button>
            )}
          </li>
        ))}
      </ol>
    </Card>
  );
}

// ── Rollups section (channel rollup + not-analysed / no-comments states) ───

function RollupsSection({
  channelId,
  videos,
}: {
  channelId: string;
  videos: CommentVideoWire[];
}) {
  const rollupsQuery = useCommentRollups(channelId);
  const runAnalysis = useRunCommentAnalysis(channelId);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const rollups = rollupsQuery.data;
  const channelRollup = rollups?.channel ?? null;
  const videoRollups = rollups?.videos ?? [];
  const hasRollups = channelRollup !== null || videoRollups.length > 0;
  const anySynced = videos.some(
    (v) => v.sync_status === "synced" && (v.comment_count ?? 0) > 0,
  );
  const anySyncedAtAll = videos.some((v) => v.sync_status === "synced");

  const handleRunAnalysis = () => {
    setNotice(null);
    setError(null);
    runAnalysis.mutate(undefined, {
      onSuccess: () =>
        setNotice("Analysis finished — the rollups below are up to date."),
      onError: (err) => setError(errorText(err)),
    });
  };

  const allRollups: CommentRollupWire[] = [
    ...(channelRollup ? [channelRollup] : []),
    ...videoRollups,
  ];

  return (
    <>
      <Card className="mt-6 p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-base font-semibold text-ink">
              <Sparkles className="h-5 w-5 text-accent2" />
              Channel rollup
            </h2>
            <p className="mt-1 max-w-2xl text-sm leading-relaxed text-ink2">
              What viewers are saying across this channel — sentiment,
              themes, praise and complaints, rolled up from the synced
              comments.
            </p>
          </div>
          {hasRollups && (
            <Button
              variant="outline"
              size="sm"
              disabled={runAnalysis.isPending}
              onClick={handleRunAnalysis}
            >
              <Sparkles className="mr-1.5 h-4 w-4" />
              {runAnalysis.isPending ? "Analysing…" : "Re-run analysis"}
            </Button>
          )}
        </div>

        {notice && <p className="mt-3 text-xs text-ink2">{notice}</p>}
        {error && <p className="mt-3 text-xs text-danger">{error}</p>}

        {rollupsQuery.isLoading && <SectionLoading text="Loading analysis…" />}
        {rollupsQuery.isError && (
          <SectionError
            error={rollupsQuery.error}
            onRetry={() => void rollupsQuery.refetch()}
          />
        )}

        {!rollupsQuery.isLoading && !rollupsQuery.isError && !hasRollups && (
          <>
            {anySynced ? (
              <div className="mt-4 rounded-lg border border-dashed border-line px-4 py-5 text-center">
                <p className="text-sm font-medium text-ink">Not analysed yet</p>
                <p className="mx-auto mt-1 max-w-md text-sm text-ink2">
                  Comments are synced for this channel, but no analysis has
                  run yet. Run it to get sentiment, themes and top
                  questions.
                </p>
                <Button
                  className="mt-3"
                  disabled={runAnalysis.isPending}
                  onClick={handleRunAnalysis}
                >
                  <Sparkles className="mr-1.5 h-4 w-4" />
                  {runAnalysis.isPending ? "Analysing…" : "Run analysis"}
                </Button>
              </div>
            ) : (
              <EmptyState>
                {anySyncedAtAll
                  ? "No comments yet — the synced videos have no comments to analyse."
                  : "No comments yet — sync a video above first, then run the analysis here."}
              </EmptyState>
            )}
          </>
        )}

        {channelRollup && <RollupBody rollup={channelRollup} />}
        {!channelRollup && hasRollups && (
          <p className="mt-4 text-sm text-muted">
            No channel-wide rollup was reported — per-video rollups are
            shown below.
          </p>
        )}
      </Card>

      <TopQuestionsCard rollups={allRollups} channelId={channelId} />

      {videoRollups.length > 0 && (
        <div className="mt-6 grid grid-cols-1 gap-6 xl:grid-cols-2">
          {videoRollups.map((rollup) => (
            <Card
              key={rollup.video_id ?? rollup.video_title ?? "video-rollup"}
              className="p-5"
            >
              <h3 className="truncate text-sm font-semibold text-ink">
                {rollup.video_title ?? "Untitled video"}
              </h3>
              <RollupBody rollup={rollup} compact />
              {rollup.top_questions.length > 0 && (
                <div className="mt-4 border-t border-line pt-3">
                  <p className="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted">
                    <HelpCircle className="h-3.5 w-3.5" />
                    Top questions
                  </p>
                  <ul className="space-y-1.5">
                    {rollup.top_questions.slice(0, 3).map((q, i) => (
                      <li key={i} className="text-sm text-ink2">
                        {q.question}
                        {q.count !== null && (
                          <span className="ml-2 font-mono text-xs text-muted tnum">
                            {q.count}×
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </Card>
          ))}
        </div>
      )}
    </>
  );
}

// ── Draft replies ──────────────────────────────────────────────────────────

function draftStatusChip(draft: CommentDraftWire): { label: string; tone: BadgeTone } {
  if (draft.status_raw) return { label: humanize(draft.status_raw), tone: "gray" };
  switch (draft.status) {
    case "approved":
      return { label: "Approved", tone: "green" };
    case "rejected":
      return { label: "Rejected", tone: "red" };
    case "discarded":
      return { label: "Discarded", tone: "gray" };
    default:
      return { label: "Proposed", tone: "amber" };
  }
}

function DraftCard({
  draft,
  channelId,
  onError,
}: {
  draft: CommentDraftWire;
  channelId: string;
  onError: (text: string) => void;
}) {
  const approve = useApproveCommentDraft(channelId);
  const reject = useRejectCommentDraft(channelId);
  const discard = useDiscardCommentDraft(channelId);
  const chip = draftStatusChip(draft);
  const deciding = approve.isPending || reject.isPending || discard.isPending;

  return (
    <div className="rounded-xl border border-line bg-raised/40 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={chip.tone}>{chip.label}</Badge>
        {draft.video_title && (
          <span className="truncate text-xs text-muted">on “{draft.video_title}”</span>
        )}
        <span className="ml-auto">
          <Provenance engine={draft.engine} />
        </span>
      </div>

      {/* The parent comment this draft replies to */}
      <div className="mt-3 border-l-2 border-line pl-3">
        <p className="text-xs font-medium text-ink2">
          {draft.parent_author ?? "Unknown commenter"}
          {draft.parent_like_count !== null && (
            <span className="ml-2 font-mono text-muted tnum">
              ♥ {draft.parent_like_count}
            </span>
          )}
        </p>
        <p className="mt-0.5 text-sm leading-relaxed text-ink2">
          {draft.parent_text ??
            (draft.comment_id
              ? `(parent comment text not included in the drafts response — comment ${draft.comment_id})`
              : "(comment text not reported)")}
        </p>
      </div>

      <div className="mt-3">
        <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">
          Draft reply
        </p>
        <p className="text-sm leading-relaxed text-ink">{draft.draft_text}</p>
      </div>

      {draft.status === "proposed" && !draft.status_raw && (
        <div className="mt-3 flex flex-wrap gap-2">
          <Button
            size="sm"
            disabled={deciding}
            onClick={() =>
              approve.mutate(draft.id, {
                onError: (err) => onError(errorText(err)),
              })
            }
          >
            <Check className="mr-1 h-3.5 w-3.5" />
            {approve.isPending ? "Approving…" : "Approve"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={deciding}
            onClick={() =>
              reject.mutate(draft.id, {
                onError: (err) => onError(errorText(err)),
              })
            }
          >
            <X className="mr-1 h-3.5 w-3.5" />
            {reject.isPending ? "Rejecting…" : "Reject"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={deciding}
            onClick={() =>
              discard.mutate(draft.id, {
                onError: (err) => onError(errorText(err)),
              })
            }
          >
            {discard.isPending ? "Discarding…" : "Discard"}
          </Button>
        </div>
      )}

      {/* Honesty requirement: an approved draft is NOT a posted reply. */}
      {draft.status === "approved" && (
        <p className="mt-3 flex items-start gap-2 rounded-lg bg-ok/10 px-3 py-2 text-xs leading-relaxed text-ok">
          <Check className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          Approved — not posted. Posting replies isn&apos;t available yet;
          approving only saved your decision in the app, and nothing was
          sent to YouTube.
        </p>
      )}
      {draft.status === "rejected" && (
        <p className="mt-3 text-xs text-muted">
          Rejected — this draft won&apos;t be used.
        </p>
      )}
      {draft.status === "discarded" && (
        <p className="mt-3 text-xs text-muted">
          Discarded — set aside without a decision; this draft
          won&apos;t be used.
        </p>
      )}
    </div>
  );
}

function DraftsSection({ channelId }: { channelId: string }) {
  const draftsQuery = useCommentDrafts(channelId);
  const [error, setError] = useState<string | null>(null);

  const drafts = draftsQuery.data ?? [];

  return (
    <Card className="mt-6 p-5">
      <div className="min-w-0">
        <h2 className="flex items-center gap-2 text-base font-semibold text-ink">
          <MessagesSquare className="h-5 w-5 text-accent2" />
          Draft replies
        </h2>
        <p className="mt-1 max-w-2xl text-sm leading-relaxed text-ink2">
          Replies drafted in the channel&apos;s voice for your review —
          draft one from any Top question above, then approve or reject
          it here. Nothing is ever posted automatically: there is no
          post button, and approving a draft only records your decision
          in the app (the backend stamps every draft
          “posting: not wired”).
        </p>
      </div>

      {error && <p className="mt-3 text-xs text-danger">{error}</p>}

      {draftsQuery.isLoading && <SectionLoading text="Loading drafts…" />}
      {draftsQuery.isError && (
        <SectionError
          error={draftsQuery.error}
          onRetry={() => void draftsQuery.refetch()}
        />
      )}
      {!draftsQuery.isLoading && !draftsQuery.isError && drafts.length === 0 && (
        <EmptyState>
          No draft replies yet — use “Draft reply” on a Top question
          above, and the proposed draft will appear here for review.
        </EmptyState>
      )}
      {drafts.length > 0 && (
        <div className="mt-4 space-y-3">
          {drafts.map((draft) => (
            <DraftCard
              key={draft.id}
              draft={draft}
              channelId={channelId}
              onError={setError}
            />
          ))}
        </div>
      )}
    </Card>
  );
}

// ── Moderation queue ───────────────────────────────────────────────────────

const MODERATION_DONE_STATUSES: ReadonlySet<string> = new Set([
  "dismissed",
  "reviewed",
  "resolved",
  "closed",
  "actioned",
]);

function moderationStatusChip(flag: ModerationFlagWire): { label: string; tone: BadgeTone } {
  switch (flag.status.toLowerCase()) {
    case "dismissed":
      return { label: "Dismissed", tone: "gray" };
    case "reviewed":
      return { label: "Reviewed", tone: "green" };
    case "actioned":
      return { label: "Actioned", tone: "green" };
    case "pending":
    case "open":
    case "flagged":
      return { label: "Waiting for review", tone: "amber" };
    default:
      return { label: humanize(flag.status), tone: "gray" };
  }
}

function ModerationSection({ channelId }: { channelId: string }) {
  const queueQuery = useModerationQueue(channelId);
  const decide = useModerationDecision(channelId);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const flags = queueQuery.data ?? [];

  const handleDecision = (flagId: string, decision: "dismiss" | "mark_reviewed") => {
    setError(null);
    setNotice(null);
    decide.mutate(
      { flagId, decision },
      {
        onSuccess: () =>
          setNotice(
            "Decision recorded locally — the flag leaves the pending queue. Nothing was done on YouTube.",
          ),
        onError: (err) => setError(errorText(err)),
      },
    );
  };

  return (
    <Card className="mt-6 p-5">
      <h2 className="flex items-center gap-2 text-base font-semibold text-ink">
        <Flag className="h-5 w-5 text-accent2" />
        Moderation queue
      </h2>
      <p className="mt-1 max-w-2xl text-sm leading-relaxed text-ink2">
        Comments the analysis flagged for a human look. Decisions here are
        local notes only — dismissing or marking a comment reviewed does
        not hide, delete or report anything on YouTube.
      </p>

      {notice && <p className="mt-3 text-xs text-ink2">{notice}</p>}
      {error && <p className="mt-3 text-xs text-danger">{error}</p>}

      {queueQuery.isLoading && <SectionLoading text="Loading moderation queue…" />}
      {queueQuery.isError && (
        <SectionError
          error={queueQuery.error}
          onRetry={() => void queueQuery.refetch()}
        />
      )}
      {!queueQuery.isLoading && !queueQuery.isError && flags.length === 0 && (
        <EmptyState>
          Nothing is waiting for moderation — no comments are flagged for
          this channel.
        </EmptyState>
      )}
      {flags.length > 0 && (
        <div className="mt-4 space-y-3">
          {flags.map((flag) => {
            const chip = moderationStatusChip(flag);
            const decided = MODERATION_DONE_STATUSES.has(flag.status.toLowerCase());
            return (
              <div
                key={flag.id}
                className="rounded-xl border border-line bg-raised/40 p-4"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone="red">{humanize(flag.flag_kind)}</Badge>
                  {flag.suggested_action && (
                    <Badge tone="violet">
                      Suggested: {humanize(flag.suggested_action)}
                    </Badge>
                  )}
                  <Badge tone={chip.tone}>{chip.label}</Badge>
                  {flag.video_title && (
                    <span className="truncate text-xs text-muted">
                      on “{flag.video_title}”
                    </span>
                  )}
                </div>
                <p className="mt-3 text-sm leading-relaxed text-ink">
                  {flag.comment_text ?? "(comment text not reported)"}
                </p>
                <p className="mt-1 text-xs text-muted">
                  {flag.author_name ? `by ${flag.author_name}` : "Unknown commenter"}
                  {flag.reason ? ` · Flagged because: ${flag.reason}` : ""}
                </p>
                {!decided && (
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={decide.isPending}
                      onClick={() => handleDecision(flag.id, "dismiss")}
                    >
                      Dismiss
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={decide.isPending}
                      onClick={() => handleDecision(flag.id, "mark_reviewed")}
                    >
                      <Check className="mr-1 h-3.5 w-3.5" />
                      Mark reviewed
                    </Button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

// ── Screen ─────────────────────────────────────────────────────────────────

/**
 * A channel counts as disconnected ONLY when the backend says so
 * (`status`/`state` = disconnected / not_connected). Any other state
 * still lets the comment queries run — the per-video sync rows carry
 * their own honest `not_connected` status from the backend.
 */
function isDisconnected(channel: ChannelWire): boolean {
  const raw = (channel.status ?? channel.state ?? "").toLowerCase();
  return raw === "disconnected" || raw === "not_connected";
}

export default function Comments() {
  const channelsQuery = useChannels();
  const [selectedChannelId, setSelectedChannelId] = useState<string | null>(null);

  const channels = channelsQuery.data ?? [];

  // Same selection pattern as the Style Lab: default to the first
  // channel and keep the selection valid as the list changes.
  useEffect(() => {
    if (channels.length === 0) return;
    if (!selectedChannelId || !channels.some((c) => c.id === selectedChannelId)) {
      setSelectedChannelId(channels[0].id);
    }
  }, [channels, selectedChannelId]);

  const channel =
    channels.find((c) => c.id === selectedChannelId) ?? channels[0] ?? null;
  const disconnected = channel !== null && isDisconnected(channel);
  // No comment queries fire for a disconnected channel — there is
  // nothing honest to show until it is connected again.
  const dataChannelId = channel && !disconnected ? channel.id : null;

  // The rollups section needs the per-video rows for its honest
  // "not analysed" vs "no comments yet" distinction; this query shares
  // its cache entry with the sync section (same key), so it costs one
  // fetch total.
  const videosQuery = useCommentVideos(dataChannelId);
  const videos = videosQuery.data ?? [];

  return (
    <div className="bg-app min-h-full p-6 text-ink">
      <header className="mb-2 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="flex items-center gap-2 text-2xl font-semibold text-ink">
            <MessagesSquare className="h-6 w-6 text-accent2" />
            Comments
          </h1>
          <p className="mt-1 max-w-2xl text-sm leading-relaxed text-muted">
            Comment intelligence for one channel at a time — what viewers
            are saying and asking, draft replies for your approval, and a
            moderation queue. Part of Growth, next to Analytics.
          </p>
        </div>
        {channels.length > 0 && channel && (
          <div className="w-full sm:w-64">
            <label className={labelClass} htmlFor="comments-channel">
              Channel
            </label>
            <select
              id="comments-channel"
              className={inputClass}
              value={channel.id}
              onChange={(e) => setSelectedChannelId(e.target.value)}
            >
              {channels.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.title || c.handle || c.id}
                </option>
              ))}
            </select>
          </div>
        )}
      </header>

      {channelsQuery.isLoading && (
        <p className="mt-6 text-sm text-muted">Loading channels…</p>
      )}
      {channelsQuery.isError && (
        <SectionError
          error={channelsQuery.error}
          onRetry={() => void channelsQuery.refetch()}
        />
      )}

      {!channelsQuery.isLoading && !channelsQuery.isError && channels.length === 0 && (
        <Card className="mt-6 p-6 text-center">
          <Link2 className="mx-auto h-8 w-8 text-muted" />
          <p className="mt-3 text-base font-semibold text-ink">
            Connect a channel to fetch comments
          </p>
          <p className="mx-auto mt-1 max-w-md text-sm leading-relaxed text-ink2">
            No channel is connected yet. Connect one on the Channels
            screen first — comments, analysis, draft replies and the
            moderation queue all work per connected channel.
          </p>
        </Card>
      )}

      {channel && disconnected && (
        <Card className="mt-6 p-6 text-center">
          <AlertTriangle className="mx-auto h-8 w-8 text-warn" />
          <p className="mt-3 text-base font-semibold text-ink">
            Connect a channel to fetch comments
          </p>
          <p className="mx-auto mt-1 max-w-md text-sm leading-relaxed text-ink2">
            “{channel.title || channel.handle || channel.id}” is
            disconnected, so its comments can&apos;t be fetched. Reconnect
            it on the Channels screen — anything already analysed stays
            saved and will show again once it is connected.
          </p>
        </Card>
      )}

      {dataChannelId && (
        <>
          <VideosSyncSection channelId={dataChannelId} />
          <RollupsSection channelId={dataChannelId} videos={videos} />
          <DraftsSection channelId={dataChannelId} />
          <ModerationSection channelId={dataChannelId} />
        </>
      )}
    </div>
  );
}
