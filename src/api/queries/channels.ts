/**
 * React Query hooks for channels (Round 2 binding).
 *
 * Wire shapes are defined LOCALLY here (per shared conventions) because
 * they mirror the backend exactly — `backend/app/routers/channels.py`
 * returns raw `channels` table rows (snake_case) with every column whose
 * name suggests credential material stripped server-side:
 *
 * - `GET    /channels`                    -> `{ items: ChannelWire[], next_cursor: null }`
 * - `POST   /channels/connect`            -> Round 3 contract:
 *   · YouTube client credentials NOT configured → HTTP 409 error
 *     envelope, code `youtube_client_not_configured`, `details.steps`
 *     = ordered plain-language Google Cloud setup steps. The screen
 *     renders those steps as a setup panel (see
 *     {@link extractYoutubeSetupSteps}).
 *   · Configured → 202/flow response: the OAuth flow has STARTED
 *     (system browser + PKCE); completion arrives later (channel list
 *     refetch / WS). A started flow is NOT a connected channel —
 *     callers must surface it as pending, never as success.
 * - `POST   /channels/{id}/revalidate`    -> `{ id, status: "ok" }`
 *   STUB: returns `ok` without a real token refresh yet (same team).
 * - `DELETE /channels/{id}`              -> `{ id, status: "disconnected" }`
 *   Soft disconnect: the row is marked `disconnected` + soft-deleted
 *   (so it leaves the list), history is kept; the full flow revokes the
 *   grant at Google first and deletes the local credential second.
 *   Already-published videos are untouched.
 *
 * Channel `status` vocabulary (schema CHECK): `active | paused |
 * disconnected | at_risk`. The token layer can also report
 * `needs_reauth` (see providers/youtube/tokens.py); the screen renders
 * state generically so either vocabulary displays correctly.
 *
 * NOTE: the backend payload carries NO subscriber/video counts and no
 * publish mode — the screen must not invent them.
 *
 * This module uses its own small fetch helper (same token header + error
 * envelope as `ApiClient`) because `ApiClient` has no revalidate method;
 * `client.ts` is owned by another worker this round.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../types";
import { getBackendConnection } from "../tauri";

// ── Wire types (backend snake_case, exactly as emitted) ────────────────────

export interface ChannelWire {
  id: string;
  youtube_channel_id?: string;
  title?: string;
  handle?: string | null;
  default_niche_profile_id?: string | null;
  /** Round 4: resolved server-side from the niche profile (channels.py). */
  default_niche_name?: string | null;
  default_voice_id?: string | null;
  default_voice_style?: string | null;
  language?: string;
  country?: string | null;
  /** `active | paused | disconnected | at_risk` (schema CHECK). */
  status?: string;
  /** Alternate state field used by the frontend types; rendered generically. */
  state?: string;
  daily_upload_cap?: number | null;
  monthly_budget_micros?: number | null;
  /** Round 5: per-channel production quality tier (`draft` |
   * `master_1080p` | `master_1440p_upscale`); always present from the
   * backend (defaults to `draft`). Writable via PATCH /channels/{id}. */
  quality_tier?: string;
  /** Only present if a payload source carries it; the DB row does not. */
  publish_mode?: string;
  /** Audit overlay fields — only present if the payload carries them. */
  audit_status?: string | null;
  audit_private_only?: boolean | number | null;
  created_at?: number | string;
  updated_at?: number | string;
}

export interface ConnectChannelResult {
  status: string;
  /** Flow identifier when the backend returns one (`flow_id`/`flowId`). */
  flowId?: string;
  /** Google sign-in URL the backend opened/returned, when present. */
  authUrl?: string;
}

/**
 * `GET /channels/connect/{flow_id}` — public flow record (no secrets).
 * Non-terminal: `awaiting_callback`, `exchanging`. Terminal success:
 * a `channel_id` is set (status `needs_audit_check` — tokens stored,
 * channel row created). Terminal failure: `connect_failed` with
 * `error_message`.
 */
export interface ConnectFlowStatusWire {
  flow_id: string;
  status: string;
  channel_id?: string | null;
  youtube_channel_id?: string | null;
  error_code?: string | null;
  error_message?: string | null;
}

export function isFlowTerminal(flow: ConnectFlowStatusWire): boolean {
  return flow.status === "connect_failed" || Boolean(flow.channel_id);
}

export function isFlowSuccess(flow: ConnectFlowStatusWire): boolean {
  return Boolean(flow.channel_id) && flow.status !== "connect_failed";
}

/** True for the Round 3 "YouTube client not configured" 409. */
export function isYoutubeClientNotConfigured(err: unknown): boolean {
  if (!(err instanceof ApiError)) return false;
  if (err.code === "youtube_client_not_configured") return true;
  // Defensive: a 409 from connect that carries setup steps is the
  // same state even if a build phrases the code differently.
  return err.status === 409 && extractYoutubeSetupSteps(err).length > 0;
}

/**
 * Pulls the ordered Google Cloud setup steps out of a connect error's
 * `details`. The landed backend puts them at `details.setup_steps`
 * (the task contract said `details.steps` — both are read), and each
 * step is an object `{ step: number, title, detail }` which is
 * rendered as "Title — detail". Plain-string steps and
 * `text`/`description` fields are also tolerated. Returns [] when the
 * error carries no usable steps (the caller then falls back to the
 * error message alone).
 */
export function extractYoutubeSetupSteps(err: unknown): string[] {
  if (!(err instanceof ApiError)) return [];
  const details = isRecord(err.details) ? err.details : null;
  const rawSteps: unknown = details
    ? (details.steps ?? details.setup_steps)
    : Array.isArray(err.details)
      ? err.details
      : undefined;
  if (!Array.isArray(rawSteps)) return [];
  const steps: string[] = [];
  for (const item of rawSteps) {
    if (typeof item === "string" && item.trim()) {
      steps.push(item.trim());
    } else if (isRecord(item)) {
      const title = typeof item.title === "string" ? item.title.trim() : "";
      const detail =
        typeof item.detail === "string"
          ? item.detail.trim()
          : typeof item.description === "string"
            ? item.description.trim()
            : typeof item.text === "string"
              ? item.text.trim()
              : typeof item.step === "string"
                ? item.step.trim()
                : "";
      const combined = title && detail ? `${title} — ${detail}` : title || detail;
      if (combined) steps.push(combined);
    }
  }
  return steps;
}

export interface RevalidateChannelResult {
  id: string;
  status: string;
}

export interface DisconnectChannelResult {
  id: string;
  status: string;
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

// ── Channels ───────────────────────────────────────────────────────────────

async function fetchChannels(): Promise<ChannelWire[]> {
  const raw = await request<unknown>("GET", "/channels");
  const items: unknown[] = isRecord(raw) && Array.isArray(raw.items) ? raw.items : [];
  return items.filter(isRecord).map((item) => {
    const wire = item as unknown as ChannelWire;
    return { ...wire, id: typeof item.id === "string" ? item.id : "" };
  });
}

export function useChannels() {
  return useQuery({
    queryKey: ["channels"],
    queryFn: fetchChannels,
    retry: 1,
  });
}

/**
 * Starts the backend connect flow (Round 3 contract — see the header
 * comment). A 409 `youtube_client_not_configured` propagates as an
 * {@link ApiError} whose `details` carry the setup steps; a 2xx means
 * the flow STARTED and is pending completion, not connected.
 */
export function useConnectChannel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (): Promise<ConnectChannelResult> => {
      const raw = await request<unknown>("POST", "/channels/connect", {});
      const r = isRecord(raw) ? raw : {};
      const flowId = r.flow_id ?? r.flowId;
      const authUrl = r.auth_url ?? r.authUrl;
      return {
        status: typeof r.status === "string" ? r.status : "",
        flowId: typeof flowId === "string" ? flowId : undefined,
        authUrl: typeof authUrl === "string" ? authUrl : undefined,
      };
    },
    onSuccess: () => {
      // If a flow completes quickly, a refetch picks the channel up;
      // the live list also refreshes on its own interval/WS events.
      void queryClient.invalidateQueries({ queryKey: ["channels"] });
    },
  });
}

/**
 * Polls a started connect flow every 3s until it reaches a terminal
 * state (channel connected or `connect_failed`), then stops.
 */
export function useConnectFlowStatus(flowId: string | null) {
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: ["channels", "connect-flow", flowId],
    enabled: flowId !== null,
    queryFn: async (): Promise<ConnectFlowStatusWire> => {
      const raw = await request<unknown>(
        "GET",
        `/channels/connect/${encodeURIComponent(flowId ?? "")}`,
      );
      const r = isRecord(raw) ? raw : {};
      const flow: ConnectFlowStatusWire = {
        flow_id:
          typeof r.flow_id === "string" ? r.flow_id : (flowId ?? ""),
        status: typeof r.status === "string" ? r.status : "",
        channel_id: typeof r.channel_id === "string" ? r.channel_id : null,
        youtube_channel_id:
          typeof r.youtube_channel_id === "string"
            ? r.youtube_channel_id
            : null,
        error_code: typeof r.error_code === "string" ? r.error_code : null,
        error_message:
          typeof r.error_message === "string" ? r.error_message : null,
      };
      if (isFlowTerminal(flow)) {
        void queryClient.invalidateQueries({ queryKey: ["channels"] });
      }
      return flow;
    },
    refetchInterval: (query) =>
      query.state.data && isFlowTerminal(query.state.data) ? false : 3_000,
    retry: 1,
  });
}

/**
 * `GET /channels/oauth-client` -> `{ client_configured }` — whether the
 * app's own YouTube (Google Cloud) OAuth client credentials have been
 * saved into the credential store. Boolean only: the stored client
 * secret is write-only and is never returned.
 */
export interface OAuthClientStatus {
  client_configured: boolean;
}

export function useOAuthClientStatus() {
  return useQuery({
    queryKey: ["channels", "oauth-client"],
    queryFn: async (): Promise<OAuthClientStatus> => {
      const raw = await request<unknown>("GET", "/channels/oauth-client");
      return {
        client_configured:
          isRecord(raw) && raw.client_configured === true,
      };
    },
    retry: 1,
  });
}

/**
 * `PUT /channels/oauth-client` `{ client_id, client_secret }` ->
 * `{ client_configured: true }`. Saves the app's YouTube OAuth client
 * credentials into the credential store (write-only). On success the
 * channels and onboarding queries are invalidated so the
 * `youtube_client_not_configured` setup panel and the onboarding
 * YouTube status refresh.
 */
export function useSaveOAuthClient() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (vars: {
      client_id: string;
      client_secret: string;
    }): Promise<OAuthClientStatus> => {
      const raw = await request<unknown>("PUT", "/channels/oauth-client", {
        client_id: vars.client_id,
        client_secret: vars.client_secret,
      });
      return {
        client_configured:
          isRecord(raw) && raw.client_configured === true,
      };
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["channels"] });
      void queryClient.invalidateQueries({ queryKey: ["onboarding"] });
    },
  });
}

/** POST /channels/{id}/revalidate — stub on the backend (returns ok). */
export function useRevalidateChannel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: string): Promise<RevalidateChannelResult> => {
      const raw = await request<unknown>(
        "POST",
        `/channels/${encodeURIComponent(id)}/revalidate`,
      );
      return {
        id,
        status: isRecord(raw) && typeof raw.status === "string" ? raw.status : "",
      };
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["channels"] });
    },
  });
}

/**
 * DELETE /channels/{id} — soft disconnect (marks the channel
 * disconnected, keeps history, published videos untouched).
 */
export function useDisconnectChannel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: string): Promise<DisconnectChannelResult> => {
      const raw = await request<unknown>(
        "DELETE",
        `/channels/${encodeURIComponent(id)}`,
      );
      return {
        id,
        status: isRecord(raw) && typeof raw.status === "string" ? raw.status : "",
      };
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["channels"] });
    },
  });
}

// ── Niche names (for the per-channel niche badge) ──────────────────────────

/**
 * id -> name map for niche profiles, used to label a channel's
 * `default_niche_profile_id`. Keyed separately from any plain list query
 * so cache shapes never collide.
 */
export function useNicheNames() {
  return useQuery({
    queryKey: ["niches", "names"],
    queryFn: async (): Promise<Record<string, string>> => {
      const raw = await request<unknown>("GET", "/niches?limit=200");
      const items: unknown[] =
        isRecord(raw) && Array.isArray(raw.items) ? raw.items : [];
      const map: Record<string, string> = {};
      for (const item of items) {
        if (isRecord(item) && typeof item.id === "string" && typeof item.name === "string") {
          map[item.id] = item.name;
        }
      }
      return map;
    },
    retry: 1,
  });
}
