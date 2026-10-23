import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { LucideIcon } from "lucide-react";
import {
  AlertTriangle,
  Check,
  Cpu,
  Fingerprint,
  Ghost,
  HeartPulse,
  Lightbulb,
  MoonStar,
  Rocket,
  ScrollText,
  Sparkles,
  MonitorPlay,
  Play,
} from "lucide-react";
import { Card } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { Badge } from "../components/ui/Badge";
import {
  getErrorMessage,
  parseNicheFieldErrors,
  useCreateNicheProfile,
  useCreateProject,
  useWizardChannels,
  useWizardNiches,
  validateOtherNiche,
} from "../api/queries/wizard";
import type { NicheFieldErrors } from "../api/queries/wizard";

const STEPS: string[] = [
  "Channel",
  "Niche",
  "Topic",
  "Length",
  "Voice",
  "Publish Mode",
  "Launch",
];

/** Icons for the 8 built-in niches (keyed by the backend's niche id). */
const BUILTIN_ICONS: Record<string, LucideIcon> = {
  facts: Lightbulb,
  islamic_stories: MoonStar,
  horror_stories: Ghost,
  history: ScrollText,
  motivation: Rocket,
  tech: Cpu,
  crime_stories: Fingerprint,
  health: HeartPulse,
};

/** Short editorial descriptors for the built-ins (UI copy, not backend data). */
const BUILTIN_DESCRIPTORS: Record<string, string> = {
  facts: "Surprising facts and curiosities, told fast and punchy.",
  islamic_stories: "Seerah and Islamic history stories, told with respect.",
  horror_stories: "Slow-burn scary stories that keep viewers hooked.",
  history: "Forgotten events and empires, explained like a story.",
  motivation: "High-energy talks on discipline, habits and success.",
  tech: "Gadgets, AI and apps — explained in simple words.",
  crime_stories: "True-crime cases, investigated step by step.",
  health: "Everyday health, food and body science, made simple.",
};

const OTHER_NICHE_ID = "other";

type TopicMode = "auto" | "manual";

interface VoiceOption {
  id: string;
  name: string;
  detail: string;
  badge: string;
  badgeTone: "violet" | "green" | "amber" | "red" | "gray";
}

const VOICES: VoiceOption[] = [
  {
    id: "asad",
    name: "ur-PK-AsadNeural",
    detail: "edge-tts · Free · default",
    badge: "Free",
    badgeTone: "green",
  },
  {
    id: "uzma",
    name: "ur-PK-UzmaNeural",
    detail: "edge-tts · Free",
    badge: "Free",
    badgeTone: "green",
  },
  {
    id: "elevenlabs",
    name: "ElevenLabs",
    detail: "Premium — needs key",
    badge: "Premium",
    badgeTone: "amber",
  },
];

type PublishMode = "review" | "auto";

const STEP_TITLES: string[] = [
  "Channel chunein",
  "Apna Niche Chunein",
  "Topic chunein",
  "Video ki length",
  "Voice chunein",
  "Publish mode",
  "Ready to launch",
];

const STEP_SUBTITLES: string[] = [
  "Video kis channel par publish hogi?",
  "Select a niche to tailor your video. Pick one or create your own to continue.",
  "Auto mode mein agent best topic dhoondhega, ya khud topic likhein.",
  "Length se script ke words aur cost dono tay hote hain.",
  "Narration ke liye voice chunein — preview sun sakte hain.",
  "Video review ke baad jayegi ya seedha publish hogi?",
  "Sab choices check kar lein, phir production start karein.",
];

export default function NewVideoWizard() {
  const navigate = useNavigate();
  const nichesQuery = useWizardNiches();
  const channelsQuery = useWizardChannels();
  const createNiche = useCreateNicheProfile();
  const createProject = useCreateProject();

  const niches = nichesQuery.data ?? [];
  const channels = channelsQuery.data ?? [];

  const [step, setStep] = useState<number>(0);
  const [channelId, setChannelId] = useState<string | null>(null);
  const [nicheId, setNicheId] = useState<string | null>(null);
  const [otherName, setOtherName] = useState<string>("");
  const [otherIdea, setOtherIdea] = useState<string>("");
  const [saveProfile, setSaveProfile] = useState<boolean>(true);
  const [nicheErrors, setNicheErrors] = useState<NicheFieldErrors>({});
  const [resolvedOther, setResolvedOther] = useState<{
    id: string;
    name: string;
    signature: string;
  } | null>(null);
  const [topicMode, setTopicMode] = useState<TopicMode>("auto");
  const [manualTopic, setManualTopic] = useState<string>("");
  const [lengthMinutes, setLengthMinutes] = useState<number>(10);
  const [voiceId, setVoiceId] = useState<string>(VOICES[0].id);
  const [publishMode, setPublishMode] = useState<PublishMode>("review");
  const [launchError, setLaunchError] = useState<string | null>(null);
  const [launching, setLaunching] = useState<boolean>(false);
  const launchGuardRef = useRef<boolean>(false);

  // Auto-select the first connected channel once the list arrives, and
  // keep the selection valid if the list changes.
  useEffect(() => {
    if (channels.length === 0) return;
    if (channelId === null || !channels.some((channel) => channel.id === channelId)) {
      setChannelId(channels[0].id);
    }
  }, [channels, channelId]);

  const selectedChannel = channels.find((channel) => channel.id === channelId) ?? null;
  const selectedNiche = niches.find((niche) => niche.id === nicheId) ?? null;
  const isOtherNiche = nicheId === OTHER_NICHE_ID;
  const otherSignature = `${otherName.trim()}${otherIdea.trim()}`;
  const otherAlreadyCreated =
    resolvedOther !== null && resolvedOther.signature === otherSignature;
  /** The niche_profile_id a project would use right now (null = not yet created). */
  const effectiveNicheId = isOtherNiche
    ? otherAlreadyCreated
      ? resolvedOther.id
      : null
    : nicheId;
  const nicheSummary = isOtherNiche
    ? otherName.trim() || "Other (custom)"
    : (selectedNiche?.name ??
      (resolvedOther && nicheId === resolvedOther.id ? resolvedOther.name : "Not selected"));
  const topicSummary =
    topicMode === "auto"
      ? "Auto — agent topic chune ga"
      : manualTopic.trim() || "Not set";
  const selectedVoice = VOICES.find((voice) => voice.id === voiceId) ?? VOICES[0];
  const wordsEstimate = lengthMinutes * 150;
  const costEstimate = (lengthMinutes * 0.042).toFixed(2);
  const timeEstimate = Math.round(lengthMinutes * 1.8);
  const backendDown = nichesQuery.isError || channelsQuery.isError;

  const summaryRows: Array<{ label: string; value: string }> = [
    {
      label: "Channel",
      value:
        selectedChannel?.displayName ??
        (channelsQuery.isLoading ? "Loading…" : "No channel connected"),
    },
    { label: "Niche", value: nicheSummary },
    { label: "Length", value: `${lengthMinutes} min · ~${wordsEstimate.toLocaleString()} words` },
    { label: "Voice", value: selectedVoice.name },
    { label: "Mode", value: publishMode === "review" ? "Review-first" : "Fully Automatic" },
  ];

  // ── Step gating ──────────────────────────────────────────────────────
  const channelStepBlocked = step === 0 && channelId === null;
  const nicheStepBlocked =
    step === 1 &&
    isOtherNiche &&
    validateOtherNiche(otherName, otherIdea) !== null;
  const topicStepBlocked = step === 2 && topicMode === "manual" && manualTopic.trim() === "";
  const stepBlocked =
    channelStepBlocked || nicheStepBlocked || topicStepBlocked || createNiche.isPending;
  const stepBlockReason = channelStepBlocked
    ? channelsQuery.isError
      ? "Backend se connect nahi ho raha — channel load nahi ho saka. Upar diye gaye message ke saath dobara koshish karein."
      : channelsQuery.isSuccess && channels.length === 0
        ? "Continue ke liye pehle Channels screen se ek YouTube channel connect karein."
        : "Channel load ho raha hai…"
    : nicheStepBlocked
      ? "Continue ke liye niche ka naam (2–40 characters) aur idea (kam az kam 10 characters) durust likhein."
      : topicStepBlocked
        ? "Manual mode mein aage badhne ke liye topic likhna zaroori hai."
        : null;

  // ── Launch gating ────────────────────────────────────────────────────
  const launchReasons: string[] = [];
  if (backendDown) {
    launchReasons.push(
      "Backend se connection nahi hai — backend online hone par hi launch hoga.",
    );
  }
  if (!selectedChannel) {
    launchReasons.push(
      "Koi channel selected nahi hai — bina channel_id ke project nahi ban sakta.",
    );
  }
  if (!isOtherNiche && !effectiveNicheId) {
    launchReasons.push("Koi niche selected nahi hai.");
  }
  if (isOtherNiche && !otherAlreadyCreated && validateOtherNiche(otherName, otherIdea)) {
    launchReasons.push(
      "Custom niche ka naam (2–40) aur idea (10–500) durust karein — launch par profile inhi se banega.",
    );
  }
  if (topicMode === "manual" && manualTopic.trim() === "") {
    launchReasons.push("Manual mode mein topic likhna zaroori hai.");
  }
  const launchDisabled = launching || launchReasons.length > 0;

  async function handleContinue() {
    if (stepBlocked) return;
    if (step === 1 && isOtherNiche) {
      const errors = validateOtherNiche(otherName, otherIdea);
      if (errors) {
        setNicheErrors(errors);
        return;
      }
      if (otherAlreadyCreated) {
        // Unchanged draft: reuse the already-created profile, no re-POST.
        setNicheId(resolvedOther.id);
      } else if (saveProfile) {
        try {
          const profile = await createNiche.mutateAsync({
            name: otherName.trim(),
            idea: otherIdea.trim(),
          });
          setResolvedOther({
            id: profile.id,
            name: profile.name,
            signature: otherSignature,
          });
          // On 201 the returned profile becomes the selected niche.
          setNicheId(profile.id);
          setNicheErrors({});
        } catch (err) {
          setNicheErrors(parseNicheFieldErrors(err));
          return;
        }
      }
      // saveProfile OFF: nothing is created here — the profile is
      // created at Launch instead (see the honest copy under the toggle).
    }
    setStep((previous) => Math.min(STEPS.length - 1, previous + 1));
  }

  async function handleLaunch() {
    if (launchGuardRef.current || launchDisabled) return;
    if (!selectedChannel) return;
    launchGuardRef.current = true;
    setLaunching(true);
    setLaunchError(null);
    try {
      let nicheProfileId = effectiveNicheId;
      if (!nicheProfileId) {
        // "Other" niche whose profile was not saved earlier (the
        // save-as-profile toggle was OFF, or the draft changed after
        // saving). The backend requires a niche_profile_id for every
        // project, so create the profile now and use it for this video.
        const errors = validateOtherNiche(otherName, otherIdea);
        if (errors) {
          setLaunchError(errors.name ?? errors.idea ?? "Custom niche durust karein.");
          return;
        }
        const profile = await createNiche.mutateAsync({
          name: otherName.trim(),
          idea: otherIdea.trim(),
        });
        setResolvedOther({
          id: profile.id,
          name: profile.name,
          signature: otherSignature,
        });
        nicheProfileId = profile.id;
      }
      const project = await createProject.mutateAsync({
        channelId: selectedChannel.id,
        nicheProfileId,
        topicMode,
        topic: topicMode === "manual" ? manualTopic.trim() : undefined,
        targetMinutes: lengthMinutes,
        publishMode: publishMode === "auto" ? "auto" : "review_first",
      });
      navigate(`/agents?projectId=${encodeURIComponent(project.id)}`);
    } catch (err) {
      // Stay on the Review & Launch step and show the envelope message.
      setLaunchError(getErrorMessage(err));
    } finally {
      launchGuardRef.current = false;
      setLaunching(false);
    }
  }

  return (
    <div className="min-h-full bg-app p-6 text-ink">
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[1fr_280px]">
        <div className="min-w-0">
          {/* Stepper */}
          <ol className="mb-6 flex items-start">
            {STEPS.map((label, index) => {
              const isDone = index < step;
              const isCurrent = index === step;
              return (
                <li key={label} className="flex flex-1 items-start last:flex-none">
                  <div className="flex flex-col items-center gap-1.5">
                    <span
                      className={
                        isDone
                          ? "flex h-9 w-9 items-center justify-center rounded-full bg-accent text-white"
                          : isCurrent
                            ? "flex h-9 w-9 items-center justify-center rounded-full bg-accent font-semibold text-white ring-4 ring-accent/30"
                            : "flex h-9 w-9 items-center justify-center rounded-full bg-raised font-semibold text-muted"
                      }
                    >
                      {isDone ? <Check className="h-4 w-4" /> : index + 1}
                    </span>
                    <span
                      className={
                        isCurrent
                          ? "text-xs font-medium whitespace-nowrap text-accent2"
                          : isDone
                            ? "text-xs whitespace-nowrap text-ink2"
                            : "text-xs whitespace-nowrap text-muted"
                      }
                    >
                      {label}
                    </span>
                  </div>
                  {index < STEPS.length - 1 && (
                    <span
                      className={
                        index < step
                          ? "mx-2 mt-4 h-px flex-1 bg-accent"
                          : "mx-2 mt-4 h-px flex-1 bg-line"
                      }
                    />
                  )}
                </li>
              );
            })}
          </ol>

          <header className="mb-5">
            <h1 className="text-2xl font-bold tracking-tight">
              {STEP_TITLES[step]} — Step {step + 1} of 7
            </h1>
            <p className="mt-1 text-sm text-ink2">{STEP_SUBTITLES[step]}</p>
          </header>

          {/* Step 1 — Channel */}
          {step === 0 && (
            <div className="space-y-3">
              {channelsQuery.isLoading && (
                <div className="rounded-xl border border-line bg-panel p-4 text-sm text-muted">
                  Channels load ho rahe hain…
                </div>
              )}
              {channelsQuery.isError && (
                <div className="rounded-xl border border-line bg-panel p-4">
                  <p className="flex items-center gap-2 text-sm font-semibold text-danger">
                    <AlertTriangle className="h-4 w-4" />
                    Backend se connect nahi ho raha
                  </p>
                  <p className="mt-1 text-xs leading-relaxed text-muted">
                    Channels load nahi ho sake — backend offline lagta hai. Backend
                    start karke dobara koshish karein.
                  </p>
                  <Button
                    variant="outline"
                    size="sm"
                    className="mt-3"
                    onClick={() => void channelsQuery.refetch()}
                  >
                    Retry
                  </Button>
                </div>
              )}
              {channelsQuery.isSuccess && channels.length === 0 && (
                <div className="rounded-xl border border-line bg-panel p-4">
                  <p className="text-sm font-semibold">Koi channel connected nahi hai</p>
                  <p className="mt-1 text-xs leading-relaxed text-muted">
                    Video launch karne ke liye pehle Channels screen se apna YouTube
                    channel connect karein — bina channel ke project nahi ban sakta,
                    is liye Launch yahan se aage nahi badhega.
                  </p>
                </div>
              )}
              {channels.map((channel) => {
                const selected = channelId === channel.id;
                const meta = [channel.handle, channel.status, channel.language]
                  .filter((part): part is string => typeof part === "string" && part !== "")
                  .join(" · ");
                return (
                  <button
                    key={channel.id}
                    type="button"
                    onClick={() => setChannelId(channel.id)}
                    className={
                      selected
                        ? "flex w-full items-center gap-3 rounded-xl border border-accent bg-raised p-4 text-left"
                        : "flex w-full items-center gap-3 rounded-xl border border-line bg-panel p-4 text-left hover:border-accent/50"
                    }
                  >
                    <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-danger text-white">
                      <MonitorPlay className="h-5 w-5" />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-semibold">
                        {channel.displayName}
                      </span>
                      {meta && <span className="block text-xs text-muted">{meta}</span>}
                    </span>
                    <span
                      className={
                        selected
                          ? "h-4 w-4 shrink-0 rounded-full border-4 border-accent bg-white"
                          : "h-4 w-4 shrink-0 rounded-full border-2 border-line"
                      }
                    />
                  </button>
                );
              })}
            </div>
          )}

          {/* Step 2 — Niche */}
          {step === 1 && (
            <div>
              {nichesQuery.isLoading && (
                <div className="rounded-xl border border-line bg-panel p-4 text-sm text-muted">
                  Niches load ho rahe hain…
                </div>
              )}
              {nichesQuery.isError && (
                <div className="mb-4 rounded-xl border border-line bg-panel p-4">
                  <p className="flex items-center gap-2 text-sm font-semibold text-danger">
                    <AlertTriangle className="h-4 w-4" />
                    Backend se connect nahi ho raha
                  </p>
                  <p className="mt-1 text-xs leading-relaxed text-muted">
                    Saved niches load nahi ho sake — backend offline lagta hai.
                    Backend start karke dobara koshish karein.
                  </p>
                  <Button
                    variant="outline"
                    size="sm"
                    className="mt-3"
                    onClick={() => void nichesQuery.refetch()}
                  >
                    Retry
                  </Button>
                </div>
              )}
              {nichesQuery.isSuccess && (
                <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
                  {niches.map((niche) => {
                    const Icon = BUILTIN_ICONS[niche.id] ?? Sparkles;
                    const descriptor =
                      BUILTIN_DESCRIPTORS[niche.id] ??
                      niche.idea ??
                      "Saved custom niche profile.";
                    const selected = nicheId === niche.id;
                    return (
                      <button
                        key={niche.id}
                        type="button"
                        onClick={() => setNicheId(niche.id)}
                        className={
                          selected
                            ? "rounded-xl border border-accent bg-raised p-4 text-left"
                            : "rounded-xl border border-line bg-panel p-4 text-left hover:border-accent/50"
                        }
                      >
                        <Icon className="mb-2 h-6 w-6 text-accent2" />
                        <span className="block text-sm font-semibold">{niche.name}</span>
                        <span className="mt-1 block text-xs leading-relaxed text-muted">
                          {descriptor}
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}

              <div
                onClick={() => setNicheId(OTHER_NICHE_ID)}
                className={
                  isOtherNiche
                    ? "mt-4 cursor-pointer rounded-xl border-2 border-accent bg-panel p-4 shadow-[0_0_28px_rgba(110,91,255,0.35)]"
                    : "mt-4 cursor-pointer rounded-xl border-2 border-accent/50 bg-panel p-4 hover:border-accent"
                }
              >
                <div className="mb-3 flex items-center justify-between">
                  <span className="flex items-center gap-2 font-semibold text-accent2">
                    <Sparkles className="h-4 w-4" />
                    OTHER — apna niche likhein
                  </span>
                  <Badge tone="violet">Custom niche</Badge>
                </div>

                <div className="mb-1 flex items-center justify-between">
                  <label className="block text-xs font-medium text-ink2">
                    Niche ka naam (2–40 characters)
                  </label>
                  <span className="tnum font-mono text-xs text-muted">
                    {otherName.trim().length} / 40
                  </span>
                </div>
                <input
                  type="text"
                  value={otherName}
                  onChange={(event) => {
                    setOtherName(event.target.value);
                    setNicheErrors((previous) => ({
                      ...previous,
                      name: undefined,
                      form: undefined,
                    }));
                  }}
                  onClick={(event) => event.stopPropagation()}
                  maxLength={40}
                  placeholder="e.g. Space Science, Productivity, Cooking"
                  className="mb-1 w-full rounded-lg border border-line bg-raised px-3 py-2 text-sm text-ink placeholder:text-muted focus:border-accent focus:outline-none"
                />
                {nicheErrors.name && (
                  <p className="mb-2 text-xs text-danger">{nicheErrors.name}</p>
                )}
                <div className="mb-3" hidden={Boolean(nicheErrors.name)} />

                <label className="mb-1 block text-xs font-medium text-ink2">
                  Apna niche idea (10–500 characters)
                </label>
                <textarea
                  value={otherIdea}
                  onChange={(event) => {
                    setOtherIdea(event.target.value);
                    setNicheErrors((previous) => ({
                      ...previous,
                      idea: undefined,
                      form: undefined,
                    }));
                  }}
                  onClick={(event) => event.stopPropagation()}
                  maxLength={500}
                  rows={3}
                  placeholder="Describe your niche idea… what kind of stories or content should the agent focus on?"
                  className="w-full resize-none rounded-lg border border-line bg-raised px-3 py-2 text-sm text-ink placeholder:text-muted focus:border-accent focus:outline-none"
                />
                <div className="mt-1 flex items-center justify-between">
                  <p className="text-xs text-muted">
                    The Niche Profiling Agent will build tone, pace and structure from
                    your idea.
                  </p>
                  <span className="tnum font-mono text-xs text-muted">
                    {otherIdea.length} / 500
                  </span>
                </div>
                {nicheErrors.idea && (
                  <p className="mt-1 text-xs text-danger">{nicheErrors.idea}</p>
                )}

                <div className="mt-3 flex items-center justify-between border-t border-line pt-3">
                  <span className="text-sm text-ink2">Save as reusable profile</span>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={saveProfile}
                    aria-label="Save as reusable profile"
                    onClick={(event) => {
                      event.stopPropagation();
                      setSaveProfile((previous) => !previous);
                    }}
                    className={
                      saveProfile
                        ? "flex h-6 w-11 items-center rounded-full bg-accent px-0.5"
                        : "flex h-6 w-11 items-center rounded-full bg-raised px-0.5"
                    }
                  >
                    <span
                      className={
                        saveProfile
                          ? "ml-auto h-5 w-5 rounded-full bg-white"
                          : "h-5 w-5 rounded-full bg-muted"
                      }
                    />
                  </button>
                </div>
                <p className="mt-2 text-xs leading-relaxed text-muted">
                  {saveProfile
                    ? "On: Continue dabane par ye profile save hoga, list mein reusable niche ban jayega, aur is video ke liye select ho jayega."
                    : "Off: abhi koi reusable profile save nahi hoga — lekin Launch ke waqt ek niche profile lazmi create hoga, kyunke backend har project ke liye niche_profile_id maangta hai. Wohi profile is video ke liye istemal hoga."}
                </p>
                {nicheErrors.form && (
                  <p className="mt-2 text-xs text-danger">{nicheErrors.form}</p>
                )}
                {createNiche.isPending && isOtherNiche && (
                  <p className="mt-2 text-xs text-muted">Profile save ho raha hai…</p>
                )}
              </div>
            </div>
          )}

          {/* Step 3 — Topic */}
          {step === 2 && (
            <div>
              <div className="mb-4 inline-flex rounded-lg border border-line bg-panel p-1">
                {(
                  [
                    { id: "auto", label: "Auto" },
                    { id: "manual", label: "Manual" },
                  ] as Array<{ id: TopicMode; label: string }>
                ).map((mode) => (
                  <button
                    key={mode.id}
                    type="button"
                    onClick={() => setTopicMode(mode.id)}
                    className={
                      topicMode === mode.id
                        ? "accent-gradient rounded-md px-4 py-1.5 text-sm font-medium text-white"
                        : "rounded-md px-4 py-1.5 text-sm text-muted hover:text-ink"
                    }
                  >
                    {mode.label}
                  </button>
                ))}
              </div>

              {topicMode === "auto" ? (
                <div className="rounded-xl border border-line bg-panel p-4">
                  <p className="text-sm font-medium">
                    Auto topic — abhi koi topic fixed nahi hai
                  </p>
                  <p className="mt-1 text-xs leading-relaxed text-muted">
                    Production start hone par Research agents is niche aur channel ke
                    hisaab se topic khud chuneinge (topic_mode: auto). Backend se koi
                    topic list ya score nahi aata, is liye yahan koi candidate list
                    nahi dikhayi gayi.
                  </p>
                </div>
              ) : (
                <div>
                  <input
                    type="text"
                    value={manualTopic}
                    onChange={(event) => setManualTopic(event.target.value)}
                    placeholder="Apna topic likhein…"
                    className="w-full rounded-lg border border-line bg-raised px-3 py-2.5 text-sm text-ink placeholder:text-muted focus:border-accent focus:outline-none"
                  />
                  <p className="mt-2 text-xs text-muted">
                    Ye topic seedha project mein save hoga (topic_mode: manual) —
                    backend manual mode mein topic lazmi maangta hai.
                  </p>
                </div>
              )}
            </div>
          )}

          {/* Step 4 — Length */}
          {step === 3 && (
            <Card className="p-5">
              <div className="mb-2 flex items-end justify-between">
                <span className="text-sm text-ink2">Target length</span>
                <span className="tnum text-3xl font-bold">
                  {lengthMinutes} <span className="text-base font-medium text-muted">min</span>
                </span>
              </div>
              <input
                type="range"
                min={3}
                max={30}
                step={1}
                value={lengthMinutes}
                onChange={(event) => setLengthMinutes(Number(event.target.value))}
                className="w-full accent-[#6E5BFF]"
                aria-label="Video length in minutes"
              />
              <div className="tnum mt-1 flex justify-between font-mono text-xs text-muted">
                <span>3 min</span>
                <span>30 min</span>
              </div>
              <div className="mt-4 flex gap-2">
                {[5, 10, 15, 20, 30].map((preset) => (
                  <button
                    key={preset}
                    type="button"
                    onClick={() => setLengthMinutes(preset)}
                    className={
                      lengthMinutes === preset
                        ? "accent-gradient rounded-lg px-3 py-1.5 text-sm font-medium text-white"
                        : "rounded-lg border border-line bg-raised px-3 py-1.5 text-sm text-ink2 hover:text-ink"
                    }
                  >
                    {preset} min
                  </button>
                ))}
              </div>
              <p className="tnum mt-4 text-sm text-ink2">
                ≈ {wordsEstimate.toLocaleString()} words of script ({lengthMinutes} min ×
                150 WPM)
              </p>
            </Card>
          )}

          {/* Step 5 — Voice */}
          {step === 4 && (
            <div className="space-y-3">
              {VOICES.map((voice) => {
                const selected = voiceId === voice.id;
                return (
                  <div
                    key={voice.id}
                    className={
                      selected
                        ? "flex items-center gap-3 rounded-xl border border-accent bg-raised p-4"
                        : "flex items-center gap-3 rounded-xl border border-line bg-panel p-4"
                    }
                  >
                    <button
                      type="button"
                      onClick={() => setVoiceId(voice.id)}
                      className="flex min-w-0 flex-1 items-center gap-3 text-left"
                    >
                      <span
                        className={
                          selected
                            ? "h-4 w-4 shrink-0 rounded-full border-4 border-accent bg-white"
                            : "h-4 w-4 shrink-0 rounded-full border-2 border-line"
                        }
                      />
                      <span className="min-w-0">
                        <span className="flex flex-wrap items-center gap-2 text-sm font-semibold">
                          <span className="font-mono">{voice.name}</span>
                          <Badge tone={voice.badgeTone}>{voice.badge}</Badge>
                        </span>
                        <span className="block text-xs text-muted">{voice.detail}</span>
                      </span>
                    </button>
                    <Button variant="ghost" size="sm">
                      <Play className="mr-1 h-3.5 w-3.5" />
                      Preview
                    </Button>
                  </div>
                );
              })}
            </div>
          )}

          {/* Step 6 — Publish Mode */}
          {step === 5 && (
            <div className="space-y-3">
              <button
                type="button"
                onClick={() => setPublishMode("review")}
                className={
                  publishMode === "review"
                    ? "w-full rounded-xl border border-accent bg-raised p-4 text-left"
                    : "w-full rounded-xl border border-line bg-panel p-4 text-left hover:border-accent/50"
                }
              >
                <span className="flex items-center gap-2 text-sm font-semibold">
                  Review-first
                  <Badge tone="green">Recommended</Badge>
                </span>
                <span className="mt-1 block text-xs leading-relaxed text-muted">
                  Video pehle Review queue mein jayegi — aap approve karenge to hi
                  publish hogi.
                </span>
              </button>

              <div className="w-full cursor-not-allowed rounded-xl border border-line bg-panel p-4 opacity-60">
                <span className="flex items-center gap-2 text-sm font-semibold">
                  Fully Automatic
                  <Badge tone="gray">Locked</Badge>
                </span>
                <span className="mt-1 block text-xs leading-relaxed text-muted">
                  Available after API audit passes + 5 clean publishes
                </span>
              </div>
            </div>
          )}

          {/* Step 7 — Launch */}
          {step === 6 && (
            <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1fr_240px]">
              <Card className="p-5">
                <h2 className="mb-3 text-base font-semibold">Summary</h2>
                <dl className="space-y-2.5 text-sm">
                  {[
                    ...summaryRows.slice(0, 2),
                    { label: "Topic", value: topicSummary },
                    ...summaryRows.slice(2),
                  ].map((row) => (
                    <div key={row.label} className="flex items-start justify-between gap-4">
                      <dt className="shrink-0 text-muted">{row.label}</dt>
                      <dd className="text-right font-medium">{row.value}</dd>
                    </div>
                  ))}
                </dl>
                {launchError && (
                  <p className="mt-4 flex items-start gap-2 rounded-lg border border-danger/40 bg-raised p-3 text-xs leading-relaxed text-danger">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    {launchError}
                  </p>
                )}
              </Card>

              <Card className="p-5">
                <p className="tnum font-mono text-lg font-semibold">
                  Estimated cost ~${costEstimate}
                </p>
                <p className="tnum mt-1 font-mono text-sm text-ink2">
                  Estimated time ~{timeEstimate} min
                </p>
                <p className="mt-2 text-xs text-muted">
                  Generation + render + upload, based on your chosen length and voice.
                </p>
                <div className="mt-4 space-y-2">
                  <Button
                    variant="primary"
                    className="w-full"
                    disabled={launchDisabled}
                    onClick={() => void handleLaunch()}
                  >
                    {launching ? "Starting…" : "Start production"}
                  </Button>
                  <Button variant="outline" className="w-full" disabled>
                    Save as template
                  </Button>
                  <p className="text-xs leading-relaxed text-muted">
                    Template/schedule abhi is wizard se wired nahi hai — schedule ke
                    liye posting cadence chahiye jo yahan collect nahi hoti, is liye
                    ye button koi template save nahi karta.
                  </p>
                  {launchReasons.length > 0 && (
                    <ul className="space-y-1">
                      {launchReasons.map((reason) => (
                        <li key={reason} className="text-xs leading-relaxed text-warn">
                          {reason}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </Card>
            </div>
          )}

          {/* Footer */}
          <div className="mt-6 flex items-center justify-between border-t border-line pt-4">
            <Button
              variant="ghost"
              onClick={() => setStep((previous) => Math.max(0, previous - 1))}
              disabled={step === 0 || launching}
            >
              Back
            </Button>
            {step < STEPS.length - 1 && (
              <Button
                variant="primary"
                onClick={() => void handleContinue()}
                disabled={stepBlocked}
              >
                {createNiche.isPending && step === 1 ? "Saving…" : "Continue"}
              </Button>
            )}
          </div>
          {stepBlockReason && (
            <p className="mt-2 text-right text-xs text-warn">{stepBlockReason}</p>
          )}
        </div>

        {/* Summary rail */}
        <aside className="hidden lg:block">
          <Card className="sticky top-6 p-5">
            <h2 className="mb-4 text-base font-semibold">Summary</h2>
            <dl className="space-y-3 text-sm">
              {summaryRows.map((row) => (
                <div key={row.label}>
                  <dt className="text-xs text-muted">{row.label}</dt>
                  <dd className="mt-0.5 truncate font-medium">{row.value}</dd>
                </div>
              ))}
            </dl>
            <div className="mt-5 border-t border-line pt-4">
              <p className="tnum font-mono text-sm font-semibold">
                Estimated cost ~${costEstimate}
              </p>
              <p className="tnum mt-1 font-mono text-xs text-muted">
                Estimated time ~{timeEstimate} min
              </p>
            </div>
          </Card>
        </aside>
      </div>
    </div>
  );
}
