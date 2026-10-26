import { NavLink } from "react-router-dom";
import {
  BarChart3,
  Bot,
  CalendarDays,
  Clapperboard,
  ClipboardCheck,
  DollarSign,
  FolderOpen,
  LayoutDashboard,
  MessagesSquare,
  MonitorPlay,
  Rocket,
  Settings,
  TrendingUp,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { useAgentsCatalogue } from "../../api/queries/agents";
import { isPendingApproval, useApprovals } from "../../api/queries/approvals";
import { useEngineStatus } from "../../api/queries/engine";
import { useConnectionStore } from "../../state/connection";

type NavItem = {
  to: string;
  label: string;
  icon: LucideIcon;
  badge?: string;
};

const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Mission Control", icon: LayoutDashboard },
  { to: "/setup", label: "First-Run Setup", icon: Rocket },
  { to: "/new", label: "New Video", icon: Clapperboard },
  { to: "/agents", label: "Agents Live", icon: Bot },
  { to: "/review", label: "Review", icon: ClipboardCheck },
  { to: "/library", label: "Library", icon: FolderOpen },
  { to: "/schedules", label: "Schedules", icon: CalendarDays },
  { to: "/channels", label: "Channels", icon: MonitorPlay },
  { to: "/analytics", label: "Analytics", icon: BarChart3 },
  { to: "/growth", label: "Growth", icon: TrendingUp },
  { to: "/comments", label: "Comments", icon: MessagesSquare },
  { to: "/costs", label: "Costs", icon: DollarSign },
  { to: "/settings", label: "Settings", icon: Settings },
];

export function Sidebar() {
  // The Review badge is the REAL pending-approval count (v22 — it was
  // a hardcoded "3" before). Pending uses the same predicate as the
  // Review queue itself, so the two can never disagree.
  const approvalsQuery = useApprovals();
  const pendingCount = (approvalsQuery.data ?? []).filter(isPendingApproval).length;

  // The footer reports the REAL engine state (v22 — it was a static
  // "Engine running / 12 agents active" placeholder before): the
  // persisted stop/pause flags win, then the live connection state;
  // the second line is the actual agent-team size from the catalogue.
  const connStatus = useConnectionStore((s) => s.status);
  const engineStatus = useEngineStatus();
  const agentsQuery = useAgentsCatalogue();

  const engine = (() => {
    if (engineStatus.data?.stopAll) {
      return { label: "Engine stopped", dot: "bg-danger", ping: false };
    }
    if (engineStatus.data?.pauseAll) {
      return { label: "Engine paused", dot: "bg-warn", ping: false };
    }
    switch (connStatus) {
      case "ready":
      case "degraded":
        return { label: "Engine running", dot: "bg-ok", ping: true };
      case "down":
        return { label: "Engine offline", dot: "bg-danger", ping: false };
      default:
        return { label: "Engine starting", dot: "bg-warn", ping: false };
    }
  })();

  return (
    <aside className="flex w-60 shrink-0 flex-col gap-1 border-r border-line bg-sidebar px-3 py-4">
      <nav className="flex flex-col gap-1">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;
          const badge =
            item.to === "/review" && pendingCount > 0
              ? String(pendingCount)
              : item.badge;
          return (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition ${
                  isActive
                    ? "accent-gradient text-white"
                    : "text-ink2 hover:bg-raised hover:text-ink"
                }`
              }
            >
              {({ isActive }) => (
                <>
                  <Icon className="h-5 w-5 shrink-0" />
                  <span className="truncate">{item.label}</span>
                  {badge ? (
                    <span
                      className={`ml-auto flex h-5 min-w-5 items-center justify-center rounded-md px-1.5 text-[11px] font-bold text-white ${
                        isActive ? "bg-white/25" : "bg-accent"
                      }`}
                    >
                      {badge}
                    </span>
                  ) : null}
                </>
              )}
            </NavLink>
          );
        })}
      </nav>

      {/* Engine status — real state (connection + persisted flags),
          and the real agent-team size from the catalogue. How many
          agents are working this minute lives on Agents Live. */}
      <div className="mt-auto rounded-2xl border border-line bg-panel p-3">
        <div className="flex items-center gap-2">
          <span className="relative flex h-2 w-2 shrink-0">
            {engine.ping ? (
              <span
                className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-75 ${engine.dot}`}
              />
            ) : null}
            <span className={`relative inline-flex h-2 w-2 rounded-full ${engine.dot}`} />
          </span>
          <span className="text-sm font-medium text-ink">{engine.label}</span>
        </div>
        {agentsQuery.data ? (
          <p className="mt-1 pl-4 text-xs text-muted">
            {agentsQuery.data.length} agents on the team
          </p>
        ) : null}
      </div>
    </aside>
  );
}
