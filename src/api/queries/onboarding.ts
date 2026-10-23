/**
 * React Query layer for the first-run wizard (Round 6).
 *
 * Wire shapes mirror `backend/app/routers/onboarding.py` exactly
 * (snake_case and all):
 *
 * - `GET  /onboarding/system-check` -> `{ checks: SystemCheckWire[],
 *    overall, platform, credential_store }` — every check is a REAL
 *    probe (WebView2 registry/disk lookup on Windows, ffmpeg +
 *    loudnorm probe, disk_usage, live health endpoints). Statuses:
 *    "pass" | "fail" | "warn" | "unverified" — "unverified" means the
 *    check cannot run on this OS and is NEVER presented as a pass.
 * - `GET  /onboarding/status` -> completion flag + honest key states:
 *    a key is `verified` only when the provider test-connection last
 *    returned ok; a stored-but-untested key is `saved_unverified`
 *    ("saved, not yet verified"). Key entry itself goes through the
 *    existing providers hooks (`useSaveProviderKey` / `useTestProvider`
 *    in ./providers) — there is no second key path.
 * - `POST /onboarding/complete` body `{ test_video: "skip" | "run" }` —
 *    skipping the test video is a first-class completion choice.
 * - `POST /onboarding/test-video` body `{ channel_id,
 *    niche_profile_id, topic? }` -> creates a ~60s project
 *    (target_minutes 1) through the existing produce path; the response
 *    says `queued` or honestly `created_not_queued`.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../types";
import { getBackendConnection } from "../tauri";

// ── Wire types ────────────────────────────────────────────────────────────

export type CheckStatusWire = "pass" | "fail" | "warn" | "unverified";

export interface SystemCheckWire {
  id: string;
  label: string;
  status: CheckStatusWire;
  detail: string;
  measured: Record<string, unknown>;
}

export interface CredentialStoreWire {
  platform: string;
  backend: string;
  available: boolean;
  windows_credential_manager: boolean;
}

export interface SystemCheckResultWire {
  checks: SystemCheckWire[];
  overall: string;
  platform: string;
  credential_store: CredentialStoreWire;
}

export type KeyStateWire =
  | "verified"
  | "saved_unverified"
  | "saved_test_failed"
  | "missing"
  | "store_unavailable";

export interface KeyStatusWire {
  provider: string;
  state: KeyStateWire;
  configured: boolean;
  verified: boolean;
  detail: string;
  key_last4: string | null;
  last_test_status: string | null;
}

export interface YoutubeStatusWire {
  state: string;
  detail: string;
  connected: boolean;
  connected_channels: { id: string | null; name: string | null }[];
  client_configured: boolean;
  verified: boolean;
}

export interface OnboardingStatusWire {
  completed: boolean;
  completed_at: number | null;
  test_video_choice: string | null;
  keys: { gemini: KeyStatusWire; pixabay: KeyStatusWire };
  youtube: YoutubeStatusWire;
  credential_store: CredentialStoreWire;
}

export interface TestVideoResultWire {
  status: "queued" | "created_not_queued";
  project_id: string;
  topic: string;
  target_minutes: number;
  estimated_duration_s: number;
  job_ids: unknown[];
  detail: string;
}

// ── Minimal request helper (mirrors ./providers) ──────────────────────────

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

// ── Queries / mutations ───────────────────────────────────────────────────

export function useSystemCheck() {
  return useQuery({
    queryKey: ["onboarding", "system-check"],
    queryFn: async () => request<SystemCheckResultWire>("GET", "/onboarding/system-check"),
    retry: 0,
    staleTime: 30_000,
  });
}

export function useOnboardingStatus() {
  return useQuery({
    queryKey: ["onboarding", "status"],
    queryFn: async () => request<OnboardingStatusWire>("GET", "/onboarding/status"),
    retry: 1,
  });
}

export function useCompleteOnboarding() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (testVideo: "skip" | "run") =>
      request<{ completed: boolean; test_video: string }>(
        "POST",
        "/onboarding/complete",
        { test_video: testVideo },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["onboarding"] });
    },
  });
}

export function useStartTestVideo() {
  return useMutation({
    mutationFn: async (vars: {
      channelId: string;
      nicheProfileId: string;
      topic?: string;
    }): Promise<TestVideoResultWire> =>
      request<TestVideoResultWire>("POST", "/onboarding/test-video", {
        channel_id: vars.channelId,
        niche_profile_id: vars.nicheProfileId,
        topic: vars.topic,
      }),
  });
}
