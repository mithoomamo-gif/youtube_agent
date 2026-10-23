/**
 * React Query hooks for the Style Lab (Style Lab mini-round).
 *
 * Wire shapes are defined LOCALLY here (per shared conventions) because
 * they mirror the backend contract exactly — snake_case fields and all.
 * Every route is nested under the OWNING channel, so competitors,
 * videos and profiles are always scoped to one of the user's channels:
 *
 * - `GET    /channels/{channelId}/competitors`
 *     -> competitor rows (contract: `{ items, next_cursor }` envelope
 *        is assumed, consistent with the other list routers; a bare
 *        array is also tolerated)
 * - `POST   /channels/{channelId}/competitors`          body `{ input, display_name? }`
 * - `POST   /channels/{channelId}/competitors/{competitorId}/archive`
 * - `POST   /channels/{channelId}/competitors/{competitorId}/metadata/fetch`
 * - `GET    /channels/{channelId}/competitors/{competitorId}/videos`
 * - `POST   /channels/{channelId}/competitors/{competitorId}/videos`
 *     JSON body `{ source, video_url?, title?, description?, tags? }`
 * - `POST   /channels/{channelId}/competitors/{competitorId}/videos/attach`
 *     multipart form: `file`, optional `thumbnail` (no JSON content-type)
 * - `POST   /channels/{channelId}/competitor-videos/{videoId}/analysis`
 * - `GET    /channels/{channelId}/style-profiles`
 * - `POST   /channels/{channelId}/style-profiles`       body `{ name, competitor_ids }`
 * - `POST   /channels/{channelId}/style-profiles/{profileId}/activate`
 *
 * Honesty rules: `metadata_status: "metadata_unavailable"` means the
 * backend cannot fetch channel info (not configured) — the screen
 * shows exactly that state with guidance; no field is ever invented
 * client-side. Timestamps follow the backend DB convention and may be
 * epoch-ms numbers or ISO strings; both are tolerated.
 *
 * This module uses its own small fetch helpers (same token header +
 * error envelope as `ApiClient`) because `channels.ts` — the module
 * the Channels screen already uses — does the same, and the multipart
 * attach needs a FormData path the JSON helper cannot serve. The
 * equivalent typed `ApiClient` methods live in `src/api/client.ts`.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../types";
import { getBackendConnection } from "../tauri";

// ── Wire types (backend snake_case, exactly as emitted) ────────────────────

export type CompetitorMetadataStatusWire =
  | "not_fetched"
  | "fetched"
  | "metadata_unavailable"
  | "fetch_failed";

export interface CompetitorChannelWire {
  id: string;
  channel_id: string;
  platform: string;
  source_input: string;
  source_kind: string;
  external_channel_id: string | null;
  handle: string | null;
  display_name: string | null;
  channel_url: string | null;
  metadata_status: CompetitorMetadataStatusWire;
  status: string;
  created_at: string | number;
  updated_at: string | number;
}

export type CompetitorVideoSourceWire =
  | "attached_file"
  | "url_metadata"
  | "pasted_metadata";

export type CompetitorVideoAnalysisStatusWire =
  | "not_queued"
  | "queued"
  | "analysing"
  | "analysed"
  | "analysis_failed";

export interface CompetitorVideoWire {
  id: string;
  competitor_channel_id: string;
  source: CompetitorVideoSourceWire;
  external_video_id: string | null;
  video_url: string | null;
  title: string | null;
  description: string | null;
  tags: string[] | string | null;
  duration_s: number | null;
  width: number | null;
  height: number | null;
  file_size_bytes: number | null;
  has_thumbnail: boolean;
  analysis_status: CompetitorVideoAnalysisStatusWire;
  created_at: string | number;
}

export type StyleProfileStatusWire = "draft" | "active" | "superseded";

export interface StyleProfileWire {
  id: string;
  channel_id: string;
  name: string;
  version: number;
  status: StyleProfileStatusWire;
  competitor_ids: string[];
  created_at: string | number;
}

// ── Mutation inputs (UI terms; serialised to the wire on send) ────────────

export interface CreateCompetitorInput {
  /** A channel link, an @handle, or a channel ID. */
  input: string;
  displayName?: string;
}

export interface CreateCompetitorVideoInput {
  source: CompetitorVideoSourceWire;
  videoUrl?: string;
  title?: string;
  description?: string;
  tags?: string[];
}

export interface AttachCompetitorVideoInput {
  file: File;
  thumbnail?: File | null;
}

export interface CreateStyleProfileInput {
  name: string;
  competitorIds: string[];
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

/**
 * Multipart POST for the attach endpoint. No `Content-Type` header is
 * set — the browser generates `multipart/form-data; boundary=…`.
 */
async function requestForm<T>(path: string, form: FormData): Promise<T> {
  const conn = await getBackendConnection();
  const baseUrl = conn.baseUrl.replace(/\/+$/, "");

  const headers: Record<string, string> = {
    "X-Local-Token": conn.token,
    "Idempotency-Key": randomId(),
  };

  let res: Response;
  try {
    res = await fetch(`${baseUrl}${path}`, { method: "POST", headers, body: form });
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

function toCompetitorWire(raw: unknown): CompetitorChannelWire {
  const r = isRecord(raw) ? raw : {};
  const str = (v: unknown): string | null =>
    typeof v === "string" && v.length > 0 ? v : null;
  const meta = r.metadata_status;
  return {
    id: typeof r.id === "string" ? r.id : "",
    channel_id: str(r.channel_id) ?? "",
    platform: typeof r.platform === "string" ? r.platform : "",
    source_input: str(r.source_input) ?? "",
    source_kind: str(r.source_kind) ?? "",
    external_channel_id: str(r.external_channel_id),
    handle: str(r.handle),
    display_name: str(r.display_name),
    channel_url: str(r.channel_url),
    metadata_status:
      meta === "fetched" || meta === "metadata_unavailable" || meta === "fetch_failed"
        ? meta
        : "not_fetched",
    status: typeof r.status === "string" ? r.status : "",
    created_at:
      typeof r.created_at === "string" || typeof r.created_at === "number"
        ? r.created_at
        : "",
    updated_at:
      typeof r.updated_at === "string" || typeof r.updated_at === "number"
        ? r.updated_at
        : "",
  };
}

function toVideoWire(raw: unknown): CompetitorVideoWire {
  const r = isRecord(raw) ? raw : {};
  const str = (v: unknown): string | null =>
    typeof v === "string" && v.length > 0 ? v : null;
  const num = (v: unknown): number | null =>
    typeof v === "number" && Number.isFinite(v) ? v : null;
  const source = r.source;
  const analysis = r.analysis_status;
  return {
    id: typeof r.id === "string" ? r.id : "",
    competitor_channel_id: str(r.competitor_channel_id) ?? "",
    source:
      source === "attached_file" || source === "url_metadata"
        ? source
        : "pasted_metadata",
    external_video_id: str(r.external_video_id),
    video_url: str(r.video_url),
    title: str(r.title),
    description: str(r.description),
    tags:
      Array.isArray(r.tags) || typeof r.tags === "string" || r.tags === null
        ? (r.tags as string[] | string | null)
        : null,
    duration_s: num(r.duration_s),
    width: num(r.width),
    height: num(r.height),
    file_size_bytes: num(r.file_size_bytes),
    has_thumbnail: r.has_thumbnail === true,
    analysis_status:
      analysis === "queued" ||
      analysis === "analysing" ||
      analysis === "analysed" ||
      analysis === "analysis_failed"
        ? analysis
        : "not_queued",
    created_at:
      typeof r.created_at === "string" || typeof r.created_at === "number"
        ? r.created_at
        : "",
  };
}

function toProfileWire(raw: unknown): StyleProfileWire {
  const r = isRecord(raw) ? raw : {};
  const status = r.status;
  return {
    id: typeof r.id === "string" ? r.id : "",
    channel_id: typeof r.channel_id === "string" ? r.channel_id : "",
    name: typeof r.name === "string" ? r.name : "",
    version: typeof r.version === "number" ? r.version : 1,
    status: status === "active" || status === "superseded" ? status : "draft",
    competitor_ids: Array.isArray(r.competitor_ids)
      ? r.competitor_ids.filter((v): v is string => typeof v === "string")
      : [],
    created_at:
      typeof r.created_at === "string" || typeof r.created_at === "number"
        ? r.created_at
        : "",
  };
}

// ── Query keys ─────────────────────────────────────────────────────────────

const competitorsKey = (channelId: string | null) =>
  ["style-lab", channelId, "competitors"] as const;
const videosKey = (channelId: string | null, competitorId: string | null) =>
  ["style-lab", channelId, "competitors", competitorId, "videos"] as const;
const profilesKey = (channelId: string | null) =>
  ["style-lab", channelId, "style-profiles"] as const;

// ── Competitors ────────────────────────────────────────────────────────────

export function useCompetitors(channelId: string | null) {
  return useQuery({
    queryKey: competitorsKey(channelId),
    enabled: channelId !== null,
    queryFn: async (): Promise<CompetitorChannelWire[]> => {
      const raw = await request<unknown>(
        "GET",
        `/channels/${encodeURIComponent(channelId ?? "")}/competitors`,
      );
      return listItems(raw).map(toCompetitorWire);
    },
    retry: 1,
  });
}

export function useCreateCompetitor(channelId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: CreateCompetitorInput): Promise<CompetitorChannelWire> => {
      const raw = await request<unknown>(
        "POST",
        `/channels/${encodeURIComponent(channelId ?? "")}/competitors`,
        {
          input: input.input,
          ...(input.displayName ? { display_name: input.displayName } : {}),
        },
      );
      return toCompetitorWire(raw);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: competitorsKey(channelId) });
    },
  });
}

export function useArchiveCompetitor(channelId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (competitorId: string): Promise<CompetitorChannelWire> => {
      const raw = await request<unknown>(
        "POST",
        `/channels/${encodeURIComponent(channelId ?? "")}/competitors/${encodeURIComponent(competitorId)}/archive`,
        {},
      );
      return toCompetitorWire(raw);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: competitorsKey(channelId) });
      void queryClient.invalidateQueries({ queryKey: profilesKey(channelId) });
    },
  });
}

/**
 * POST …/metadata/fetch. The returned row's `metadata_status` is the
 * honest outcome — `fetched`, `metadata_unavailable` (fetch not
 * configured) or `fetch_failed`. Nothing is synthesised client-side.
 */
export function useFetchCompetitorMetadata(channelId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (competitorId: string): Promise<CompetitorChannelWire> => {
      const raw = await request<unknown>(
        "POST",
        `/channels/${encodeURIComponent(channelId ?? "")}/competitors/${encodeURIComponent(competitorId)}/metadata/fetch`,
        {},
      );
      return toCompetitorWire(raw);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: competitorsKey(channelId) });
    },
  });
}

// ── Competitor videos ──────────────────────────────────────────────────────

export function useCompetitorVideos(
  channelId: string | null,
  competitorId: string | null,
) {
  return useQuery({
    queryKey: videosKey(channelId, competitorId),
    enabled: channelId !== null && competitorId !== null,
    queryFn: async (): Promise<CompetitorVideoWire[]> => {
      const raw = await request<unknown>(
        "GET",
        `/channels/${encodeURIComponent(channelId ?? "")}/competitors/${encodeURIComponent(competitorId ?? "")}/videos`,
      );
      return listItems(raw).map(toVideoWire);
    },
    retry: 1,
  });
}

export function useCreateCompetitorVideo(
  channelId: string | null,
  competitorId: string | null,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (
      input: CreateCompetitorVideoInput,
    ): Promise<CompetitorVideoWire> => {
      const raw = await request<unknown>(
        "POST",
        `/channels/${encodeURIComponent(channelId ?? "")}/competitors/${encodeURIComponent(competitorId ?? "")}/videos`,
        {
          source: input.source,
          ...(input.videoUrl ? { video_url: input.videoUrl } : {}),
          ...(input.title ? { title: input.title } : {}),
          ...(input.description ? { description: input.description } : {}),
          ...(input.tags && input.tags.length > 0 ? { tags: input.tags } : {}),
        },
      );
      return toVideoWire(raw);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: videosKey(channelId, competitorId),
      });
    },
  });
}

export function useAttachCompetitorVideo(
  channelId: string | null,
  competitorId: string | null,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (
      input: AttachCompetitorVideoInput,
    ): Promise<CompetitorVideoWire> => {
      const form = new FormData();
      form.append("file", input.file);
      if (input.thumbnail) form.append("thumbnail", input.thumbnail);
      const raw = await requestForm<unknown>(
        `/channels/${encodeURIComponent(channelId ?? "")}/competitors/${encodeURIComponent(competitorId ?? "")}/videos/attach`,
        form,
      );
      return toVideoWire(raw);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: videosKey(channelId, competitorId),
      });
    },
  });
}

/** POST /channels/{channelId}/competitor-videos/{videoId}/analysis. */
export function useQueueVideoAnalysis(channelId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (args: {
      competitorId: string;
      videoId: string;
    }): Promise<CompetitorVideoWire> => {
      const raw = await request<unknown>(
        "POST",
        `/channels/${encodeURIComponent(channelId ?? "")}/competitor-videos/${encodeURIComponent(args.videoId)}/analysis`,
        {},
      );
      return toVideoWire(raw);
    },
    onSuccess: (_data, args) => {
      void queryClient.invalidateQueries({
        queryKey: videosKey(channelId, args.competitorId),
      });
    },
  });
}

// ── Style profiles ─────────────────────────────────────────────────────────

export function useStyleProfiles(channelId: string | null) {
  return useQuery({
    queryKey: profilesKey(channelId),
    enabled: channelId !== null,
    queryFn: async (): Promise<StyleProfileWire[]> => {
      const raw = await request<unknown>(
        "GET",
        `/channels/${encodeURIComponent(channelId ?? "")}/style-profiles`,
      );
      return listItems(raw).map(toProfileWire);
    },
    retry: 1,
  });
}

export function useCreateStyleProfile(channelId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: CreateStyleProfileInput): Promise<StyleProfileWire> => {
      const raw = await request<unknown>(
        "POST",
        `/channels/${encodeURIComponent(channelId ?? "")}/style-profiles`,
        { name: input.name, competitor_ids: input.competitorIds },
      );
      return toProfileWire(raw);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: profilesKey(channelId) });
    },
  });
}

export function useActivateStyleProfile(channelId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (profileId: string): Promise<StyleProfileWire> => {
      const raw = await request<unknown>(
        "POST",
        `/channels/${encodeURIComponent(channelId ?? "")}/style-profiles/${encodeURIComponent(profileId)}/activate`,
        {},
      );
      return toProfileWire(raw);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: profilesKey(channelId) });
    },
  });
}
