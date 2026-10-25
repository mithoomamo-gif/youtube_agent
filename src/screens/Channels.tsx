import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Archive,
  ExternalLink,
  Film,
  FlaskConical,
  Link2,
  Play,
  Plus,
  RefreshCw,
  Upload,
} from "lucide-react";
import { Card } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { Badge } from "../components/ui/Badge";
import { ApiError } from "../api/types";
import {
  extractYoutubeSetupSteps,
  isFlowSuccess,
  isYoutubeClientNotConfigured,
  useChannels,
  useConnectChannel,
  useConnectFlowStatus,
  useDisconnectChannel,
  useNicheNames,
  useRevalidateChannel,
  useSaveOAuthClient,
} from "../api/queries/channels";
import type { ChannelWire } from "../api/queries/channels";
import { useChannelSelectionStore } from "../state/channelSelection";
import {
  useActivateStyleProfile,
  useArchiveCompetitor,
  useAttachCompetitorVideo,
  useCompetitorVideos,
  useCompetitors,
  useCreateCompetitor,
  useCreateCompetitorVideo,
  useCreateStyleProfile,
  useFetchCompetitorMetadata,
  useQueueVideoAnalysis,
  useStyleProfiles,
} from "../api/queries/styleLab";
import type {
  CompetitorChannelWire,
  CompetitorVideoWire,
} from "../api/queries/styleLab";

type BadgeTone = "violet" | "green" | "amber" | "red" | "gray";

type Notice = { kind: "info" | "error"; text: string };

/** The 409 `youtube_client_not_configured` state, rendered as a panel. */
interface SetupPanelState {
  message: string;
  steps: string[];
}

function humanize(value: string): string {
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function errorText(err: unknown): string {
  if (err instanceof ApiError && err.code === "NETWORK_ERROR") {
    return "The backend isn't reachable. Check that the app backend is running, then retry.";
  }
  return err instanceof Error ? err.message : "Something went wrong.";
}

function initialsOf(channel: ChannelWire): string {
  const source = channel.title || channel.handle || channel.id;
  const words = source.replace(/^@/, "").split(/\s+/).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  return source.slice(0, 2).toUpperCase() || "—";
}

/**
 * State badges are derived ONLY from fields the backend actually sends:
 * `status` (active | paused | disconnected | at_risk), a tolerated
 * `state` string (connected / needs_reauth / token-expired …), and the
 * audit private-only overlay when the payload carries it.
 */
function channelStates(
  channel: ChannelWire,
): Array<{ label: string; tone: BadgeTone }> {
  const states: Array<{ label: string; tone: BadgeTone }> = [];
  const raw = (channel.status ?? channel.state ?? "").toLowerCase();
  switch (raw) {
    case "active":
    case "connected":
    case "ok":
      states.push({ label: "Connected", tone: "green" });
      break;
    case "paused":
      states.push({ label: "Paused", tone: "gray" });
      break;
    case "disconnected":
      states.push({ label: "Disconnected", tone: "red" });
      break;
    case "at_risk":
      states.push({ label: "At risk", tone: "amber" });
      break;
    case "needs_reauth":
    case "token-expired":
    case "token_expired":
    case "expired":
      states.push({ label: "Needs re-auth", tone: "amber" });
      break;
    default:
      if (raw) states.push({ label: humanize(raw), tone: "gray" });
  }

  const audit = channel.audit_status?.toLowerCase();
  if (
    channel.audit_private_only === true ||
    channel.audit_private_only === 1 ||
    audit === "private_only" ||
    audit === "audit_pending" ||
    audit === "pending"
  ) {
    states.push({ label: "Audit: private-only", tone: "violet" });
  } else if (audit) {
    states.push({ label: humanize(audit), tone: "violet" });
  }
  return states;
}

// ── Style Lab ─────────────────────────────────────────────────────────────
//
// Competitor intake + style profiles, scoped to ONE selected channel
// (the backend nests every Style Lab route under the owning channel).
// Honesty rules: channel-info fetch results are shown exactly as the
// backend reports them — `metadata_unavailable` renders its own state
// plus guidance, and no competitor field is ever invented client-side.

const inputClass =
  "w-full rounded-lg border border-line bg-raised px-3 py-2 text-sm text-ink outline-none focus:border-accent placeholder:text-muted";
const labelClass = "mb-1 block text-xs font-medium text-ink2";

/** Exact guidance shown whenever channel-info fetch is not configured. */
const METADATA_UNAVAILABLE_GUIDANCE =
  "Channel info fetch is not configured — attach a video or paste details instead";

function formatDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const ss = String(sec).padStart(2, "0");
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${ss}`
    : `${m}:${ss}`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function videoTagList(tags: CompetitorVideoWire["tags"]): string[] {
  if (Array.isArray(tags)) return tags;
  if (typeof tags === "string" && tags.trim()) {
    return tags.split(",").map((t) => t.trim()).filter(Boolean);
  }
  return [];
}

function metadataChip(status: CompetitorChannelWire["metadata_status"]): {
  label: string;
  tone: BadgeTone;
} {
  switch (status) {
    case "fetched":
      return { label: "Channel info fetched", tone: "green" };
    case "metadata_unavailable":
      return { label: "Metadata unavailable", tone: "amber" };
    case "fetch_failed":
      return { label: "Channel info fetch failed", tone: "red" };
    default:
      return { label: "Channel info not fetched", tone: "gray" };
  }
}

function analysisChip(status: CompetitorVideoWire["analysis_status"]): {
  label: string;
  tone: BadgeTone;
} {
  switch (status) {
    case "queued":
      return { label: "Queued", tone: "amber" };
    case "analysing":
      return { label: "Analysing", tone: "amber" };
    case "analysed":
      return { label: "Analysed", tone: "green" };
    case "analysis_failed":
      return { label: "Analysis failed", tone: "red" };
    default:
      return { label: "Not queued", tone: "gray" };
  }
}

function videoSourceLabel(source: CompetitorVideoWire["source"]): string {
  switch (source) {
    case "attached_file":
      return "Attached file";
    case "url_metadata":
      return "From video URL";
    default:
      return "Pasted details";
  }
}

function competitorTitle(competitor: CompetitorChannelWire): string {
  return (
    competitor.display_name ||
    competitor.handle ||
    competitor.source_input ||
    competitor.id
  );
}

function CompetitorCard({
  channelId,
  competitor,
}: {
  channelId: string;
  competitor: CompetitorChannelWire;
}) {
  const videosQuery = useCompetitorVideos(channelId, competitor.id);
  const fetchMeta = useFetchCompetitorMetadata(channelId);
  const archive = useArchiveCompetitor(channelId);
  const createVideo = useCreateCompetitorVideo(channelId, competitor.id);
  const attachVideo = useAttachCompetitorVideo(channelId, competitor.id);
  const queueAnalysis = useQueueVideoAnalysis(channelId);

  const [metaNotice, setMetaNotice] = useState<Notice | null>(null);
  const [queueError, setQueueError] = useState<string | null>(null);

  const [showAttach, setShowAttach] = useState(false);
  const [attachFile, setAttachFile] = useState<File | null>(null);
  const [attachThumb, setAttachThumb] = useState<File | null>(null);
  const [attachError, setAttachError] = useState<string | null>(null);
  const [attachNotice, setAttachNotice] = useState<string | null>(null);
  const [attachFormKey, setAttachFormKey] = useState(0);

  const [showPaste, setShowPaste] = useState(false);
  const [pasteTitle, setPasteTitle] = useState("");
  const [pasteDescription, setPasteDescription] = useState("");
  const [pasteTags, setPasteTags] = useState("");
  const [pasteUrl, setPasteUrl] = useState("");
  const [pasteError, setPasteError] = useState<string | null>(null);
  const [pasteNotice, setPasteNotice] = useState<string | null>(null);

  const archived = competitor.status === "archived";
  const meta = metadataChip(competitor.metadata_status);
  const videos = videosQuery.data ?? [];

  const handleFetchMetadata = () => {
    setMetaNotice(null);
    fetchMeta.mutate(competitor.id, {
      onSuccess: (updated) => {
        if (updated.metadata_status === "metadata_unavailable") {
          setMetaNotice({ kind: "info", text: METADATA_UNAVAILABLE_GUIDANCE });
        } else if (updated.metadata_status === "fetched") {
          setMetaNotice({
            kind: "info",
            text: "Channel info fetched — the details shown are what the backend returned.",
          });
        } else if (updated.metadata_status === "fetch_failed") {
          setMetaNotice({
            kind: "error",
            text: "Channel info fetch failed — attach a video or paste details instead, or try again later.",
          });
        }
      },
      onError: (err) => setMetaNotice({ kind: "error", text: errorText(err) }),
    });
  };

  const handleAttach = () => {
    setAttachError(null);
    setAttachNotice(null);
    if (!attachFile) {
      setAttachError("Choose a video file first.");
      return;
    }
    attachVideo.mutate(
      { file: attachFile, thumbnail: attachThumb },
      {
        onSuccess: (video) => {
          setAttachNotice(
            `“${video.title || attachFile.name}” attached — it appears in the video list below.`,
          );
          setAttachFile(null);
          setAttachThumb(null);
          setAttachFormKey((k) => k + 1);
        },
        onError: (err) => setAttachError(errorText(err)),
      },
    );
  };

  const handlePaste = () => {
    setPasteError(null);
    setPasteNotice(null);
    const title = pasteTitle.trim();
    const videoUrl = pasteUrl.trim();
    if (!title && !videoUrl) {
      setPasteError("Add at least a title or a video URL.");
      return;
    }
    const tags = pasteTags
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
    createVideo.mutate(
      {
        // A supplied video URL makes this URL-metadata intake; without
        // one it is pasted metadata. Both are first-class sources.
        source: videoUrl ? "url_metadata" : "pasted_metadata",
        videoUrl: videoUrl || undefined,
        title: title || undefined,
        description: pasteDescription.trim() || undefined,
        tags: tags.length > 0 ? tags : undefined,
      },
      {
        onSuccess: (video) => {
          setPasteNotice(
            `“${video.title || title || "Video"}” saved — it appears in the video list below.`,
          );
          setPasteTitle("");
          setPasteDescription("");
          setPasteTags("");
          setPasteUrl("");
        },
        onError: (err) => setPasteError(errorText(err)),
      },
    );
  };

  const handleQueue = (video: CompetitorVideoWire) => {
    setQueueError(null);
    queueAnalysis.mutate(
      { competitorId: competitor.id, videoId: video.id },
      { onError: (err) => setQueueError(errorText(err)) },
    );
  };

  return (
    <div className="rounded-xl border border-line bg-raised/40 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-ink">
            {competitorTitle(competitor)}
          </p>
          <p className="mt-0.5 text-xs text-muted">
            {competitor.platform ? `${humanize(competitor.platform)} · ` : ""}
            Added as {competitor.source_input || "—"}
            {competitor.source_kind
              ? ` (${humanize(competitor.source_kind)})`
              : ""}
          </p>
          {competitor.handle && (
            <p className="mt-0.5 text-xs text-ink2">{competitor.handle}</p>
          )}
          {competitor.channel_url && (
            <a
              className="mt-0.5 inline-flex max-w-full items-center gap-1 truncate text-xs text-accent2 hover:underline"
              href={competitor.channel_url}
              target="_blank"
              rel="noreferrer"
            >
              <span className="truncate">{competitor.channel_url}</span>
              <ExternalLink className="h-3 w-3 shrink-0" />
            </a>
          )}
          {competitor.external_channel_id && (
            <p className="mt-0.5 font-mono text-[11px] text-muted">
              Channel ID: {competitor.external_channel_id}
            </p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge tone={meta.tone}>{meta.label}</Badge>
          {archived && <Badge tone="gray">Archived</Badge>}
        </div>
      </div>

      {competitor.metadata_status === "metadata_unavailable" && (
        <p className="mt-3 rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-xs leading-relaxed text-ink2">
          {METADATA_UNAVAILABLE_GUIDANCE}
        </p>
      )}
      {metaNotice && (
        <p
          className={`mt-3 text-xs leading-relaxed ${
            metaNotice.kind === "error" ? "text-danger" : "text-ink2"
          }`}
        >
          {metaNotice.text}
        </p>
      )}

      {!archived && (
        <div className="mt-3 flex flex-wrap gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={fetchMeta.isPending}
            onClick={handleFetchMetadata}
          >
            <RefreshCw className="mr-1.5 h-4 w-4" />
            {fetchMeta.isPending ? "Fetching…" : "Fetch channel info"}
          </Button>
          <Button
            variant={showAttach ? "primary" : "outline"}
            size="sm"
            onClick={() => setShowAttach((v) => !v)}
          >
            <Upload className="mr-1.5 h-4 w-4" />
            Attach video
          </Button>
          <Button
            variant={showPaste ? "primary" : "outline"}
            size="sm"
            onClick={() => setShowPaste((v) => !v)}
          >
            <Link2 className="mr-1.5 h-4 w-4" />
            Paste details
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={archive.isPending}
            onClick={() => archive.mutate(competitor.id)}
          >
            <Archive className="mr-1.5 h-4 w-4" />
            Archive
          </Button>
        </div>
      )}
      {archive.isError && (
        <p className="mt-2 text-xs text-danger">{errorText(archive.error)}</p>
      )}

      {showAttach && !archived && (
        <div
          key={attachFormKey}
          className="mt-3 rounded-lg border border-line bg-panel p-3"
        >
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className={labelClass} htmlFor={`attach-file-${competitor.id}`}>
                Video file (1–2 videos from this channel work best)
              </label>
              <input
                id={`attach-file-${competitor.id}`}
                type="file"
                accept="video/*"
                className="w-full text-xs text-ink2 file:mr-2 file:rounded-lg file:border file:border-line file:bg-raised file:px-2.5 file:py-1.5 file:text-xs file:text-ink"
                onChange={(e) => setAttachFile(e.target.files?.[0] ?? null)}
              />
            </div>
            <div>
              <label className={labelClass} htmlFor={`attach-thumb-${competitor.id}`}>
                Thumbnail (optional)
              </label>
              <input
                id={`attach-thumb-${competitor.id}`}
                type="file"
                accept="image/*"
                className="w-full text-xs text-ink2 file:mr-2 file:rounded-lg file:border file:border-line file:bg-raised file:px-2.5 file:py-1.5 file:text-xs file:text-ink"
                onChange={(e) => setAttachThumb(e.target.files?.[0] ?? null)}
              />
            </div>
          </div>
          {attachError && <p className="mt-2 text-xs text-danger">{attachError}</p>}
          {attachNotice && <p className="mt-2 text-xs text-ink2">{attachNotice}</p>}
          <div className="mt-3">
            <Button
              size="sm"
              disabled={attachVideo.isPending}
              onClick={handleAttach}
            >
              <Upload className="mr-1.5 h-4 w-4" />
              {attachVideo.isPending ? "Uploading…" : "Upload video"}
            </Button>
          </div>
        </div>
      )}

      {showPaste && !archived && (
        <div className="mt-3 rounded-lg border border-line bg-panel p-3">
          <div className="grid gap-3">
            <div>
              <label className={labelClass} htmlFor={`paste-title-${competitor.id}`}>
                Title
              </label>
              <input
                id={`paste-title-${competitor.id}`}
                className={inputClass}
                value={pasteTitle}
                onChange={(e) => setPasteTitle(e.target.value)}
                placeholder="The video's title, exactly as published"
              />
            </div>
            <div>
              <label className={labelClass} htmlFor={`paste-desc-${competitor.id}`}>
                Description
              </label>
              <textarea
                id={`paste-desc-${competitor.id}`}
                className={`${inputClass} min-h-20`}
                value={pasteDescription}
                onChange={(e) => setPasteDescription(e.target.value)}
                placeholder="Paste the video description (optional)"
              />
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label className={labelClass} htmlFor={`paste-tags-${competitor.id}`}>
                  Tags (comma-separated)
                </label>
                <input
                  id={`paste-tags-${competitor.id}`}
                  className={inputClass}
                  value={pasteTags}
                  onChange={(e) => setPasteTags(e.target.value)}
                  placeholder="tag one, tag two"
                />
              </div>
              <div>
                <label className={labelClass} htmlFor={`paste-url-${competitor.id}`}>
                  Video URL (optional)
                </label>
                <input
                  id={`paste-url-${competitor.id}`}
                  className={inputClass}
                  value={pasteUrl}
                  onChange={(e) => setPasteUrl(e.target.value)}
                  placeholder="https://www.youtube.com/watch?v=…"
                />
              </div>
            </div>
          </div>
          {pasteError && <p className="mt-2 text-xs text-danger">{pasteError}</p>}
          {pasteNotice && <p className="mt-2 text-xs text-ink2">{pasteNotice}</p>}
          <div className="mt-3">
            <Button
              size="sm"
              disabled={createVideo.isPending}
              onClick={handlePaste}
            >
              {createVideo.isPending ? "Saving…" : "Save video details"}
            </Button>
          </div>
        </div>
      )}

      {/* Videos for this competitor */}
      <div className="mt-4 border-t border-line pt-3">
        <p className="text-xs font-semibold tracking-wide text-ink2 uppercase">
          Videos
        </p>
        {videosQuery.isLoading && (
          <p className="mt-2 text-xs text-muted">Loading videos…</p>
        )}
        {videosQuery.isError && (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <p className="text-xs text-danger">{errorText(videosQuery.error)}</p>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void videosQuery.refetch()}
            >
              Retry
            </Button>
          </div>
        )}
        {!videosQuery.isLoading && !videosQuery.isError && videos.length === 0 && (
          <p className="mt-2 text-xs leading-relaxed text-muted">
            No videos yet — attach a video file or paste its details to start
            the analysis.
          </p>
        )}
        {queueError && <p className="mt-2 text-xs text-danger">{queueError}</p>}
        <div className="mt-2 space-y-2">
          {videos.map((video) => {
            const chip = analysisChip(video.analysis_status);
            const canQueue =
              video.analysis_status === "not_queued" ||
              video.analysis_status === "analysis_failed";
            const tags = videoTagList(video.tags);
            return (
              <div
                key={video.id}
                className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-line bg-panel px-3 py-2.5"
              >
                <div className="min-w-0 flex-1">
                  <p className="flex items-center gap-1.5 text-sm font-medium text-ink">
                    <Film className="h-4 w-4 shrink-0 text-accent2" />
                    <span className="truncate">
                      {video.title || "Untitled video"}
                    </span>
                  </p>
                  <p className="tnum mt-1 font-mono text-[11px] text-muted">
                    {videoSourceLabel(video.source)}
                    {video.duration_s !== null &&
                      ` · ${formatDuration(video.duration_s)}`}
                    {video.file_size_bytes !== null &&
                      ` · ${formatBytes(video.file_size_bytes)}`}
                    {video.width !== null && video.height !== null &&
                      ` · ${video.width}×${video.height}`}
                    {video.has_thumbnail ? " · Thumbnail attached" : ""}
                  </p>
                  {video.video_url && (
                    <a
                      className="mt-0.5 inline-flex max-w-full items-center gap-1 truncate text-xs text-accent2 hover:underline"
                      href={video.video_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      <span className="truncate">{video.video_url}</span>
                      <ExternalLink className="h-3 w-3 shrink-0" />
                    </a>
                  )}
                  {tags.length > 0 && (
                    <p className="mt-1 truncate text-xs text-muted">
                      Tags: {tags.join(", ")}
                    </p>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <Badge tone={chip.tone}>{chip.label}</Badge>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={!canQueue || queueAnalysis.isPending}
                    onClick={() => handleQueue(video)}
                  >
                    {video.analysis_status === "analysis_failed"
                      ? "Re-queue analysis"
                      : "Queue for analysis"}
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function StyleLabSection({
  channelId,
  channels,
  onSelectChannel,
}: {
  channelId: string;
  channels: ChannelWire[];
  onSelectChannel: (channelId: string) => void;
}) {
  const competitorsQuery = useCompetitors(channelId);
  const profilesQuery = useStyleProfiles(channelId);
  const createCompetitor = useCreateCompetitor(channelId);
  const createProfile = useCreateStyleProfile(channelId);
  const activateProfile = useActivateStyleProfile(channelId);

  const [compInput, setCompInput] = useState("");
  const [compName, setCompName] = useState("");
  const [compError, setCompError] = useState<string | null>(null);
  const [compNotice, setCompNotice] = useState<string | null>(null);

  const [profileName, setProfileName] = useState("");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [profileNotice, setProfileNotice] = useState<string | null>(null);

  const competitors = competitorsQuery.data ?? [];
  const profiles = profilesQuery.data ?? [];
  const selectable = competitors.filter((c) => c.status !== "archived");
  const nameOf = (id: string): string => {
    const found = competitors.find((c) => c.id === id);
    return found ? competitorTitle(found) : id;
  };

  const handleAddCompetitor = () => {
    setCompError(null);
    setCompNotice(null);
    const input = compInput.trim();
    if (!input) {
      setCompError("Paste a channel link, an @handle, or a channel ID first.");
      return;
    }
    createCompetitor.mutate(
      { input, displayName: compName.trim() || undefined },
      {
        onSuccess: (created) => {
          setCompNotice(
            `“${competitorTitle(created)}” added to this channel's Style Lab.`,
          );
          setCompInput("");
          setCompName("");
        },
        // Inline error straight from the API error envelope.
        onError: (err) => setCompError(errorText(err)),
      },
    );
  };

  const toggleCompetitor = (id: string) => {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
  };

  const handleCreateProfile = () => {
    setProfileError(null);
    setProfileNotice(null);
    const name = profileName.trim();
    if (!name) {
      setProfileError("Give the style profile a name first.");
      return;
    }
    if (selectedIds.length === 0) {
      setProfileError("Select at least one competitor for the profile.");
      return;
    }
    createProfile.mutate(
      { name, competitorIds: selectedIds },
      {
        onSuccess: (created) => {
          setProfileNotice(
            `Style profile “${created.name}” created (${humanize(created.status)}) — activate it when you're ready to generate in this style.`,
          );
          setProfileName("");
          setSelectedIds([]);
        },
        onError: (err) => setProfileError(errorText(err)),
      },
    );
  };

  return (
    <Card className="mt-6 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="flex items-center gap-2 text-base font-semibold text-ink">
            <FlaskConical className="h-5 w-5 text-accent2" />
            Style Lab
          </h2>
          <p className="mt-1 max-w-2xl text-sm leading-relaxed text-ink2">
            Add competitor channels for the selected channel only, attach
            1–2 of their videos (or paste the details), and the app studies
            the structure — hook, pacing, visuals, titles — as a style
            profile for this channel. Style is learned; footage, scripts
            and thumbnails are never copied.
          </p>
        </div>
        <div className="w-full sm:w-64">
          <label className={labelClass} htmlFor="style-lab-channel">
            Channel
          </label>
          <select
            id="style-lab-channel"
            className={inputClass}
            value={channelId}
            onChange={(e) => onSelectChannel(e.target.value)}
          >
            {channels.map((c) => (
              <option key={c.id} value={c.id}>
                {c.title || c.handle || c.id}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Add competitor */}
      <div className="mt-5 rounded-xl border border-line bg-raised/40 p-4">
        <p className="text-sm font-semibold text-ink">Add competitor</p>
        <div className="mt-3 grid gap-3 sm:grid-cols-[1fr_220px_auto] sm:items-end">
          <div>
            <label className={labelClass} htmlFor="style-lab-comp-input">
              Channel link, @handle, or channel ID
            </label>
            <input
              id="style-lab-comp-input"
              className={inputClass}
              value={compInput}
              onChange={(e) => setCompInput(e.target.value)}
              placeholder="https://www.youtube.com/@channel or @handle or UC…"
            />
          </div>
          <div>
            <label className={labelClass} htmlFor="style-lab-comp-name">
              Name (optional)
            </label>
            <input
              id="style-lab-comp-name"
              className={inputClass}
              value={compName}
              onChange={(e) => setCompName(e.target.value)}
              placeholder="Label for your list"
            />
          </div>
          <Button
            disabled={createCompetitor.isPending}
            onClick={handleAddCompetitor}
          >
            <Plus className="mr-1.5 h-4 w-4" />
            {createCompetitor.isPending ? "Adding…" : "Add competitor"}
          </Button>
        </div>
        {compError && <p className="mt-2 text-xs text-danger">{compError}</p>}
        {compNotice && <p className="mt-2 text-xs text-ink2">{compNotice}</p>}
      </div>

      {/* Competitor list — this channel only */}
      <div className="mt-5">
        <h3 className="text-sm font-semibold text-ink">
          Competitors for this channel
        </h3>
        {competitorsQuery.isLoading && (
          <p className="mt-3 text-sm text-muted">Loading competitors…</p>
        )}
        {competitorsQuery.isError && (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <p className="text-sm text-danger">
              {errorText(competitorsQuery.error)}
            </p>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void competitorsQuery.refetch()}
            >
              Retry
            </Button>
          </div>
        )}
        {!competitorsQuery.isLoading &&
          !competitorsQuery.isError &&
          competitors.length === 0 && (
            <p className="mt-3 rounded-lg border border-dashed border-line px-4 py-5 text-center text-sm text-ink2">
              No competitors yet — add one to start a style profile.
            </p>
          )}
        <div className="mt-3 space-y-3">
          {competitors.map((competitor) => (
            <CompetitorCard
              key={competitor.id}
              channelId={channelId}
              competitor={competitor}
            />
          ))}
        </div>
      </div>

      {/* Style profiles */}
      <div className="mt-6 border-t border-line pt-5">
        <h3 className="text-sm font-semibold text-ink">Style profiles</h3>
        <p className="mt-1 text-xs leading-relaxed text-muted">
          A profile bundles the selected competitors' analysed style for
          this channel. New profiles start as drafts; activate one to make
          it the channel's working style.
        </p>

        {profilesQuery.isLoading && (
          <p className="mt-3 text-sm text-muted">Loading style profiles…</p>
        )}
        {profilesQuery.isError && (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <p className="text-sm text-danger">{errorText(profilesQuery.error)}</p>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void profilesQuery.refetch()}
            >
              Retry
            </Button>
          </div>
        )}
        {!profilesQuery.isLoading &&
          !profilesQuery.isError &&
          profiles.length === 0 && (
            <p className="mt-3 text-sm text-muted">
              No style profiles yet for this channel.
            </p>
          )}
        <div className="mt-3 space-y-2">
          {profiles.map((profile) => (
            <div
              key={profile.id}
              className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-line bg-raised/40 px-3 py-2.5"
            >
              <div className="min-w-0">
                <p className="text-sm font-medium text-ink">
                  {profile.name}{" "}
                  <span className="font-mono text-[11px] text-muted">
                    v{profile.version}
                  </span>
                </p>
                <p className="mt-0.5 truncate text-xs text-muted">
                  {profile.competitor_ids.length} competitor
                  {profile.competitor_ids.length === 1 ? "" : "s"}
                  {profile.competitor_ids.length > 0
                    ? `: ${profile.competitor_ids.map(nameOf).join(", ")}`
                    : ""}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <Badge
                  tone={
                    profile.status === "active"
                      ? "green"
                      : profile.status === "superseded"
                        ? "gray"
                        : "amber"
                  }
                >
                  {humanize(profile.status)}
                </Badge>
                {profile.status !== "active" && (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={activateProfile.isPending}
                    onClick={() =>
                      activateProfile.mutate(profile.id, {
                        onError: (err) => setProfileError(errorText(err)),
                      })
                    }
                  >
                    Activate
                  </Button>
                )}
              </div>
            </div>
          ))}
        </div>

        {/* Create profile */}
        <div className="mt-4 rounded-xl border border-line bg-raised/40 p-4">
          <p className="text-sm font-semibold text-ink">Create style profile</p>
          <div className="mt-3 grid gap-3">
            <div>
              <label className={labelClass} htmlFor="style-lab-profile-name">
                Profile name
              </label>
              <input
                id="style-lab-profile-name"
                className={inputClass}
                value={profileName}
                onChange={(e) => setProfileName(e.target.value)}
                placeholder="e.g. Fast-cut facts style"
              />
            </div>
            <div>
              <span className={labelClass}>Competitors in this profile</span>
              {selectable.length === 0 ? (
                <p className="text-xs text-muted">
                  No competitors yet — add one to start a style profile.
                </p>
              ) : (
                <div className="space-y-1.5">
                  {selectable.map((competitor) => (
                    <label
                      key={competitor.id}
                      className="flex cursor-pointer items-center gap-2 text-sm text-ink2"
                    >
                      <input
                        type="checkbox"
                        className="h-4 w-4 accent-[#6E5BFF]"
                        checked={selectedIds.includes(competitor.id)}
                        onChange={() => toggleCompetitor(competitor.id)}
                      />
                      {competitorTitle(competitor)}
                      {competitor.handle && (
                        <span className="text-xs text-muted">
                          {competitor.handle}
                        </span>
                      )}
                    </label>
                  ))}
                </div>
              )}
            </div>
          </div>
          {profileError && (
            <p className="mt-2 text-xs text-danger">{profileError}</p>
          )}
          {profileNotice && (
            <p className="mt-2 text-xs text-ink2">{profileNotice}</p>
          )}
          <div className="mt-3">
            <Button
              size="sm"
              disabled={createProfile.isPending}
              onClick={handleCreateProfile}
            >
              {createProfile.isPending ? "Creating…" : "Create profile"}
            </Button>
          </div>
        </div>
      </div>
    </Card>
  );
}

/**
 * Save the app's own YouTube (Google Cloud) OAuth client credentials
 * into the credential store (PUT /channels/oauth-client). Shown inside
 * the `youtube_client_not_configured` setup panel. The client secret
 * is a password field, is never prefilled, and is cleared on save —
 * the backend stores it write-only and never returns it.
 */
function OAuthClientForm() {
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [saved, setSaved] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const saveOAuthClient = useSaveOAuthClient();

  const canSave =
    clientId.trim() !== "" &&
    clientSecret.trim() !== "" &&
    !saveOAuthClient.isPending;

  const handleSave = () => {
    if (!canSave) return;
    setFormError(null);
    saveOAuthClient.mutate(
      {
        client_id: clientId.trim(),
        client_secret: clientSecret.trim(),
      },
      {
        onSuccess: () => {
          setClientId("");
          setClientSecret("");
          setSaved(true);
        },
        onError: (err) => {
          setSaved(false);
          setFormError(
            `Couldn't save the client credentials: ${errorText(err)}`,
          );
        },
      },
    );
  };

  return (
    <div className="mt-4 rounded-md border border-line bg-raised/50 p-4">
      <h3 className="text-sm font-semibold text-ink">
        Already have a Google Cloud OAuth client? Save it here
      </h3>
      <p className="mt-1 text-xs leading-relaxed text-muted">
        Paste the Client ID and Client secret of your “Desktop app”
        OAuth client. They are stored only in this PC's credential
        store and are never shown again — the secret field stays empty
        after saving.
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <input
          type="text"
          value={clientId}
          onChange={(event) => {
            setClientId(event.target.value);
            setSaved(false);
          }}
          placeholder="Client ID"
          aria-label="YouTube OAuth Client ID"
          autoComplete="off"
          className="w-72 rounded-md border border-line bg-raised px-3 py-2 font-mono text-sm text-ink"
        />
        <input
          type="password"
          value={clientSecret}
          onChange={(event) => {
            setClientSecret(event.target.value);
            setSaved(false);
          }}
          placeholder="Client secret"
          aria-label="YouTube OAuth Client secret"
          autoComplete="off"
          className="w-64 rounded-md border border-line bg-raised px-3 py-2 font-mono text-sm text-ink"
        />
        <Button
          variant="primary"
          size="sm"
          disabled={!canSave}
          onClick={handleSave}
        >
          {saveOAuthClient.isPending ? "Saving…" : "Save credentials"}
        </Button>
      </div>
      {formError ? (
        <p className="mt-2 text-sm text-danger">{formError}</p>
      ) : null}
      {saved ? (
        <p className="mt-2 text-sm text-ok">
          Saved — YouTube sign-in is now configured. Press “Connect
          channel” above to sign in with your channel's Google account.
        </p>
      ) : null}
    </div>
  );
}

export default function Channels() {
  const [notice, setNotice] = useState<Notice | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [setupPanel, setSetupPanel] = useState<SetupPanelState | null>(null);
  const [flowPending, setFlowPending] = useState(false);
  const [flowId, setFlowId] = useState<string | null>(null);
  const selectedChannelId = useChannelSelectionStore(
    (s) => s.selectedChannelId,
  );
  const setSelectedChannelId = useChannelSelectionStore(
    (s) => s.setSelectedChannelId,
  );

  const channelsQuery = useChannels();
  const nicheNamesQuery = useNicheNames();
  const connect = useConnectChannel();
  const revalidate = useRevalidateChannel();
  const disconnect = useDisconnectChannel();

  const channels = channelsQuery.data ?? [];
  const nicheNames = nicheNamesQuery.data ?? {};

  // Style Lab is scoped to one selected channel; default to the first
  // channel and keep the selection valid as the list changes.
  useEffect(() => {
    if (channels.length === 0) return;
    if (!selectedChannelId || !channels.some((c) => c.id === selectedChannelId)) {
      setSelectedChannelId(channels[0].id);
    }
  }, [channels, selectedChannelId]);

  const selectedChannel =
    channels.find((c) => c.id === selectedChannelId) ?? channels[0] ?? null;

  // Poll the started connect flow; resolve the pending panel honestly
  // when the backend reports a terminal state.
  const flowStatus = useConnectFlowStatus(flowId);
  const flow = flowStatus.data;
  useEffect(() => {
    if (!flowId || !flow) return;
    if (isFlowSuccess(flow)) {
      setFlowPending(false);
      setFlowId(null);
      setNotice({
        kind: "info",
        text: "Channel connected and saved — it appears in the list below. It may show as needing an audit check until YouTube's audit clears.",
      });
      void channelsQuery.refetch();
    } else if (flow.status === "connect_failed") {
      setFlowPending(false);
      setFlowId(null);
      setNotice({
        kind: "error",
        text: `The connect flow didn't finish${
          flow.error_message ? `: ${flow.error_message}` : "."
        }`,
      });
    }
    // channelsQuery.refetch is stable enough for this one-shot effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flow, flowId]);

  const handleConnect = () => {
    setNotice(null);
    setSetupPanel(null);
    setFlowPending(false);
    setFlowId(null);
    connect.mutate(undefined, {
      onSuccess: (res) => {
        // A 2xx means the OAuth flow STARTED — the channel is only
        // connected once Google sign-in completes and the backend
        // confirms it, so this is shown as pending, not success.
        setFlowPending(true);
        setFlowId(res.flowId ?? null);
        setNotice({
          kind: "info",
          text: `Connect flow started${
            res.status ? ` (${res.status})` : ""
          } — finish the Google sign-in in your browser. No channel is connected yet.`,
        });
      },
      onError: (err) => {
        if (isYoutubeClientNotConfigured(err)) {
          // 409: the backend has no YouTube client credentials yet.
          // Render its ordered setup steps as a panel, not an error.
          setSetupPanel({
            message:
              err instanceof ApiError
                ? err.message
                : "YouTube client credentials are not configured.",
            steps: extractYoutubeSetupSteps(err),
          });
          return;
        }
        setNotice({ kind: "error", text: errorText(err) });
      },
    });
  };

  const handleRevalidate = (channel: ChannelWire) => {
    setNotice(null);
    revalidate.mutate(channel.id, {
      onSuccess: () =>
        setNotice({
          kind: "info",
          text: `Revalidation finished for ${
            channel.title || channel.id
          } — the channel's credential status above is the result YouTube returned.`,
        }),
      onError: (err) => setNotice({ kind: "error", text: errorText(err) }),
    });
  };

  const handleDisconnect = (channel: ChannelWire) => {
    setNotice(null);
    disconnect.mutate(channel.id, {
      onSuccess: () => {
        setConfirmingId(null);
        setNotice({
          kind: "info",
          text: `${channel.title || "Channel"} disconnected.`,
        });
      },
      onError: (err) => setNotice({ kind: "error", text: errorText(err) }),
    });
  };

  return (
    <div className="min-h-full bg-app p-6 text-ink">
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink">Channels</h1>
          <p className="mt-0.5 text-sm text-ink2">
            One niche per channel keeps each audience and identity clean.
          </p>
        </div>
        <Button variant="primary" disabled={connect.isPending} onClick={handleConnect}>
          <Plus className="mr-1.5 h-4 w-4" />
          {connect.isPending ? "Starting…" : "Connect channel"}
        </Button>
      </div>

      {notice && (
        <Card
          className={`mb-4 px-4 py-3 text-sm ${
            notice.kind === "error" ? "text-danger" : "text-ink2"
          }`}
        >
          {notice.text}
        </Card>
      )}

      {setupPanel && (
        <Card className="mb-4 border-warn/50 p-5">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-warn" />
            <div className="min-w-0">
              <h2 className="text-base font-semibold text-ink">
                YouTube sign-in isn't set up yet
              </h2>
              <p className="mt-1 text-sm leading-relaxed text-ink2">
                The app doesn't have its own YouTube (Google Cloud) client
                credentials yet, so there is nothing to sign in with.{" "}
                {setupPanel.message}
              </p>
              <OAuthClientForm />
              {setupPanel.steps.length > 0 ? (
                <ol className="mt-3 list-decimal space-y-1.5 pl-5 text-sm leading-relaxed text-ink2">
                  {setupPanel.steps.map((step, index) => (
                    <li key={index}>{step}</li>
                  ))}
                </ol>
              ) : (
                <p className="mt-3 text-sm text-ink2">
                  The backend didn't include the setup steps with this
                  response — check the backend log for details.
                </p>
              )}
              <p className="mt-3 text-xs text-muted">
                Once that's done, come back here and press “Connect
                channel” again.
              </p>
              <div className="mt-3">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setSetupPanel(null)}
                >
                  Dismiss
                </Button>
              </div>
            </div>
          </div>
        </Card>
      )}

      {flowPending && !setupPanel && (
        <Card className="mb-4 flex flex-wrap items-center gap-3 px-4 py-3">
          <RefreshCw className="h-4 w-4 shrink-0 animate-spin text-accent2" />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold text-ink">
              {flow?.status === "exchanging"
                ? "Sign-in received — finishing the connection…"
                : "Waiting for Google sign-in…"}
            </p>
            <p className="text-xs leading-relaxed text-ink2">
              Complete the sign-in in your browser window. The channel
              will appear below as soon as the backend confirms it — it
              isn't connected until then.
              {flowStatus.isError
                ? " (Couldn't check the flow status just now — use Refresh channels.)"
                : ""}
            </p>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void channelsQuery.refetch()}
          >
            Refresh channels
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setFlowPending(false);
              setFlowId(null);
            }}
          >
            Dismiss
          </Button>
        </Card>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {channelsQuery.isLoading &&
          [0, 1].map((i) => (
            <Card key={i} className="animate-pulse p-5">
              <div className="flex items-start gap-3">
                <div className="h-12 w-12 shrink-0 rounded-full bg-raised" />
                <div className="min-w-0 flex-1">
                  <div className="h-4 w-2/3 rounded bg-raised" />
                  <div className="mt-2 h-3 w-1/2 rounded bg-raised" />
                </div>
              </div>
              <div className="mt-4 h-5 w-24 rounded-full bg-raised" />
              <div className="mt-4 h-10 rounded bg-raised" />
            </Card>
          ))}

        {channelsQuery.isError && (
          <Card className="py-10 text-center lg:col-span-3">
            <p className="text-sm font-medium text-ink">Couldn't reach the backend</p>
            <p className="mx-auto mt-1 max-w-md text-sm text-muted">
              {errorText(channelsQuery.error)}
            </p>
            <div className="mt-4">
              <Button
                variant="outline"
                size="sm"
                onClick={() => void channelsQuery.refetch()}
              >
                Retry
              </Button>
            </div>
          </Card>
        )}

        {!channelsQuery.isLoading &&
          !channelsQuery.isError &&
          channels.map((channel) => {
            const nicheName =
              channel.default_niche_name ??
              (channel.default_niche_profile_id
                ? nicheNames[channel.default_niche_profile_id]
                : undefined);
            const voiceLabel =
              channel.default_voice_id || channel.default_voice_style || null;
            const confirming = confirmingId === channel.id;
            return (
              <Card key={channel.id} className="flex flex-col p-5">
                <div className="flex items-start gap-3">
                  <div className="accent-gradient flex h-12 w-12 shrink-0 items-center justify-center rounded-full text-sm font-bold text-ink">
                    {initialsOf(channel)}
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-semibold text-ink">
                      {channel.title || "Untitled channel"}
                    </p>
                    <p className="truncate text-xs text-muted">
                      {channel.handle || channel.youtube_channel_id || ""}
                    </p>
                  </div>
                </div>

                <div className="mt-3 flex flex-wrap items-center gap-1.5">
                  {nicheName && <Badge tone="violet">{nicheName}</Badge>}
                  {channelStates(channel).map((state) => (
                    <Badge key={state.label} tone={state.tone}>
                      {state.label}
                    </Badge>
                  ))}
                  {selectedChannel?.id === channel.id && (
                    <Badge tone="violet">In Style Lab</Badge>
                  )}
                </div>
                <p className="mt-1.5 text-xs text-muted">
                  {nicheName ? "1 niche = 1 channel" : "No niche assigned yet"}
                </p>

                <div className="tnum mt-4 grid grid-cols-2 gap-3 border-t border-line pt-3 font-mono">
                  <div>
                    <p className="text-sm font-semibold text-ink">
                      {channel.language || "—"}
                    </p>
                    <p className="font-sans text-xs text-muted">Language</p>
                  </div>
                  <div>
                    <p className="text-sm font-semibold text-ink">
                      {channel.daily_upload_cap !== null &&
                      channel.daily_upload_cap !== undefined
                        ? channel.daily_upload_cap
                        : "No cap"}
                    </p>
                    <p className="font-sans text-xs text-muted">Daily upload cap</p>
                  </div>
                  <div>
                    <p className="truncate text-sm font-semibold text-ink" title={voiceLabel ?? undefined}>
                      {voiceLabel ?? "Not set"}
                    </p>
                    <p className="font-sans text-xs text-muted">
                      Voice (from niche profile)
                    </p>
                  </div>
                  <div>
                    <p className="text-sm font-semibold text-ink">
                      {channel.monthly_budget_micros !== null &&
                      channel.monthly_budget_micros !== undefined
                        ? `$${(channel.monthly_budget_micros / 1_000_000).toFixed(2)}`
                        : "No cap"}
                    </p>
                    <p className="font-sans text-xs text-muted">Monthly budget</p>
                  </div>
                  <div>
                    <p className="text-sm font-semibold text-ink">
                      {channel.quality_tier ? humanize(channel.quality_tier) : "Draft"}
                    </p>
                    <p className="font-sans text-xs text-muted">Quality tier</p>
                  </div>
                </div>

                {channel.publish_mode && (
                  <p className="mt-3 flex items-center gap-1.5 text-xs text-ink2">
                    <Play className="h-3.5 w-3.5 text-accent2" />
                    Publish mode:{" "}
                    <span className="font-medium text-ink">
                      {humanize(channel.publish_mode)}
                    </span>
                  </p>
                )}

                {confirming ? (
                  <div className="mt-4 rounded-lg border border-line bg-raised px-3 py-2.5">
                    <p className="text-xs leading-relaxed text-ink2">
                      Disconnect {channel.title || "this channel"}? It will be
                      marked disconnected and its stored credential removed.
                      Already-published videos and project history are not
                      affected.
                    </p>
                    <div className="mt-2.5 flex gap-2">
                      <Button
                        variant="danger"
                        size="sm"
                        disabled={disconnect.isPending}
                        onClick={() => handleDisconnect(channel)}
                      >
                        Disconnect channel
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setConfirmingId(null)}
                      >
                        Keep channel
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="mt-4 flex flex-wrap gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={revalidate.isPending}
                      onClick={() => handleRevalidate(channel)}
                    >
                      <RefreshCw className="mr-1.5 h-4 w-4" />
                      Revalidate
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setSelectedChannelId(channel.id)}
                    >
                      <FlaskConical className="mr-1.5 h-4 w-4" />
                      Style Lab
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setConfirmingId(channel.id)}
                    >
                      Disconnect
                    </Button>
                  </div>
                )}
              </Card>
            );
          })}

        {/* Connect / empty state */}
        {!channelsQuery.isLoading && !channelsQuery.isError && (
          <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-line bg-panel p-6 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-raised text-muted">
              <Plus className="h-6 w-6" />
            </div>
            <p className="mt-3 text-sm font-semibold text-ink">
              {channels.length === 0
                ? "No channels connected yet"
                : "Connect another channel"}
            </p>
            <p className="mt-1.5 max-w-60 text-xs leading-relaxed text-ink2">
              Connecting opens your system browser for Google sign-in — the app
              never sees your password. Pick the exact channel in the picker and
              confirm it before anything is bound.
            </p>
            <div className="mt-4">
              <Button
                variant="outline"
                size="sm"
                disabled={connect.isPending}
                onClick={handleConnect}
              >
                <ExternalLink className="mr-1.5 h-4 w-4" />
                Connect channel
              </Button>
            </div>
            <p className="mt-3 text-xs text-muted">1 niche = 1 channel</p>
          </div>
        )}
      </div>

      {/* Style Lab — scoped to the selected channel only */}
      {selectedChannel ? (
        <StyleLabSection
          key={selectedChannel.id}
          channelId={selectedChannel.id}
          channels={channels}
          onSelectChannel={setSelectedChannelId}
        />
      ) : (
        !channelsQuery.isLoading &&
        !channelsQuery.isError && (
          <Card className="mt-6 p-5">
            <h2 className="flex items-center gap-2 text-base font-semibold text-ink">
              <FlaskConical className="h-5 w-5 text-accent2" />
              Style Lab
            </h2>
            <p className="mt-1 text-sm leading-relaxed text-ink2">
              Connect a channel first — competitor lists and style profiles
              are scoped to one of your own channels.
            </p>
          </Card>
        )
      )}
    </div>
  );
}
