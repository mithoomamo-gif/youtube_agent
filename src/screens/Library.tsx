import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  ExternalLink,
  FolderOpen,
  Pause,
  Play,
  RotateCcw,
  Search,
  X,
} from "lucide-react";
import { Card } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { Badge } from "../components/ui/Badge";
import { ApiError } from "../api/types";
import {
  PROJECT_STATUSES,
  PROJECT_STATUS_LABELS,
  projectStatusLabel,
  usePipelineAction,
  useProject,
  useProjectJobs,
  useProjectPublication,
  useProjects,
} from "../api/queries/library";
import type {
  PipelineAction,
  ProjectStatus,
  ProjectWire,
} from "../api/queries/library";
import { useChannels, useNicheNames } from "../api/queries/channels";

type BadgeTone = "violet" | "green" | "amber" | "red" | "gray";

const STATUS_TONES: Record<string, BadgeTone> = {
  draft: "gray",
  in_production: "violet",
  awaiting_review: "amber",
  qa: "violet",
  ready: "green",
  scheduled: "gray",
  published: "green",
  failed: "red",
  blocked: "red",
  cancelled: "gray",
};

function statusTone(status: string | undefined): BadgeTone {
  return (status && STATUS_TONES[status]) || "gray";
}

function jobTone(status: string | undefined): BadgeTone {
  switch (status) {
    case "done":
    case "succeeded":
      return "green";
    case "failed":
    case "dead":
      return "red";
    case "running":
    case "leased":
      return "violet";
    default:
      return "gray";
  }
}

function toDate(value: number | string | null | undefined): Date | null {
  if (value === null || value === undefined) return null;
  const d = typeof value === "number" ? new Date(value) : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

function relativeTime(value: number | string | null | undefined): string {
  const d = toDate(value);
  if (!d) return "—";
  const seconds = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return d.toLocaleDateString();
}

function fullDate(value: number | string | null | undefined): string {
  const d = toDate(value);
  return d ? d.toLocaleString() : "—";
}

function formatCost(micros: number | null | undefined): string {
  if (micros === null || micros === undefined) return "—";
  return `$${(micros / 1_000_000).toFixed(2)}`;
}

function humanize(value: string | null | undefined): string {
  if (!value) return "—";
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function projectTitle(project: ProjectWire): string {
  return project.topic || project.title || "Untitled project";
}

const STARTABLE: ReadonlySet<string> = new Set([
  "draft",
  "ready",
  "blocked",
  "failed",
  "cancelled",
]);
const RUNNING: ReadonlySet<string> = new Set(["in_production", "qa"]);
const ACTIVE: ReadonlySet<string> = new Set([
  "in_production",
  "qa",
  "awaiting_review",
  "scheduled",
]);

type Notice = { kind: "ok" | "error"; text: string };

function errorText(err: unknown): string {
  if (err instanceof ApiError && err.code === "NETWORK_ERROR") {
    return "The backend isn't reachable. Check that the app backend is running, then retry.";
  }
  return err instanceof Error ? err.message : "Something went wrong.";
}

// ── Detail view ────────────────────────────────────────────────────────────

type DetailTab = "Overview" | "History" | "Cost" | "Performance";
const DETAIL_TABS: DetailTab[] = ["Overview", "History", "Cost", "Performance"];

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="text-xs text-muted">{label}</p>
      <div className="mt-0.5 text-sm text-ink">{children}</div>
    </div>
  );
}

function ProjectDetail({
  projectId,
  channelNames,
  nicheNames,
  onBack,
}: {
  projectId: string;
  channelNames: Record<string, string>;
  nicheNames: Record<string, string>;
  onBack: () => void;
}) {
  const [tab, setTab] = useState<DetailTab>("Overview");
  const [notice, setNotice] = useState<Notice | null>(null);
  const projectQuery = useProject(projectId);
  const jobsQuery = useProjectJobs(projectId);
  const publicationQuery = useProjectPublication(projectId);
  const pipelineAction = usePipelineAction();
  const project = projectQuery.data;

  const runAction = (action: PipelineAction) => {
    setNotice(null);
    pipelineAction.mutate(
      { id: projectId, action },
      {
        onSuccess: (res) =>
          setNotice({
            kind: "ok",
            text: `Pipeline ${action} accepted by the backend${
              res.jobId ? ` (job ${res.jobId.slice(0, 8)}…)` : ""
            }. Status will update here as work progresses.`,
          }),
        onError: (err) => setNotice({ kind: "error", text: errorText(err) }),
      },
    );
  };

  const busy = pipelineAction.isPending;

  return (
    <div>
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="sm" onClick={onBack}>
            <ArrowLeft className="mr-1.5 h-4 w-4" />
            Library
          </Button>
          <div>
            <h1 className="max-w-xl truncate text-xl font-semibold text-ink">
              {project ? projectTitle(project) : "Project"}
            </h1>
            <p className="mt-0.5 text-sm text-ink2">
              {project
                ? `${channelNames[project.channel_id ?? ""] ?? "Unknown channel"} · Updated ${relativeTime(project.updated_at)}`
                : "Loading project…"}
            </p>
          </div>
        </div>
        {project && (
          <div className="flex shrink-0 items-center gap-1.5">
            {STARTABLE.has(project.status ?? "") && (
              <Button
                variant="primary"
                size="sm"
                disabled={busy}
                onClick={() => runAction("start")}
              >
                <Play className="mr-1.5 h-4 w-4" />
                Start
              </Button>
            )}
            {RUNNING.has(project.status ?? "") && (
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => runAction("pause")}
              >
                <Pause className="mr-1.5 h-4 w-4" />
                Pause
              </Button>
            )}
            {(RUNNING.has(project.status ?? "") ||
              project.status === "awaiting_review") && (
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => runAction("resume")}
              >
                <RotateCcw className="mr-1.5 h-4 w-4" />
                Resume
              </Button>
            )}
            {ACTIVE.has(project.status ?? "") && (
              <Button
                variant="ghost"
                size="sm"
                disabled={busy}
                onClick={() => runAction("cancel")}
              >
                <X className="mr-1.5 h-4 w-4" />
                Cancel
              </Button>
            )}
          </div>
        )}
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

      <div className="mb-5 flex flex-wrap gap-2">
        {DETAIL_TABS.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={`rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${
              tab === t
                ? "accent-gradient border-transparent text-ink"
                : "border-line bg-panel text-ink2 hover:text-ink"
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      {projectQuery.isLoading && (
        <Card className="animate-pulse p-5">
          <div className="h-4 w-1/3 rounded bg-raised" />
          <div className="mt-3 h-4 w-2/3 rounded bg-raised" />
          <div className="mt-3 h-4 w-1/2 rounded bg-raised" />
        </Card>
      )}

      {projectQuery.isError && (
        <Card className="py-10 text-center">
          <p className="text-sm font-medium text-ink">Couldn't load this project</p>
          <p className="mx-auto mt-1 max-w-md text-sm text-muted">
            {errorText(projectQuery.error)}
          </p>
          <div className="mt-4">
            <Button variant="outline" size="sm" onClick={() => void projectQuery.refetch()}>
              Retry
            </Button>
          </div>
        </Card>
      )}

      {project && tab === "Overview" && (
        <Card className="p-5">
          <div className="grid grid-cols-2 gap-x-6 gap-y-4 lg:grid-cols-3">
            <Field label="Status">
              <Badge tone={statusTone(project.status)}>
                {projectStatusLabel(project.status)}
              </Badge>
            </Field>
            <Field label="Phase">
              {project.current_phase ? humanize(project.current_phase) : "Not started"}
            </Field>
            <Field label="Channel">
              {channelNames[project.channel_id ?? ""] ?? project.channel_id ?? "—"}
            </Field>
            <Field label="Niche">
              {nicheNames[project.niche_profile_id ?? ""] ??
                project.niche_profile_id ??
                "—"}
            </Field>
            <Field label="Target length">
              {project.target_minutes ? `${project.target_minutes} min` : "—"}
            </Field>
            <Field label="Publish mode">{humanize(project.publish_mode)}</Field>
            <Field label="Topic source">
              {humanize(project.topic_source ?? project.topic_mode)}
            </Field>
            <Field label="Created">{fullDate(project.created_at)}</Field>
            <Field label="Updated">{fullDate(project.updated_at)}</Field>
            {project.youtube_video_id && (
              <Field label="YouTube video ID">{project.youtube_video_id}</Field>
            )}
            {project.published_at && (
              <Field label="Published">{fullDate(project.published_at)}</Field>
            )}
          </div>
          {project.error_summary && (
            <p className="mt-4 rounded-lg bg-danger/10 px-3 py-2 text-sm text-danger">
              {project.error_summary}
            </p>
          )}
          {project.task_dag && (
            <div className="mt-5 border-t border-line pt-4">
              <p className="text-xs text-muted">Pipeline plan</p>
              <p className="mt-1 text-sm text-ink2">
                {humanize(project.task_dag.status)}
                {project.task_dag.total_agents
                  ? ` · ${project.task_dag.total_agents} agents`
                  : ""}
              </p>
              {project.task_dag.phases && project.task_dag.phases.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {project.task_dag.phases.map((phase) => (
                    <Badge key={phase} tone="gray">
                      {humanize(phase)}
                    </Badge>
                  ))}
                </div>
              )}
            </div>
          )}
        </Card>
      )}

      {project && tab === "History" && (
        <div className="flex flex-col gap-2">
          {jobsQuery.isLoading && (
            <Card className="animate-pulse px-4 py-3">
              <div className="h-4 w-1/2 rounded bg-raised" />
            </Card>
          )}
          {jobsQuery.isError && (
            <Card className="py-8 text-center text-sm text-muted">
              Couldn't load history — {errorText(jobsQuery.error)}
            </Card>
          )}
          {jobsQuery.data && jobsQuery.data.length === 0 && (
            <Card className="py-10 text-center text-sm text-muted">
              No pipeline activity recorded for this project yet.
            </Card>
          )}
          {jobsQuery.data?.map((job) => (
            <Card key={job.id} className="flex items-center gap-4 px-4 py-3">
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-ink">
                  {job.kind ?? "Pipeline job"}
                  {job.agent_key ? (
                    <span className="text-ink2"> · {job.agent_key}</span>
                  ) : null}
                </p>
                <p className="mt-0.5 text-xs text-ink2">
                  Updated {relativeTime(job.updated_at ?? job.created_at)}
                  {typeof job.attempts === "number" && job.attempts > 0
                    ? ` · ${job.attempts} attempt${job.attempts === 1 ? "" : "s"}`
                    : ""}
                </p>
              </div>
              <Badge tone={jobTone(job.status)}>{humanize(job.status)}</Badge>
            </Card>
          ))}
        </div>
      )}

      {project && tab === "Cost" && (
        <Card className="p-5">
          {project.total_cost_micros !== null &&
          project.total_cost_micros !== undefined ? (
            <div>
              <p className="tnum font-mono text-2xl font-semibold text-ink">
                {formatCost(project.total_cost_micros)}
              </p>
              <p className="mt-1 text-sm text-muted">
                Total production cost recorded by the backend for this project.
              </p>
            </div>
          ) : (
            <p className="py-4 text-center text-sm text-muted">
              No cost has been recorded for this project yet.
            </p>
          )}
        </Card>
      )}

      {project && tab === "Performance" && (
        <div>
          {publicationQuery.isLoading && (
            <Card className="animate-pulse p-5">
              <div className="h-4 w-1/2 rounded bg-raised" />
            </Card>
          )}
          {publicationQuery.isError && (
            <Card className="py-8 text-center text-sm text-muted">
              Couldn't load publication data — {errorText(publicationQuery.error)}
            </Card>
          )}
          {publicationQuery.data === null && (
            <Card className="py-10 text-center text-sm text-muted">
              This video hasn't been published yet, so there's no performance
              data.
            </Card>
          )}
          {publicationQuery.data && (
            <Card className="p-5">
              <div className="grid grid-cols-2 gap-x-6 gap-y-4 lg:grid-cols-3">
                <Field label="Published title">
                  {publicationQuery.data.title ?? projectTitle(project)}
                </Field>
                <Field label="Privacy">
                  <Badge tone="gray">
                    {humanize(publicationQuery.data.privacy_status)}
                  </Badge>
                </Field>
                <Field label="Published">
                  {fullDate(publicationQuery.data.published_at)}
                </Field>
                {publicationQuery.data.youtube_video_id && (
                  <Field label="YouTube video ID">
                    {publicationQuery.data.youtube_video_id}
                  </Field>
                )}
                {publicationQuery.data.video_url && (
                  <Field label="Video">
                    <a
                      href={publicationQuery.data.video_url}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 text-accent2 hover:underline"
                    >
                      Watch on YouTube
                      <ExternalLink className="h-3.5 w-3.5" />
                    </a>
                  </Field>
                )}
              </div>
              <p className="mt-4 border-t border-line pt-3 text-xs text-muted">
                View and engagement metrics aren't provided by the backend
                yet — only the publication record is shown.
              </p>
            </Card>
          )}
        </div>
      )}
    </div>
  );
}

// ── Library list ───────────────────────────────────────────────────────────

export default function Library() {
  const [activeFilter, setActiveFilter] = useState<"all" | ProjectStatus>("all");
  const [query, setQuery] = useState<string>("");
  const [debouncedQuery, setDebouncedQuery] = useState<string>("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);

  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query.trim()), 300);
    return () => clearTimeout(t);
  }, [query]);

  const projectsQuery = useProjects({
    status: activeFilter === "all" ? undefined : activeFilter,
    q: debouncedQuery || undefined,
  });
  const channelsQuery = useChannels();
  const nicheNamesQuery = useNicheNames();
  const pipelineAction = usePipelineAction();

  const videos = useMemo(
    () => projectsQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [projectsQuery.data],
  );

  const channelNames = useMemo(() => {
    const map: Record<string, string> = {};
    for (const ch of channelsQuery.data ?? []) {
      map[ch.id] = ch.title || ch.handle || ch.id;
    }
    return map;
  }, [channelsQuery.data]);

  const nicheNames = nicheNamesQuery.data ?? {};

  const runAction = (id: string, action: PipelineAction) => {
    setNotice(null);
    pipelineAction.mutate(
      { id, action },
      {
        onSuccess: (res) =>
          setNotice({
            kind: "ok",
            text: `Pipeline ${action} accepted by the backend${
              res.jobId ? ` (job ${res.jobId.slice(0, 8)}…)` : ""
            }. The list will refresh as work progresses.`,
          }),
        onError: (err) => setNotice({ kind: "error", text: errorText(err) }),
      },
    );
  };

  const busyId = pipelineAction.isPending ? pipelineAction.variables?.id : undefined;
  const filtersActive = activeFilter !== "all" || debouncedQuery !== "";

  return (
    <div className="min-h-full bg-app p-6 text-ink">
      {selectedId ? (
        <ProjectDetail
          projectId={selectedId}
          channelNames={channelNames}
          nicheNames={nicheNames}
          onBack={() => setSelectedId(null)}
        />
      ) : (
        <>
          <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
            <div>
              <h1 className="text-xl font-semibold text-ink">Video Library</h1>
              <p className="mt-0.5 text-sm text-ink2">
                Every project across all channels, in one place.
              </p>
            </div>
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search titles…"
                aria-label="Search videos"
                className="w-72 rounded-lg border border-line bg-panel py-2 pl-9 pr-3 text-sm text-ink placeholder:text-muted"
              />
            </div>
          </div>

          <div className="mb-5 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => setActiveFilter("all")}
              className={`rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${
                activeFilter === "all"
                  ? "accent-gradient border-transparent text-ink"
                  : "border-line bg-panel text-ink2 hover:text-ink"
              }`}
            >
              All
            </button>
            {PROJECT_STATUSES.map((status) => (
              <button
                key={status}
                type="button"
                onClick={() => setActiveFilter(status)}
                className={`rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${
                  activeFilter === status
                    ? "accent-gradient border-transparent text-ink"
                    : "border-line bg-panel text-ink2 hover:text-ink"
                }`}
              >
                {PROJECT_STATUS_LABELS[status]}
              </button>
            ))}
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

          <div className="flex flex-col gap-2">
            {projectsQuery.isLoading &&
              [0, 1, 2, 3].map((i) => (
                <Card key={i} className="flex animate-pulse items-center gap-4 px-4 py-3">
                  <div className="h-14 w-24 shrink-0 rounded-md bg-raised" />
                  <div className="min-w-0 flex-1">
                    <div className="h-4 w-1/2 rounded bg-raised" />
                    <div className="mt-2 h-3 w-1/3 rounded bg-raised" />
                  </div>
                  <div className="h-5 w-16 rounded-full bg-raised" />
                  <div className="h-5 w-20 rounded-full bg-raised" />
                </Card>
              ))}

            {projectsQuery.isError && (
              <Card className="py-10 text-center">
                <p className="text-sm font-medium text-ink">
                  Couldn't reach the backend
                </p>
                <p className="mx-auto mt-1 max-w-md text-sm text-muted">
                  {errorText(projectsQuery.error)}
                </p>
                <div className="mt-4">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void projectsQuery.refetch()}
                  >
                    Retry
                  </Button>
                </div>
              </Card>
            )}

            {!projectsQuery.isLoading &&
              !projectsQuery.isError &&
              videos.map((video) => (
                <Card key={video.id} className="flex items-center gap-4 px-4 py-3">
                  <div className="flex h-14 w-24 shrink-0 items-center justify-center rounded-md bg-raised text-muted">
                    <Play className="h-5 w-5" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-ink">
                      {projectTitle(video)}
                    </p>
                    <p className="mt-0.5 text-xs text-ink2">
                      {channelNames[video.channel_id ?? ""] ?? "Unknown channel"} ·
                      Updated {relativeTime(video.updated_at ?? video.created_at)}
                    </p>
                  </div>
                  {nicheNames[video.niche_profile_id ?? ""] && (
                    <Badge tone="violet">
                      {nicheNames[video.niche_profile_id ?? ""]}
                    </Badge>
                  )}
                  <Badge tone={statusTone(video.status)}>
                    {projectStatusLabel(video.status)}
                  </Badge>
                  <span className="tnum w-12 text-right font-mono text-xs text-ink2">
                    {video.target_minutes ? `${video.target_minutes} min` : "—"}
                  </span>
                  <span className="tnum w-14 text-right font-mono text-xs text-ink">
                    {formatCost(video.total_cost_micros)}
                  </span>
                  <div className="flex shrink-0 items-center gap-1.5">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setSelectedId(video.id)}
                    >
                      <FolderOpen className="mr-1.5 h-4 w-4" />
                      Open
                    </Button>
                    {STARTABLE.has(video.status ?? "") && (
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={busyId === video.id}
                        onClick={() => runAction(video.id, "start")}
                        aria-label={`Start pipeline for ${projectTitle(video)}`}
                      >
                        <Play className="mr-1.5 h-4 w-4" />
                        Start
                      </Button>
                    )}
                    {RUNNING.has(video.status ?? "") && (
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={busyId === video.id}
                        onClick={() => runAction(video.id, "pause")}
                        aria-label={`Pause pipeline for ${projectTitle(video)}`}
                      >
                        <Pause className="h-4 w-4" />
                      </Button>
                    )}
                    {ACTIVE.has(video.status ?? "") && (
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={busyId === video.id}
                        onClick={() => runAction(video.id, "cancel")}
                        aria-label={`Cancel pipeline for ${projectTitle(video)}`}
                      >
                        <X className="h-4 w-4" />
                      </Button>
                    )}
                  </div>
                </Card>
              ))}

            {!projectsQuery.isLoading &&
              !projectsQuery.isError &&
              videos.length === 0 && (
                <Card className="py-10 text-center text-sm text-muted">
                  {filtersActive
                    ? "No videos match this filter yet."
                    : "No projects yet. Create one from New Video and it will appear here."}
                </Card>
              )}
          </div>

          {projectsQuery.hasNextPage && (
            <div className="mt-4 text-center">
              <Button
                variant="outline"
                size="sm"
                disabled={projectsQuery.isFetchingNextPage}
                onClick={() => void projectsQuery.fetchNextPage()}
              >
                {projectsQuery.isFetchingNextPage ? "Loading…" : "Load more"}
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
