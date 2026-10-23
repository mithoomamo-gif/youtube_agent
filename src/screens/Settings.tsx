import { useEffect, useRef, useState } from "react";
import type { ComponentType } from "react";
import {
  AlertTriangle,
  ArrowUpCircle,
  Bell,
  CheckCircle2,
  Clapperboard,
  Cpu,
  Download,
  Eye,
  EyeOff,
  Image,
  Info,
  KeyRound,
  Lock,
  Mic,
  Power,
  Settings as SettingsIcon,
  Shield,
  Upload,
  Wrench,
  MonitorPlay,
  XCircle,
} from "lucide-react";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { ApiError } from "../api/types";
import type {
  BudgetSettingsWire,
  ProviderWire,
} from "../api/queries/providers";
import {
  useBudgetSettings,
  usePrivacySettings,
  useProviders,
  useRemoveProviderKey,
  useSaveProviderKey,
  useTestProvider,
  useUpdateBudgetSettings,
  useUpdatePrivacySettings,
} from "../api/queries/providers";
import {
  useBackupInfo,
  useExportBackup,
  useImportBackup,
} from "../api/queries/backup";
import type {
  ComplianceItemStatusWire,
  ComplianceItemWire,
} from "../api/queries/compliance";
import {
  useCompliance,
  useComplianceTransition,
} from "../api/queries/compliance";
import { useBackendBootstrapStore } from "../api/bootstrap";
import { autostartGet, autostartSet, isTauriRuntime } from "../api/tauri";
import {
  checkForUpdatesNow,
  installPendingUpdate,
  loadCurrentVersion,
  useUpdaterStore,
} from "../api/updater";

interface NavItem {
  label: string;
  icon: ComponentType<{ className?: string }>;
}

const NAV_ITEMS: NavItem[] = [
  { label: "General", icon: SettingsIcon },
  { label: "Channels & YouTube", icon: MonitorPlay },
  { label: "API Keys & Providers", icon: KeyRound },
  { label: "Models & Defaults", icon: Cpu },
  { label: "Voice & Audio", icon: Mic },
  { label: "Stock & Images", icon: Image },
  { label: "Automation & Guardrails", icon: Shield },
  { label: "Rendering & Performance", icon: Clapperboard },
  { label: "Notifications", icon: Bell },
  { label: "Data & Privacy", icon: Lock },
  { label: "Advanced", icon: Wrench },
  { label: "About", icon: Info },
];

const ACTIVE_NAV = "API Keys & Providers";

/**
 * Display metadata is presentation-only and keyed by the backend's
 * provider id. Everything about key state (configured / last-4) comes
 * from `GET /providers` — never from this table.
 */
interface ProviderMeta {
  name: string;
  requirement: string;
  capability: string;
  usedBy: string;
  note?: string;
  addLabel?: string;
}

const PROVIDER_META: Record<string, ProviderMeta> = {
  gemini: {
    name: "Gemini",
    requirement: "Required",
    capability: "Scripts · Research",
    usedBy: "Used by: Research, Script Writer, SEO agents",
  },
  openai: {
    name: "OpenAI",
    requirement: "Optional",
    capability: "Fallback LLM · Rewrites",
    usedBy: "Used by: Script polish and fallback generation",
  },
  anthropic: {
    name: "Anthropic",
    requirement: "Optional",
    capability: "Fallback LLM",
    usedBy: "Used by: Research and script fallback generation",
  },
  elevenlabs: {
    name: "ElevenLabs",
    requirement: "Optional",
    capability: "Premium voiceover",
    usedBy: "Used by: Voiceover agent (premium voices)",
  },
  pixabay: {
    name: "Pixabay",
    requirement: "Required for stock",
    capability: "Stock video · Images · Music",
    usedBy: "Used by: Visuals and B-roll agents",
  },
  pexels: {
    name: "Pexels",
    requirement: "Optional",
    capability: "Stock video · Images",
    usedBy: "Used by: Visuals and B-roll agents (when connected)",
    note: "Existing key only — new keys paused",
    addLabel: "Add existing key",
  },
  azure_tts: {
    name: "Azure TTS",
    requirement: "Optional",
    capability: "Text-to-speech",
    usedBy: "Used by: Voiceover agent",
  },
  google_tts: {
    name: "Google TTS",
    requirement: "Optional",
    capability: "Text-to-speech",
    usedBy: "Used by: Voiceover agent",
  },
};

function metaFor(provider: ProviderWire): ProviderMeta {
  return (
    PROVIDER_META[provider.id] ?? {
      name: provider.id,
      requirement: "Optional",
      capability: "",
      usedBy: `Used by: ${provider.id} provider`,
    }
  );
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.message} (${err.code})`;
  if (err instanceof Error) return err.message;
  return "Something went wrong. Please try again.";
}

/**
 * Round 3 test outcomes, driven by the backend's explicit `status`:
 * `ok` = genuinely verified, `not_configured` = no key saved, `error`
 * = the check ran and failed (with a `category`). `unverified` covers
 * responses with no status at all (the pre-Round-3 stub) — those are
 * never presented as a verified connection.
 */
type TestOutcome =
  | { kind: "verified"; detail?: string }
  | { kind: "not_configured"; detail?: string }
  | { kind: "error"; category?: string; detail?: string }
  | { kind: "unverified"; detail?: string }
  | { kind: "request_error"; message: string };

/**
 * Plain-language rendering of backend test-failure categories
 * (the backend's section-28 taxonomy: invalid_key / wrong_project /
 * quota_exhausted / insufficient_credits / network / timeout /
 * model_not_available; a few generic aliases are tolerated too).
 */
const TEST_CATEGORY_TEXT: Record<string, string> = {
  invalid_key: "the saved key was rejected as invalid",
  wrong_project:
    "the key belongs to a different project than the one the provider expects",
  quota_exhausted: "the provider account is out of quota",
  insufficient_credits: "the provider account doesn't have enough credits",
  network: "the provider couldn't be reached (network problem)",
  timeout: "the provider took too long to respond (timed out)",
  model_not_available: "the provider's model isn't available for this key",
  auth: "authentication failed — the provider rejected the saved key",
  authentication: "authentication failed — the provider rejected the saved key",
  rate_limit: "the provider is rate-limiting requests right now",
  quota: "the provider account is out of quota",
  server: "the provider returned a server error",
  provider_error: "the provider returned an error",
  not_found: "the provider or model was not found",
};

function categoryText(category?: string): string {
  if (!category) return "the check failed";
  const known = TEST_CATEGORY_TEXT[category.toLowerCase()];
  if (known) return known;
  return category.replace(/_/g, " ");
}

function Toggle({
  checked,
  onChange,
  label,
  disabled = false,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:pointer-events-none disabled:opacity-50 ${
        checked ? "bg-accent" : "bg-raised"
      }`}
    >
      <span
        className={`absolute top-0.5 h-5 w-5 rounded-full bg-white transition-all ${
          checked ? "left-[22px]" : "left-0.5"
        }`}
      />
    </button>
  );
}

type CardMode = "view" | "add" | "replace" | "confirm-remove";

function ProviderCard({ provider }: { provider: ProviderWire }) {
  const meta = metaFor(provider);
  const [mode, setMode] = useState<CardMode>("view");
  // The typed key lives in state ONLY while the add/replace form is
  // open. It is cleared on save success, on cancel, and never copied
  // from (or back into) the stored-key display, which is built purely
  // from the refetched `key_last4` hint.
  const [draft, setDraft] = useState("");
  const [showDraft, setShowDraft] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [testOutcome, setTestOutcome] = useState<TestOutcome | null>(null);

  const saveKey = useSaveProviderKey();
  const removeKey = useRemoveProviderKey();
  const testProvider = useTestProvider();

  const maskedKey = provider.configured
    ? provider.key_last4
      ? `••••••••••••${provider.key_last4}`
      : "••••••••••••"
    : null;

  const closeForm = () => {
    setDraft("");
    setShowDraft(false);
    setFormError(null);
    setMode("view");
  };

  const handleSave = () => {
    const value = draft.trim();
    if (!value) {
      setFormError("Paste a key first — empty keys can't be saved.");
      return;
    }
    setFormError(null);
    saveKey.mutate(
      { providerId: provider.id, value },
      {
        onSuccess: () => {
          // Clear the typed key immediately; the card's saved state
          // now comes only from the invalidated ["providers"] refetch.
          setDraft("");
          setShowDraft(false);
          setMode("view");
        },
        onError: (err) => {
          // The previously saved key (if any) is untouched — the card
          // above still shows the refetched state, not this draft.
          setFormError(`Couldn't save the new key: ${errorText(err)}`);
        },
      },
    );
  };

  const handleRemove = () => {
    setFormError(null);
    removeKey.mutate(provider.id, {
      onSuccess: () => setMode("view"),
      onError: (err) => {
        setFormError(`Couldn't remove the key: ${errorText(err)}`);
      },
    });
  };

  const handleTest = () => {
    testProvider.mutate(provider.id, {
      onSuccess: (result) => {
        const detail = result.detail ?? result.message;
        // Round 3: the explicit status is authoritative. A response
        // with NO status is the old stub shape (ok:true without
        // contacting the provider) — surface it as unverified.
        if (result.status === "ok") {
          setTestOutcome({ kind: "verified", detail });
        } else if (result.status === "not_configured") {
          setTestOutcome({ kind: "not_configured", detail });
        } else if (result.status === "error") {
          setTestOutcome({ kind: "error", category: result.category, detail });
        } else if (result.ok) {
          setTestOutcome({ kind: "unverified", detail });
        } else {
          setTestOutcome({ kind: "error", detail });
        }
      },
      onError: (err) => {
        setTestOutcome({ kind: "request_error", message: errorText(err) });
      },
    });
  };

  const lastTestSummary =
    testOutcome === null
      ? "Not tested yet this session"
      : testOutcome.kind === "verified"
        ? "Last test: verified OK (this session)"
        : testOutcome.kind === "not_configured"
          ? "Last test: no key saved"
          : testOutcome.kind === "error"
            ? `Last test: failed — ${categoryText(testOutcome.category)}`
            : testOutcome.kind === "unverified"
              ? "Last test: not verified (backend stub)"
              : "Last test: couldn't run (this session)";

  const editing = mode === "add" || mode === "replace";

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-base font-semibold text-ink">{meta.name}</h3>
        <span className="text-xs text-muted">{meta.requirement}</span>
        {provider.configured ? (
          <Badge tone="green">
            <CheckCircle2 className="h-3 w-3" />
            Key saved
          </Badge>
        ) : (
          <Badge tone="gray">
            <XCircle className="h-3 w-3" />
            No key saved
          </Badge>
        )}
        {testOutcome?.kind === "verified" ? (
          <Badge tone="green">
            <CheckCircle2 className="h-3 w-3" />
            Verified
          </Badge>
        ) : null}
        {meta.note ? <Badge tone="amber">{meta.note}</Badge> : null}
        {meta.capability ? (
          <span className="ml-auto text-xs text-accent2">{meta.capability}</span>
        ) : null}
      </div>

      <p className="mt-2 text-sm text-ink2">{meta.usedBy}</p>

      {maskedKey ? (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input
            type="text"
            readOnly
            value={maskedKey}
            aria-label={`${meta.name} API key (masked)`}
            className="w-64 rounded-md border border-line bg-raised px-3 py-2 font-mono text-sm text-ink2"
          />
          {provider.key_last4 ? (
            <span className="font-mono text-xs text-muted tnum">
              last-4: {provider.key_last4}
            </span>
          ) : (
            <span className="text-xs text-muted">Last 4 unavailable</span>
          )}
        </div>
      ) : (
        <p className="mt-3 text-sm text-muted">No key saved yet.</p>
      )}

      {editing ? (
        <div className="mt-4">
          <div className="flex flex-wrap items-center gap-2">
            <input
              type={showDraft ? "text" : "password"}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              autoComplete="off"
              autoFocus
              placeholder={
                mode === "replace" ? "Paste the new key" : "Paste API key"
              }
              aria-label={`${meta.name} new API key`}
              className="w-64 rounded-md border border-line bg-raised px-3 py-2 font-mono text-sm text-ink"
            />
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setShowDraft((prev) => !prev)}
              aria-label={showDraft ? "Hide typed key" : "Show typed key"}
            >
              {showDraft ? (
                <EyeOff className="h-3.5 w-3.5" />
              ) : (
                <Eye className="h-3.5 w-3.5" />
              )}
              {showDraft ? "Hide" : "Show"}
            </Button>
            <Button
              variant="primary"
              size="sm"
              disabled={!draft.trim() || saveKey.isPending}
              onClick={handleSave}
            >
              {saveKey.isPending ? "Saving…" : "Save key"}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={saveKey.isPending}
              onClick={closeForm}
            >
              Cancel
            </Button>
          </div>
          {formError ? (
            <p className="mt-2 text-sm text-danger">{formError}</p>
          ) : null}
          <p className="mt-1 text-xs text-muted">
            The key is sent once to this PC's credential store and is
            never shown again — only its last 4 characters.
          </p>
        </div>
      ) : mode === "confirm-remove" ? (
        <div className="mt-4">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm text-ink">
              Remove the saved key for {meta.name}?
            </span>
            <Button
              variant="danger"
              size="sm"
              disabled={removeKey.isPending}
              onClick={handleRemove}
            >
              {removeKey.isPending ? "Removing…" : "Remove key"}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={removeKey.isPending}
              onClick={() => setMode("view")}
            >
              Cancel
            </Button>
          </div>
          {formError ? (
            <p className="mt-2 text-sm text-danger">{formError}</p>
          ) : null}
        </div>
      ) : (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          {provider.configured ? (
            <>
              <Button
                variant="outline"
                size="sm"
                disabled={testProvider.isPending}
                onClick={handleTest}
              >
                {testProvider.isPending ? "Testing…" : "Test"}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setFormError(null);
                  setMode("replace");
                }}
              >
                Replace
              </Button>
              <Button
                variant="danger"
                size="sm"
                onClick={() => {
                  setFormError(null);
                  setMode("confirm-remove");
                }}
              >
                Remove
              </Button>
            </>
          ) : (
            <Button
              variant="primary"
              size="sm"
              onClick={() => {
                setFormError(null);
                setMode("add");
              }}
            >
              {meta.addLabel ?? "Add key"}
            </Button>
          )}
          <span className="ml-auto text-xs text-muted">{lastTestSummary}</span>
        </div>
      )}

      {testOutcome ? (
        <div className="mt-3">
          {testOutcome.kind === "unverified" ? (
            <p className="flex items-start gap-1.5 text-sm text-warn">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              Test not verified — the backend returned a response
              without a test status and did not confirm it contacted{" "}
              {meta.name}, so this key has not been verified.
            </p>
          ) : testOutcome.kind === "verified" ? (
            <p className="flex items-start gap-1.5 text-sm text-ok">
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
              Test verified — the backend reached {meta.name} and the
              saved key works.
            </p>
          ) : testOutcome.kind === "not_configured" ? (
            <p className="flex items-start gap-1.5 text-sm text-warn">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              No key saved — the backend has no key for {meta.name} to
              test. Add one first, then test again.
            </p>
          ) : testOutcome.kind === "error" ? (
            <p className="flex items-start gap-1.5 text-sm text-danger">
              <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
              Test failed — {categoryText(testOutcome.category)}.
            </p>
          ) : (
            <p className="text-sm text-danger">
              Test couldn't run: {testOutcome.message}
            </p>
          )}
          {"detail" in testOutcome && testOutcome.detail ? (
            <details className="mt-1">
              <summary className="cursor-pointer text-xs text-muted">
                Details
              </summary>
              <pre className="mt-1 whitespace-pre-wrap break-all rounded-md bg-raised p-2 font-mono text-xs text-ink2">
                {testOutcome.detail}
              </pre>
            </details>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}

function ProviderGroup({
  title,
  providers,
}: {
  title: string;
  providers: ProviderWire[];
}) {
  if (providers.length === 0) return null;
  return (
    <section>
      <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-muted">
        {title}
      </h2>
      <div className="space-y-4">
        {providers.map((provider) => (
          <ProviderCard key={provider.id} provider={provider} />
        ))}
      </div>
    </section>
  );
}

// ── Budget settings (GET|PUT /settings/budget) ────────────────────────────

/** Editable string form of the wire budget settings. */
interface BudgetDraft {
  perVideoCap: string;
  dailyCap: string;
  monthlyCap: string;
  warnPct: string;
  softPct: string;
}

function draftFromWire(wire: BudgetSettingsWire): BudgetDraft {
  return {
    perVideoCap: wire.per_video_cap_usd.toFixed(2),
    dailyCap: wire.daily_cap_usd.toFixed(2),
    monthlyCap: wire.monthly_cap_usd.toFixed(2),
    warnPct: String(wire.warn_pct),
    softPct: String(wire.soft_pct),
  };
}

function draftsEqual(a: BudgetDraft, b: BudgetDraft): boolean {
  return (
    a.perVideoCap === b.perVideoCap &&
    a.dailyCap === b.dailyCap &&
    a.monthlyCap === b.monthlyCap &&
    a.warnPct === b.warnPct &&
    a.softPct === b.softPct
  );
}

/**
 * First validation problem with the draft, or null when it is saveable.
 * Mirrors the backend's rules (422 `budget_policy_invalid`): caps must
 * be positive, percentages must be whole numbers with
 * 0 &lt; warn &lt; soft &lt; 100. The server stays the final authority —
 * its rejection message is shown verbatim if it still refuses a save.
 */
function budgetDraftError(draft: BudgetDraft): string | null {
  const caps: Array<[string, string]> = [
    ["Per-video cap", draft.perVideoCap],
    ["Daily cap", draft.dailyCap],
    ["Monthly cap", draft.monthlyCap],
  ];
  for (const [label, value] of caps) {
    const parsed = Number(value);
    if (value.trim() === "" || !Number.isFinite(parsed) || parsed <= 0) {
      return `${label} must be more than $0.00.`;
    }
  }
  const pcts: Array<[string, string]> = [
    ["Warn at", draft.warnPct],
    ["Soft stop at", draft.softPct],
  ];
  for (const [label, value] of pcts) {
    const parsed = Number(value);
    if (
      value.trim() === "" ||
      !Number.isFinite(parsed) ||
      !Number.isInteger(parsed) ||
      parsed < 1 ||
      parsed > 99
    ) {
      return `${label} must be a whole percentage between 1 and 99.`;
    }
  }
  if (Number(draft.warnPct) >= Number(draft.softPct)) {
    return "“Warn at” must be lower than “Soft stop at”.";
  }
  return null;
}

function wireFromDraft(draft: BudgetDraft): BudgetSettingsWire {
  return {
    per_video_cap_usd: Number(draft.perVideoCap),
    daily_cap_usd: Number(draft.dailyCap),
    monthly_cap_usd: Number(draft.monthlyCap),
    warn_pct: Number(draft.warnPct),
    soft_pct: Number(draft.softPct),
  };
}

function BudgetField({
  id,
  label,
  description,
  value,
  onChange,
  prefix,
  suffix,
}: {
  id: string;
  label: string;
  description: string;
  value: string;
  onChange: (next: string) => void;
  prefix?: string;
  suffix?: string;
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-3">
      <div>
        <label htmlFor={id} className="text-sm font-medium text-ink">
          {label}
        </label>
        <p className="mt-0.5 text-xs text-muted">{description}</p>
      </div>
      <div className="flex items-center gap-1.5">
        {prefix ? <span className="text-sm text-muted">{prefix}</span> : null}
        <input
          id={id}
          type="text"
          inputMode="decimal"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="w-24 rounded-md border border-line bg-raised px-3 py-2 text-right font-mono text-sm text-ink tnum"
        />
        {suffix ? <span className="text-sm text-muted">{suffix}</span> : null}
      </div>
    </div>
  );
}

// ── Backup & restore (GET /settings/backup, …/export, …/import) ──────────

function BackupRestoreCard() {
  const infoQuery = useBackupInfo();
  const exportBackup = useExportBackup();
  const importBackup = useImportBackup();

  const fileInputRef = useRef<HTMLInputElement>(null);
  // The file chosen for import. Nothing is sent to the backend until
  // the user explicitly confirms the restore — choosing a file only
  // stages it.
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [exportedName, setExportedName] = useState<string | null>(null);

  const info = infoQuery.data;

  const handleExport = () => {
    setExportedName(null);
    exportBackup.mutate(undefined, {
      onSuccess: (filename) => setExportedName(filename),
    });
  };

  const handleFileChosen = (file: File | null) => {
    importBackup.reset();
    setPendingFile(file);
  };

  const handleConfirmImport = () => {
    if (!pendingFile) return;
    importBackup.mutate(pendingFile, {
      onSuccess: () => setPendingFile(null),
    });
  };

  const restored = importBackup.data;

  return (
    <Card className="mt-6 p-5">
      <h2 className="text-base font-semibold text-ink">
        Backup &amp; Restore
      </h2>
      <p className="mt-1 text-xs text-muted">
        A backup is a single zip file with your database — channels,
        schedules, projects and style profiles. API keys and sign-ins
        are never in a backup: they stay in this PC's credential store,
        so reconnect channels after restoring on a different PC.
        Competitor video files are not included either (their analysis
        and metadata are).
      </p>

      {infoQuery.isLoading ? (
        <p className="mt-3 text-sm text-muted">
          Loading backup information from the backend…
        </p>
      ) : infoQuery.isError ? (
        <div className="mt-3">
          <p className="text-sm text-danger">
            Backup information couldn't be loaded:{" "}
            {errorText(infoQuery.error)}
          </p>
          <Button
            variant="outline"
            size="sm"
            className="mt-2"
            onClick={() => void infoQuery.refetch()}
          >
            Retry
          </Button>
        </div>
      ) : info ? (
        <div className="mt-3 grid gap-4 text-sm sm:grid-cols-2">
          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">
              Included
            </h3>
            <ul className="mt-1 list-disc space-y-0.5 pl-5 text-ink2">
              {info.includes.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">
              Not included
            </h3>
            <ul className="mt-1 list-disc space-y-0.5 pl-5 text-ink2">
              {info.excludes.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}

      <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-4">
        <Button
          variant="primary"
          size="sm"
          disabled={exportBackup.isPending || info?.database_present === false}
          onClick={handleExport}
        >
          <Download className="h-3.5 w-3.5" />
          {exportBackup.isPending ? "Preparing…" : "Export backup"}
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={importBackup.isPending}
          onClick={() => fileInputRef.current?.click()}
        >
          <Upload className="h-3.5 w-3.5" />
          Import backup…
        </Button>
        <input
          ref={fileInputRef}
          type="file"
          accept=".zip,application/zip"
          className="hidden"
          aria-label="Choose a backup file to import"
          onChange={(event) => {
            handleFileChosen(event.target.files?.[0] ?? null);
            event.target.value = "";
          }}
        />
        {exportedName ? (
          <span className="flex items-center gap-1.5 text-xs font-medium text-ok">
            <CheckCircle2 className="h-3.5 w-3.5" />
            Backup downloaded: {exportedName}
          </span>
        ) : null}
      </div>

      {exportBackup.isError ? (
        <p className="mt-2 text-sm text-danger">
          Export failed: {errorText(exportBackup.error)}
        </p>
      ) : null}

      {pendingFile ? (
        <div className="mt-4 rounded-md border border-warn/50 bg-raised p-4">
          <p className="flex items-start gap-1.5 text-sm text-ink">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warn" />
            Restore “{pendingFile.name}”? This replaces ALL current
            data — channels, schedules, projects and style profiles —
            with the backup's contents. The app first keeps a safety
            copy of your current database in its backups folder, and a
            backup made by a newer version of the app will be refused
            without changing anything.
          </p>
          <div className="mt-3 flex items-center gap-2">
            <Button
              variant="danger"
              size="sm"
              disabled={importBackup.isPending}
              onClick={handleConfirmImport}
            >
              {importBackup.isPending
                ? "Restoring…"
                : "Yes, replace my data and restore"}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={importBackup.isPending}
              onClick={() => setPendingFile(null)}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : null}

      {importBackup.isError ? (
        <p className="mt-3 flex items-start gap-1.5 text-sm text-danger">
          <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
          Restore didn't happen — {errorText(importBackup.error)} Your
          current data was not changed.
        </p>
      ) : null}

      {restored?.restored ? (
        <div className="mt-3">
          <p className="flex items-start gap-1.5 text-sm text-ok">
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
            Backup restored: {restored.counts.channels} channel(s),{" "}
            {restored.counts.schedules} schedule(s) and{" "}
            {restored.counts.style_profiles} style profile(s) are now
            in the app.
          </p>
          {restored.safety_copy ? (
            <p className="mt-1 text-xs text-muted">
              Your previous database was kept as a safety copy (
              {restored.safety_copy}) in the app's backups folder.
              Channels may need reconnecting, since sign-ins are not
              part of a backup.
            </p>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}

// ── Data & Privacy: uninstall data policy (GET|PUT /settings/privacy) ─────

/**
 * "Remove all my data on uninstall" — backend-backed, applies
 * immediately (no separate Save): the backend persists the flag
 * (`privacy.remove_data_on_uninstall`, default OFF) and writes or
 * removes the `uninstall-purge.marker` file in the data folder, which
 * is what the uninstaller checks. Nothing is deleted by toggling
 * this — it only takes effect if the app is later uninstalled.
 */
function DataPrivacyCard() {
  const privacyQuery = usePrivacySettings();
  const updatePrivacy = useUpdatePrivacySettings();
  const [saveError, setSaveError] = useState<string | null>(null);

  const privacy = privacyQuery.data;
  const enabled = privacy?.remove_data_on_uninstall ?? false;

  const handleToggle = (next: boolean) => {
    setSaveError(null);
    updatePrivacy.mutate(next, {
      onError: (err) => {
        setSaveError(`Couldn't save this setting: ${errorText(err)}`);
      },
    });
  };

  return (
    <Card className="mt-6 p-5">
      <h2 className="text-base font-semibold text-ink">Data &amp; Privacy</h2>
      <p className="mt-1 text-xs text-muted">
        These settings are loaded from — and saved to — the backend as
        soon as you change them.
      </p>

      {privacyQuery.isLoading ? (
        <p className="mt-3 text-sm text-muted">
          Loading privacy settings from the backend…
        </p>
      ) : privacyQuery.isError ? (
        <div className="mt-3">
          <p className="text-sm text-danger">
            Privacy settings couldn't be loaded:{" "}
            {errorText(privacyQuery.error)}
          </p>
          <Button
            variant="outline"
            size="sm"
            className="mt-2"
            onClick={() => void privacyQuery.refetch()}
          >
            Retry
          </Button>
        </div>
      ) : privacy ? (
        <>
          <div className="mt-2 rounded-md bg-raised p-3">
            <p className="text-sm text-ink">
              By default, uninstalling YouTube Agent{" "}
              <strong>keeps your data</strong>. Your channels, projects
              and video history, Style Lab profiles, settings and
              database stay in the app's data folder
              (%APPDATA%\YouTubeAgent), so reinstalling or upgrading
              picks up right where you left off.
            </p>
          </div>

          <div className="flex items-center justify-between gap-4 py-3">
            <div>
              <p className="text-sm font-medium text-ink">
                Remove all my data on uninstall
              </p>
              <p className="mt-0.5 text-xs text-muted">
                Turn this on only if you want uninstalling to also
                permanently delete the app's data folder — your
                channels and their settings, project and video
                history, Style Lab profiles, app settings, database
                and logs. Deleted data cannot be recovered. This
                changes nothing right now: it takes effect only when
                the app is uninstalled, and turning it back off before
                then keeps your data. Videos you exported or rendered
                to your output folder are not app data and are never
                deleted by uninstalling.
              </p>
            </div>
            <Toggle
              checked={enabled}
              onChange={handleToggle}
              label="Remove all my data on uninstall"
            />
          </div>

          {updatePrivacy.isPending ? (
            <p className="text-xs text-muted">Saving…</p>
          ) : saveError ? (
            <p className="text-sm text-danger">{saveError}</p>
          ) : enabled ? (
            <p className="flex items-start gap-1.5 text-sm text-warn">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              On — saved to the backend. The uninstall marker (
              {privacy.marker_filename}) is in place in your data
              folder, so uninstalling this app will delete that data
              permanently.
            </p>
          ) : (
            <p className="flex items-start gap-1.5 text-sm text-ok">
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
              Off — saved to the backend. No uninstall marker is
              present, so uninstalling will keep your data.
            </p>
          )}
          {privacy.marker_present !== enabled ? (
            <p className="mt-1 text-xs text-warn">
              Note: the backend reports the marker file as{" "}
              {privacy.marker_present ? "present" : "absent"} while
              this setting is {enabled ? "on" : "off"} — toggling the
              setting again rewrites it to match.
            </p>
          ) : null}
        </>
      ) : null}
    </Card>
  );
}

// ── Compliance Gate (GET /settings/compliance, POST …/transitions) ────────
//
// The roadmap's #1 launch blocker, shown honestly: the stored
// checklist with per-item status + who/when + evidence, the derived
// overall state, and its practical effect. There is NO "mark passed"
// flip here — every change goes through the deliberate transition
// action (actor required; evidence required for a pass), which the
// backend appends to the audit history. Until the overall state is
// "passed", Gate C forces uploads to Private and refuses public
// publishes.

const COMPLIANCE_STATUS_TONES: Record<
  ComplianceItemStatusWire,
  "green" | "amber" | "red" | "gray"
> = {
  passed: "green",
  in_progress: "amber",
  failed: "red",
  pending: "gray",
};

const COMPLIANCE_STATUS_LABELS: Record<ComplianceItemStatusWire, string> = {
  passed: "Passed",
  in_progress: "In progress",
  failed: "Failed",
  pending: "Pending",
};

const COMPLIANCE_TARGETS: ComplianceItemStatusWire[] = [
  "in_progress",
  "passed",
  "failed",
  "pending",
];

interface ComplianceItemDraft {
  actor: string;
  evidence: string;
  note: string;
  toStatus: ComplianceItemStatusWire;
}

function defaultDraftFor(item: ComplianceItemWire): ComplianceItemDraft {
  return {
    actor: "",
    evidence: "",
    note: "",
    toStatus: item.status === "passed" ? "in_progress" : "passed",
  };
}

function formatStamp(ms: number | null): string | null {
  if (!ms) return null;
  const date = new Date(ms);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleString();
}

function ComplianceItemRow({ item }: { item: ComplianceItemWire }) {
  const transition = useComplianceTransition();
  const [draft, setDraft] = useState<ComplianceItemDraft | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const current = draft ?? defaultDraftFor(item);
  const updatedStamp = formatStamp(item.updated_at);

  const evidenceSatisfied =
    current.toStatus !== "passed" ||
    current.evidence.trim() !== "" ||
    Boolean(item.evidence);
  const canSubmit =
    current.actor.trim() !== "" &&
    current.toStatus !== item.status &&
    evidenceSatisfied &&
    !transition.isPending;

  const edit = (patch: Partial<ComplianceItemDraft>) => {
    setDraft({ ...current, ...patch });
    setFormError(null);
  };

  const handleSubmit = () => {
    if (!canSubmit) return;
    setFormError(null);
    transition.mutate(
      {
        item_key: item.key,
        to_status: current.toStatus,
        actor: current.actor.trim(),
        evidence: current.evidence.trim() || undefined,
        note: current.note.trim() || undefined,
      },
      {
        onSuccess: () => setDraft(null),
        onError: (err) => {
          setFormError(`Couldn't record the transition: ${errorText(err)}`);
        },
      },
    );
  };

  return (
    <div className="py-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-ink">{item.label}</h3>
        <Badge tone={COMPLIANCE_STATUS_TONES[item.status]}>
          {COMPLIANCE_STATUS_LABELS[item.status]}
        </Badge>
      </div>
      <p className="mt-1 text-xs text-muted">{item.description}</p>

      <div className="mt-2 space-y-1 text-xs text-ink2">
        {item.updated_by ? (
          <p>
            Last updated by {item.updated_by}
            {updatedStamp ? ` · ${updatedStamp}` : ""}
          </p>
        ) : (
          <p className="text-muted">Not started yet — no transition recorded.</p>
        )}
        {item.evidence ? (
          <p>
            <span className="font-medium text-ink">Evidence:</span>{" "}
            {item.evidence}
          </p>
        ) : null}
        {item.note ? (
          <p>
            <span className="font-medium text-ink">Note:</span> {item.note}
          </p>
        ) : null}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <select
          aria-label={`New status for ${item.label}`}
          value={current.toStatus}
          onChange={(event) =>
            edit({
              toStatus: event.target.value as ComplianceItemStatusWire,
            })
          }
          className="rounded-md border border-line bg-raised px-2 py-2 text-sm text-ink"
        >
          {COMPLIANCE_TARGETS.filter((s) => s !== item.status).map((s) => (
            <option key={s} value={s}>
              {COMPLIANCE_STATUS_LABELS[s]}
            </option>
          ))}
        </select>
        <input
          type="text"
          value={current.actor}
          onChange={(event) => edit({ actor: event.target.value })}
          placeholder="Your name (recorded as who)"
          aria-label="Who is making this change"
          className="w-52 rounded-md border border-line bg-raised px-3 py-2 text-sm text-ink"
        />
        <input
          type="text"
          value={current.evidence}
          onChange={(event) => edit({ evidence: event.target.value })}
          placeholder={
            current.toStatus === "passed"
              ? "Evidence (required for a pass)"
              : "Evidence (reference / URL)"
          }
          aria-label={`Evidence for ${item.label}`}
          className="w-64 rounded-md border border-line bg-raised px-3 py-2 text-sm text-ink"
        />
        <input
          type="text"
          value={current.note}
          onChange={(event) => edit({ note: event.target.value })}
          placeholder="Note (optional)"
          aria-label={`Note for ${item.label}`}
          className="w-52 rounded-md border border-line bg-raised px-3 py-2 text-sm text-ink"
        />
        <Button
          variant="primary"
          size="sm"
          disabled={!canSubmit}
          onClick={handleSubmit}
        >
          {transition.isPending ? "Recording…" : "Record transition"}
        </Button>
      </div>
      {current.toStatus === "passed" && !evidenceSatisfied ? (
        <p className="mt-1 text-xs text-warn">
          A pass can't be recorded without evidence — add the audit
          reference, live policy URL, or verified project id first.
        </p>
      ) : null}
      {current.actor.trim() === "" ? (
        <p className="mt-1 text-xs text-muted">
          Every transition is logged with who made it, so a name is
          required.
        </p>
      ) : null}
      {formError ? (
        <p className="mt-2 text-sm text-danger">{formError}</p>
      ) : null}
    </div>
  );
}

function ComplianceCard() {
  const complianceQuery = useCompliance();
  const state = complianceQuery.data;
  const passed = state?.overall === "passed";

  return (
    <Card className="mt-6 p-5">
      <div className="flex flex-wrap items-center gap-2">
        <Shield className="h-5 w-5 shrink-0 text-accent2" />
        <h2 className="text-base font-semibold text-ink">
          Compliance Gate — YouTube API audit
        </h2>
        {state ? (
          <Badge tone={passed ? "green" : "amber"}>
            {passed
              ? "Passed"
              : state.overall === "in_progress"
                ? "In progress"
                : "Not started"}
          </Badge>
        ) : null}
      </div>
      <p className="mt-1 text-xs text-muted">
        The launch-audit checklist is stored in the backend and every
        change is a logged transition — there is no free-form edit and
        no one-click pass.
      </p>

      {complianceQuery.isLoading ? (
        <p className="mt-3 text-sm text-muted">
          Loading compliance state from the backend…
        </p>
      ) : complianceQuery.isError ? (
        <div className="mt-3">
          <p className="flex items-start gap-1.5 text-sm text-danger">
            <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
            Compliance state couldn't be loaded:{" "}
            {errorText(complianceQuery.error)}
          </p>
          <p className="mt-1 text-xs text-muted">
            Until the backend confirms a pass, public publishing stays
            blocked and uploads are forced to Private.
          </p>
          <Button
            variant="outline"
            size="sm"
            className="mt-2"
            onClick={() => void complianceQuery.refetch()}
          >
            Retry
          </Button>
        </div>
      ) : state ? (
        <>
          <div
            className={`mt-4 rounded-md border p-3 ${
              passed ? "border-ok/40" : "border-warn/40 bg-raised"
            }`}
          >
            {passed ? (
              <p className="flex items-start gap-1.5 text-sm text-ok">
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
                {state.effect}
              </p>
            ) : (
              <p className="flex items-start gap-1.5 text-sm text-warn">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                Public publishing is blocked until the compliance
                audit passes — uploads are forced to Private.
              </p>
            )}
          </div>

          <div className="divide-y divide-line">
            {state.items.map((item) => (
              <ComplianceItemRow key={item.key} item={item} />
            ))}
          </div>

          {state.history.length > 0 ? (
            <div className="mt-4 border-t border-line pt-4">
              <h3 className="text-sm font-semibold text-ink">
                Transition history
              </h3>
              <ul className="mt-2 space-y-1.5">
                {state.history.slice(0, 8).map((entry) => {
                  const stamp = formatStamp(entry.created_at);
                  const label =
                    state.items.find((i) => i.key === entry.item_key)
                      ?.label ?? entry.item_key;
                  return (
                    <li key={entry.id} className="text-xs text-ink2">
                      <span className="font-medium text-ink">{label}</span>{" "}
                      {COMPLIANCE_STATUS_LABELS[entry.from_status]} →{" "}
                      {COMPLIANCE_STATUS_LABELS[entry.to_status]} by{" "}
                      {entry.actor}
                      {stamp ? ` · ${stamp}` : ""}
                    </li>
                  );
                })}
              </ul>
            </div>
          ) : (
            <p className="mt-4 border-t border-line pt-4 text-xs text-muted">
              No transitions recorded yet — the history will appear
              here as checklist items move.
            </p>
          )}
        </>
      ) : null}
    </Card>
  );
}

// ── App updates (shell updater: check_for_updates / install_update) ───────
//
// The shell owns update policy (§11.4): background checks run 30 s
// after launch and every 6 h, and a found update is announced by the
// app-level banner. This card is the foreground surface: current
// version, a manual check, the honest result of the last check
// (available / up to date / error — exactly as the shell reported),
// and the same install action as the banner. Checking never installs;
// installing re-verifies in the shell, follows its passive install
// flow on Windows, and relaunches the app. Outside the desktop app
// (browser / Vite dev) the card renders disabled — there is no shell
// to ask, and it never invents an "update available" state.

function UpdatesCard() {
  const inTauri = isTauriRuntime();
  const pendingUpdate = useUpdaterStore((s) => s.pendingUpdate);
  const checkStatus = useUpdaterStore((s) => s.checkStatus);
  const shellVersion = useUpdaterStore((s) => s.currentVersion);
  const backendAppVersion = useBackendBootstrapStore(
    (s) => s.systemInfo?.appVersion ?? null,
  );

  useEffect(() => {
    void loadCurrentVersion();
  }, []);

  const displayVersion = shellVersion ?? backendAppVersion;
  const busy =
    checkStatus.kind === "checking" || checkStatus.kind === "installing";

  const statusText = (() => {
    if (!inTauri) {
      return "Update checks need the desktop app — this build has no shell to check with.";
    }
    switch (checkStatus.kind) {
      case "idle":
        return pendingUpdate
          ? `Update available: YouTube Agent ${pendingUpdate.version} (found by a background check).`
          : "Not checked yet this session. The shell also checks automatically — 30 seconds after launch, then every 6 hours.";
      case "checking":
        return "Checking for updates…";
      case "installing":
        return "Installing… the update is verified by the shell and the app relaunches when it is done.";
      case "install_done": {
        const result = checkStatus.result;
        if (result.state === "error") {
          return `Install didn't happen: ${(result.detail ?? "install failed").replace(/_/g, " ")}. The running app was left untouched.`;
        }
        if (result.state === "up_to_date") {
          return "No update was available to install — you're up to date.";
        }
        return `Update ${result.version ?? ""} is ready — use Install update to apply it.`;
      }
      case "done": {
        const result = checkStatus.result;
        const when = new Date(checkStatus.checkedAt).toLocaleTimeString();
        if (result.state === "available") {
          return `Update available: YouTube Agent ${result.version ?? ""} (checked ${when}). Nothing has been installed.`;
        }
        if (result.state === "up_to_date") {
          return `You're up to date (checked ${when}).`;
        }
        return `Last check failed (${when}): ${(result.detail ?? "unknown error").replace(/_/g, " ")}`;
      }
    }
  })();

  const statusIsError =
    (checkStatus.kind === "done" && checkStatus.result.state === "error") ||
    (checkStatus.kind === "install_done" &&
      checkStatus.result.state === "error");

  return (
    <Card className="mt-6 p-5">
      <div className="flex flex-wrap items-center gap-2">
        <ArrowUpCircle className="h-5 w-5 shrink-0 text-accent2" />
        <h2 className="text-base font-semibold text-ink">Updates</h2>
        {pendingUpdate ? (
          <Badge tone="amber">Update available: {pendingUpdate.version}</Badge>
        ) : null}
      </div>
      <p className="mt-1 text-xs text-muted">
        Shell + interface updates (update channel A). The engine has its
        own separate channel, verified by the shell.
      </p>

      <div className="flex items-center justify-between gap-4 py-3">
        <div>
          <p className="text-sm font-medium text-ink">Current version</p>
          <p className="mt-0.5 text-xs text-muted">
            The version of YouTube Agent running on this PC.
          </p>
        </div>
        <span className="font-mono text-sm text-ink tnum">
          {displayVersion ? `v${displayVersion}` : "Unavailable in this build"}
        </span>
      </div>

      {pendingUpdate?.notes ? (
        <p className="rounded-md bg-raised p-3 text-sm text-ink2">
          {pendingUpdate.notes}
        </p>
      ) : null}

      <p
        className={`mt-2 flex items-start gap-1.5 text-sm ${
          statusIsError ? "text-danger" : "text-ink2"
        }`}
      >
        {statusIsError ? (
          <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
        ) : pendingUpdate ? (
          <ArrowUpCircle className="mt-0.5 h-4 w-4 shrink-0 text-warn" />
        ) : (
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-ok" />
        )}
        {statusText}
      </p>

      <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-4">
        <Button
          variant="outline"
          size="sm"
          disabled={!inTauri || busy}
          onClick={() => void checkForUpdatesNow()}
        >
          {checkStatus.kind === "checking"
            ? "Checking…"
            : "Check for updates"}
        </Button>
        {pendingUpdate ? (
          <Button
            variant="primary"
            size="sm"
            disabled={!inTauri || busy}
            onClick={() => void installPendingUpdate()}
          >
            <ArrowUpCircle className="h-3.5 w-3.5" />
            {checkStatus.kind === "installing"
              ? "Installing…"
              : `Install update (${pendingUpdate.version})`}
          </Button>
        ) : null}
      </div>
      <p className="mt-2 text-xs text-muted">
        Install follows the shell's passive flow: the update is
        downloaded and signature-verified, Windows shows the install
        progress, and the app relaunches. Only install when no video is
        rendering or uploading — a check on its own never installs
        anything and never interrupts work.
      </p>
    </Card>
  );
}

// ── Autostart (shell features: autostart_get / autostart_set, §11.9) ──────
//
// "Start with Windows" is a SEPARATE, OPT-IN, per-user setting: the
// shell writes an HKCU Run entry (no administrator rights), default
// OFF, changed only when the user flips this toggle. It can also be
// undone in Task Manager's Startup tab. Outside the desktop app the
// toggle renders disabled — there is no shell to read or change.

function AutostartCard() {
  const inTauri = isTauriRuntime();
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    if (!inTauri) return;
    let cancelled = false;
    void autostartGet().then((value) => {
      if (cancelled) return;
      if (value === null) {
        setLoadFailed(true);
      } else {
        setEnabled(value);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [inTauri]);

  const handleToggle = (next: boolean) => {
    if (enabled === null || saving) return;
    const previous = enabled;
    setEnabled(next);
    setSaveError(null);
    setSaving(true);
    void autostartSet(next)
      .catch((err: unknown) => {
        setEnabled(previous);
        setSaveError(
          `Couldn't change this setting: ${
            err instanceof Error ? err.message : String(err)
          }`,
        );
      })
      .finally(() => setSaving(false));
  };

  return (
    <Card className="mt-6 p-5">
      <div className="flex flex-wrap items-center gap-2">
        <Power className="h-5 w-5 shrink-0 text-accent2" />
        <h2 className="text-base font-semibold text-ink">Autostart</h2>
      </div>
      <p className="mt-1 text-xs text-muted">
        Start YouTube Agent automatically when you sign in to Windows,
        so scheduled videos keep running.
      </p>

      <div className="flex items-center justify-between gap-4 py-3">
        <div>
          <p className="text-sm font-medium text-ink">Start with Windows</p>
          <p className="mt-0.5 text-xs text-muted">
            Opt-in and per-user (§11.9): the app adds itself to your
            user account's startup list (HKCU Run entry) — no
            administrator rights are needed, it is off by default, and
            you can undo it here or in Task Manager's Startup tab.
            Nothing changes until you flip this switch.
          </p>
        </div>
        <Toggle
          checked={enabled ?? false}
          onChange={handleToggle}
          label="Start with Windows"
          disabled={!inTauri || enabled === null || saving}
        />
      </div>

      {!inTauri ? (
        <p className="text-sm text-muted">
          Available in the desktop app only — autostart is a shell
          setting, so it can't be read or changed from this build.
        </p>
      ) : loadFailed ? (
        <p className="flex items-start gap-1.5 text-sm text-danger">
          <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
          Autostart state couldn't be read from the shell — the switch
          above is disabled rather than guessing.
        </p>
      ) : enabled === null ? (
        <p className="text-sm text-muted">
          Loading autostart state from the shell…
        </p>
      ) : saving ? (
        <p className="text-xs text-muted">Saving…</p>
      ) : saveError ? (
        <p className="text-sm text-danger">{saveError}</p>
      ) : enabled ? (
        <p className="flex items-start gap-1.5 text-sm text-ok">
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
          On — saved via the shell. YouTube Agent will start (in the
          tray) when you sign in.
        </p>
      ) : (
        <p className="flex items-start gap-1.5 text-sm text-muted">
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-ok" />
          Off — the app only starts when you open it.
        </p>
      )}
    </Card>
  );
}

export default function Settings() {
  const providersQuery = useProviders();
  const budgetQuery = useBudgetSettings();
  const updateBudget = useUpdateBudgetSettings();

  // Budget fields are REAL (GET|PUT /settings/budget, enforced by the
  // backend's budget guard) with an explicit Save. `budgetDraft` holds
  // the user's edits as strings; null means "showing the server values".
  const [budgetDraft, setBudgetDraft] = useState<BudgetDraft | null>(null);
  // The values from the last successful save; the "Saved" confirmation
  // shows only while the server values still match them.
  const [savedDraft, setSavedDraft] = useState<BudgetDraft | null>(null);
  const [budgetError, setBudgetError] = useState<string | null>(null);

  // The remaining guardrail controls have NO backend field yet, so
  // they stay local-only and are labelled as such — they must not
  // pretend to be saved or enforced.
  const [reviewFirst, setReviewFirst] = useState(true);
  const [killSwitchArmed, setKillSwitchArmed] = useState(true);
  const [pauseOnCap, setPauseOnCap] = useState(true);

  const serverDraft = budgetQuery.data ? draftFromWire(budgetQuery.data) : null;
  const draft = budgetDraft ?? serverDraft;
  const budgetDirty =
    budgetDraft !== null && serverDraft !== null && !draftsEqual(budgetDraft, serverDraft);
  const budgetValidationError = draft ? budgetDraftError(draft) : null;
  const budgetSaved =
    savedDraft !== null &&
    serverDraft !== null &&
    !budgetDirty &&
    draftsEqual(savedDraft, serverDraft);

  const editBudget = (patch: Partial<BudgetDraft>) => {
    if (!draft) return;
    setBudgetDraft({ ...draft, ...patch });
    setBudgetError(null);
  };

  const handleSaveBudget = () => {
    if (!draft || !budgetDirty || budgetValidationError) return;
    setBudgetError(null);
    updateBudget.mutate(wireFromDraft(draft), {
      onSuccess: (saved) => {
        // The mutation already wrote the saved values into the query
        // cache; clearing the draft makes the form show exactly what
        // the backend accepted.
        setBudgetDraft(null);
        setSavedDraft(draftFromWire(saved));
      },
      onError: (err) => {
        setBudgetError(`Couldn't save budget settings: ${errorText(err)}`);
      },
    });
  };

  const providers = providersQuery.data ?? [];
  const required = providers.filter((p) =>
    metaFor(p).requirement.startsWith("Required"),
  );
  const optional = providers.filter(
    (p) => !metaFor(p).requirement.startsWith("Required"),
  );

  return (
    <div className="bg-app flex min-h-full text-ink">
      <nav
        aria-label="Settings sections"
        className="w-56 shrink-0 border-r border-line bg-panel p-3"
      >
        <ul className="space-y-1">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            const active = item.label === ACTIVE_NAV;
            return (
              <li key={item.label}>
                <button
                  type="button"
                  aria-current={active ? "page" : undefined}
                  className={`flex w-full items-center gap-2.5 rounded-md px-3 py-2 text-left text-sm ${
                    active
                      ? "accent-gradient font-medium text-white"
                      : "text-ink2 hover:bg-raised hover:text-ink"
                  }`}
                >
                  <Icon className="h-4 w-4 shrink-0" />
                  {item.label}
                </button>
              </li>
            );
          })}
        </ul>
      </nav>

      <div className="min-w-0 flex-1 p-6">
        <header className="mb-6">
          <h1 className="text-2xl font-semibold text-ink">
            API Keys &amp; Providers
          </h1>
          <p className="mt-1 text-sm text-muted">
            Connect the services the agents use. The kill switch is always
            available in the app header.
          </p>
        </header>

        <Card className="mb-6 border-accent/40 bg-raised p-5">
          <p className="text-sm text-ink">
            Keys are stored only on this PC in Windows Credential Manager —
            never uploaded, never shown again after saving.
          </p>
        </Card>

        {providersQuery.isLoading ? (
          <Card className="p-5">
            <p className="text-sm text-muted">
              Loading providers from the backend…
            </p>
          </Card>
        ) : providersQuery.isError ? (
          <Card className="border-danger/40 p-5">
            <div className="flex items-start gap-3">
              <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-danger" />
              <div>
                <h2 className="text-base font-semibold text-ink">
                  Backend offline — provider status unavailable
                </h2>
                <p className="mt-1 text-sm text-ink2">
                  No provider can be shown as connected while the backend
                  is unreachable. Check that the app backend is running,
                  then retry.
                </p>
                <p className="mt-1 text-xs text-muted">
                  {errorText(providersQuery.error)}
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-3"
                  onClick={() => void providersQuery.refetch()}
                >
                  Retry
                </Button>
              </div>
            </div>
          </Card>
        ) : providers.length === 0 ? (
          <Card className="p-5">
            <p className="text-sm text-muted">
              The backend reported no providers.
            </p>
          </Card>
        ) : (
          <div className="space-y-6">
            <ProviderGroup title="Required" providers={required} />
            <ProviderGroup title="Optional" providers={optional} />
          </div>
        )}

        <Card className="mt-6 p-5">
          <h2 className="text-base font-semibold text-ink">
            Budgets &amp; Guardrails
          </h2>
          <p className="mt-1 text-xs text-muted">
            Budget caps are loaded from — and saved to — the backend,
            which validates and enforces them. Changes apply only when
            you press Save.
          </p>

          {budgetQuery.isLoading ? (
            <p className="mt-3 text-sm text-muted">
              Loading budget settings from the backend…
            </p>
          ) : budgetQuery.isError ? (
            <div className="mt-3">
              <p className="text-sm text-danger">
                Budget settings couldn't be loaded:{" "}
                {errorText(budgetQuery.error)}
              </p>
              <Button
                variant="outline"
                size="sm"
                className="mt-2"
                onClick={() => void budgetQuery.refetch()}
              >
                Retry
              </Button>
            </div>
          ) : draft ? (
            <>
              <div className="divide-y divide-line">
                <BudgetField
                  id="per-video-cap"
                  label="Per-video cap"
                  description="Most the agents may spend producing one video."
                  value={draft.perVideoCap}
                  onChange={(next) => editBudget({ perVideoCap: next })}
                  prefix="$"
                />
                <BudgetField
                  id="daily-cap"
                  label="Daily cap"
                  description="Most the agents may spend across all videos in a day."
                  value={draft.dailyCap}
                  onChange={(next) => editBudget({ dailyCap: next })}
                  prefix="$"
                />
                <BudgetField
                  id="monthly-cap"
                  label="Monthly cap"
                  description="Most the agents may spend in a calendar month."
                  value={draft.monthlyCap}
                  onChange={(next) => editBudget({ monthlyCap: next })}
                  prefix="$"
                />
                <BudgetField
                  id="warn-pct"
                  label="Warn at"
                  description="Show a budget warning when spending reaches this share of a cap."
                  value={draft.warnPct}
                  onChange={(next) => editBudget({ warnPct: next })}
                  suffix="% of cap"
                />
                <BudgetField
                  id="soft-pct"
                  label="Soft stop at"
                  description="Soft-stop new work when spending reaches this share of a cap."
                  value={draft.softPct}
                  onChange={(next) => editBudget({ softPct: next })}
                  suffix="% of cap"
                />
              </div>

              {budgetDirty && budgetValidationError ? (
                <p className="mt-2 text-sm text-danger">
                  {budgetValidationError}
                </p>
              ) : null}
              {budgetError ? (
                <p className="mt-2 text-sm text-danger">{budgetError}</p>
              ) : null}

              <div className="mt-3 flex items-center justify-end gap-3">
                {budgetDirty ? (
                  <span className="text-xs font-medium text-warn">
                    Unsaved changes
                  </span>
                ) : budgetSaved ? (
                  <span className="flex items-center gap-1.5 text-xs font-medium text-ok">
                    <CheckCircle2 className="h-3.5 w-3.5" />
                    Saved to backend
                  </span>
                ) : (
                  <span className="text-xs text-muted">
                    Matches the saved backend values
                  </span>
                )}
                <Button
                  variant="primary"
                  size="sm"
                  disabled={
                    !budgetDirty ||
                    budgetValidationError !== null ||
                    updateBudget.isPending
                  }
                  onClick={handleSaveBudget}
                >
                  {updateBudget.isPending ? "Saving…" : "Save budgets"}
                </Button>
              </div>
            </>
          ) : null}

          <div className="mt-5 border-t border-line pt-4">
            <h3 className="text-sm font-semibold text-ink">
              Other guardrails
            </h3>
            <p className="mt-1 flex items-start gap-1.5 text-xs text-warn">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              Not saved to backend yet — the controls below are local to
              this window only. They are not persisted and are not
              enforced by the backend.
            </p>

            <div className="divide-y divide-line">
              <div className="flex items-center justify-between gap-4 py-3">
                <div>
                  <p className="text-sm font-medium text-ink">
                    Review-first mode
                  </p>
                  <p className="mt-0.5 text-xs text-muted">
                    Every video waits for your approval before publishing.
                    Default ON.
                  </p>
                </div>
                <Toggle
                  checked={reviewFirst}
                  onChange={setReviewFirst}
                  label="Review-first mode"
                />
              </div>

              <div className="flex items-center justify-between gap-4 py-3">
                <div>
                  <p className="text-sm font-medium text-ink">Kill switch</p>
                  <p className="mt-0.5 text-xs text-muted">
                    Also present in the app header — stops all agents and
                    uploads immediately.
                  </p>
                </div>
                <Toggle
                  checked={killSwitchArmed}
                  onChange={setKillSwitchArmed}
                  label="Kill switch armed"
                />
              </div>

              <div className="flex items-center justify-between gap-4 py-3">
                <div>
                  <p className="text-sm font-medium text-ink">
                    Pause automation at budget cap
                  </p>
                  <p className="mt-0.5 text-xs text-muted">
                    Stop starting new videos when a cap is hit.
                  </p>
                </div>
                <Toggle
                  checked={pauseOnCap}
                  onChange={setPauseOnCap}
                  label="Pause automation at budget cap"
                />
              </div>

              <div className="flex items-center justify-between gap-4 py-3 last:pb-0">
                <label
                  htmlFor="daily-upload-cap"
                  className="text-sm font-medium text-ink"
                >
                  Daily upload cap
                </label>
                <input
                  id="daily-upload-cap"
                  type="text"
                  inputMode="numeric"
                  defaultValue="10"
                  className="w-24 rounded-md border border-line bg-raised px-3 py-2 text-right font-mono text-sm text-ink tnum"
                />
              </div>
            </div>
          </div>
        </Card>

        <BackupRestoreCard />

        <DataPrivacyCard />

        <ComplianceCard />

        <UpdatesCard />

        <AutostartCard />

        <div className="mt-6 flex items-center justify-end gap-3 border-t border-line pt-4">
          <span className="text-sm text-muted">
            Provider keys save individually on each card above. Budget
            caps save with the button in Budgets &amp; Guardrails; the
            other guardrail toggles aren't saved to the backend yet.
            The uninstall data setting in Data &amp; Privacy saves to
            the backend immediately. Updates and Autostart are desktop
            shell settings: Autostart saves via the shell as soon as
            you flip it, and an update check never installs anything
            on its own.
          </span>
        </div>
      </div>
    </div>
  );
}
