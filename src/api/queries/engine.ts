/**
 * Engine control — the STOP ALL / Pause All / Resume wire.
 *
 * The backend has always honoured the persisted execution flags
 * (`execution.stop_all` / `execution.pause_all`); patch v22 added the
 * HTTP routes (`app/routers/engine.py`) and this module is their
 * client. Before v22 the title-bar STOP ALL button was a visual
 * placeholder precisely because this wire did not exist.
 *
 * - `GET  /api/v1/engine/status`  — { pause_all, stop_all, service_attached }
 * - `POST /api/v1/engine/stop-all` — set both flags + cancel waiting jobs
 * - `POST /api/v1/engine/pause`    — set pause_all only
 * - `POST /api/v1/engine/resume`   — clear both flags + restart the
 *   execution service loops (clearing alone does not revive a runner
 *   that exited on stop_all)
 *
 * Like `agents.ts`, reads/writes here use a raw fetch against the
 * shared backend connection because the shared `ApiClient` has no
 * engine methods.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getBackendConnection } from "../tauri";
import { ApiError } from "../types";

export interface EngineStatus {
  pauseAll: boolean;
  stopAll: boolean;
  serviceAttached: boolean;
}

async function rawRequest(path: string, method: "GET" | "POST"): Promise<unknown> {
  const conn = await getBackendConnection();
  const baseUrl = conn.baseUrl.replace(/\/+$/, "");
  let res: Response;
  try {
    res = await fetch(`${baseUrl}${path}`, {
      method,
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
  const text = await res.text().catch(() => "");
  let body: unknown;
  try {
    body = text ? (JSON.parse(text) as unknown) : undefined;
  } catch {
    body = text;
  }
  const ok = typeof res.ok === "boolean" ? res.ok : res.status >= 200 && res.status < 300;
  if (!ok) {
    throw new ApiError({
      code: `HTTP_${res.status}`,
      message: `Request failed (${res.status})`,
      status: res.status,
      retryable: res.status >= 500,
    });
  }
  return body;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

export async function fetchEngineStatus(): Promise<EngineStatus> {
  const raw = await rawRequest("/engine/status", "GET");
  const rec = isRecord(raw) ? raw : {};
  return {
    pauseAll: rec.pause_all === true,
    stopAll: rec.stop_all === true,
    serviceAttached: rec.service_attached === true,
  };
}

export function useEngineStatus() {
  return useQuery({
    queryKey: ["engine", "status"],
    queryFn: fetchEngineStatus,
    retry: 1,
    refetchInterval: 15_000,
  });
}

function useEngineAction(path: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      await rawRequest(path, "POST");
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["engine"] });
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
}

export function useStopAll() {
  return useEngineAction("/engine/stop-all");
}

export function usePauseAll() {
  return useEngineAction("/engine/pause");
}

export function useResumeEngine() {
  return useEngineAction("/engine/resume");
}
