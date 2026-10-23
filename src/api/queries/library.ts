/**
 * React Query hooks for the video library (Round 2 binding).
 *
 * Wire shapes are defined LOCALLY here (per shared conventions) because
 * they mirror the backend exactly — `backend/app/routers/projects.py`
 * returns raw `projects` table rows / in-memory project dicts
 * (snake_case):
 *
 * - `GET  /projects?status=&q=&cursor=&limit=` -> `{ items, next_cursor }`
 *   Filters are the backend's snake_case params. `q` searches the
 *   project topic (a project's title IS its topic — there is no
 *   separate title column).
 * - `GET  /projects/{id}`                       -> one project
 *   NOTE: there is NO `GET /projects/{id}/snapshot` route in the
 *   backend (the Round 1 `ApiClient.projectSnapshot` would 404), so
 *   the detail view is built from this payload + jobs + publications.
 *   There is also NO `DELETE /projects/{id}` route — the backend has
 *   no project delete, so the UI offers none.
 * - `POST /projects/{id}/pipeline/{action}`     -> 202 `{ status:
 *   "accepted", project_id, action, job_id }` for action in
 *   start | pause | resume | cancel.
 * - `GET  /jobs?project_id={id}`               -> History tab (real
 *   pipeline activity for the project).
 * - `GET  /publications`                        -> Performance tab;
 *   the endpoint has no filter params, so the project's record is
 *   picked out client-side. Publish records carry NO view/like
 *   metrics — the tab shows publication facts only.
 *
 * Project status vocabulary — the SINGLE source of truth is the
 * backend schema CHECK on `projects.status`; the filter chips and
 * badges are built from PROJECT_STATUSES below, never invented:
 * draft | in_production | awaiting_review | qa | ready | scheduled |
 * published | failed | blocked | cancelled.
 *
 * Timestamps arrive as epoch-ms numbers (DB rows) or ISO strings
 * (in-memory projects); formatters in the screen handle both.
 *
 * This module uses its own small fetch helper (same token header +
 * error envelope as `ApiClient`) because `ApiClient.listProjects` /
 * `listJobs` send camelCase query keys (`channelId`, `projectId`)
 * that the backend's snake_case params silently ignore; `client.ts`
 * is owned by another worker this round.
 */

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { ApiError } from "../types";
import { getBackendConnection } from "../tauri";

// ── Status vocabulary (backend `projects.status` CHECK) ────────────────────

export const PROJECT_STATUSES = [
  "draft",
  "in_production",
  "awaiting_review",
  "qa",
  "ready",
  "scheduled",
  "published",
  "failed",
  "blocked",
  "cancelled",
] as const;

export type ProjectStatus = (typeof PROJECT_STATUSES)[number];

export const PROJECT_STATUS_LABELS: Record<ProjectStatus, string> = {
  draft: "Draft",
  in_production: "In Production",
  awaiting_review: "Awaiting Review",
  qa: "QA",
  ready: "Ready",
  scheduled: "Scheduled",
  published: "Published",
  failed: "Failed",
  blocked: "Blocked",
  cancelled: "Cancelled",
};

export function projectStatusLabel(status: string | undefined): string {
  if (status && (PROJECT_STATUSES as readonly string[]).includes(status)) {
    return PROJECT_STATUS_LABELS[status as ProjectStatus];
  }
  return status ?? "Unknown";
}

// ── Wire types (backend snake_case, exactly as emitted) ────────────────────

export interface ProjectWire {
  id: string;
  channel_id?: string;
  niche_profile_id?: string;
  /** A project's title is its topic; `title` tolerated if ever present. */
  topic?: string | null;
  title?: string;
  topic_mode?: string;
  topic_source?: string;
  status?: string;
  current_phase?: string | null;
  publish_mode?: string;
  target_minutes?: number | null;
  target_words?: number | null;
  /** Only on DB-backed rows; absent on fresh in-memory projects. */
  total_cost_micros?: number | null;
  youtube_video_id?: string | null;
  published_at?: number | string | null;
  error_summary?: string | null;
  schedule_id?: string | null;
  task_dag?: {
    status?: string;
    total_agents?: number;
    phases?: string[];
  } | null;
  created_at?: number | string;
  updated_at?: number | string;
}

export interface JobWire {
  id: string;
  project_id?: string | null;
  agent_key?: string;
  kind?: string;
  status?: string;
  attempts?: number;
  created_at?: number | string;
  updated_at?: number | string;
}

export interface PublicationWire {
  id: string;
  project_id?: string;
  youtube_video_id?: string;
  video_url?: string | null;
  title?: string | null;
  privacy_status?: string;
  published_at?: number | string | null;
  created_at?: number | string;
}

export type PipelineAction = "start" | "pause" | "resume" | "cancel";

export interface PipelineActionResult {
  status: string;
  projectId: string;
  action: PipelineAction;
  jobId: string;
}

export interface ProjectFilters {
  status?: string;
  q?: string;
}

// ── Minimal request helper (mirrors ApiClient's envelope handling) ─────────

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

  if (res.status === 204) return undefined as T;

  const text = await res.text();
  let parsed: unknown;
  if (text) {
    try {
      parsed = JSON.parse(text) as unknown;
    } catch {
      parsed = text;
    }
  }

  if (!res.ok) {
    if (isRecord(parsed) && isRecord(parsed.error)) {
      const err = parsed.error;
      throw new ApiError({
        code: typeof err.code === "string" ? err.code : `HTTP_${res.status}`,
        message:
          typeof err.message === "string"
            ? err.message
            : `Request failed (${res.status})`,
        status: res.status,
        retryable:
          typeof err.retryable === "boolean"
            ? err.retryable
            : res.status === 429 || res.status >= 500,
        requestId: typeof err.request_id === "string" ? err.request_id : undefined,
        details: err.details,
      });
    }
    throw new ApiError({
      code: `HTTP_${res.status}`,
      message: `Request failed (${res.status})`,
      status: res.status,
      retryable: res.status === 429 || res.status >= 500,
    });
  }

  return parsed as T;
}

interface PageWire<T> {
  items: T[];
  nextCursor: string | null;
}

function toPage<T>(raw: unknown): PageWire<T> {
  if (isRecord(raw) && Array.isArray(raw.items)) {
    const next = raw.next_cursor;
    return {
      items: raw.items as T[],
      nextCursor: typeof next === "string" ? next : null,
    };
  }
  return { items: [], nextCursor: null };
}

// ── Projects ───────────────────────────────────────────────────────────────

async function fetchProjectsPage(
  filters: ProjectFilters,
  cursor: string | undefined,
): Promise<PageWire<ProjectWire>> {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.q) params.set("q", filters.q);
  if (cursor) params.set("cursor", cursor);
  params.set("limit", "50");
  const qs = params.toString();
  return toPage<ProjectWire>(await request<unknown>("GET", `/projects?${qs}`));
}

/** Cursor-paginated project list; filters go to the backend as-is. */
export function useProjects(filters: ProjectFilters) {
  return useInfiniteQuery({
    queryKey: ["projects", { status: filters.status ?? null, q: filters.q ?? null }],
    queryFn: ({ pageParam }) => fetchProjectsPage(filters, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.nextCursor ?? undefined,
    retry: 1,
  });
}

export function useProject(id: string | null) {
  return useQuery({
    queryKey: ["projects", id],
    queryFn: async (): Promise<ProjectWire> => {
      const raw = await request<unknown>(
        "GET",
        `/projects/${encodeURIComponent(id ?? "")}`,
      );
      return raw as ProjectWire;
    },
    enabled: typeof id === "string" && id.length > 0,
    retry: 1,
  });
}

/** POST /projects/{id}/pipeline/{action} — 202 accepted + job_id. */
export function usePipelineAction() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (vars: {
      id: string;
      action: PipelineAction;
    }): Promise<PipelineActionResult> => {
      const raw = await request<unknown>(
        "POST",
        `/projects/${encodeURIComponent(vars.id)}/pipeline/${vars.action}`,
      );
      const rec = isRecord(raw) ? raw : {};
      return {
        status: typeof rec.status === "string" ? rec.status : "",
        projectId: vars.id,
        action: vars.action,
        jobId: typeof rec.job_id === "string" ? rec.job_id : "",
      };
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
  });
}

// ── Detail-tab data ────────────────────────────────────────────────────────

/** History tab: real pipeline jobs recorded for this project. */
export function useProjectJobs(id: string | null) {
  return useQuery({
    queryKey: ["projects", id, "jobs"],
    queryFn: async (): Promise<JobWire[]> => {
      const params = new URLSearchParams();
      params.set("project_id", id ?? "");
      params.set("limit", "200");
      const page = toPage<JobWire>(
        await request<unknown>("GET", `/jobs?${params.toString()}`),
      );
      return page.items;
    },
    enabled: typeof id === "string" && id.length > 0,
    retry: 1,
  });
}

/** Performance tab: the project's publish record, if it has one. */
export function useProjectPublication(id: string | null) {
  return useQuery({
    queryKey: ["projects", id, "publication"],
    queryFn: async (): Promise<PublicationWire | null> => {
      const page = toPage<PublicationWire>(
        await request<unknown>("GET", "/publications"),
      );
      return page.items.find((item) => item.project_id === id) ?? null;
    },
    enabled: typeof id === "string" && id.length > 0,
    retry: 1,
  });
}
