import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { MonitorPlay, TrendingUp } from "lucide-react";
import { Card } from "../components/ui/Card";
import { useChannels } from "../api/queries/channels";
import { useNicheProfiles } from "../api/queries/growth";
import { LearningRulesSection } from "./growth/LearningRulesSection";
import { PublishWindowsSection } from "./growth/PublishWindowsSection";
import { CommentInsightsSection } from "./growth/CommentInsightsSection";

/**
 * Growth (Round 5 — W6).
 *
 * NAVIGATION CHOICE: a standalone screen at `/growth` with its own
 * sidebar entry, NOT tabs inside Analytics. The app's navigation is
 * one flat route + one sidebar item per screen (see App.tsx /
 * Sidebar.tsx), Analytics has no tab infrastructure, and the
 * Analytics screen is owned by another Round 5 worker — a separate
 * screen keeps this view independently reachable and avoids editing
 * that screen at all.
 *
 * Everything here is per-channel (learning rules, publish windows
 * and comment intelligence are all channel-scoped in the backend
 * stores — never pooled), so the screen is built around a channel
 * picker. Every surface degrades to an honest state: no channel, no
 * data yet, or the team's read API not landed — never placeholder
 * content.
 */
export default function Growth() {
  const channelsQuery = useChannels();
  const channels = (channelsQuery.data ?? []).filter((c) => c.id);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Default to the first channel once the list arrives; keep the
  // user's pick while it still exists.
  useEffect(() => {
    if (channels.length === 0) return;
    if (!selectedId || !channels.some((c) => c.id === selectedId)) {
      setSelectedId(channels[0].id);
    }
  }, [channels, selectedId]);

  const selected =
    channels.find((c) => c.id === selectedId) ?? channels[0] ?? null;

  // Learning scope: rules are read per (channel, niche) and the
  // backend refuses unscoped reads. The scope is the channel's
  // default niche profile — its `key`, falling back to the profile
  // id (builtin profiles have id == key). No default niche -> no
  // scope, and the rules section says so instead of guessing.
  const nicheProfilesQuery = useNicheProfiles();
  const defaultProfile = selected?.default_niche_profile_id
    ? (nicheProfilesQuery.data ?? []).find(
        (p) => p.id === selected.default_niche_profile_id,
      )
    : undefined;
  const nicheScope = defaultProfile
    ? (defaultProfile.key ?? defaultProfile.id)
    : (selected?.default_niche_profile_id ?? null);
  const nicheLabel =
    defaultProfile?.name ?? selected?.default_niche_name ?? nicheScope;

  return (
    <div className="bg-app min-h-full p-6 text-ink">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold text-ink">
            <TrendingUp className="h-6 w-6" />
            Growth
          </h1>
          <p className="mt-1 text-sm text-muted">
            What the agent has learned, when to publish, and what
            viewers are saying — per channel, from that channel&rsquo;s
            own data only.
          </p>
        </div>

        {channels.length > 0 ? (
          <label className="flex items-center gap-2 text-sm text-muted">
            Channel
            <select
              className="rounded-xl border border-line bg-panel px-3 py-2 text-sm font-medium text-ink"
              value={selected?.id ?? ""}
              onChange={(e) => setSelectedId(e.target.value)}
            >
              {channels.map((channel) => (
                <option key={channel.id} value={channel.id}>
                  {channel.title ??
                    channel.handle ??
                    "Untitled channel"}
                </option>
              ))}
            </select>
          </label>
        ) : null}
      </header>

      {channelsQuery.isLoading ? (
        <Card className="p-5">
          <div className="space-y-3" aria-label="Loading channels">
            <div className="h-4 w-1/3 animate-pulse rounded bg-raised" />
            <div className="h-4 w-1/2 animate-pulse rounded bg-raised" />
          </div>
        </Card>
      ) : channels.length === 0 ? (
        <Card className="flex flex-col items-center gap-2 p-8 text-center">
          <MonitorPlay className="h-8 w-8 text-muted" />
          <p className="text-sm font-medium text-ink">
            No channel connected yet
          </p>
          <p className="max-w-md text-sm text-muted">
            Growth insights are learned per channel from published
            videos and their comments. Connect a channel first — once
            it has published videos, this screen fills in from real
            data.
          </p>
          <Link
            to="/channels"
            className="mt-2 text-sm font-medium text-accent2 underline"
          >
            Go to Channels
          </Link>
        </Card>
      ) : selected ? (
        <div className="space-y-6">
          <LearningRulesSection
            channelId={selected.id}
            niche={nicheScope}
            nicheLabel={nicheLabel}
          />
          <PublishWindowsSection channelId={selected.id} />
          <CommentInsightsSection channelId={selected.id} />
        </div>
      ) : null}
    </div>
  );
}
