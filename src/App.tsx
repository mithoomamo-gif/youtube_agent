import { HashRouter, Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./components/shell/AppShell";
import { BackendGate } from "./components/shell/BackendGate";
import AgentsLive from "./screens/AgentsLive";
import Analytics from "./screens/Analytics";
import Comments from "./screens/Comments";
import Schedules from "./screens/Schedules";
import Channels from "./screens/Channels";
import Costs from "./screens/Costs";
import Growth from "./screens/Growth";
import Library from "./screens/Library";
import MissionControl from "./screens/MissionControl";
import NewVideoWizard from "./screens/NewVideoWizard";
import Onboarding from "./screens/Onboarding";
import Review from "./screens/Review";
import Settings from "./screens/Settings";
import { useLiveSync } from "./state/liveSync";

function Dashboard() {
  // Live sync: WS → React Query invalidations + connection health
  // monitor. Mounted once here (inside the QueryClientProvider in
  // main.tsx); see src/state/liveSync.ts. It mounts only when the
  // BackendGate lets the dashboard render, i.e. against a READY (or
  // degraded / dev) backend — never against a starting/failed one.
  useLiveSync();

  return (
    <HashRouter>
      <AppShell>
        <Routes>
          <Route path="/" element={<MissionControl />} />
          <Route path="/setup" element={<Onboarding />} />
          <Route path="/new" element={<NewVideoWizard />} />
          <Route path="/agents" element={<AgentsLive />} />
          <Route path="/review" element={<Review />} />
          <Route path="/library" element={<Library />} />
          <Route path="/schedules" element={<Schedules />} />
          <Route path="/calendar" element={<Navigate to="/schedules" replace />} />
          <Route path="/channels" element={<Channels />} />
          <Route path="/analytics" element={<Analytics />} />
          <Route path="/growth" element={<Growth />} />
          <Route path="/comments" element={<Comments />} />
          <Route path="/costs" element={<Costs />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </AppShell>
    </HashRouter>
  );
}

export default function App() {
  // BackendGate (§4.6/§4.7): owns the backend bootstrap and renders
  // the starting / failure / out-of-sync screens instead of the
  // dashboard until the backend is READY.
  return (
    <BackendGate>
      <Dashboard />
    </BackendGate>
  );
}
