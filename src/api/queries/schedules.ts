/**
 * React Query hooks for schedules (Round 4 live binding).
 *
 * Wire shapes mirror `backend/app/routers/schedules.py` exactly — the
 * router returns ONE normalised shape for every schedule:
 *
 * - `GET    /schedules`                  -> `{ items: ScheduleWire[], next_cursor }`
 * - `POST   /schedules`                  -> 201 ScheduleWire (idempotency-keyed)
 * - `GET    /schedules/{id}`             -> ScheduleWire
 * - `PATCH  /schedules/{id}`             -> ScheduleWire (partial edit)
 * - `POST   /schedules/{id}/pause`       -> ScheduleWire (enabled=false)
 * - `POST   /schedules/{id}/resume`      -> ScheduleWire (enabled=true, next run recomputed)
 * - `POST   /schedules/{id}/run-now`     -> 202 `{ status, schedule_id, tick, run, project_id }`:
 *   the backend marks the schedule due and runs one real scheduler
 *   tick; `run` is the `schedule_runs` row the tick wrote (or null
 *   when it wrote none). A paused schedule rejects with 409
 *   `schedule_disabled`.
 * - `GET    /schedules/{id}/runs`        -> `{ items: ScheduleRunWire[], next_cursor }`
 * - `DELETE /schedules/{id}`             -> 204
 *
 * Timestamps are epoch-ms numbers (the backend DB convention).
 * Cadence is the structured object the scheduler reads:
 * `{type:"daily",time:"HH:MM"} | {type:"weekly",weekday:0..6,time} |
 *  {type:"interval",interval_hours:N}` (+ optional `topics` for the
 * fixed_list topic mode). Legacy free-text rules surface as
 * `{type:"rule",rule:"…"}` with `next_run_at: null`.
 *
 * Form data (channels / niches) is fetched under schedules-scoped
 * query keys so cache shapes never collide with the Channels or
 * Wizard queries that share those endpoints.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../types";
import { getBackendConnection } from "../tauri";
import type { ChannelWire } from "./channels";

// ── Wire types (backend snake_case, exactly as emitted) ────────────────────

export type CadenceWire =
  | { type: "daily"; time: string; topics?: string[] }
  | { type: "weekly"; weekday: number; time: string; topics?: string[] }
  | { type: "interval"; interval_hours: number; topics?: string[] }
  | { type: "rule"; rule: string; topics?: string[] };

export interface ScheduleWire {
  id: string;
  name?: string | null;
  channel_id: string;
  channel_title?: string | null;
  niche_profile_id: string;
  niche_name?: string | null;
  cadence?: CadenceWire | null;
  cadence_summary?: string;
  topic_mode?: string;
  target_minutes?: number | null;
  publish_mode?: string;
  daily_cost_cap_micros?: number | null;
  daily_budget_cap_usd?: number | null;
  enabled: boolean;
  is_enabled?: number;
  next_run_at?: number | null;
  last_run_at?: number | null;
  created_at?: number | null;
  updated_at?: number | null;
}

export interface ScheduleRunWire {
  id: string;
  schedule_id: string;
  project_id?: string | null;
  status: string;
  reason?: string | null;
  detail?: Record<string, unknown> | null;
  started_at?: number | null;
  finished_at?: number | null;
  created_at?: number | null;
}

export interface RunNowResult {
  status: string;
  scheduleId: string;
  tick: {
    checked: number;
    materialised: number;
    collapsed: number;
    failed: number;
  } | null;
  run: ScheduleRunWire | null;
  projectId: string | null;
}

/** Editable fields, in UI (camelCase) terms; serialised on send. */
export interface ScheduleFormInput {
  name?: string;
  channelId: string;
  nicheProfileId: string;
  cadence: CadenceWire;
  topicMode: "auto" | "queue" | "fixed_list";
  targetMinutes: number | null;
  publishMode: "auto" | "review_first";
  dailyBudgetCapUsd: number | null;
  enabled: boolean;
}

export interface NicheOption {
  id: string;
  name: string;
  source?: string;
}

// ── Minimal request helper (mirrors ApiClient's envelope handling) ───────

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
  const upper = method.toUpperCase();
  if (upper === "POST" || upper === "PATCH") {
    headers["Idempotency-Key"] = randomId();
  }

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

function toWireBody(input: ScheduleFormInput): Record<string, unknown> {
  return {
    name: input.name?.trim() ? input.name.trim() : undefined,
    channel_id: input.channelId,
    niche_profile_id: input.nicheProfileId,
    cadence: input.cadence,
    topic_mode: input.topicMode,
    target_minutes: input.targetMinutes,
    publish_mode: input.publishMode,
    daily_budget_cap_usd: input.dailyBudgetCapUsd,
    enabled: input.enabled,
  };
}

// ── Queries ────────────────────────────────────────────────────────────────

export function useSchedules() {
  return useQuery({
    queryKey: ["schedules", "list"],
    queryFn: async (): Promise<ScheduleWire[]> => {
      const raw = await request<unknown>("GET", "/schedules");
      const items: unknown[] =
        isRecord(raw) && Array.isArray(raw.items) ? raw.items : [];
      return items.filter(isRecord) as unknown as ScheduleWire[];
    },
    retry: 1,
  });
}

export function useScheduleRuns(scheduleId: string | null) {
  return useQuery({
    queryKey: ["schedules", "runs", scheduleId],
    enabled: scheduleId !== null,
    queryFn: async (): Promise<ScheduleRunWire[]> => {
      const raw = await request<unknown>(
        "GET",
        `/schedules/${encodeURIComponent(scheduleId ?? "")}/runs`,
      );
      const items: unknown[] =
        isRecord(raw) && Array.isArray(raw.items) ? raw.items : [];
      return items.filter(isRecord) as unknown as ScheduleRunWire[];
    },
    retry: 1,
  });
}

/** Channels for the schedule form (real rows; empty until one connects). */
export function useScheduleFormChannels() {
  return useQuery({
    queryKey: ["schedules", "form-channels"],
    queryFn: async (): Promise<ChannelWire[]> => {
      const raw = await request<unknown>("GET", "/channels");
      const items: unknown[] =
        isRecord(raw) && Array.isArray(raw.items) ? raw.items : [];
      return items.filter(isRecord) as unknown as ChannelWire[];
    },
    retry: 1,
  });
}

/** Niches for the schedule form (built-ins + saved profiles). */
export function useScheduleFormNiches() {
  return useQuery({
    queryKey: ["schedules", "form-niches"],
    queryFn: async (): Promise<NicheOption[]> => {
      const raw = await request<unknown>("GET", "/niches?limit=200");
      const items: unknown[] =
        isRecord(raw) && Array.isArray(raw.items) ? raw.items : [];
      const options: NicheOption[] = [];
      for (const item of items) {
        if (
          isRecord(item) &&
          typeof item.id === "string" &&
          typeof item.name === "string"
        ) {
          options.push({
            id: item.id,
            name: item.name,
            source: typeof item.source === "string" ? item.source : undefined,
          });
        }
      }
      return options;
    },
    retry: 1,
  });
}

// ── Mutations ──────────────────────────────────────────────────────────────

function useScheduleMutation<TInput, TOutput>(
  fn: (input: TInput) => Promise<TOutput>,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["schedules"] });
    },
  });
}

export function useCreateSchedule() {
  return useScheduleMutation(
    async (input: ScheduleFormInput): Promise<ScheduleWire> => {
      const raw = await request<unknown>("POST", "/schedules", toWireBody(input));
      return raw as ScheduleWire;
    },
  );
}

export function useUpdateSchedule() {
  return useScheduleMutation(
    async (args: { id: string; input: ScheduleFormInput }): Promise<ScheduleWire> => {
      const raw = await request<unknown>(
        "PATCH",
        `/schedules/${encodeURIComponent(args.id)}`,
        toWireBody(args.input),
      );
      return raw as ScheduleWire;
    },
  );
}

export function usePauseSchedule() {
  return useScheduleMutation(async (id: string): Promise<ScheduleWire> => {
    const raw = await request<unknown>(
      "POST",
      `/schedules/${encodeURIComponent(id)}/pause`,
      {},
    );
    return raw as ScheduleWire;
  });
}

export function useResumeSchedule() {
  return useScheduleMutation(async (id: string): Promise<ScheduleWire> => {
    const raw = await request<unknown>(
      "POST",
      `/schedules/${encodeURIComponent(id)}/resume`,
      {},
    );
    return raw as ScheduleWire;
  });
}

export function useDeleteSchedule() {
  return useScheduleMutation(async (id: string): Promise<void> => {
    await request<unknown>("DELETE", `/schedules/${encodeURIComponent(id)}`);
  });
}

export function useRunScheduleNow() {
  return useScheduleMutation(async (id: string): Promise<RunNowResult> => {
    const raw = await request<unknown>(
      "POST",
      `/schedules/${encodeURIComponent(id)}/run-now`,
      {},
    );
    const r = isRecord(raw) ? raw : {};
    const tick = isRecord(r.tick) ? r.tick : null;
    const num = (v: unknown): number => (typeof v === "number" ? v : 0);
    return {
      status: typeof r.status === "string" ? r.status : "",
      scheduleId: typeof r.schedule_id === "string" ? r.schedule_id : id,
      tick: tick
        ? {
            checked: num(tick.checked),
            materialised: num(tick.materialised),
            collapsed: num(tick.collapsed),
            failed: num(tick.failed),
          }
        : null,
      run: isRecord(r.run) ? (r.run as unknown as ScheduleRunWire) : null,
      projectId: typeof r.project_id === "string" ? r.project_id : null,
    };
  });
}
