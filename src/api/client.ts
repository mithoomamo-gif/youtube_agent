/**
 * REST client for the local Python sidecar (protocol section 31).
 *
 * - Base URL is `http://127.0.0.1:{port}/api/v1` (from the Tauri shell).
 * - Every `/api/v1/*` request carries `X-Local-Token`. No cookies, ever.
 * - Errors arrive in one envelope and are thrown as {@link ApiError};
 *   callers branch on the stable `code`, never on `message`.
 * - Work-creating POSTs always send an `Idempotency-Key`.
 * - Long operations return 202 + job_id; progress arrives over the
 *   WebSocket and results are fetched back through REST.
 */

import { getBackendConnection, sameConnection } from "./tauri";
import { ApiError } from "./types";
import type {
  AcceptedResponse,
  ApprovalItem,
  BackendConnection,
  BudgetSettings,
  Channel,
  CompetitorChannel,
  CompetitorMetadataStatus,
  CompetitorVideo,
  CompetitorVideoAnalysisStatus,
  CompetitorVideoSource,
  CreateCompetitorPayload,
  CreateCompetitorVideoPayload,
  CreateNichePayload,
  CreateProjectPayload,
  CreateSchedulePayload,
  CreateStyleProfilePayload,
  DecideApprovalPayload,
  ErrorEnvelope,
  HealthReady,
  Job,
  JobFilters,
  ListParams,
  NicheProfile,
  Page,
  Project,
  ProjectFilters,
  ProjectSnapshot,
  Provider,
  ProviderTestResult,
  ProviderTestStatus,
  Publication,
  QueueStats,
  QuotaBucketUsage,
  QuotaUsage,
  Schedule,
  StyleProfile,
  StyleProfileStatus,
  SystemInfo,
  UsageQuery,
  UsageSummary,
  ValidateNichePayload,
} from "./types";

export interface RequestOptions {
  body?: unknown;
  /** Overrides the auto-generated idempotency key (POST only). */
  idempotencyKey?: string;
  /** Sent as the `If-Match` header on PUT/PATCH edits. */
  ifMatch?: string;
  signal?: AbortSignal;
  query?: Record<string, string | number | boolean | null | undefined>;
}

function randomId(): string {
  const cryptoObj = globalThis.crypto;
  if (cryptoObj && typeof cryptoObj.randomUUID === "function") {
    return cryptoObj.randomUUID();
  }
  return `id-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** Normalises a list response (page envelope or bare array) to {@link Page}. */
function toPage<T>(raw: unknown): Page<T> {
  if (Array.isArray(raw)) {
    return { items: raw as T[], nextCursor: null };
  }
  if (isRecord(raw) && Array.isArray(raw.items)) {
    const next = raw.next_cursor ?? raw.nextCursor;
    return {
      items: raw.items as T[],
      nextCursor: typeof next === "string" ? next : null,
    };
  }
  return { items: [], nextCursor: null };
}

/**
 * Result of a provider-key write/delete: metadata only — the backend
 * never returns key material, at most whether a key is configured and
 * its last-4 hint. Defined locally (wire is snake_case; see
 * backend/app/routers/providers.py).
 */
export interface ProviderKeyResult {
  provider: string;
  configured: boolean;
  keyLast4: string | null;
}

/** Normalises a provider-key response (`{ provider, configured, key_last4 }`). */
function toProviderKeyResult(raw: unknown): ProviderKeyResult {
  const r = isRecord(raw) ? raw : {};
  const last4 = r.key_last4 ?? r.keyLast4;
  return {
    provider: typeof r.provider === "string" ? r.provider : "",
    configured: r.configured === true,
    keyLast4: typeof last4 === "string" ? last4 : null,
  };
}

function toQuotaBucket(raw: unknown): QuotaBucketUsage {
  const r = isRecord(raw) ? raw : {};
  return {
    used: typeof r.used === "number" ? r.used : 0,
    limit: typeof r.limit === "number" ? r.limit : 0,
  };
}

/**
 * Normalises a quota payload (`GET /usage/quota`, or the `quota` object
 * embedded in `GET /queue/snapshot`) to {@link QuotaUsage}. Wire field
 * names are already the contract names; camelCase spellings are
 * tolerated defensively.
 */
export function toQuotaUsage(raw: unknown): QuotaUsage {
  const r = isRecord(raw) ? raw : {};
  const buckets = isRecord(r.buckets) ? r.buckets : r;
  const resetsAt = r.resetsAt ?? r.resets_at;
  const pacificDate = r.pacificDate ?? r.pacific_date;
  const seconds = r.secondsUntilReset ?? r.seconds_until_reset;
  return {
    buckets: {
      uploads: toQuotaBucket(buckets.uploads),
      search: toQuotaBucket(buckets.search),
      shared: toQuotaBucket(buckets.shared ?? buckets.shared_units),
    },
    pacificDate: typeof pacificDate === "string" ? pacificDate : undefined,
    resetsAt: typeof resetsAt === "string" ? resetsAt : undefined,
    secondsUntilReset: typeof seconds === "number" ? seconds : undefined,
  };
}

/** Normalises a `GET /settings/budget` body to {@link BudgetSettings}. */
export function toBudgetSettings(raw: unknown): BudgetSettings {
  const r = isRecord(raw) ? raw : {};
  const num = (camel: string, snake: string): number => {
    const v = r[camel] ?? r[snake];
    return typeof v === "number" ? v : 0;
  };
  return {
    perVideoCapUsd: num("perVideoCapUsd", "per_video_cap_usd"),
    dailyCapUsd: num("dailyCapUsd", "daily_cap_usd"),
    monthlyCapUsd: num("monthlyCapUsd", "monthly_cap_usd"),
    warnPct: num("warnPct", "warn_pct"),
    softPct: num("softPct", "soft_pct"),
  };
}

// ── Style Lab wire normalisers (snake_case wire -> camelCase domain) ──────

function strOrNull(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function numOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function toTimestampString(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) {
    // Backend timestamps are epoch-ms numbers (DB convention); ISO
    // strings pass through unchanged above.
    return new Date(value).toISOString();
  }
  return "";
}

const METADATA_STATUSES: ReadonlySet<string> = new Set([
  "not_fetched",
  "fetched",
  "metadata_unavailable",
  "fetch_failed",
]);

const VIDEO_SOURCES: ReadonlySet<string> = new Set([
  "attached_file",
  "url_metadata",
  "pasted_metadata",
]);

const ANALYSIS_STATUSES: ReadonlySet<string> = new Set([
  "not_queued",
  "queued",
  "analysing",
  "analysed",
  "analysis_failed",
]);

const PROFILE_STATUSES: ReadonlySet<string> = new Set([
  "draft",
  "active",
  "superseded",
]);

/** Normalises one competitor row (contract snake_case) to {@link CompetitorChannel}. */
export function toCompetitorChannel(raw: unknown): CompetitorChannel {
  const r = isRecord(raw) ? raw : {};
  const metadataRaw = r.metadata_status ?? r.metadataStatus;
  return {
    id: typeof r.id === "string" ? r.id : "",
    channelId: strOrNull(r.channel_id ?? r.channelId) ?? "",
    platform: typeof r.platform === "string" ? r.platform : "",
    sourceInput: strOrNull(r.source_input ?? r.sourceInput) ?? "",
    sourceKind: strOrNull(r.source_kind ?? r.sourceKind) ?? "",
    externalChannelId: strOrNull(r.external_channel_id ?? r.externalChannelId),
    handle: strOrNull(r.handle),
    displayName: strOrNull(r.display_name ?? r.displayName),
    channelUrl: strOrNull(r.channel_url ?? r.channelUrl),
    metadataStatus: (typeof metadataRaw === "string" && METADATA_STATUSES.has(metadataRaw)
      ? metadataRaw
      : "not_fetched") as CompetitorMetadataStatus,
    status: typeof r.status === "string" ? r.status : "",
    createdAt: toTimestampString(r.created_at ?? r.createdAt),
    updatedAt: toTimestampString(r.updated_at ?? r.updatedAt),
  };
}

/** Normalises one competitor-video row to {@link CompetitorVideo}. */
export function toCompetitorVideo(raw: unknown): CompetitorVideo {
  const r = isRecord(raw) ? raw : {};
  const sourceRaw = r.source;
  const analysisRaw = r.analysis_status ?? r.analysisStatus;
  const tagsRaw = r.tags;
  const tags: string[] | null = Array.isArray(tagsRaw)
    ? tagsRaw.filter((t): t is string => typeof t === "string")
    : typeof tagsRaw === "string" && tagsRaw.trim()
      ? tagsRaw.split(",").map((t) => t.trim()).filter(Boolean)
      : null;
  return {
    id: typeof r.id === "string" ? r.id : "",
    competitorChannelId:
      strOrNull(r.competitor_channel_id ?? r.competitorChannelId) ?? "",
    source: (typeof sourceRaw === "string" && VIDEO_SOURCES.has(sourceRaw)
      ? sourceRaw
      : "pasted_metadata") as CompetitorVideoSource,
    externalVideoId: strOrNull(r.external_video_id ?? r.externalVideoId),
    videoUrl: strOrNull(r.video_url ?? r.videoUrl),
    title: strOrNull(r.title),
    description: strOrNull(r.description),
    tags,
    durationS: numOrNull(r.duration_s ?? r.durationS),
    width: numOrNull(r.width),
    height: numOrNull(r.height),
    fileSizeBytes: numOrNull(r.file_size_bytes ?? r.fileSizeBytes),
    hasThumbnail: r.has_thumbnail === true || r.hasThumbnail === true,
    analysisStatus: (typeof analysisRaw === "string" && ANALYSIS_STATUSES.has(analysisRaw)
      ? analysisRaw
      : "not_queued") as CompetitorVideoAnalysisStatus,
    createdAt: toTimestampString(r.created_at ?? r.createdAt),
  };
}

/** Normalises one style-profile row to {@link StyleProfile}. */
export function toStyleProfile(raw: unknown): StyleProfile {
  const r = isRecord(raw) ? raw : {};
  const statusRaw = r.status;
  const idsRaw = r.competitor_ids ?? r.competitorIds;
  return {
    id: typeof r.id === "string" ? r.id : "",
    channelId: strOrNull(r.channel_id ?? r.channelId) ?? "",
    name: typeof r.name === "string" ? r.name : "",
    version: typeof r.version === "number" ? r.version : 1,
    status: (typeof statusRaw === "string" && PROFILE_STATUSES.has(statusRaw)
      ? statusRaw
      : "draft") as StyleProfileStatus,
    competitorIds: Array.isArray(idsRaw)
      ? idsRaw.filter((v): v is string => typeof v === "string")
      : [],
    createdAt: toTimestampString(r.created_at ?? r.createdAt),
  };
}

/** Extracts the item list from a bare array or `{ items }` envelope. */
function rawItems(raw: unknown): unknown[] {
  if (Array.isArray(raw)) return raw;
  if (isRecord(raw) && Array.isArray(raw.items)) return raw.items;
  return [];
}

/** Normalises a provider-test body (Round 3 status vocabulary). */
export function toProviderTestResult(raw: unknown): ProviderTestResult {
  const r = isRecord(raw) ? raw : {};
  const statusRaw = r.status;
  const status: ProviderTestStatus | undefined =
    statusRaw === "ok" || statusRaw === "not_configured" || statusRaw === "error"
      ? statusRaw
      : undefined;
  const detail =
    typeof r.detail === "string"
      ? r.detail
      : typeof r.message === "string"
        ? r.message
        : undefined;
  return {
    // Round 3: `status` is authoritative when present. Pre-Round-3 the
    // backend stub returned ok:true without contacting the provider.
    ok: status ? status === "ok" : r.ok === true,
    status,
    category: typeof r.category === "string" ? r.category : undefined,
    message: detail,
    detail,
    latencyMs:
      typeof r.latency_ms === "number"
        ? r.latency_ms
        : typeof r.latencyMs === "number"
          ? r.latencyMs
          : undefined,
  };
}

/** Normalises a 202 body (`{ job_id }` / `{ jobId }` / `{ id }`). */
function toAccepted(raw: unknown): AcceptedResponse {
  if (isRecord(raw)) {
    const jobId = raw.jobId ?? raw.job_id ?? raw.id;
    return {
      jobId: typeof jobId === "string" ? jobId : "",
      status: typeof raw.status === "string" ? raw.status : undefined,
    };
  }
  return { jobId: "" };
}

function toSystemInfo(raw: unknown): SystemInfo {
  const r = isRecord(raw) ? raw : {};
  const str = (camel: string, snake: string): string => {
    const v = r[camel] ?? r[snake];
    return typeof v === "string" ? v : "";
  };
  const num = (camel: string, snake: string): number => {
    const v = r[camel] ?? r[snake];
    return typeof v === "number" ? v : 0;
  };
  const minUiApi = r.minUiApi ?? r.min_ui_api;
  return {
    appVersion: str("appVersion", "app_version"),
    backendVersion: str("backendVersion", "backend_version"),
    apiVersion: num("apiVersion", "api_version"),
    wsSchema: num("wsSchema", "ws_schema"),
    minUiApi: typeof minUiApi === "number" ? minUiApi : undefined,
    bootId: str("bootId", "boot_id"),
    capabilities: Array.isArray(r.capabilities)
      ? r.capabilities.filter((c): c is string => typeof c === "string")
      : [],
  };
}

async function readBody(res: Response): Promise<unknown> {
  if (typeof res.text === "function") {
    const text = await res.text();
    if (!text) return undefined;
    try {
      return JSON.parse(text) as unknown;
    } catch {
      return text;
    }
  }
  if (typeof res.json === "function") {
    try {
      return (await res.json()) as unknown;
    } catch {
      return undefined;
    }
  }
  return undefined;
}

function toApiError(status: number, body: unknown, headerRequestId?: string): ApiError {
  if (isRecord(body) && isRecord(body.error)) {
    const err = body.error as ErrorEnvelope["error"];
    return new ApiError({
      code: typeof err.code === "string" ? err.code : `HTTP_${status}`,
      message: typeof err.message === "string" ? err.message : `Request failed (${status})`,
      status,
      retryable:
        typeof err.retryable === "boolean" ? err.retryable : status === 429 || status >= 500,
      requestId: typeof err.request_id === "string" ? err.request_id : headerRequestId,
      details: err.details,
    });
  }
  return new ApiError({
    code: `HTTP_${status}`,
    message: `Request failed (${status})`,
    status,
    retryable: status === 429 || status >= 500,
    requestId: headerRequestId,
  });
}

export class ApiClient {
  private readonly conn: BackendConnection;
  private readonly baseUrl: string;

  constructor(conn: BackendConnection) {
    this.conn = conn;
    this.baseUrl = conn.baseUrl.replace(/\/+$/, "");
  }

  // ── Core request ────────────────────────────────────────────────────────

  private async request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    let url = path.startsWith("http://") || path.startsWith("https://")
      ? path
      : `${this.baseUrl}${path.startsWith("/") ? path : `/${path}`}`;

    if (opts.query) {
      const params = new URLSearchParams();
      for (const [key, value] of Object.entries(opts.query)) {
        if (value !== undefined && value !== null) params.set(key, String(value));
      }
      const qs = params.toString();
      if (qs) url += `${url.includes("?") ? "&" : "?"}${qs}`;
    }

    const headers: Record<string, string> = {
      "X-Local-Token": this.conn.token,
    };
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    if (method.toUpperCase() === "POST") {
      // Required on all work-creating POSTs; harmless elsewhere.
      headers["Idempotency-Key"] = opts.idempotencyKey ?? randomId();
    } else if (opts.idempotencyKey) {
      headers["Idempotency-Key"] = opts.idempotencyKey;
    }
    if (opts.ifMatch) headers["If-Match"] = opts.ifMatch;

    let res: Response;
    try {
      res = await fetch(url, {
        method,
        headers,
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        signal: opts.signal,
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

    const status = typeof res.status === "number" ? res.status : 200;
    const ok = typeof res.ok === "boolean" ? res.ok : status >= 200 && status < 300;
    if (status === 204) return undefined as T;

    const body = await readBody(res);
    if (!ok) {
      let headerRequestId: string | undefined;
      try {
        headerRequestId = res.headers?.get("X-Request-Id") ?? undefined;
      } catch {
        headerRequestId = undefined;
      }
      throw toApiError(status, body, headerRequestId);
    }
    return body as T;
  }

  private async requestPage<T>(path: string, query?: RequestOptions["query"]): Promise<Page<T>> {
    return toPage<T>(await this.request<unknown>("GET", path, { query }));
  }

  /**
   * Multipart variant of {@link request}: sends a `FormData` body and
   * deliberately sets NO `Content-Type` — the browser must generate the
   * `multipart/form-data; boundary=…` header itself. Auth and
   * idempotency headers match the JSON path; errors use the same
   * envelope and are thrown as {@link ApiError}.
   */
  private async requestMultipart<T>(path: string, form: FormData): Promise<T> {
    const url = `${this.baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
    const headers: Record<string, string> = {
      "X-Local-Token": this.conn.token,
      "Idempotency-Key": randomId(),
    };

    let res: Response;
    try {
      res = await fetch(url, { method: "POST", headers, body: form });
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") throw err;
      throw new ApiError({
        code: "NETWORK_ERROR",
        message: err instanceof Error ? err.message : "Network request failed",
        status: 0,
        retryable: true,
      });
    }

    const status = typeof res.status === "number" ? res.status : 200;
    const ok = typeof res.ok === "boolean" ? res.ok : status >= 200 && status < 300;
    if (status === 204) return undefined as T;

    const body = await readBody(res);
    if (!ok) {
      let headerRequestId: string | undefined;
      try {
        headerRequestId = res.headers?.get("X-Request-Id") ?? undefined;
      } catch {
        headerRequestId = undefined;
      }
      throw toApiError(status, body, headerRequestId);
    }
    return body as T;
  }

  /** Origin of the backend (`http://127.0.0.1:{port}`), for /health/*. */
  private origin(): string {
    try {
      return new URL(this.baseUrl).origin;
    } catch {
      return this.baseUrl.replace(/\/api\/v1$/, "");
    }
  }

  // ── System ──────────────────────────────────────────────────────────────

  /** First call the UI makes after connect: version/capability handshake. */
  async systemInfo(): Promise<SystemInfo> {
    return toSystemInfo(await this.request<unknown>("GET", "/system/info"));
  }

  /** Readiness probe — lives outside /api/v1, still token-authenticated. */
  async healthReady(): Promise<HealthReady> {
    return this.request<HealthReady>("GET", `${this.origin()}/health/ready`);
  }

  // ── Channels ────────────────────────────────────────────────────────────

  listChannels(params?: ListParams): Promise<Page<Channel>> {
    return this.requestPage<Channel>("/channels", { ...params });
  }

  /**
   * Starts the YouTube OAuth flow in the backend; completion via WS.
   * Round 3: when no YouTube client credentials are configured this
   * rejects with HTTP 409, code `youtube_client_not_configured`, and
   * `details.steps` — an ordered, plain-language Google Cloud setup
   * list the Channels screen renders as a setup panel.
   */
  async connectChannel(payload?: Record<string, unknown>): Promise<AcceptedResponse> {
    return toAccepted(
      await this.request<unknown>("POST", "/channels/connect", { body: payload ?? {} }),
    );
  }

  async disconnectChannel(id: string): Promise<void> {
    await this.request<void>("DELETE", `/channels/${encodeURIComponent(id)}`);
  }

  // ── Niches ──────────────────────────────────────────────────────────────

  listNiches(params?: ListParams): Promise<Page<NicheProfile>> {
    return this.requestPage<NicheProfile>("/niches", { ...params });
  }

  createNiche(
    payload: CreateNichePayload,
    opts?: { idempotencyKey?: string },
  ): Promise<NicheProfile> {
    return this.request<NicheProfile>("POST", "/niches", {
      body: payload,
      idempotencyKey: opts?.idempotencyKey,
    });
  }

  /** Dry-run profile expansion/validation without saving (wizard). */
  validateNiche(payload: ValidateNichePayload): Promise<NicheProfile> {
    return this.request<NicheProfile>("POST", "/niches/validate", { body: payload });
  }

  // ── Projects & pipeline ─────────────────────────────────────────────────

  listProjects(filters: ProjectFilters = {}): Promise<Page<Project>> {
    return this.requestPage<Project>("/projects", {
      channelId: filters.channelId,
      status: filters.status,
      phase: filters.phase,
      q: filters.q,
      cursor: filters.cursor,
      limit: filters.limit,
    });
  }

  createProject(
    payload: CreateProjectPayload,
    opts?: { idempotencyKey?: string },
  ): Promise<Project> {
    return this.request<Project>("POST", "/projects", {
      body: payload,
      idempotencyKey: opts?.idempotencyKey,
    });
  }

  getProject(id: string): Promise<Project> {
    return this.request<Project>("GET", `/projects/${encodeURIComponent(id)}`);
  }

  projectSnapshot(id: string): Promise<ProjectSnapshot> {
    return this.request<ProjectSnapshot>(
      "GET",
      `/projects/${encodeURIComponent(id)}/snapshot`,
    );
  }

  async startPipeline(id: string): Promise<AcceptedResponse> {
    return toAccepted(
      await this.request<unknown>("POST", `/projects/${encodeURIComponent(id)}/pipeline/start`),
    );
  }

  async pausePipeline(id: string): Promise<AcceptedResponse> {
    return toAccepted(
      await this.request<unknown>("POST", `/projects/${encodeURIComponent(id)}/pipeline/pause`),
    );
  }

  async resumePipeline(id: string): Promise<AcceptedResponse> {
    return toAccepted(
      await this.request<unknown>("POST", `/projects/${encodeURIComponent(id)}/pipeline/resume`),
    );
  }

  async cancelPipeline(id: string): Promise<AcceptedResponse> {
    return toAccepted(
      await this.request<unknown>("POST", `/projects/${encodeURIComponent(id)}/pipeline/cancel`),
    );
  }

  // ── Approvals ───────────────────────────────────────────────────────────

  listApprovals(params?: ListParams): Promise<Page<ApprovalItem>> {
    return this.requestPage<ApprovalItem>("/approvals", { ...params });
  }

  getApproval(id: string): Promise<ApprovalItem> {
    return this.request<ApprovalItem>("GET", `/approvals/${encodeURIComponent(id)}`);
  }

  /** Maps the decision onto the backend's approve|reject endpoints (409 on a second decision). */
  decideApproval(id: string, payload: DecideApprovalPayload): Promise<ApprovalItem> {
    const action = payload.decision === "reject" ? "reject" : "approve";
    return this.request<ApprovalItem>(
      "POST",
      `/approvals/${encodeURIComponent(id)}/${action}`,
      { body: payload },
    );
  }

  // ── Jobs & queue ────────────────────────────────────────────────────────

  listJobs(filters: JobFilters = {}): Promise<Page<Job>> {
    return this.requestPage<Job>("/jobs", {
      status: filters.status,
      projectId: filters.projectId,
      agent: filters.agent,
      pool: filters.pool,
      cursor: filters.cursor,
      limit: filters.limit,
    });
  }

  cancelJob(id: string): Promise<Job> {
    return this.request<Job>("POST", `/jobs/${encodeURIComponent(id)}/cancel`);
  }

  retryJob(id: string): Promise<Job> {
    return this.request<Job>("POST", `/jobs/${encodeURIComponent(id)}/retry`);
  }

  queueStats(): Promise<QueueStats> {
    return this.request<QueueStats>("GET", "/queue/stats");
  }

  /**
   * `GET /queue/snapshot` — Round 3: the snapshot now also carries the
   * same `quota` object as `GET /usage/quota`; only the quota slice is
   * normalised/returned here (other panels use their own endpoints).
   */
  async queueSnapshotQuota(): Promise<QuotaUsage> {
    const raw = await this.request<unknown>("GET", "/queue/snapshot");
    const r = isRecord(raw) ? raw : {};
    return toQuotaUsage(r.quota);
  }

  // ── Schedules ───────────────────────────────────────────────────────────

  listSchedules(params?: ListParams): Promise<Page<Schedule>> {
    return this.requestPage<Schedule>("/schedules", { ...params });
  }

  createSchedule(payload: CreateSchedulePayload): Promise<Schedule> {
    return this.request<Schedule>("POST", "/schedules", { body: payload });
  }

  // ── Providers & usage ───────────────────────────────────────────────────

  listProviders(params?: ListParams): Promise<Page<Provider>> {
    return this.requestPage<Provider>("/providers", { ...params });
  }

  /**
   * Write-only: the key is stored by the backend (OS credential store)
   * and never read back. The wire body is `{ value }` —
   * backend/app/routers/providers.py `KeyBody { value }`; sending
   * `{ key }` 422s. The response is metadata only
   * (`{ provider, configured, key_last4 }`), normalised to camelCase.
   */
  async putProviderKey(id: string, key: string): Promise<ProviderKeyResult> {
    const raw = await this.request<unknown>(
      "PUT",
      `/providers/${encodeURIComponent(id)}/key`,
      { body: { value: key } },
    );
    return toProviderKeyResult(raw);
  }

  /** Removes the stored key for a provider (DELETE …/key, no body). */
  async deleteProviderKey(id: string): Promise<ProviderKeyResult> {
    const raw = await this.request<unknown>(
      "DELETE",
      `/providers/${encodeURIComponent(id)}/key`,
    );
    return toProviderKeyResult(raw);
  }

  /**
   * POST …/test (no body). Round 3 wire response carries an explicit
   * `status: ok | not_configured | error` (+ `category` on error):
   * `ok` is a genuine verification, `not_configured` means no key is
   * saved, `error` means the check ran and failed. The pre-Round-3
   * stub returned `{ provider, ok: true, detail }` without contacting
   * the provider — when `status` is absent callers must not present
   * the result as verified.
   */
  async testProvider(id: string): Promise<ProviderTestResult> {
    const raw = await this.request<unknown>(
      "POST",
      `/providers/${encodeURIComponent(id)}/test`,
    );
    return toProviderTestResult(raw);
  }

  /** `GET /usage/quota` — YouTube quota buckets + Pacific-midnight reset. */
  async getQuota(): Promise<QuotaUsage> {
    return toQuotaUsage(await this.request<unknown>("GET", "/usage/quota"));
  }

  // ── Settings ────────────────────────────────────────────────────────────

  /** `GET /settings/budget` — budget caps + warn/soft thresholds. */
  async getBudgetSettings(): Promise<BudgetSettings> {
    return toBudgetSettings(
      await this.request<unknown>("GET", "/settings/budget"),
    );
  }

  /**
   * `PUT /settings/budget` — same shape as the GET; the backend
   * validates server-side and rejects bad values with the standard
   * error envelope (thrown here as {@link ApiError}).
   */
  async updateBudgetSettings(settings: BudgetSettings): Promise<BudgetSettings> {
    const raw = await this.request<unknown>("PUT", "/settings/budget", {
      body: {
        per_video_cap_usd: settings.perVideoCapUsd,
        daily_cap_usd: settings.dailyCapUsd,
        monthly_cap_usd: settings.monthlyCapUsd,
        warn_pct: settings.warnPct,
        soft_pct: settings.softPct,
      },
    });
    // Tolerate a PUT that echoes the saved settings; if it returns an
    // envelope without them, the caller's input is what was accepted.
    return isRecord(raw) &&
      (typeof raw.per_video_cap_usd === "number" ||
        typeof raw.perVideoCapUsd === "number")
      ? toBudgetSettings(raw)
      : settings;
  }

  async getUsage(range?: UsageQuery | string): Promise<UsageSummary[]> {
    const query: RequestOptions["query"] =
      typeof range === "string" ? { range } : range ? { ...range } : undefined;
    const raw = await this.request<unknown>("GET", "/usage", { query });
    if (Array.isArray(raw)) return raw as UsageSummary[];
    return toPage<UsageSummary>(raw).items;
  }

  // ── Publications ────────────────────────────────────────────────────────

  listPublications(params?: ListParams): Promise<Page<Publication>> {
    return this.requestPage<Publication>("/publications", { ...params });
  }

  // ── Style Lab: competitors (scoped to the OWNING channel) ───────────────

  /** `GET /channels/{channelId}/competitors` — this channel's competitors only. */
  async listCompetitors(channelId: string): Promise<Page<CompetitorChannel>> {
    const raw = await this.request<unknown>(
      "GET",
      `/channels/${encodeURIComponent(channelId)}/competitors`,
    );
    return { items: rawItems(raw).map(toCompetitorChannel), nextCursor: null };
  }

  /** `POST /channels/{channelId}/competitors` — `{ input, display_name? }`. */
  async createCompetitor(
    channelId: string,
    payload: CreateCompetitorPayload,
  ): Promise<CompetitorChannel> {
    const raw = await this.request<unknown>(
      "POST",
      `/channels/${encodeURIComponent(channelId)}/competitors`,
      {
        body: {
          input: payload.input,
          ...(payload.displayName ? { display_name: payload.displayName } : {}),
        },
      },
    );
    return toCompetitorChannel(raw);
  }

  /** `POST /channels/{channelId}/competitors/{competitorId}/archive`. */
  async archiveCompetitor(
    channelId: string,
    competitorId: string,
  ): Promise<CompetitorChannel> {
    const raw = await this.request<unknown>(
      "POST",
      `/channels/${encodeURIComponent(channelId)}/competitors/${encodeURIComponent(competitorId)}/archive`,
      { body: {} },
    );
    return toCompetitorChannel(raw);
  }

  /**
   * `POST …/competitors/{competitorId}/metadata/fetch` — asks the backend
   * to fetch channel info. The result is honest: `metadataStatus` may come
   * back `metadata_unavailable` (fetch not configured) or `fetch_failed`;
   * callers must surface that state, never substitute invented data.
   */
  async fetchCompetitorMetadata(
    channelId: string,
    competitorId: string,
  ): Promise<CompetitorChannel> {
    const raw = await this.request<unknown>(
      "POST",
      `/channels/${encodeURIComponent(channelId)}/competitors/${encodeURIComponent(competitorId)}/metadata/fetch`,
      { body: {} },
    );
    return toCompetitorChannel(raw);
  }

  // ── Style Lab: competitor videos ────────────────────────────────────────

  /** `GET /channels/{channelId}/competitors/{competitorId}/videos`. */
  async listCompetitorVideos(
    channelId: string,
    competitorId: string,
  ): Promise<Page<CompetitorVideo>> {
    const raw = await this.request<unknown>(
      "GET",
      `/channels/${encodeURIComponent(channelId)}/competitors/${encodeURIComponent(competitorId)}/videos`,
    );
    return { items: rawItems(raw).map(toCompetitorVideo), nextCursor: null };
  }

  /** `POST …/videos` — JSON intake (paste details / URL metadata). */
  async createCompetitorVideo(
    channelId: string,
    competitorId: string,
    payload: CreateCompetitorVideoPayload,
  ): Promise<CompetitorVideo> {
    const raw = await this.request<unknown>(
      "POST",
      `/channels/${encodeURIComponent(channelId)}/competitors/${encodeURIComponent(competitorId)}/videos`,
      {
        body: {
          source: payload.source,
          ...(payload.videoUrl ? { video_url: payload.videoUrl } : {}),
          ...(payload.title ? { title: payload.title } : {}),
          ...(payload.description ? { description: payload.description } : {}),
          ...(payload.tags && payload.tags.length > 0 ? { tags: payload.tags } : {}),
        },
      },
    );
    return toCompetitorVideo(raw);
  }

  /**
   * `POST …/videos/attach` — multipart upload: form field `file` (the
   * video) plus optional `thumbnail`. No JSON content-type is sent.
   */
  async attachCompetitorVideo(
    channelId: string,
    competitorId: string,
    file: File,
    thumbnail?: File | null,
  ): Promise<CompetitorVideo> {
    const form = new FormData();
    form.append("file", file);
    if (thumbnail) form.append("thumbnail", thumbnail);
    const raw = await this.requestMultipart<unknown>(
      `/channels/${encodeURIComponent(channelId)}/competitors/${encodeURIComponent(competitorId)}/videos/attach`,
      form,
    );
    return toCompetitorVideo(raw);
  }

  /**
   * `POST /channels/{channelId}/competitor-videos/{videoId}/analysis` —
   * queues one video for analysis (response carries
   * `analysis_status: "queued"`).
   */
  async queueCompetitorVideoAnalysis(
    channelId: string,
    videoId: string,
  ): Promise<CompetitorVideo> {
    const raw = await this.request<unknown>(
      "POST",
      `/channels/${encodeURIComponent(channelId)}/competitor-videos/${encodeURIComponent(videoId)}/analysis`,
      { body: {} },
    );
    return toCompetitorVideo(raw);
  }

  // ── Style Lab: style profiles ───────────────────────────────────────────

  /** `GET /channels/{channelId}/style-profiles`. */
  async listStyleProfiles(channelId: string): Promise<Page<StyleProfile>> {
    const raw = await this.request<unknown>(
      "GET",
      `/channels/${encodeURIComponent(channelId)}/style-profiles`,
    );
    return { items: rawItems(raw).map(toStyleProfile), nextCursor: null };
  }

  /** `POST /channels/{channelId}/style-profiles` — `{ name, competitor_ids }`. */
  async createStyleProfile(
    channelId: string,
    payload: CreateStyleProfilePayload,
  ): Promise<StyleProfile> {
    const raw = await this.request<unknown>(
      "POST",
      `/channels/${encodeURIComponent(channelId)}/style-profiles`,
      { body: { name: payload.name, competitor_ids: payload.competitorIds } },
    );
    return toStyleProfile(raw);
  }

  /** `POST /channels/{channelId}/style-profiles/{profileId}/activate`. */
  async activateStyleProfile(
    channelId: string,
    profileId: string,
  ): Promise<StyleProfile> {
    const raw = await this.request<unknown>(
      "POST",
      `/channels/${encodeURIComponent(channelId)}/style-profiles/${encodeURIComponent(profileId)}/activate`,
      { body: {} },
    );
    return toStyleProfile(raw);
  }
}

// ── Shared client (rebuilt on connection change) ─────────────────────────

let sharedClient: ApiClient | null = null;
let sharedClientConn: BackendConnection | null = null;

/**
 * Shared ApiClient for the query modules that keep a singleton.
 * Keyed by the CURRENT in-memory connection: after the shell pushes
 * a new connection (backend restart — new port/token), the next call
 * builds a fresh client against it instead of reusing the dead one.
 * The connection (and its token) stays in module memory only.
 */
export async function getSharedApiClient(): Promise<ApiClient> {
  const conn = await getBackendConnection();
  if (!sharedClient || !sameConnection(sharedClientConn, conn)) {
    sharedClient = new ApiClient(conn);
    sharedClientConn = conn;
  }
  return sharedClient;
}

/**
 * Exchanges the launch token (REST header) for a single-use 60s WS ticket.
 * The token itself must never appear in the WebSocket URL, so the ticket
 * travels in the `client.hello` frame instead.
 */
export async function fetchWsTicket(conn: BackendConnection): Promise<string> {
  const baseUrl = conn.baseUrl.replace(/\/+$/, "");
  let res: Response;
  try {
    res = await fetch(`${baseUrl}/ws-ticket`, {
      method: "POST",
      headers: { "X-Local-Token": conn.token },
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

  const status = typeof res.status === "number" ? res.status : 200;
  const body = await readBody(res);
  const ok = typeof res.ok === "boolean" ? res.ok : status >= 200 && status < 300;
  if (!ok) throw toApiError(status, body);

  const ticket = isRecord(body) ? body.ticket : undefined;
  if (typeof ticket !== "string" || ticket.length === 0) {
    throw new ApiError({
      code: "WS_TICKET_MISSING",
      message: "Backend did not return a WebSocket ticket",
      status,
      retryable: true,
    });
  }
  return ticket;
}
