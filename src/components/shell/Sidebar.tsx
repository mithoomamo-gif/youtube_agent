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
  { to: "/review", label: "Review", icon: ClipboardCheck, badge: "3" },
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
  return (
    <aside className="flex w-60 shrink-0 flex-col gap-1 border-r border-line bg-sidebar px-3 py-4">
      <nav className="flex flex-col gap-1">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;
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
                  {item.badge ? (
                    <span
                      className={`ml-auto flex h-5 min-w-5 items-center justify-center rounded-md px-1.5 text-[11px] font-bold text-white ${
                        isActive ? "bg-white/25" : "bg-accent"
                      }`}
                    >
                      {item.badge}
                    </span>
                  ) : null}
                </>
              )}
            </NavLink>
          );
        })}
      </nav>

      {/* Engine status (placeholder) */}
      <div className="mt-auto rounded-2xl border border-line bg-panel p-3">
        <div className="flex items-center gap-2">
          <span className="relative flex h-2 w-2 shrink-0">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-ok opacity-75" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-ok" />
          </span>
          <span className="text-sm font-medium text-ink">Engine running</span>
        </div>
        <p className="mt-1 pl-4 text-xs text-muted">12 agents active</p>
      </div>
    </aside>
  );
}
