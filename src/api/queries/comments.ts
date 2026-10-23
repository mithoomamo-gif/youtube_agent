/**
 * React Query hooks for Comments Intelligence (Round 5, Agent 28).
 *
 * Wire shapes are defined LOCALLY here (per shared conventions) because
 * they mirror the backend contract exactly — snake_case fields and all.
 * Every route is nested under the OWNING channel:
 *
 * - `GET    /channels/{channelId}/comments/videos`
 *     -> per-video sync rows: `{ video_id, title, sync_status,
 *        comment_count, last_synced_at }`. `sync_status` vocabulary
 *        (backend `comment_sync_state` + the sync service's rich
 *        fetch-level statuses): `never_synced | synced |
 *        not_connected | error`, with `comments_disabled` and other
 *        fetch-level values shown humanised when they appear.
 * - `POST   /channels/{channelId}/comments/sync`        body `{ video_id? }`
 * - `POST   /channels/{channelId}/comments/analysis`    body `{ video_id? }`
 * - `GET    /channels/{channelId}/comments/rollups`
 *     -> the channel rollup + one rollup per analysed video. Each
 *        rollup carries sentiment band shares, theme chips,
 *        praise/complaint topics, top questions and an `engine`
 *        provenance field (an LLM model id, or a deterministic-engine
 *        marker) — the screen renders that label verbatim and never
 *        invents one when the backend omits it.
 * - `POST   /channels/{channelId}/comments/{commentId}/drafts`  (201)
 *     -> generate ONE draft reply for a stored comment. There is no
 *        bulk-generate endpoint; the UI offers generation per comment
 *        (from the Top Questions list, whose items carry comment ids).
 * - `GET    /channels/{channelId}/comment-drafts`
 * - `POST   /channels/{channelId}/comment-drafts/{draftId}/approve`
 * - `POST   /channels/{channelId}/comment-drafts/{draftId}/reject`
 *     Draft views carry the backend's own posting boundary stamp:
 *     `posted: false`, `posting: "not_wired"` — approval is local.
 * - `GET    /channels/{channelId}/moderation-queue`
 *     -> defaults to `status=pending` server-side (the actionable
 *        queue); decided flags leave the list on refetch.
 * - `POST   /channels/{channelId}/moderation-queue/{flagId}/decision`
 *     body `{ decision: "dismiss" | "mark_reviewed" }` — a LOCAL
 *     decision only; flag views carry `action_executed: false` and a
 *     `suggested_action_label` that is a label, never an execution.
 *     Stored flag status after a decision: `dismissed` | `actioned`.
 *
 * Honesty rules:
 * - There is NO post/publish endpoint in this contract and the UI has
 *   no post button: approving a draft only records the decision.
 * - List responses use the `{ items, next_cursor }` envelope
 *   (consistent with the other list routers); bare arrays and the
 *   rollup object shapes are also tolerated.
 * - Timestamps follow the backend DB convention and may be epoch-ms
 *   numbers or ISO strings; both are tolerated.
 *
 * This module uses its own small fetch helpers (same token header +
 * error envelope as `ApiClient`) because `styleLab.ts` — the freshest
 * precedent — does the same.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../types";
import { getBackendConnection } from "../tauri";

// ── Wire types (backend snake_case, exactly as emitted) ────────────────────

export type CommentSyncStatusWire =
  | "never_synced"
  | "synced"
  | "not_connected"
  | "comments_disabled"
  | "syncing"
  | "sync_failed"
  | "error";

export interface CommentVideoWire {
  /** Row id when the backend gives the sync row its own id. */
  id: string;
  video_id: string;
  title: string | null;
  sync_status: CommentSyncStatusWire;
  /** Raw status when the backend used a value outside the vocabulary. */
  sync_status_raw: string | null;
  comment_count: number | null;
  last_synced_at: string | number | null;
  /** Why the last sync failed (backend `error_reason`), when it did. */
  error_reason: string | null;
  /** Present only when the backend tracks analysis per video. */
  analysis_status: string | null;
}

/** Sentiment band shares, normalised to fractions in [0, 1]. */
export interface SentimentSharesWire {
  positive: number;
  neutral: number;
  negative: number;
  questions: number;
}

export interface SentimentCountsWire {
  positive: number;
  neutral: number;
  negative: number;
  questions: number;
}

export interface TopicCountWire {
  label: string;
  count: number | null;
  /** Share of analysed comments (fraction), when the engine reports it. */
  share: number | null;
}

export interface TopQuestionWire {
  question: string;
  count: number | null;
  /** The source comment's like count, when reported (engine shape). */
  like_count: number | null;
  /** The source comment's id — enables per-comment draft generation. */
  comment_id: string | null;
  video_id: string | null;
  video_title: string | null;
  author_name: string | null;
}

export interface CommentRollupWire {
  scope: "channel" | "video";
  video_id: string | null;
  video_title: string | null;
  comment_count: number | null;
  /** Band shares (fractions). Null when the backend reported none. */
  sentiment: SentimentSharesWire | null;
  /** Band counts — only when the backend reported counts explicitly. */
  sentiment_counts: SentimentCountsWire | null;
  themes: TopicCountWire[];
  praise_topics: TopicCountWire[];
  complaint_topics: TopicCountWire[];
  top_questions: TopQuestionWire[];
  /**
   * Analysis provenance, verbatim from the backend: an LLM model id
   * (e.g. "gemini-…") or a deterministic-engine marker. Null when the
   * backend did not report an engine — the UI says exactly that.
   */
  engine: string | null;
  analysed_at: string | number | null;
}

export interface CommentRollupsWire {
  channel: CommentRollupWire | null;
  videos: CommentRollupWire[];
}

export type CommentDraftStatusWire =
  | "proposed"
  | "approved"
  | "rejected"
  | "discarded";

export interface CommentDraftWire {
  id: string;
  comment_id: string | null;
  video_id: string | null;
  video_title: string | null;
  /** The comment the draft replies to (the "parent comment"). */
  parent_author: string | null;
  parent_text: string | null;
  parent_like_count: number | null;
  draft_text: string;
  status: CommentDraftStatusWire;
  /** Raw status when outside the known vocabulary. */
  status_raw: string | null;
  engine: string | null;
  /** The backend's posting boundary stamp: always false in this round. */
  posted: boolean;
  /** e.g. "not_wired" — posting is not implemented; approval is local. */
  posting: string | null;
  created_at: string | number | null;
}

export interface ModerationFlagWire {
  id: string;
  comment_id: string | null;
  video_id: string | null;
  video_title: string | null;
  author_name: string | null;
  comment_text: string | null;
  /** e.g. "spam" | "abuse" | "self_promotion" | "off_topic" — rendered humanised. */
  flag_kind: string;
  /** The engine's suggested action label, shown as a suggestion only. */
  suggested_action: string | null;
  reason: string | null;
  /** e.g. "pending" | "dismissed" | "reviewed" — rendered generically. */
  status: string;
  created_at: string | number | null;
}

export type ModerationDecision = "dismiss" | "mark_reviewed";

export interface CommentSyncResultWire {
  status: string | null;
  synced_count: number | null;
}

// ── Minimal request helpers (mirror ApiClient's envelope handling) ────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function randomId(): string {
  const cryptoObj = globalThis.crypto;
  if (cryptoObj && typeof cryptoObj.randomUUID === "function") {
    return cryptoObj.randomUUID();
  }
  return `id-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

function toApiErrorFromBody(status: number, parsed: unknown): ApiError {
  if (isRecord(parsed) && isRecord(parsed.error)) {
    const err = parsed.error;
    return new ApiError({
      code: typeof err.code === "string" ? err.code : `HTTP_${status}`,
      message:
        typeof err.message === "string"
          ? err.message
          : `Request failed (${status})`,
      status,
      retryable:
        typeof err.retryable === "boolean"
          ? err.retryable
          : status === 429 || status >= 500,
      requestId: typeof err.request_id === "string" ? err.request_id : undefined,
      details: err.details,
    });
  }
  return new ApiError({
    code: `HTTP_${status}`,
    message: `Request failed (${status})`,
    status,
    retryable: status === 429 || status >= 500,
  });
}

async function parseBody(res: Response): Promise<unknown> {
  if (res.status === 204) return undefined;
  const text = await res.text();
  if (!text) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const conn = await getBackendConnection();
  const baseUrl = conn.baseUrl.replace(/\/+$/, "");

  const headers: Record<string, string> = { "X-Local-Token": conn.token };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method.toUpperCase() === "POST") headers["Idempotency-Key"] = randomId();

  let res: Response;
  try {
    res = await fetch(`${baseUrl}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") throw err;
    throw new ApiError({
      code: "NETWORK_ERROR",
      message: err instanceof Error ? err.message : "Network request failed",
      status: 0,
      retryable: true,
    });
  }

  const parsed = await parseBody(res);
  if (!res.ok) throw toApiErrorFromBody(res.status, parsed);
  return parsed as T;
}

/** List responses: `{ items }` envelope assumed; bare arrays tolerated. */
function listItems(raw: unknown): unknown[] {
  if (Array.isArray(raw)) return raw;
  if (isRecord(raw) && Array.isArray(raw.items)) return raw.items;
  return [];
}

// ── Row normalisers (tolerant of missing/null optional fields) ────────────

function strOrNull(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function numOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function timeOrNull(value: unknown): string | number | null {
  return typeof value === "string" || typeof value === "number" ? value : null;
}

const SYNC_STATUSES: ReadonlySet<string> = new Set([
  "never_synced",
  "synced",
  "not_connected",
  "comments_disabled",
  "syncing",
  "sync_failed",
  "error",
]);

function toVideoWire(raw: unknown): CommentVideoWire {
  const r = isRecord(raw) ? raw : {};
  const rawStatus = strOrNull(r.sync_status ?? r.status);
  const status =
    rawStatus && SYNC_STATUSES.has(rawStatus)
      ? (rawStatus as CommentSyncStatusWire)
      : rawStatus
        ? // Unknown backend value: keep it visible via sync_status_raw
          // and fall back to the safest honest chip, never_synced only
          // when nothing was reported at all.
          "never_synced"
        : "never_synced";
  return {
    id: strOrNull(r.id) ?? strOrNull(r.video_id ?? r.external_video_id) ?? "",
    video_id: strOrNull(r.video_id ?? r.external_video_id ?? r.id) ?? "",
    title: strOrNull(r.title ?? r.video_title),
    sync_status: status,
    sync_status_raw: rawStatus && !SYNC_STATUSES.has(rawStatus) ? rawStatus : null,
    comment_count: numOrNull(
      r.comment_count ?? r.comments_count ?? r.synced_comment_count ?? r.count,
    ),
    last_synced_at: timeOrNull(r.last_synced_at ?? r.synced_at ?? r.last_sync_at),
    error_reason: strOrNull(r.error_reason ?? r.error ?? r.reason),
    analysis_status: strOrNull(r.analysis_status),
  };
}

function readBand(record: Record<string, unknown>): SentimentCountsWire | null {
  const pos = numOrNull(record.positive);
  const neu = numOrNull(record.neutral);
  const neg = numOrNull(record.negative);
  const que = numOrNull(record.questions ?? record.question);
  if (pos === null && neu === null && neg === null && que === null) return null;
  return {
    positive: pos ?? 0,
    neutral: neu ?? 0,
    negative: neg ?? 0,
    questions: que ?? 0,
  };
}

function toShares(band: SentimentCountsWire): SentimentSharesWire | null {
  const total = band.positive + band.neutral + band.negative + band.questions;
  if (total <= 0) return null;
  return {
    positive: band.positive / total,
    neutral: band.neutral / total,
    negative: band.negative / total,
    questions: band.questions / total,
  };
}

/**
 * Sentiment arrives in one of three honest shapes: explicit shares
 * (fractions), explicit counts, or one generic `sentiment` object of
 * weights. Weights are normalised to shares — that is exact for both
 * counts and percents — but raw numbers are only kept as `counts`
 * when the backend labelled them as counts.
 */
function readSentiment(r: Record<string, unknown>): {
  shares: SentimentSharesWire | null;
  counts: SentimentCountsWire | null;
} {
  if (isRecord(r.sentiment_shares)) {
    const band = readBand(r.sentiment_shares);
    if (band) return { shares: toShares(band), counts: null };
  }
  if (isRecord(r.sentiment_counts)) {
    const band = readBand(r.sentiment_counts);
    if (band) return { shares: toShares(band), counts: band };
  }
  if (isRecord(r.sentiment)) {
    const band = readBand(r.sentiment);
    if (band) return { shares: toShares(band), counts: null };
  }
  return { shares: null, counts: null };
}

function toTopicList(raw: unknown): TopicCountWire[] {
  if (!Array.isArray(raw)) return [];
  const out: TopicCountWire[] = [];
  for (const item of raw) {
    if (typeof item === "string" && item.trim()) {
      out.push({ label: item.trim(), count: null, share: null });
    } else if (isRecord(item)) {
      const label = strOrNull(item.label ?? item.theme ?? item.topic ?? item.name ?? item.text);
      if (label) {
        out.push({
          label,
          // The engine's TopicItem carries an explicit count; its
          // ThemeCluster carries comment_ids instead — the length of
          // that list IS the count, so deriving it is factual.
          count:
            numOrNull(item.count ?? item.mentions ?? item.n) ??
            (Array.isArray(item.comment_ids) ? item.comment_ids.length : null),
          share: numOrNull(item.share),
        });
      }
    }
  }
  return out;
}

function toQuestionList(raw: unknown): TopQuestionWire[] {
  if (!Array.isArray(raw)) return [];
  const out: TopQuestionWire[] = [];
  for (const item of raw) {
    if (typeof item === "string" && item.trim()) {
      out.push({
        question: item.trim(),
        count: null,
        like_count: null,
        comment_id: null,
        video_id: null,
        video_title: null,
        author_name: null,
      });
    } else if (isRecord(item)) {
      // Engine shape (app.comment_intel): `question_text` is the
      // viewer's verbatim question sentence — never paraphrased.
      const question = strOrNull(
        item.question_text ?? item.question ?? item.text ?? item.prompt,
      );
      if (question) {
        out.push({
          question,
          count: numOrNull(item.count ?? item.mentions ?? item.n),
          like_count: numOrNull(item.like_count ?? item.likes),
          comment_id: strOrNull(item.comment_id ?? item.commentId),
          video_id: strOrNull(item.video_id ?? item.external_video_id),
          video_title: strOrNull(item.video_title ?? item.video),
          author_name: strOrNull(item.author_name ?? item.author),
        });
      }
    }
  }
  return out;
}

/** Parses a value that may be a record or a JSON string of one. */
function asRecord(value: unknown): Record<string, unknown> | null {
  if (isRecord(value)) return value;
  if (typeof value === "string" && value.trim()) {
    try {
      const parsed: unknown = JSON.parse(value);
      return isRecord(parsed) ? parsed : null;
    } catch {
      return null;
    }
  }
  return null;
}

/**
 * Provenance resolution. The backend's canonical shape is a nested
 * provenance block (analysis rows: parsed `provenance_json`; engine
 * rollups: `provenance`): `{ engine: "llm" | "deterministic",
 * model: <model id> | null, generated_at, … }`. Flat `engine` /
 * `model_id` columns (comment_analyses) and plain strings are also
 * accepted. Returns the model id for LLM runs, the exact marker
 * "deterministic" for deterministic runs, "llm" when an LLM ran but
 * no model id was recorded, and null when nothing was reported.
 */
function readEngine(r: Record<string, unknown>): string | null {
  const prov = asRecord(r.provenance) ?? asRecord(r.provenance_json);
  if (prov) {
    const engine = strOrNull(prov.engine ?? prov.type ?? prov.kind);
    if (engine === "deterministic") return "deterministic";
    if (engine === "llm") return strOrNull(prov.model ?? prov.model_id) ?? "llm";
    const model = strOrNull(prov.model ?? prov.model_id);
    if (model) return model;
  }
  const raw = r.engine ?? r.model ?? r.model_id ?? r.analysis_engine ?? r.analysed_by ?? r.analyzer;
  if (typeof raw === "string" && raw.length > 0) {
    if (raw === "llm") return strOrNull(r.model_id ?? r.model) ?? "llm";
    return raw;
  }
  if (isRecord(raw)) {
    const model = strOrNull(raw.model ?? raw.id ?? raw.name);
    if (model) return model;
    if (raw.type === "deterministic" || raw.kind === "deterministic") {
      return "deterministic";
    }
  }
  return null;
}

function toRollupWire(raw: unknown): CommentRollupWire {
  const outer = isRecord(raw) ? raw : {};
  // Analysis rows nest the engine rollup under `rollup` (parsed
  // `rollup_json`); the row's own columns (scope, engine, model_id,
  // comment_count, provenance) overlay it when present.
  const nested = asRecord(outer.rollup) ?? asRecord(outer.rollup_json);
  const r: Record<string, unknown> = { ...(nested ?? {}), ...outer };
  const videoId = strOrNull(r.video_id ?? r.external_video_id);
  const scopeRaw = strOrNull(r.scope);
  const { shares, counts } = readSentiment(r);
  const prov = asRecord(r.provenance) ?? asRecord(r.provenance_json);
  return {
    scope: scopeRaw === "video" || (scopeRaw === null && videoId !== null) ? "video" : "channel",
    video_id: videoId,
    video_title: strOrNull(r.video_title ?? r.title),
    comment_count: numOrNull(r.comment_count ?? r.comments_count ?? r.total_comments ?? r.count),
    sentiment: shares,
    sentiment_counts: counts,
    themes: toTopicList(r.themes ?? r.theme_chips ?? r.top_themes),
    praise_topics: toTopicList(r.praise_topics ?? r.praise ?? r.top_praise),
    complaint_topics: toTopicList(r.complaint_topics ?? r.complaints ?? r.top_complaints),
    top_questions: toQuestionList(r.top_questions ?? r.questions),
    engine: readEngine(r),
    analysed_at: timeOrNull(
      r.analysed_at ??
        r.analyzed_at ??
        (prov ? prov.generated_at : undefined) ??
        r.created_at ??
        r.updated_at,
    ),
  };
}

/** Rollups payload: array, `{ channel, videos }`, or `{ items }` envelope. */
function toRollupsWire(raw: unknown): CommentRollupsWire {
  if (Array.isArray(raw) || (isRecord(raw) && Array.isArray(raw.items))) {
    const all = listItems(raw).map(toRollupWire);
    return {
      channel: all.find((x) => x.scope === "channel") ?? null,
      videos: all.filter((x) => x.scope === "video"),
    };
  }
  if (isRecord(raw)) {
    const channelRaw = raw.channel_rollup ?? raw.channel;
    const videosRaw = raw.video_rollups ?? raw.videos ?? raw.rollups;
    const videos = Array.isArray(videosRaw) ? videosRaw.map(toRollupWire) : [];
    return {
      channel: isRecord(channelRaw) ? toRollupWire(channelRaw) : null,
      videos,
    };
  }
  return { channel: null, videos: [] };
}

const DRAFT_STATUSES: ReadonlySet<string> = new Set([
  "proposed",
  "approved",
  "rejected",
  "discarded",
]);

function toDraftWire(raw: unknown): CommentDraftWire {
  const r = isRecord(raw) ? raw : {};
  const parent = isRecord(r.parent_comment)
    ? r.parent_comment
    : isRecord(r.comment)
      ? r.comment
      : null;
  const rawStatus = strOrNull(r.status);
  return {
    id: strOrNull(r.id) ?? "",
    comment_id: strOrNull(r.comment_id ?? r.parent_comment_id) ?? (parent ? strOrNull(parent.id) : null),
    video_id: strOrNull(r.video_id ?? r.external_video_id),
    video_title: strOrNull(r.video_title ?? r.title),
    parent_author:
      strOrNull(
        r.parent_author ?? r.comment_author ?? r.author_name ?? r.author_display_name,
      ) ??
      (parent
        ? strOrNull(parent.author_name ?? parent.author_display_name ?? parent.author)
        : null),
    parent_text:
      strOrNull(r.parent_text ?? r.comment_text) ??
      (parent ? strOrNull(parent.text ?? parent.body) : null) ??
      // Joined-row shape: when a row carries BOTH the draft's own
      // `draft_text` and a bare `text`, the bare text is the parent
      // comment's (the comments table's column name).
      (strOrNull(r.draft_text) && strOrNull(r.text) !== strOrNull(r.draft_text)
        ? strOrNull(r.text)
        : null),
    parent_like_count:
      numOrNull(r.parent_like_count) ??
      (parent ? numOrNull(parent.like_count ?? parent.likes) : null),
    draft_text: strOrNull(r.draft_text ?? r.text ?? r.body ?? r.reply_text) ?? "",
    status: (rawStatus && DRAFT_STATUSES.has(rawStatus)
      ? rawStatus
      : "proposed") as CommentDraftStatusWire,
    status_raw: rawStatus && !DRAFT_STATUSES.has(rawStatus) ? rawStatus : null,
    engine: readEngine(r),
    posted: r.posted === true,
    posting: strOrNull(r.posting),
    created_at: timeOrNull(r.created_at ?? r.updated_at),
  };
}

function toFlagWire(raw: unknown): ModerationFlagWire {
  const r = isRecord(raw) ? raw : {};
  const comment = isRecord(r.comment) ? r.comment : null;
  return {
    id: strOrNull(r.id) ?? "",
    comment_id: strOrNull(r.comment_id) ?? (comment ? strOrNull(comment.id) : null),
    video_id: strOrNull(r.video_id ?? r.external_video_id),
    video_title: strOrNull(r.video_title),
    author_name:
      strOrNull(r.author_name ?? r.comment_author ?? r.author_display_name) ??
      (comment
        ? strOrNull(comment.author_name ?? comment.author_display_name ?? comment.author)
        : null),
    comment_text:
      strOrNull(r.comment_text ?? r.text) ??
      (comment ? strOrNull(comment.text ?? comment.body) : null),
    flag_kind: strOrNull(r.flag_kind ?? r.kind ?? r.category) ?? "flagged",
    // The queue view's display field is `suggested_action_label`
    // (a label for the human reviewer — never executed).
    suggested_action: strOrNull(
      r.suggested_action_label ?? r.suggested_action ?? r.suggestion ?? r.action,
    ),
    reason: strOrNull(r.reason ?? r.detail),
    status: strOrNull(r.status ?? r.decision) ?? "pending",
    created_at: timeOrNull(r.created_at ?? r.updated_at),
  };
}

function toSyncResult(raw: unknown): CommentSyncResultWire {
  const r = isRecord(raw) ? raw : {};
  return {
    status: strOrNull(r.status ?? r.sync_status),
    synced_count: numOrNull(
      r.synced_count ?? r.comment_count ?? r.comments_count ?? r.new_comments ?? r.count,
    ),
  };
}

// ── Query keys ─────────────────────────────────────────────────────────────

const videosKey = (channelId: string | null) =>
  ["comments", channelId, "videos"] as const;
const rollupsKey = (channelId: string | null) =>
  ["comments", channelId, "rollups"] as const;
const draftsKey = (channelId: string | null) =>
  ["comments", channelId, "drafts"] as const;
const moderationKey = (channelId: string | null) =>
  ["comments", channelId, "moderation"] as const;

const channelPath = (channelId: string | null) =>
  `/channels/${encodeURIComponent(channelId ?? "")}/comments`;

// ── Per-video sync status ──────────────────────────────────────────────────

export function useCommentVideos(channelId: string | null) {
  return useQuery({
    queryKey: videosKey(channelId),
    enabled: channelId !== null,
    queryFn: async (): Promise<CommentVideoWire[]> => {
      const raw = await request<unknown>("GET", `${channelPath(channelId)}/videos`);
      return listItems(raw).map(toVideoWire);
    },
    retry: 1,
  });
}

/**
 * POST …/comments/sync — fetches the channel's comments from YouTube
 * (all videos, or one when `videoId` is given). The result only says
 * what the backend reports; lists are refetched on success.
 */
export function useSyncComments(channelId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (videoId?: string): Promise<CommentSyncResultWire> => {
      const raw = await request<unknown>("POST", `${channelPath(channelId)}/sync`, {
        ...(videoId ? { video_id: videoId } : {}),
      });
      return toSyncResult(raw);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: videosKey(channelId) });
      void queryClient.invalidateQueries({ queryKey: rollupsKey(channelId) });
    },
  });
}

// ── Rollups ────────────────────────────────────────────────────────────────

export function useCommentRollups(channelId: string | null) {
  return useQuery({
    queryKey: rollupsKey(channelId),
    enabled: channelId !== null,
    queryFn: async (): Promise<CommentRollupsWire> => {
      const raw = await request<unknown>("GET", `${channelPath(channelId)}/rollups`);
      return toRollupsWire(raw);
    },
    retry: 1,
  });
}

/** POST …/comments/analysis — (re)runs comment analysis for the channel. */
export function useRunCommentAnalysis(channelId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (videoId?: string): Promise<void> => {
      await request<unknown>("POST", `${channelPath(channelId)}/analysis`, {
        ...(videoId ? { video_id: videoId } : {}),
      });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: rollupsKey(channelId) });
      void queryClient.invalidateQueries({ queryKey: videosKey(channelId) });
      void queryClient.invalidateQueries({ queryKey: draftsKey(channelId) });
      void queryClient.invalidateQueries({ queryKey: moderationKey(channelId) });
    },
  });
}

// ── Draft replies ──────────────────────────────────────────────────────────

export function useCommentDrafts(channelId: string | null) {
  return useQuery({
    queryKey: draftsKey(channelId),
    enabled: channelId !== null,
    queryFn: async (): Promise<CommentDraftWire[]> => {
      const raw = await request<unknown>(
        "GET",
        `/channels/${encodeURIComponent(channelId ?? "")}/comment-drafts`,
      );
      return listItems(raw).map(toDraftWire);
    },
    retry: 1,
  });
}

/**
 * POST /channels/{channelId}/comments/{commentId}/drafts (201) —
 * generate ONE draft reply for a stored comment. The backend has no
 * bulk-generate endpoint; generation is offered per comment (the UI
 * surfaces it on Top Questions, whose items carry comment ids). The
 * created draft is `proposed` and posts nothing.
 */
export function useGenerateCommentDraft(channelId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (commentId: string): Promise<CommentDraftWire> => {
      const raw = await request<unknown>(
        "POST",
        `${channelPath(channelId)}/${encodeURIComponent(commentId)}/drafts`,
        {},
      );
      return toDraftWire(raw);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: draftsKey(channelId) });
    },
  });
}

/**
 * POST …/comment-drafts/{id}/approve — records the approval LOCALLY.
 * Nothing is posted to YouTube; the response itself carries
 * `posted: false` / `posting: "not_wired"`.
 */
export function useApproveCommentDraft(channelId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (draftId: string): Promise<CommentDraftWire> => {
      const raw = await request<unknown>(
        "POST",
        `/channels/${encodeURIComponent(channelId ?? "")}/comment-drafts/${encodeURIComponent(draftId)}/approve`,
        {},
      );
      return toDraftWire(raw);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: draftsKey(channelId) });
    },
  });
}

/**
 * POST …/comment-drafts/{id}/discard — records the discard locally.
 * `discarded` is a terminal state DISTINCT from `rejected`: the
 * reviewer throws the draft away without deciding against it.
 * Nothing is posted to YouTube.
 */
export function useDiscardCommentDraft(channelId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (draftId: string): Promise<CommentDraftWire> => {
      const raw = await request<unknown>(
        "POST",
        `/channels/${encodeURIComponent(channelId ?? "")}/comment-drafts/${encodeURIComponent(draftId)}/discard`,
        {},
      );
      return toDraftWire(raw);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: draftsKey(channelId) });
    },
  });
}

/**
 * PATCH …/comment-drafts/{id} — edit a PROPOSED draft's text locally.
 * The backend records the edit in the draft's provenance; editing a
 * terminal draft fails. Nothing is posted to YouTube.
 */
export function useEditCommentDraft(channelId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (args: {
      draftId: string;
      draftText: string;
    }): Promise<CommentDraftWire> => {
      const raw = await request<unknown>(
        "PATCH",
        `/channels/${encodeURIComponent(channelId ?? "")}/comment-drafts/${encodeURIComponent(args.draftId)}`,
        { draft_text: args.draftText },
      );
      return toDraftWire(raw);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: draftsKey(channelId) });
    },
  });
}

/** POST …/comment-drafts/{id}/reject — records the rejection locally. */
export function useRejectCommentDraft(channelId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (draftId: string): Promise<CommentDraftWire> => {
      const raw = await request<unknown>(
        "POST",
        `/channels/${encodeURIComponent(channelId ?? "")}/comment-drafts/${encodeURIComponent(draftId)}/reject`,
        {},
      );
      return toDraftWire(raw);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: draftsKey(channelId) });
    },
  });
}

// ── Moderation queue ───────────────────────────────────────────────────────

export function useModerationQueue(channelId: string | null) {
  return useQuery({
    queryKey: moderationKey(channelId),
    enabled: channelId !== null,
    queryFn: async (): Promise<ModerationFlagWire[]> => {
      // No `status` param: the router defaults to the pending queue.
      const raw = await request<unknown>(
        "GET",
        `/channels/${encodeURIComponent(channelId ?? "")}/moderation-queue`,
      );
      return listItems(raw).map(toFlagWire);
    },
    retry: 1,
  });
}

/**
 * POST …/moderation-queue/{id}/decision — a LOCAL decision only
 * (`dismiss` | `mark_reviewed`); the comment itself is not hidden,
 * deleted or reported on YouTube from here (the flag view's
 * `action_executed` is always false).
 */
export function useModerationDecision(channelId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (args: {
      flagId: string;
      decision: ModerationDecision;
    }): Promise<ModerationFlagWire> => {
      const raw = await request<unknown>(
        "POST",
        `/channels/${encodeURIComponent(channelId ?? "")}/moderation-queue/${encodeURIComponent(args.flagId)}/decision`,
        { decision: args.decision },
      );
      return toFlagWire(raw);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: moderationKey(channelId) });
    },
  });
}
