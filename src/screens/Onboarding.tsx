import { useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  CheckCircle2,
  Clapperboard,
  HelpCircle,
  KeyRound,
  Loader2,
  MonitorPlay,
  RefreshCw,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { ApiError } from "../api/types";
import {
  useProviders,
  useSaveProviderKey,
  useTestProvider,
} from "../api/queries/providers";
import {
  useCompleteOnboarding,
  useOnboardingStatus,
  useStartTestVideo,
  useSystemCheck,
} from "../api/queries/onboarding";
import type {
  CheckStatusWire,
  KeyStatusWire,
  SystemCheckWire,
} from "../api/queries/onboarding";
import { useWizardChannels, useWizardNiches } from "../api/queries/wizard";

// ── First-run wizard (Round 6) ────────────────────────────────────────────
// The wizard PROVES the machine works; it does not just collect settings:
//  1. System check — real probes via GET /onboarding/system-check
//     (WebView2, FFmpeg + loudnorm probe, disk space, sidecar health).
//     A status is shown exactly as the backend measured it: "unverified"
//     is rendered as unverified, never as a green tick.
//  2. Keys — Gemini / Pixabay are saved through the existing providers
//     endpoints (OS credential store; on Windows the backend pins
//     Windows Credential Manager) and verified with the real provider
//     test call. A saved-but-untested key says so, plainly. YouTube is
//     an interactive connect on the Channels screen; here we only
//     report its real state.
//  3. Finish — an OPTIONAL ~60-second test video through the existing
//     produce path. Skipping is a first-class choice and completes
//     onboarding exactly the same way.

type BadgeTone = "violet" | "green" | "amber" | "red" | "gray";

const CHECK_STATUS_META: Record<
  CheckStatusWire,
  { tone: BadgeTone; label: string; Icon: typeof CheckCircle2 }
> = {
  pass: { tone: "green", label: "Passed", Icon: CheckCircle2 },
  fail: { tone: "red", label: "Failed", Icon: XCircle },
  warn: { tone: "amber", label: "Warning", Icon: AlertTriangle },
  unverified: { tone: "gray", label: "Cannot verify here", Icon: HelpCircle },
};

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return "Something went wrong";
}

function measuredLine(check: SystemCheckWire): string | null {
  const m = check.measured;
  if (check.id === "ffmpeg") {
    const parts: string[] = [];
    if (typeof m.version === "string" && m.version) parts.push(m.version);
    if (typeof m.loudnorm_input_i_lufs === "number")
      parts.push(`loudnorm probe: ${m.loudnorm_input_i_lufs.toFixed(2)} LUFS`);
    if (typeof m.path === "string" && m.path) parts.push(m.path);
    return parts.length ? parts.join(" · ") : null;
  }
  if (check.id === "disk" && typeof m.free_gb === "number")
    return `${m.free_gb} GB free of ${typeof m.total_bytes === "number" ? Math.round(m.total_bytes / 1024 ** 3) : "?"} GB`;
  if (check.id === "webview2" && typeof m.version === "string")
    return `Version ${m.version} (via ${String(m.source ?? "lookup")})`;
  return null;
}

function SystemCheckStep() {
  const query = useSystemCheck();
  return (
    <Card className="p-5">
      <div className="flex items-center justify-between gap-4">
        <h2 className="flex items-center gap-2 text-base font-semibold text-ink">
          <ShieldCheck className="h-5 w-5 text-accent2" />
          1 · System check — real probes, real results
        </h2>
        <Button
          size="sm"
          variant="outline"
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
        >
          <RefreshCw className="h-3.5 w-3.5" />
          {query.isFetching ? "Checking…" : "Re-run checks"}
        </Button>
      </div>

      {query.isPending ? (
        <p className="mt-4 flex items-center gap-2 text-sm text-ink2">
          <Loader2 className="h-4 w-4 animate-spin" />
          Running the checks — the FFmpeg loudnorm probe takes a few seconds…
        </p>
      ) : query.isError ? (
        <p className="mt-4 flex items-center gap-2 text-sm text-danger">
          <XCircle className="h-4 w-4" />
          The system check could not run: {errorMessage(query.error)}. The
          sidecar may be down — nothing here is marked as passed.
        </p>
      ) : (
        <>
          <ul className="mt-4 flex flex-col gap-3">
            {query.data.checks.map((check) => {
              const meta = CHECK_STATUS_META[check.status];
              const Icon = meta.Icon;
              const measured = measuredLine(check);
              return (
                <li
                  key={check.id}
                  className="flex items-start gap-3 rounded-xl border border-line bg-raised/50 p-3"
                >
                  <Icon
                    className={`mt-0.5 h-5 w-5 shrink-0 ${
                      check.status === "pass"
                        ? "text-ok"
                        : check.status === "fail"
                          ? "text-danger"
                          : check.status === "warn"
                            ? "text-warn"
                            : "text-ink2"
                    }`}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-ink">
                        {check.label}
                      </span>
                      <Badge tone={meta.tone}>{meta.label}</Badge>
                    </div>
                    <p className="mt-1 text-sm text-ink2">{check.detail}</p>
                    {measured ? (
                      <p className="mt-1 text-xs text-ink2/80">{measured}</p>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ul>
          <p className="mt-3 text-xs text-ink2">
            Credential store: {query.data.credential_store.backend} ·{" "}
            {query.data.credential_store.windows_credential_manager
              ? "Windows Credential Manager is selected."
              : query.data.credential_store.available
                ? "OS credential store available (Windows Credential Manager is selected automatically on Windows)."
                : "OS credential store NOT available on this machine — keys cannot be saved safely here."}
          </p>
        </>
      )}
    </Card>
  );
}

function KeyRow({
  providerId,
  label,
  status,
}: {
  providerId: "gemini" | "pixabay";
  label: string;
  status: KeyStatusWire | undefined;
}) {
  const [value, setValue] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const saveKey = useSaveProviderKey();
  const testProvider = useTestProvider();

  const verified = status?.verified === true;
  const configured = status?.configured === true;

  return (
    <div className="rounded-xl border border-line bg-raised/50 p-3">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium text-ink">{label}</span>
        {verified ? (
          <Badge tone="green">Verified</Badge>
        ) : configured ? (
          <Badge tone="amber">Saved — not yet verified</Badge>
        ) : (
          <Badge tone="gray">Not saved</Badge>
        )}
        {status?.key_last4 ? (
          <span className="text-xs text-ink2">last-4: {status.key_last4}</span>
        ) : null}
      </div>
      {status ? <p className="mt-1 text-sm text-ink2">{status.detail}</p> : null}
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <input
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={configured ? "Paste a new key to replace…" : "Paste your key…"}
          autoComplete="off"
          className="min-w-56 flex-1 rounded-xl border border-line bg-panel px-3 py-2 text-sm text-ink outline-none focus:border-accent"
        />
        <Button
          size="sm"
          disabled={!value.trim() || saveKey.isPending}
          onClick={() => {
            saveKey.mutate(
              { providerId, value: value.trim() },
              {
                onSuccess: () => {
                  setValue("");
                  setNotice(
                    "Key saved to the OS credential store. It is saved — not yet verified; run Test to verify it for real.",
                  );
                },
                onError: (err) => setNotice(errorMessage(err)),
              },
            );
          }}
        >
          {saveKey.isPending ? "Saving…" : "Save key"}
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={!configured || testProvider.isPending}
          onClick={() => {
            testProvider.mutate(providerId, {
              onSuccess: (result) => {
                if (result.status === "ok")
                  setNotice("Verified — the provider answered a real test call.");
                else if (result.status === "not_configured")
                  setNotice("No key is saved, so there was nothing to verify.");
                else
                  setNotice(
                    `Test failed${result.category ? ` (${result.category})` : ""}: ${result.detail ?? "the provider rejected the call."}`,
                  );
              },
              onError: (err) => setNotice(errorMessage(err)),
            });
          }}
        >
          {testProvider.isPending ? "Testing…" : "Test"}
        </Button>
      </div>
      {notice ? <p className="mt-2 text-xs text-ink2">{notice}</p> : null}
    </div>
  );
}

function KeysStep() {
  const statusQuery = useOnboardingStatus();
  const providersQuery = useProviders();
  const status = statusQuery.data;
  const youtube = status?.youtube;

  return (
    <Card className="p-5">
      <h2 className="flex items-center gap-2 text-base font-semibold text-ink">
        <KeyRound className="h-5 w-5 text-accent2" />
        2 · Keys — stored in the OS credential store, verified for real
      </h2>
      <p className="mt-1 text-sm text-ink2">
        Keys go to the OS credential store (Windows Credential Manager on
        Windows) via the same secret layer as Settings — never into the
        database or this page's state after saving.
        {providersQuery.isError
          ? " (Provider list could not be loaded; key states below come from the onboarding status.)"
          : ""}
      </p>
      <div className="mt-4 flex flex-col gap-3">
        <KeyRow providerId="gemini" label="Gemini API key" status={status?.keys.gemini} />
        <KeyRow providerId="pixabay" label="Pixabay API key" status={status?.keys.pixabay} />
        <div className="rounded-xl border border-line bg-raised/50 p-3">
          <div className="flex items-center gap-2">
            <MonitorPlay className="h-4 w-4 text-ink2" />
            <span className="text-sm font-medium text-ink">YouTube channel</span>
            {youtube?.connected ? (
              <Badge tone="green">Connected</Badge>
            ) : youtube?.client_configured ? (
              <Badge tone="amber">Ready to connect</Badge>
            ) : (
              <Badge tone="gray">Not set up</Badge>
            )}
          </div>
          <p className="mt-1 text-sm text-ink2">
            {youtube
              ? youtube.detail
              : "Loading YouTube state…"}
          </p>
          <p className="mt-1 text-xs text-ink2">
            Connecting needs you at a browser signing in with your channel
            account, so it happens on the Channels screen — the wizard
            only reports the real state.
          </p>
          <Link
            to="/channels"
            className="mt-2 inline-block text-sm font-medium text-accent2 hover:underline"
          >
            Go to Channels →
          </Link>
        </div>
      </div>
    </Card>
  );
}

function FinishStep() {
  const statusQuery = useOnboardingStatus();
  const channelsQuery = useWizardChannels();
  const nichesQuery = useWizardNiches();
  const complete = useCompleteOnboarding();
  const startTestVideo = useStartTestVideo();
  const [channelId, setChannelId] = useState("");
  const [nicheId, setNicheId] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const channels = channelsQuery.data ?? [];
  const niches = nichesQuery.data ?? [];
  const effectiveChannel = channelId || channels[0]?.id || "";
  const effectiveNiche = nicheId || niches[0]?.id || "";
  const canTest = Boolean(effectiveChannel && effectiveNiche);
  const completed = done || statusQuery.data?.completed === true;

  const finish = (choice: "skip" | "run") => {
    complete.mutate(choice, {
      onSuccess: () => setDone(true),
      onError: (err) => setNotice(errorMessage(err)),
    });
  };

  return (
    <Card className="p-5">
      <h2 className="flex items-center gap-2 text-base font-semibold text-ink">
        <Clapperboard className="h-5 w-5 text-accent2" />
        3 · Finish — optional 60-second test video
      </h2>
      <p className="mt-1 text-sm text-ink2">
        Want proof the whole pipeline works on this machine? Make a
        ~60-second test video through the normal production path. It is
        entirely optional — skipping completes setup just the same.
      </p>

      {completed ? (
        <p className="mt-4 flex items-center gap-2 text-sm text-ok">
          <CheckCircle2 className="h-4 w-4" />
          Setup is complete. You can re-run this wizard any time from the
          sidebar.
        </p>
      ) : (
        <>
          <div className="mt-4 flex flex-wrap items-end gap-2">
            <label className="flex flex-col gap-1 text-xs text-ink2">
              Channel
              <select
                value={effectiveChannel}
                onChange={(e) => setChannelId(e.target.value)}
                className="rounded-xl border border-line bg-panel px-3 py-2 text-sm text-ink"
              >
                {channels.length === 0 ? (
                  <option value="">No channels yet</option>
                ) : (
                  channels.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.displayName}
                    </option>
                  ))
                )}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-xs text-ink2">
              Niche
              <select
                value={effectiveNiche}
                onChange={(e) => setNicheId(e.target.value)}
                className="rounded-xl border border-line bg-panel px-3 py-2 text-sm text-ink"
              >
                {niches.length === 0 ? (
                  <option value="">No niches yet</option>
                ) : (
                  niches.map((n) => (
                    <option key={n.id} value={n.id}>
                      {n.name}
                    </option>
                  ))
                )}
              </select>
            </label>
            <Button
              disabled={!canTest || startTestVideo.isPending || complete.isPending}
              onClick={() => {
                startTestVideo.mutate(
                  { channelId: effectiveChannel, nicheProfileId: effectiveNiche },
                  {
                    onSuccess: (result) => {
                      setNotice(result.detail);
                      finish("run");
                    },
                    onError: (err) => setNotice(errorMessage(err)),
                  },
                );
              }}
            >
              {startTestVideo.isPending ? "Starting…" : "Make my 60-second test video"}
            </Button>
            <Button
              variant="outline"
              disabled={complete.isPending || startTestVideo.isPending}
              onClick={() => finish("skip")}
            >
              Skip — finish setup
            </Button>
          </div>
          {!canTest ? (
            <p className="mt-2 text-xs text-ink2">
              The test video needs at least one channel and one niche
              profile. You can create them later and skip this step now —
              setup still completes.
            </p>
          ) : null}
        </>
      )}
      {notice ? <p className="mt-2 text-xs text-ink2">{notice}</p> : null}
    </Card>
  );
}

export default function Onboarding() {
  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-4 p-6">
      <header>
        <h1 className="text-xl font-bold text-ink">First-run setup</h1>
        <p className="mt-1 text-sm text-ink2">
          Three steps: prove this machine can produce videos, save your
          keys safely, and — only if you want — make a one-minute test
          video. Nothing here is pre-ticked: every status below is the
          result of a real check.
        </p>
      </header>
      <SystemCheckStep />
      <KeysStep />
      <FinishStep />
    </div>
  );
}
