/**
 * React Query hooks for providers + app settings (Round 2 binding).
 *
 * Wire shapes are defined LOCALLY here (per shared conventions) because
 * they mirror the backend exactly — snake_case fields and all:
 *
 * - `GET    /providers`              -> `{ items: ProviderWire[], next_cursor }`
 * - `PUT    /providers/{id}/key`     body `{ value }`  (WRITE-ONLY: the
 *   backend stores the key in the OS credential store and only ever
 *   returns `configured` + `key_last4` metadata — never the value.)
 * - `DELETE /providers/{id}/key`     -> `{ provider, configured: false, … }`
 * - `POST   /providers/{id}/test`    -> Round 3: `{ provider, status, … }`
 *   with `status: "ok" | "not_configured" | "error"` (+ `category` on
 *   error, `detail` human-readable). `ok` is a genuine verification,
 *   `not_configured` means no key is saved. (The pre-Round-3 stub
 *   returned `{ ok: true, detail: "stub — …" }` without contacting the
 *   provider; that shape is still tolerated but never called verified.)
 * - `GET    /settings`               -> flat `{ key: value }` map
 * - `PUT    /settings`               body `{ values }`; the backend
 *   rejects secret-looking keys (containing key/token/secret) with
 *   422 `secret_in_settings`. Never send key material here.
 * - `GET    /settings/budget`        -> `BudgetSettingsWire` (Round 3)
 * - `PUT    /settings/budget`        same shape; validated server-side,
 *   bad values come back as the standard error envelope.
 * - `GET    /settings/privacy`       -> `PrivacySettingsWire` (Round 6)
 * - `PUT    /settings/privacy`       body `{ remove_data_on_uninstall }`;
 *   the backend persists the flag (`privacy.remove_data_on_uninstall`,
 *   default OFF) and writes/removes the `uninstall-purge.marker` file
 *   in the data root that the uninstaller checks.
 *
 * This module talks to the backend with its own small fetch helper
 * (same token header + error envelope as `ApiClient`) instead of
 * `ApiClient.putProviderKey`, because that Round 1 method posts
 * `{ key }` while `backend/app/routers/providers.py` expects
 * `{ value }` — using it would 422 every save. `client.ts` is owned
 * by another worker this round; the mismatch is flagged in the
 * Round 2 report.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../types";
import { getBackendConnection } from "../tauri";

// ── Wire types (backend snake_case, exactly as emitted) ────────────────────

export interface ProviderWire {
  id: string;
  /** True when a key is stored in the credential store. NOT proof the
   *  provider was contacted — no live verification exists yet. */
  configured: boolean;
  has_key: boolean;
  /** Last-4 hint only; the full key is never returned. */
  key_last4: string | null;
}

export interface ProviderKeyResult {
  provider: string;
  configured: boolean;
  key_last4: string | null;
}

export type ProviderTestStatusWire = "ok" | "not_configured" | "error";

export interface ProviderTestWire {
  provider: string;
  /** Round 3 status. Absent on the pre-Round-3 stub response. */
  status?: ProviderTestStatusWire;
  /** Failure category when `status` is "error" (e.g. "auth"). */
  category?: string;
  /** Legacy boolean from the pre-Round-3 shape. */
  ok?: boolean;
  detail?: string;
  message?: string;
}

/** `GET|PUT /settings/budget` wire shape (backend snake_case). */
export interface BudgetSettingsWire {
  per_video_cap_usd: number;
  daily_cap_usd: number;
  monthly_cap_usd: number;
  warn_pct: number;
  soft_pct: number;
}

/** `GET /settings` returns a flat key/value map (non-secret values only). */
export type SettingsMap = Record<string, unknown>;

export interface SettingsPutResult {
  updated: string[];
  settings: SettingsMap;
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

// ── Providers ──────────────────────────────────────────────────────────────

async function fetchProviders(): Promise<ProviderWire[]> {
  const raw = await request<unknown>("GET", "/providers");
  const items: unknown[] = isRecord(raw) && Array.isArray(raw.items) ? raw.items : [];
  return items.filter(isRecord).map((item) => ({
    id: typeof item.id === "string" ? item.id : "",
    configured: Boolean(item.configured ?? item.has_key),
    has_key: Boolean(item.has_key ?? item.configured),
    key_last4: typeof item.key_last4 === "string" ? item.key_last4 : null,
  }));
}

export function useProviders() {
  return useQuery({
    queryKey: ["providers"],
    queryFn: fetchProviders,
    retry: 1,
  });
}

export function useSaveProviderKey() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (vars: { providerId: string; value: string }) => {
      // Body shape per backend/app/routers/providers.py KeyBody: { value }.
      // Write-only: the response carries metadata only, never the key.
      return request<ProviderKeyResult>(
        "PUT",
        `/providers/${encodeURIComponent(vars.providerId)}/key`,
        { value: vars.value },
      );
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["providers"] });
      // The First-Run wizard's key cards read their state from the
      // onboarding status (["onboarding", "status"]), NOT from
      // ["providers"] — without this invalidation a saved key leaves
      // that card stuck on "Not saved" with its Test button disabled
      // until a full reload.
      void queryClient.invalidateQueries({ queryKey: ["onboarding"] });
    },
  });
}

export function useRemoveProviderKey() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (providerId: string) =>
      request<ProviderKeyResult>(
        "DELETE",
        `/providers/${encodeURIComponent(providerId)}/key`,
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["providers"] });
      void queryClient.invalidateQueries({ queryKey: ["onboarding"] });
    },
  });
}

export function useTestProvider() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (providerId: string): Promise<ProviderTestWire> => {
      const raw = await request<unknown>(
        "POST",
        `/providers/${encodeURIComponent(providerId)}/test`,
      );
      const r = isRecord(raw) ? raw : {};
      const status =
        r.status === "ok" || r.status === "not_configured" || r.status === "error"
          ? r.status
          : undefined;
      return {
        provider: typeof r.provider === "string" ? r.provider : providerId,
        status,
        category: typeof r.category === "string" ? r.category : undefined,
        ok: typeof r.ok === "boolean" ? r.ok : undefined,
        detail: typeof r.detail === "string" ? r.detail : undefined,
        message: typeof r.message === "string" ? r.message : undefined,
      };
    },
    onSuccess: () => {
      // A completed test changes the read models on both surfaces:
      // GET /providers gains status/connected/last_test, and the
      // onboarding status flips the key to verified (or test-failed).
      void queryClient.invalidateQueries({ queryKey: ["providers"] });
      void queryClient.invalidateQueries({ queryKey: ["onboarding"] });
    },
  });
}

// ── Settings (non-secret only) ─────────────────────────────────────────────

export function useSettings() {
  return useQuery({
    queryKey: ["settings"],
    queryFn: async () => {
      const raw = await request<unknown>("GET", "/settings");
      return (isRecord(raw) ? raw : {}) as SettingsMap;
    },
    retry: 1,
  });
}

export function useUpdateSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (values: SettingsMap) =>
      request<SettingsPutResult>("PUT", "/settings", { values }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["settings"] });
    },
  });
}

// ── Budget settings — GET|PUT /settings/budget (Round 3) ──────────────────

function toBudgetSettingsWire(raw: unknown): BudgetSettingsWire {
  const r = isRecord(raw) ? raw : {};
  const num = (camel: string, snake: string): number => {
    const v = r[camel] ?? r[snake];
    return typeof v === "number" ? v : 0;
  };
  return {
    per_video_cap_usd: num("perVideoCapUsd", "per_video_cap_usd"),
    daily_cap_usd: num("dailyCapUsd", "daily_cap_usd"),
    monthly_cap_usd: num("monthlyCapUsd", "monthly_cap_usd"),
    warn_pct: num("warnPct", "warn_pct"),
    soft_pct: num("softPct", "soft_pct"),
  };
}

export function useBudgetSettings() {
  return useQuery({
    queryKey: ["settings", "budget"],
    queryFn: async (): Promise<BudgetSettingsWire> =>
      toBudgetSettingsWire(await request<unknown>("GET", "/settings/budget")),
    retry: 1,
  });
}

export function useUpdateBudgetSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (values: BudgetSettingsWire): Promise<BudgetSettingsWire> => {
      const raw = await request<unknown>("PUT", "/settings/budget", values);
      // The PUT echoes the saved settings per contract; if a build
      // returns an empty/envelope body instead, the accepted input is
      // the saved state.
      return isRecord(raw) &&
        (typeof raw.per_video_cap_usd === "number" ||
          typeof raw.perVideoCapUsd === "number")
        ? toBudgetSettingsWire(raw)
        : values;
    },
    onSuccess: (saved) => {
      queryClient.setQueryData(["settings", "budget"], saved);
      void queryClient.invalidateQueries({ queryKey: ["settings", "budget"] });
    },
  });
}

// ── Privacy / uninstall data policy — GET|PUT /settings/privacy (Round 6) ──

/** `GET|PUT /settings/privacy` wire shape (backend snake_case). */
export interface PrivacySettingsWire {
  /** The opt-in flag. Default false: uninstalling preserves data. */
  remove_data_on_uninstall: boolean;
  /** Settings-table key the flag is persisted under. */
  settings_key: string;
  /** Marker filename the uninstaller hook checks for. */
  marker_filename: string;
  /** Whether the marker file is actually present in the data root. */
  marker_present: boolean;
  /** Always true — states the default policy in the wire contract. */
  data_preserved_by_default: boolean;
}

function toPrivacySettingsWire(raw: unknown): PrivacySettingsWire {
  const r = isRecord(raw) ? raw : {};
  return {
    remove_data_on_uninstall: r.remove_data_on_uninstall === true,
    settings_key:
      typeof r.settings_key === "string"
        ? r.settings_key
        : "privacy.remove_data_on_uninstall",
    marker_filename:
      typeof r.marker_filename === "string"
        ? r.marker_filename
        : "uninstall-purge.marker",
    marker_present: r.marker_present === true,
    data_preserved_by_default: r.data_preserved_by_default !== false,
  };
}

export function usePrivacySettings() {
  return useQuery({
    queryKey: ["settings", "privacy"],
    queryFn: async (): Promise<PrivacySettingsWire> =>
      toPrivacySettingsWire(await request<unknown>("GET", "/settings/privacy")),
    retry: 1,
  });
}

export function useUpdatePrivacySettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (removeDataOnUninstall: boolean): Promise<PrivacySettingsWire> => {
      const raw = await request<unknown>("PUT", "/settings/privacy", {
        remove_data_on_uninstall: removeDataOnUninstall,
      });
      return toPrivacySettingsWire(raw);
    },
    onSuccess: (saved) => {
      queryClient.setQueryData(["settings", "privacy"], saved);
      void queryClient.invalidateQueries({ queryKey: ["settings", "privacy"] });
      void queryClient.invalidateQueries({ queryKey: ["settings"] });
    },
  });
}
