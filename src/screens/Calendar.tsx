import { useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Globe,
} from "lucide-react";
import { Card } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { Badge } from "../components/ui/Badge";

type SlotState = "Published" | "Scheduled" | "At risk";

type CalendarSlot = {
  id: string;
  time: string;
  title: string;
  channelColor: string;
  state: SlotState;
};

type CalendarDay = {
  id: string;
  weekday: string;
  date: string;
  slots: CalendarSlot[];
};

type UpcomingSlot = {
  id: string;
  title: string;
  channel: string;
  publishAt: string;
  uploadBy: string;
  productionStart: string;
  state: SlotState;
};

const WEEK_DAYS: CalendarDay[] = [
  {
    id: "mon",
    weekday: "Mon",
    date: "26 Oct",
    slots: [
      {
        id: "s1",
        time: "18:00",
        title: "10 Ocean Mysteries Science Can't Explain",
        channelColor: "#6e5bff",
        state: "Published",
      },
    ],
  },
  {
    id: "tue",
    weekday: "Tue",
    date: "27 Oct",
    slots: [],
  },
  {
    id: "wed",
    weekday: "Wed",
    date: "28 Oct",
    slots: [
      {
        id: "s2",
        time: "17:30",
        title: "The Lost City Beneath the Sahara",
        channelColor: "#6e5bff",
        state: "Scheduled",
      },
      {
        id: "s3",
        time: "20:00",
        title: "The Prophet's Night Journey — Full Story",
        channelColor: "#34c77b",
        state: "Scheduled",
      },
    ],
  },
  {
    id: "thu",
    weekday: "Thu",
    date: "29 Oct",
    slots: [],
  },
  {
    id: "fri",
    weekday: "Fri",
    date: "30 Oct",
    slots: [
      {
        id: "s4",
        time: "19:00",
        title: "Why the Roman Empire Really Fell",
        channelColor: "#f5b544",
        state: "At risk",
      },
    ],
  },
  {
    id: "sat",
    weekday: "Sat",
    date: "31 Oct",
    slots: [
      {
        id: "s5",
        time: "16:00",
        title: "The Haunted Lighthouse Keeper's Last Log",
        channelColor: "#f04438",
        state: "Scheduled",
      },
    ],
  },
  {
    id: "sun",
    weekday: "Sun",
    date: "1 Nov",
    slots: [],
  },
];

const UPCOMING_SLOTS: UpcomingSlot[] = [
  {
    id: "u1",
    title: "The Lost City Beneath the Sahara",
    channel: "FactForge",
    publishAt: "Wed 28 Oct · 17:30",
    uploadBy: "Wed 28 Oct · 11:30",
    productionStart: "Tue 27 Oct · 09:00",
    state: "Scheduled",
  },
  {
    id: "u2",
    title: "Why the Roman Empire Really Fell",
    channel: "History Vault",
    publishAt: "Fri 30 Oct · 19:00",
    uploadBy: "Fri 30 Oct · 13:00",
    productionStart: "Thu 29 Oct · 09:00",
    state: "At risk",
  },
];

function SlotStateIcon({ state }: { state: SlotState }) {
  if (state === "Published") {
    return <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-ok" />;
  }
  if (state === "At risk") {
    return <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warn" />;
  }
  return <Clock className="h-3.5 w-3.5 shrink-0 text-accent2" />;
}

export default function Calendar() {
  const [view, setView] = useState<"Week" | "List">("Week");

  return (
    <div className="min-h-full bg-app p-6 text-ink">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold text-ink">
          Publishing Calendar
        </h1>
        <div className="flex gap-1 rounded-lg border border-line bg-panel p-1">
          {(["Week", "List"] as const).map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => setView(option)}
              className={`rounded-md px-3 py-1 text-xs font-medium ${
                view === option
                  ? "accent-gradient text-ink"
                  : "text-ink2 hover:text-ink"
              }`}
            >
              {option}
            </button>
          ))}
        </div>
      </div>
      <p className="mb-5 flex items-center gap-1.5 text-sm text-ink2">
        <Globe className="h-4 w-4 text-muted" />
        Times shown in Audience timezone (Asia/Karachi)
      </p>

      <div className="grid grid-cols-7 gap-2">
        {WEEK_DAYS.map((day) => (
          <div
            key={day.id}
            className="min-h-40 rounded-xl border border-line bg-panel p-2"
          >
            <div className="mb-2 px-1">
              <p className="text-xs font-semibold text-ink">{day.weekday}</p>
              <p className="tnum text-xs text-muted">{day.date}</p>
            </div>
            <div className="flex flex-col gap-1.5">
              {day.slots.map((slot) => (
                <div
                  key={slot.id}
                  className="rounded-lg border border-line bg-raised p-2"
                >
                  <p className="tnum flex items-center gap-1.5 font-mono text-[11px] text-ink2">
                    <span
                      className="h-2 w-2 shrink-0 rounded-full"
                      style={{ backgroundColor: slot.channelColor }}
                    />
                    {slot.time}
                  </p>
                  <p className="mt-1 line-clamp-2 text-[11px] font-medium leading-snug text-ink">
                    {slot.title}
                  </p>
                  <p className="mt-1.5 flex items-center gap-1 text-[11px] text-ink2">
                    <SlotStateIcon state={slot.state} />
                    {slot.state}
                  </p>
                </div>
              ))}
              {day.slots.length === 0 && (
                <p className="px-1 text-[11px] text-muted">No slots</p>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-4 text-xs text-ink2">
        <span className="font-medium text-muted">Legend</span>
        <span className="flex items-center gap-1.5">
          <CheckCircle2 className="h-3.5 w-3.5 text-ok" /> Published
        </span>
        <span className="flex items-center gap-1.5">
          <Clock className="h-3.5 w-3.5 text-accent2" /> Scheduled
        </span>
        <span className="flex items-center gap-1.5">
          <AlertTriangle className="h-3.5 w-3.5 text-warn" /> At risk —
          production may miss this slot
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full bg-accent" /> Dot = channel
          colour
        </span>
      </div>

      <Card className="mt-5 p-5">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-ink">Upcoming slots</h2>
          <Badge tone="gray">Next 7 days</Badge>
        </div>
        <div className="flex flex-col gap-2">
          {UPCOMING_SLOTS.map((slot) => (
            <div
              key={slot.id}
              className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-lg border border-line bg-raised px-4 py-3"
            >
              <div className="min-w-52 flex-1">
                <p className="flex items-center gap-2 text-sm font-medium text-ink">
                  <SlotStateIcon state={slot.state} />
                  {slot.title}
                </p>
                <p className="mt-0.5 text-xs text-muted">{slot.channel}</p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-muted">
                  Publish
                </p>
                <p className="tnum font-mono text-xs text-ink">
                  {slot.publishAt}
                </p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-muted">
                  Upload by
                </p>
                <p className="tnum font-mono text-xs text-ink2">
                  {slot.uploadBy}
                </p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-muted">
                  Production start
                </p>
                <p className="tnum font-mono text-xs text-ink2">
                  {slot.productionStart}
                </p>
              </div>
              <Button variant="ghost" size="sm">
                Open slot
              </Button>
            </div>
          ))}
        </div>
        <p className="mt-3 text-xs leading-relaxed text-muted">
          Videos upload early as private and publish at the slot time via
          YouTube — your PC does not need to be on when a video goes live.
        </p>
      </Card>
    </div>
  );
}
