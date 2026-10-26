import { useEffect, useState } from "react";
import {
  CalendarClock,
  History,
  Pause,
  Play,
  Plus,
  ShieldCheck,
  Trash2,
  Zap,
} from "lucide-react";
import { Card } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { Badge } from "../components/ui/Badge";
import { ApiError } from "../api/types";
import { isPendingApproval, useApprovals } from "../api/queries/approvals";
import {
  useCreateSchedule,
  useDeleteSchedule,
  usePauseSchedule,
  useResumeSchedule,
  useRunScheduleNow,
  useScheduleFormChannels,
  useScheduleFormNiches,
  useScheduleRuns,
  useSchedules,
  useUpdateSchedule,
} from "../api/queries/schedules";
import type {
  CadenceWire,
  RunNowResult,
  ScheduleFormInput,
  ScheduleWire,
} from "../api/queries/schedules";

type Notice = { kind: "info" | "error"; text: string };

const WEEKDAY_LABELS = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
];

interface FormState {
  name: string;
  channelId: string;
  nicheProfileId: string;
  cadenceType: "daily" | "weekly" | "interval";
  time: string;
  weekday: number;
  intervalHours: string;
  topicMode: "auto" | "queue" | "fixed_list";
  topicsText: string;
  targetMinutes: string;
  publishMode: "auto" | "review_first";
  dailyCapUsd: string;
  enabled: boolean;
}

const EMPTY_FORM: FormState = {
  name: "",
  channelId: "",
  nicheProfileId: "",
  cadenceType: "daily",
  time: "09:00",
  weekday: 0,
  intervalHours: "24",
  topicMode: "auto",
  topicsText: "",
  targetMinutes: "",
  publishMode: "review_first",
  dailyCapUsd: "",
  enabled: true,
};

function errorText(err: unknown): string {
  if (err instanceof ApiError && err.code === "NETWORK_ERROR") {
    return "The backend isn't reachable. Check that the app backend is running, then retry.";
  }
  return err instanceof Error ? err.message : "Something went wrong.";
}

function fmtDateTime(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  return new Date(ms).toLocaleString();
}

function formFromSchedule(schedule: ScheduleWire): FormState {
  const cadence = schedule.cadence;
  const base: FormState = {
    ...EMPTY_FORM,
    name: schedule.name ?? "",
    channelId: schedule.channel_id,
    nicheProfileId: schedule.niche_profile_id,
    topicMode:
      schedule.topic_mode === "queue" || schedule.topic_mode === "fixed_list"
        ? schedule.topic_mode
        : "auto",
    topicsText: cadence?.topics?.join("\n") ?? "",
    targetMinutes:
      schedule.target_minutes !== null && schedule.target_minutes !== undefined
        ? String(schedule.target_minutes)
        : "",
    publishMode: schedule.publish_mode === "auto" ? "auto" : "review_first",
    dailyCapUsd:
      schedule.daily_budget_cap_usd !== null &&
      schedule.daily_budget_cap_usd !== undefined
        ? String(schedule.daily_budget_cap_usd)
        : "",
    enabled: schedule.enabled,
  };
  if (cadence?.type === "daily") {
    base.cadenceType = "daily";
    base.time = cadence.time;
  } else if (cadence?.type === "weekly") {
    base.cadenceType = "weekly";
    base.time = cadence.time;
    base.weekday = cadence.weekday;
  } else if (cadence?.type === "interval") {
    base.cadenceType = "interval";
    base.intervalHours = String(cadence.interval_hours);
  }
  return base;
}

function formToInput(form: FormState): ScheduleFormInput {
  const topics = form.topicsText
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  let cadence: CadenceWire;
  if (form.cadenceType === "weekly") {
    cadence = { type: "weekly", weekday: form.weekday, time: form.time };
  } else if (form.cadenceType === "interval") {
    cadence = {
      type: "interval",
      interval_hours: Number(form.intervalHours) || 24,
    };
  } else {
    cadence = { type: "daily", time: form.time };
  }
  if (form.topicMode === "fixed_list") cadence.topics = topics;
  return {
    name: form.name,
    channelId: form.channelId,
    nicheProfileId: form.nicheProfileId,
    cadence,
    topicMode: form.topicMode,
    targetMinutes: form.targetMinutes ? Number(form.targetMinutes) : null,
    publishMode: form.publishMode,
    dailyBudgetCapUsd: form.dailyCapUsd ? Number(form.dailyCapUsd) : null,
    enabled: form.enabled,
  };
}

function runNowNotice(result: RunNowResult, scheduleName: string): string {
  if (result.run?.status === "created") {
    return `Run started for ${scheduleName} — a new project was created${
      result.projectId ? ` (${result.projectId.slice(0, 8)}…)` : ""
    } and production was queued.`;
  }
  if (result.run?.status === "failed") {
    return `The scheduler ran but this slot failed${
      result.run.reason ? `: ${result.run.reason}` : "."
    }`;
  }
  if (result.tick) {
    return `Scheduler tick finished (checked ${result.tick.checked}, created ${result.tick.materialised}, failed ${result.tick.failed}) — no run was recorded for ${scheduleName}.`;
  }
  return `Run requested for ${scheduleName}.`;
}

const inputClass =
  "w-full rounded-lg border border-line bg-raised px-3 py-2 text-sm text-ink outline-none focus:border-accent";
const labelClass = "mb-1 block text-xs font-medium text-ink2";

export default function Schedules() {
  const [notice, setNotice] = useState<Notice | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<ScheduleWire | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null);
  const [historyId, setHistoryId] = useState<string | null>(null);

  const schedulesQuery = useSchedules();
  const channelsQuery = useScheduleFormChannels();
  const nichesQuery = useScheduleFormNiches();
  const approvalsQuery = useApprovals();
  const runsQuery = useScheduleRuns(historyId);

  const createSchedule = useCreateSchedule();
  const updateSchedule = useUpdateSchedule();
  const pauseSchedule = usePauseSchedule();
  const resumeSchedule = useResumeSchedule();
  const deleteSchedule = useDeleteSchedule();
  const runNow = useRunScheduleNow();

  const schedules = schedulesQuery.data ?? [];
  const channels = channelsQuery.data ?? [];
  const niches = nichesQuery.data ?? [];
  const pendingApprovals = (approvalsQuery.data ?? []).filter(isPendingApproval).length;

  // Preselect the first real channel/niche in a fresh create form.
  useEffect(() => {
    if (!formOpen || editing) return;
    setForm((current) => ({
      ...current,
      channelId: current.channelId || channels[0]?.id || "",
      nicheProfileId: current.nicheProfileId || niches[0]?.id || "",
    }));
  }, [formOpen, editing, channels, niches]);

  const openCreate = () => {
    setEditing(null);
    setForm(EMPTY_FORM);
    setFormOpen(true);
    setNotice(null);
  };

  const openEdit = (schedule: ScheduleWire) => {
    setEditing(schedule);
    setForm(formFromSchedule(schedule));
    setFormOpen(true);
    setNotice(null);
  };

  const handleSubmit = () => {
    setNotice(null);
    const input = formToInput(form);
    if (!input.channelId || !input.nicheProfileId) {
      setNotice({
        kind: "error",
        text: "Pick a channel and a niche first — a schedule needs both.",
      });
      return;
    }
    if (editing) {
      updateSchedule.mutate(
        { id: editing.id, input },
        {
          onSuccess: () => {
            setFormOpen(false);
            setEditing(null);
            setNotice({ kind: "info", text: "Schedule saved." });
          },
          onError: (err) => setNotice({ kind: "error", text: errorText(err) }),
        },
      );
    } else {
      createSchedule.mutate(input, {
        onSuccess: () => {
          setFormOpen(false);
          setNotice({ kind: "info", text: "Schedule created." });
        },
        onError: (err) => setNotice({ kind: "error", text: errorText(err) }),
      });
    }
  };

  const handleRunNow = (schedule: ScheduleWire) => {
    setNotice(null);
    runNow.mutate(schedule.id, {
      onSuccess: (result) =>
        setNotice({
          kind: "info",
          text: runNowNotice(result, schedule.name || "the schedule"),
        }),
      onError: (err) => setNotice({ kind: "error", text: errorText(err) }),
    });
  };

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((current) => ({ ...current, [key]: value }));

  return (
    <div className="min-h-full bg-app p-6 text-ink">
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink">Schedules</h1>
          <p className="mt-0.5 max-w-2xl text-sm text-ink2">
            When a schedule fires, the backend creates the video project
            automatically. <span className="font-medium text-ink">Review
            first</span> schedules park at Gate A / Gate B for your
            approval; <span className="font-medium text-ink">Full
            auto</span> schedules run end-to-end unattended.
          </p>
        </div>
        <Button
          variant="primary"
          onClick={openCreate}
          disabled={channelsQuery.isLoading || nichesQuery.isLoading}
        >
          <Plus className="mr-1.5 h-4 w-4" />
          New schedule
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

      {pendingApprovals > 0 && (
        <Card className="mb-4 flex flex-wrap items-center gap-2 border-warn/50 px-4 py-3 text-sm text-ink2">
          <ShieldCheck className="h-4 w-4 shrink-0 text-warn" />
          {pendingApprovals} gate approval{pendingApprovals === 1 ? "" : "s"}{" "}
          waiting — review-first schedules park there until you decide in
          the Review screen.
        </Card>
      )}

      {formOpen && (
        <Card className="mb-5 p-5">
          <h2 className="text-sm font-semibold text-ink">
            {editing ? `Edit schedule — ${editing.name || editing.id}` : "New schedule"}
          </h2>
          {editing?.cadence?.type === "rule" && (
            <p className="mt-2 rounded-lg border border-warn/50 bg-raised px-3 py-2 text-xs leading-relaxed text-ink2">
              This schedule still has a free-text rule (“{editing.cadence.rule}”)
              which the scheduler cannot fire on. Saving replaces it with
              the structured cadence below.
            </p>
          )}
          {channels.length === 0 && !channelsQuery.isLoading ? (
            <p className="mt-3 text-sm text-ink2">
              No channels are connected yet — connect a channel on the
              Channels screen first, then create its schedule here.
            </p>
          ) : (
            <>
              <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-3">
                <div>
                  <label className={labelClass}>Name</label>
                  <input
                    className={inputClass}
                    value={form.name}
                    onChange={(e) => set("name", e.target.value)}
                    placeholder="e.g. Morning facts"
                  />
                </div>
                <div>
                  <label className={labelClass}>Channel</label>
                  <select
                    className={inputClass}
                    value={form.channelId}
                    onChange={(e) => set("channelId", e.target.value)}
                  >
                    <option value="">Select a channel…</option>
                    {channels.map((channel) => (
                      <option key={channel.id} value={channel.id}>
                        {channel.title || channel.handle || channel.id}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Niche</label>
                  <select
                    className={inputClass}
                    value={form.nicheProfileId}
                    onChange={(e) => set("nicheProfileId", e.target.value)}
                  >
                    <option value="">Select a niche…</option>
                    {niches.map((niche) => (
                      <option key={niche.id} value={niche.id}>
                        {niche.name}
                        {niche.source === "other" ? " (custom)" : ""}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Cadence</label>
                  <select
                    className={inputClass}
                    value={form.cadenceType}
                    onChange={(e) =>
                      set("cadenceType", e.target.value as FormState["cadenceType"])
                    }
                  >
                    <option value="daily">Daily</option>
                    <option value="weekly">Weekly</option>
                    <option value="interval">Every N hours</option>
                  </select>
                </div>
                {form.cadenceType !== "interval" ? (
                  <div>
                    <label className={labelClass}>Time (UTC)</label>
                    <input
                      type="time"
                      className={inputClass}
                      value={form.time}
                      onChange={(e) => set("time", e.target.value)}
                    />
                  </div>
                ) : (
                  <div>
                    <label className={labelClass}>Interval (hours)</label>
                    <input
                      type="number"
                      min={1}
                      className={inputClass}
                      value={form.intervalHours}
                      onChange={(e) => set("intervalHours", e.target.value)}
                    />
                  </div>
                )}
                {form.cadenceType === "weekly" && (
                  <div>
                    <label className={labelClass}>Weekday</label>
                    <select
                      className={inputClass}
                      value={form.weekday}
                      onChange={(e) => set("weekday", Number(e.target.value))}
                    >
                      {WEEKDAY_LABELS.map((label, index) => (
                        <option key={label} value={index}>
                          {label}
                        </option>
                      ))}
                    </select>
                  </div>
                )}
                <div>
                  <label className={labelClass}>Topic mode</label>
                  <select
                    className={inputClass}
                    value={form.topicMode}
                    onChange={(e) =>
                      set("topicMode", e.target.value as FormState["topicMode"])
                    }
                  >
                    <option value="auto">Auto — agent picks the topic</option>
                    <option value="queue">Queue — next queued topic</option>
                    <option value="fixed_list">Fixed list — rotate topics</option>
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Target minutes (optional)</label>
                  <input
                    type="number"
                    min={1}
                    max={600}
                    className={inputClass}
                    value={form.targetMinutes}
                    onChange={(e) => set("targetMinutes", e.target.value)}
                    placeholder="Niche default"
                  />
                </div>
                <div>
                  <label className={labelClass}>Daily cost cap, USD (optional)</label>
                  <input
                    type="number"
                    min={0}
                    step="0.5"
                    className={inputClass}
                    value={form.dailyCapUsd}
                    onChange={(e) => set("dailyCapUsd", e.target.value)}
                    placeholder="No cap"
                  />
                </div>
              </div>

              {form.topicMode === "fixed_list" && (
                <div className="mt-4">
                  <label className={labelClass}>
                    Topics — one per line, used in rotation
                  </label>
                  <textarea
                    className={`${inputClass} min-h-24`}
                    value={form.topicsText}
                    onChange={(e) => set("topicsText", e.target.value)}
                    placeholder={"First topic\nSecond topic"}
                  />
                </div>
              )}

              <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2">
                <button
                  type="button"
                  onClick={() => set("publishMode", "review_first")}
                  className={`rounded-xl border p-3 text-left ${
                    form.publishMode === "review_first"
                      ? "border-accent bg-accent/10"
                      : "border-line bg-raised"
                  }`}
                >
                  <p className="flex items-center gap-1.5 text-sm font-semibold text-ink">
                    <ShieldCheck className="h-4 w-4 text-accent2" /> Review first
                  </p>
                  <p className="mt-1 text-xs leading-relaxed text-ink2">
                    Production parks at Gate A (script) and Gate B
                    (pre-publish) until you approve in the Review screen.
                  </p>
                </button>
                <button
                  type="button"
                  onClick={() => set("publishMode", "auto")}
                  className={`rounded-xl border p-3 text-left ${
                    form.publishMode === "auto"
                      ? "border-ok bg-ok/10"
                      : "border-line bg-raised"
                  }`}
                >
                  <p className="flex items-center gap-1.5 text-sm font-semibold text-ink">
                    <Zap className="h-4 w-4 text-ok" /> Full auto
                  </p>
                  <p className="mt-1 text-xs leading-relaxed text-ink2">
                    Runs end-to-end unattended — no gate approvals. The
                    daily cost cap and budget guard still apply.
                  </p>
                </button>
              </div>

              <label className="mt-4 flex items-center gap-2 text-sm text-ink2">
                <input
                  type="checkbox"
                  checked={form.enabled}
                  onChange={(e) => set("enabled", e.target.checked)}
                />
                Enabled — the scheduler may fire this schedule
              </label>

              <div className="mt-4 flex gap-2">
                <Button
                  variant="primary"
                  size="sm"
                  disabled={createSchedule.isPending || updateSchedule.isPending}
                  onClick={handleSubmit}
                >
                  {editing ? "Save schedule" : "Create schedule"}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setFormOpen(false);
                    setEditing(null);
                  }}
                >
                  Cancel
                </Button>
              </div>
            </>
          )}
        </Card>
      )}

      {schedulesQuery.isLoading && (
        <div className="flex flex-col gap-3">
          {[0, 1].map((i) => (
            <Card key={i} className="animate-pulse p-5">
              <div className="h-4 w-1/3 rounded bg-raised" />
              <div className="mt-3 h-3 w-2/3 rounded bg-raised" />
            </Card>
          ))}
        </div>
      )}

      {schedulesQuery.isError && (
        <Card className="py-10 text-center">
          <p className="text-sm font-medium text-ink">Couldn't reach the backend</p>
          <p className="mx-auto mt-1 max-w-md text-sm text-muted">
            {errorText(schedulesQuery.error)}
          </p>
          <div className="mt-4">
            <Button
              variant="outline"
              size="sm"
              onClick={() => void schedulesQuery.refetch()}
            >
              Retry
            </Button>
          </div>
        </Card>
      )}

      {!schedulesQuery.isLoading && !schedulesQuery.isError && schedules.length === 0 && (
        <Card className="py-10 text-center">
          <CalendarClock className="mx-auto h-8 w-8 text-muted" />
          <p className="mt-3 text-sm font-medium text-ink">No schedules yet</p>
          <p className="mx-auto mt-1 max-w-md text-sm text-muted">
            Create the first schedule and the automation engine will
            start a new video project at every slot — no manual start
            needed.
          </p>
        </Card>
      )}

      <div className="flex flex-col gap-3">
        {schedules.map((schedule) => {
          const isAuto = schedule.publish_mode === "auto";
          const due =
            schedule.enabled &&
            schedule.next_run_at !== null &&
            schedule.next_run_at !== undefined &&
            schedule.next_run_at <= Date.now();
          const confirming = confirmingDeleteId === schedule.id;
          const historyOpen = historyId === schedule.id;
          return (
            <Card
              key={schedule.id}
              className={`p-5 ${isAuto ? "border-ok/40" : "border-accent/40"}`}
            >
              <div className="flex flex-wrap items-start gap-3">
                <div className="min-w-52 flex-1">
                  <p className="text-sm font-semibold text-ink">
                    {schedule.name || "Untitled schedule"}
                  </p>
                  <p className="mt-0.5 text-xs text-muted">
                    {schedule.channel_title || schedule.channel_id} ·{" "}
                    {schedule.niche_name || schedule.niche_profile_id}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-1.5">
                  {isAuto ? (
                    <Badge tone="green">
                      <Zap className="h-3 w-3" /> Full auto — no gate stops
                    </Badge>
                  ) : (
                    <Badge tone="violet">
                      <ShieldCheck className="h-3 w-3" /> Review first — parks at gates
                    </Badge>
                  )}
                  {schedule.enabled ? (
                    <Badge tone="green">Enabled</Badge>
                  ) : (
                    <Badge tone="gray">Paused</Badge>
                  )}
                  {due && <Badge tone="amber">Due now</Badge>}
                </div>
              </div>

              <div className="tnum mt-4 grid grid-cols-2 gap-3 border-t border-line pt-3 font-mono md:grid-cols-5">
                <div>
                  <p className="text-xs font-semibold text-ink">
                    {schedule.cadence_summary || "No cadence set"}
                  </p>
                  <p className="font-sans text-xs text-muted">Cadence (UTC)</p>
                </div>
                <div>
                  <p className="text-xs font-semibold text-ink">
                    {fmtDateTime(schedule.next_run_at)}
                  </p>
                  <p className="font-sans text-xs text-muted">Next run</p>
                </div>
                <div>
                  <p className="text-xs font-semibold text-ink">
                    {fmtDateTime(schedule.last_run_at)}
                  </p>
                  <p className="font-sans text-xs text-muted">Last run</p>
                </div>
                <div>
                  <p className="text-xs font-semibold text-ink">
                    {schedule.topic_mode || "auto"}
                    {schedule.target_minutes
                      ? ` · ${schedule.target_minutes} min`
                      : ""}
                  </p>
                  <p className="font-sans text-xs text-muted">Topics · length</p>
                </div>
                <div>
                  <p className="text-xs font-semibold text-ink">
                    {schedule.daily_budget_cap_usd !== null &&
                    schedule.daily_budget_cap_usd !== undefined
                      ? `$${schedule.daily_budget_cap_usd.toFixed(2)}`
                      : "No cap"}
                  </p>
                  <p className="font-sans text-xs text-muted">Daily cost cap</p>
                </div>
              </div>

              {confirming ? (
                <div className="mt-4 rounded-lg border border-line bg-raised px-3 py-2.5">
                  <p className="text-xs leading-relaxed text-ink2">
                    Delete this schedule? Its run history is kept, but it
                    will never fire again.
                  </p>
                  <div className="mt-2.5 flex gap-2">
                    <Button
                      variant="danger"
                      size="sm"
                      disabled={deleteSchedule.isPending}
                      onClick={() =>
                        deleteSchedule.mutate(schedule.id, {
                          onSuccess: () => setConfirmingDeleteId(null),
                          onError: (err) =>
                            setNotice({ kind: "error", text: errorText(err) }),
                        })
                      }
                    >
                      Delete schedule
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setConfirmingDeleteId(null)}
                    >
                      Keep it
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="mt-4 flex flex-wrap gap-2">
                  <Button
                    variant="primary"
                    size="sm"
                    disabled={!schedule.enabled || runNow.isPending}
                    onClick={() => handleRunNow(schedule)}
                    title={
                      schedule.enabled
                        ? "Mark due and run one scheduler tick now"
                        : "Resume the schedule before running it"
                    }
                  >
                    <Play className="mr-1.5 h-4 w-4" />
                    Run now
                  </Button>
                  {schedule.enabled ? (
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={pauseSchedule.isPending}
                      onClick={() =>
                        pauseSchedule.mutate(schedule.id, {
                          onError: (err) =>
                            setNotice({ kind: "error", text: errorText(err) }),
                        })
                      }
                    >
                      <Pause className="mr-1.5 h-4 w-4" />
                      Pause
                    </Button>
                  ) : (
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={resumeSchedule.isPending}
                      onClick={() =>
                        resumeSchedule.mutate(schedule.id, {
                          onError: (err) =>
                            setNotice({ kind: "error", text: errorText(err) }),
                        })
                      }
                    >
                      <Play className="mr-1.5 h-4 w-4" />
                      Resume
                    </Button>
                  )}
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setHistoryId(historyOpen ? null : schedule.id)}
                  >
                    <History className="mr-1.5 h-4 w-4" />
                    {historyOpen ? "Hide history" : "Run history"}
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => openEdit(schedule)}>
                    Edit
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setConfirmingDeleteId(schedule.id)}
                  >
                    <Trash2 className="mr-1.5 h-4 w-4" />
                    Delete
                  </Button>
                </div>
              )}

              {historyOpen && (
                <div className="mt-4 border-t border-line pt-3">
                  {runsQuery.isLoading && (
                    <p className="text-xs text-muted">Loading run history…</p>
                  )}
                  {runsQuery.isError && (
                    <p className="text-xs text-danger">
                      Couldn't load run history: {errorText(runsQuery.error)}
                    </p>
                  )}
                  {!runsQuery.isLoading &&
                    !runsQuery.isError &&
                    (runsQuery.data ?? []).length === 0 && (
                      <p className="text-xs text-muted">
                        No runs yet — this schedule hasn't fired or been
                        run manually.
                      </p>
                    )}
                  <div className="flex flex-col gap-2">
                    {(runsQuery.data ?? []).map((run) => (
                      <div
                        key={run.id}
                        className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-line bg-raised px-3 py-2 text-xs"
                      >
                        <Badge
                          tone={
                            run.status === "created"
                              ? "green"
                              : run.status === "failed"
                                ? "red"
                                : "amber"
                          }
                        >
                          {run.status}
                        </Badge>
                        <span className="tnum font-mono text-ink2">
                          {fmtDateTime(run.started_at ?? run.created_at)}
                        </span>
                        {run.project_id && (
                          <span className="text-ink2">
                            Project {run.project_id.slice(0, 8)}…
                          </span>
                        )}
                        {run.reason && (
                          <span className="text-danger">{run.reason}</span>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </Card>
          );
        })}
      </div>
    </div>
  );
}
