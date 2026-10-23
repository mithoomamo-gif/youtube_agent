/**
 * Shared types for the local-backend API layer.
 *
 * Protocol: MASTER_ROADMAP Part 4 / roadmap section 31.
 * - REST is the source of truth; the WebSocket is a notification channel.
 * - Domain objects use camelCase. WebSocket envelopes and event payloads
 *   keep the wire (snake_case) field names exactly as the backend emits
 *   them, so no translation is needed on the hot path.
 */

// ── Connection ──────────────────────────────────────────────────────────────

/** Connection info handed to the WebView by the Tauri shell over IPC. */
export interface BackendConnection {
  /** REST base, e.g. `http://127.0.0.1:{port}/api/v1`. */
  baseUrl: string;
  /** WebSocket URL, e.g. `ws://127.0.0.1:{port}/api/v1/ws`. */
  wsUrl: string;
  /** Per-launch token. Never persisted, never placed in a URL. */
  token: string;
}

// ── Errors ──────────────────────────────────────────────────────────────────

/** The one REST error envelope: `{ error: { code, message, … } }`. */
export interface ErrorEnvelope {
  error: {
    /** Stable machine-readable code. Never parse `message` instead. */
    code: string;
    message: string;
    details?: unknown;
    request_id?: string;
    retryable?: boolean;
  };
}

export interface ApiErrorInit {
  code: string;
  message: string;
  /** HTTP status, or 0 for a network-level failure. */
  status: number;
  retryable?: boolean;
  requestId?: string;
  details?: unknown;
}

/** Error thrown by {@link ApiClient} for every non-2xx / failed request. */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly retryable: boolean;
  readonly requestId?: string;
  readonly details?: unknown;

  constructor(init: ApiErrorInit) {
    super(init.message);
    this.name = "ApiError";
    Object.setPrototypeOf(this, ApiError.prototype);
    this.code = init.code;
    this.status = init.status;
    this.retryable = init.retryable ?? false;
    this.requestId = init.requestId;
    this.details = init.details;
  }
}

// ── Pagination ──────────────────────────────────────────────────────────────

/** Cursor-paginated list response (wire: `{ items, next_cursor }`). */
export interface Page<T> {
  items: T[];
  nextCursor: string | null;
}

export interface ListParams {
  cursor?: string;
  limit?: number;
}

// ── System ──────────────────────────────────────────────────────────────────

export interface SystemInfo {
  appVersion: string;
  backendVersion: string;
  apiVersion: number;
  wsSchema: number;
  minUiApi?: number;
  bootId: string;
  capabilities: string[];
}

export interface HealthReady {
  status: string;
  ready?: boolean;
  checks?: Record<string, unknown>;
}

/** A long operation accepted by the backend (HTTP 202). */
export interface AcceptedResponse {
  jobId: string;
  status?: string;
}

// ── Domain ──────────────────────────────────────────────────────────────────

export interface Channel {
  id: string;
  youtubeChannelId: string;
  title: string;
  handle: string;
  nicheProfileId: string | null;
  /** e.g. "ok" | "token-expired" | "audit-pending" — rendered generically. */
  state: string;
  publishMode: string;
  thumbnailUrl?: string;
}

export interface NicheProfile {
  id: string;
  name: string;
  source: "builtin" | "other";
  /** The user's one-line idea; present on `source: "other"` profiles. */
  idea?: string;
  language: string;
  tone?: string;
  wpm: number;
  targetMinutes?: number;
}

export interface CreateNichePayload {
  name: string;
  /** Required for the "Other" path; the backend expands it to a profile. */
  idea?: string;
  language?: string;
  tone?: string;
  targetMinutes?: number;
}

export type ValidateNichePayload = CreateNichePayload;

export interface Project {
  id: string;
  channelId: string;
  nicheProfileId: string;
  title: string;
  status: string;
  phase: string;
  publishMode: string;
  targetMinutes: number;
  totalCostMicros: number;
}

export interface CreateProjectPayload {
  channelId: string;
  nicheProfileId: string;
  topicMode?: "auto" | "manual";
  topic?: string;
  targetMinutes?: number;
  publishMode?: string;
}

export interface ProjectFilters extends ListParams {
  channelId?: string;
  status?: string;
  phase?: string;
  q?: string;
}

/** `GET /projects/{id}/snapshot` — one project, renderable in one call. */
export interface ProjectSnapshot {
  project: Project;
  runs: AgentRun[];
  approvals?: ApprovalItem[];
  totalCostMicros?: number;
}

export interface AgentRun {
  id: string;
  projectId: string;
  agentId: string;
  status: string;
  stepLabel: string;
  costMicros: number;
}

export interface ApprovalItem {
  id: string;
  projectId: string;
  gate: "A" | "B";
  createdAt: string;
  status?: string;
}

export type ApprovalDecision = "approve" | "reject";

export interface DecideApprovalPayload {
  decision: ApprovalDecision;
  /** Required by the backend when rejecting. */
  reasonCode?: string;
  note?: string;
}

export interface Job {
  id: string;
  kind: string;
  status: string;
  projectId?: string;
}

export interface JobFilters extends ListParams {
  status?: string;
  projectId?: string;
  agent?: string;
  pool?: string;
}

export interface PoolStats {
  running: number;
  queued: number;
  limit?: number;
}

export interface QueueStats {
  pools: Record<string, PoolStats>;
  running?: number;
  queued?: number;
  governorThrottled?: boolean;
}

export interface Schedule {
  id: string;
  channelId: string;
  nicheProfileId: string;
  /** Cron-like rule owned by the backend. */
  rule: string;
  topicMode?: string;
  targetMinutes?: number;
  publishMode?: string;
  dailyBudgetCapUsd?: number;
  enabled: boolean;
  nextRunAt?: string;
}

export interface CreateSchedulePayload {
  channelId: string;
  nicheProfileId: string;
  rule: string;
  topicMode?: string;
  targetMinutes?: number;
  publishMode?: string;
  dailyBudgetCapUsd?: number;
  enabled?: boolean;
}

export interface Provider {
  id: string;
  name?: string;
  configured: boolean;
  /** Key material is never returned — only whether one is stored. */
  hasKey: boolean;
  status?: string;
  defaultModel?: string;
}

/** Round 3 provider-test status vocabulary (backend contract). */
export type ProviderTestStatus = "ok" | "not_configured" | "error";

export interface ProviderTestResult {
  ok: boolean;
  latencyMs?: number;
  message?: string;
  /**
   * Round 3: explicit test outcome. `ok` = genuinely verified against
   * the provider, `not_configured` = no key saved, `error` = the test
   * ran and failed (see `category`). Absent on the pre-Round-3 backend,
   * whose test endpoint was a stub returning `ok: true` — callers must
   * fall back to `ok`/`message` and must not call that a verification.
   */
  status?: ProviderTestStatus;
  /** Failure category for `status: "error"` (e.g. "auth", "network"). */
  category?: string;
  detail?: string;
}

/** One YouTube quota bucket from `GET /usage/quota` (Round 3). */
export interface QuotaBucketUsage {
  used: number;
  limit: number;
}

export interface QuotaBuckets {
  uploads: QuotaBucketUsage;
  search: QuotaBucketUsage;
  shared: QuotaBucketUsage;
}

/**
 * `GET /usage/quota` (Round 3) — also embedded as `quota` in
 * `GET /queue/snapshot`. Quota resets at Pacific midnight.
 */
export interface QuotaUsage {
  buckets: QuotaBuckets;
  /** Current date in the Pacific timezone (YYYY-MM-DD). */
  pacificDate?: string;
  /** ISO-8601 instant of the next Pacific-midnight reset. */
  resetsAt?: string;
  /** Seconds until that reset, as reported by the backend. */
  secondsUntilReset?: number;
}

/** `GET|PUT /settings/budget` (Round 3) — budget caps + guardrail thresholds. */
export interface BudgetSettings {
  perVideoCapUsd: number;
  dailyCapUsd: number;
  monthlyCapUsd: number;
  /** Percent of a cap at which the backend warns. */
  warnPct: number;
  /** Percent of a cap at which the backend applies the soft stop. */
  softPct: number;
}

export interface UsageSummary {
  provider: string;
  usedMicros?: number;
  units: number;
  cap?: number;
  resetAt?: string;
}

export interface UsageQuery {
  range?: string;
  from?: string;
  to?: string;
  provider?: string;
  projectId?: string;
  cursor?: string;
  limit?: number;
}

export interface Publication {
  id: string;
  projectId?: string;
  channelId?: string;
  videoId?: string;
  url?: string;
  title?: string;
  privacyStatus?: string;
  processingStatus?: string;
  publishedAt?: string;
}

// ── Style Lab: competitors & style profiles ─────────────────────────────────
//
// Wire contract (Style Lab mini-round — backend routes are nested under
// the OWNING channel, so every competitor / video / profile belongs to
// exactly one of the user's channels):
//
// - GET    /channels/{channelId}/competitors
// - POST   /channels/{channelId}/competitors            { input, display_name? }
// - POST   /channels/{channelId}/competitors/{competitorId}/archive
// - POST   /channels/{channelId}/competitors/{competitorId}/metadata/fetch
// - GET    /channels/{channelId}/competitors/{competitorId}/videos
// - POST   /channels/{channelId}/competitors/{competitorId}/videos
//          JSON { source, video_url?, title?, description?, tags? }
// - POST   /channels/{channelId}/competitors/{competitorId}/videos/attach
//          multipart form: `file`, optional `thumbnail`
// - POST   /channels/{channelId}/competitor-videos/{videoId}/analysis
// - GET    /channels/{channelId}/style-profiles
// - POST   /channels/{channelId}/style-profiles          { name, competitor_ids }
// - POST   /channels/{channelId}/style-profiles/{profileId}/activate
//
// The wire is snake_case (see the contract field lists); the domain
// types below are camelCase per this file's convention. The ApiClient
// normalises wire -> domain, and the React Query layer
// (`src/api/queries/styleLab.ts`) keeps its own snake_case wire types,
// mirroring how `queries/channels.ts` handles channel rows.

/** Honest metadata state: `metadata_unavailable` means the backend
 *  cannot fetch channel info (not configured) — never fake data. */
export type CompetitorMetadataStatus =
  | "not_fetched"
  | "fetched"
  | "metadata_unavailable"
  | "fetch_failed";

export type CompetitorVideoSource = "attached_file" | "url_metadata" | "pasted_metadata";

export type CompetitorVideoAnalysisStatus =
  | "not_queued"
  | "queued"
  | "analysing"
  | "analysed"
  | "analysis_failed";

export type StyleProfileStatus = "draft" | "active" | "superseded";

export interface CompetitorChannel {
  id: string;
  /** The user's OWN channel this competitor is scoped to. */
  channelId: string;
  platform: string;
  /** Exactly what the user typed (channel link, @handle or channel ID). */
  sourceInput: string;
  /** How the backend classified `sourceInput` (e.g. "url" | "handle" | "channel_id"). */
  sourceKind: string;
  externalChannelId: string | null;
  handle: string | null;
  displayName: string | null;
  channelUrl: string | null;
  metadataStatus: CompetitorMetadataStatus;
  /** Lifecycle status (e.g. "active" | "archived") — rendered generically. */
  status: string;
  createdAt: string;
  updatedAt: string;
}

export interface CreateCompetitorPayload {
  /** A channel link, an @handle, or a channel ID. */
  input: string;
  displayName?: string;
}

export interface CompetitorVideo {
  id: string;
  competitorChannelId: string;
  source: CompetitorVideoSource;
  externalVideoId: string | null;
  videoUrl: string | null;
  title: string | null;
  description: string | null;
  tags: string[] | null;
  durationS: number | null;
  width: number | null;
  height: number | null;
  fileSizeBytes: number | null;
  hasThumbnail: boolean;
  analysisStatus: CompetitorVideoAnalysisStatus;
  createdAt: string;
}

/** JSON body for `POST …/competitors/{id}/videos` (paste / URL details). */
export interface CreateCompetitorVideoPayload {
  source: CompetitorVideoSource;
  videoUrl?: string;
  title?: string;
  description?: string;
  tags?: string[];
}

export interface StyleProfile {
  id: string;
  /** The user's OWN channel this profile is scoped to. */
  channelId: string;
  name: string;
  version: number;
  status: StyleProfileStatus;
  competitorIds: string[];
  createdAt: string;
}

export interface CreateStyleProfilePayload {
  name: string;
  competitorIds: string[];
}

// ── Comments Intelligence (Round 5, Agent 28) ───────────────────────────────
//
// Wire contract — every route is nested under the OWNING channel:
//
// - GET    /channels/{channelId}/comments/videos
// - POST   /channels/{channelId}/comments/sync            { video_id? }
// - POST   /channels/{channelId}/comments/analysis        { video_id? }
// - GET    /channels/{channelId}/comments/rollups
// - POST   /channels/{channelId}/comments/{commentId}/drafts   (201, one draft)
// - GET    /channels/{channelId}/comment-drafts
// - POST   /channels/{channelId}/comment-drafts/{draftId}/approve
// - POST   /channels/{channelId}/comment-drafts/{draftId}/reject
// - GET    /channels/{channelId}/moderation-queue        (pending by default)
// - POST   /channels/{channelId}/moderation-queue/{flagId}/decision
//          { decision: "dismiss" | "mark_reviewed" } (local decision only)
//
// The wire is snake_case; the domain types below are camelCase per this
// file's convention. The React Query layer (`src/api/queries/comments.ts`)
// keeps its own snake_case wire types, mirroring the Style Lab split.
//
// HONESTY: there is deliberately NO post/publish operation here.
// Approving a draft records a local decision only — nothing is posted
// to YouTube. Rollups carry an `engine` provenance string (an LLM
// model id or a deterministic-engine marker); when the backend omits
// it, `engine` is null and the UI says the engine was not reported —
// it never invents a label.

/** Per-video comment sync state, exactly as the backend reports it. */
export type CommentSyncStatus =
  | "never_synced"
  | "synced"
  | "not_connected"
  | "comments_disabled"
  | "syncing"
  | "sync_failed"
  | "error";

export interface CommentVideoSync {
  id: string;
  videoId: string;
  title: string | null;
  syncStatus: CommentSyncStatus;
  commentCount: number | null;
  lastSyncedAt: string | null;
  analysisStatus: string | null;
}

/** Sentiment band shares as fractions in [0, 1] (they sum to ~1). */
export interface CommentSentimentShares {
  positive: number;
  neutral: number;
  negative: number;
  questions: number;
}

export interface CommentSentimentCounts {
  positive: number;
  neutral: number;
  negative: number;
  questions: number;
}

export interface CommentTopicCount {
  label: string;
  count: number | null;
}

export interface CommentTopQuestion {
  question: string;
  count: number | null;
  /** Source comment id — enables per-comment draft generation. */
  commentId: string | null;
  videoTitle: string | null;
  authorName: string | null;
}

export interface CommentRollup {
  scope: "channel" | "video";
  videoId: string | null;
  videoTitle: string | null;
  commentCount: number | null;
  sentiment: CommentSentimentShares | null;
  sentimentCounts: CommentSentimentCounts | null;
  themes: CommentTopicCount[];
  praiseTopics: CommentTopicCount[];
  complaintTopics: CommentTopicCount[];
  topQuestions: CommentTopQuestion[];
  /** LLM model id or deterministic-engine marker; null = not reported. */
  engine: string | null;
  analysedAt: string | null;
}

export type CommentDraftStatus = "proposed" | "approved" | "rejected";

export interface CommentDraft {
  id: string;
  commentId: string | null;
  videoId: string | null;
  videoTitle: string | null;
  parentAuthor: string | null;
  parentText: string | null;
  parentLikeCount: number | null;
  draftText: string;
  status: CommentDraftStatus;
  engine: string | null;
  /** Backend posting-boundary stamp: always false this round. */
  posted: boolean;
  /** e.g. "not_wired" — posting is not implemented. */
  posting: string | null;
  createdAt: string | null;
}

export interface CommentModerationFlag {
  id: string;
  commentId: string | null;
  videoId: string | null;
  videoTitle: string | null;
  authorName: string | null;
  commentText: string | null;
  flagKind: string;
  suggestedAction: string | null;
  reason: string | null;
  status: string;
  createdAt: string | null;
}

/** Local moderation decisions — never an action taken on YouTube. */
export type CommentModerationDecision = "dismiss" | "mark_reviewed";

// ── WebSocket ───────────────────────────────────────────────────────────────

/**
 * Envelope carried by every WebSocket frame, both directions.
 * Clients MUST ignore unknown `type` values (additive evolution rule).
 */
export interface WsEvent {
  v: number;
  /** Dotted namespace, e.g. `"agent.status_changed"`. */
  type: string;
  event_id: string;
  /** Strictly monotonic per `boot_id`; `(boot_id, seq)` is the cursor. */
  seq: number;
  ts: string;
  boot_id: string;
  project_id?: string;
  job_id?: string;
  run_id?: string;
  agent_id?: string;
  payload: unknown;
}

export interface SystemHelloAckPayload {
  ws_schema?: number;
  backend_version?: string;
  boot_id: string;
  last_seq?: number;
  replay_from?: number;
}

export interface AgentStatusChangedPayload {
  agent_id: string;
  run_id: string;
  status: "queued" | "running" | "done" | "failed" | "blocked" | "cancelled" | (string & {});
  /** Plain-language step, e.g. "Writing chapter 3 of 7". */
  step_label: string;
  started_at?: string;
  duration_ms?: number;
  error_code?: string;
}

export interface GatePendingPayload {
  project_id: string;
  gate: "A" | "B";
  approval_id: string;
}

export interface QuotaBucket {
  used: number;
  limit: number;
}

export interface QuotaUpdatedPayload {
  uploads: QuotaBucket;
  search: QuotaBucket;
  shared_units: QuotaBucket;
  /** Pacific-midnight reset time (ISO-8601). */
  resets_at: string;
}

export interface BudgetWarningPayload {
  scope: "project" | "channel" | "day" | (string & {});
  cap_usd: number;
  spent_usd: number;
  action: "warned" | "paused" | (string & {});
}

export interface SnapshotRequiredPayload {
  reason: "seq_gap" | "boot_changed" | "buffer_overflow" | (string & {});
}

export type AgentStatusChangedEvent = WsEvent & {
  type: "agent.status_changed";
  payload: AgentStatusChangedPayload;
};

export type GatePendingEvent = WsEvent & {
  type: "gate.pending";
  payload: GatePendingPayload;
};

export type QuotaUpdatedEvent = WsEvent & {
  type: "quota.updated";
  payload: QuotaUpdatedPayload;
};

export type BudgetWarningEvent = WsEvent & {
  type: "budget.warning";
  payload: BudgetWarningPayload;
};

export type SnapshotRequiredEvent = WsEvent & {
  type: "system.snapshot_required";
  payload: SnapshotRequiredPayload;
};
