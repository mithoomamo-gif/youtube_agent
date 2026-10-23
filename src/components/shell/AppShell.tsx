import type { ReactNode } from "react";
import { Sidebar } from "./Sidebar";
import { StatusBar } from "./StatusBar";
import { TitleBar } from "./TitleBar";

// App shell layout. Tray residency (close-to-tray, background engine) is
// handled by the Tauri shell in src-tauri/, not by this component.
export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full flex-col overflow-hidden bg-app font-sans text-ink">
      <TitleBar />
      <div className="flex min-h-0 flex-1">
        <Sidebar />
        <main className="flex-1 overflow-y-auto p-6">{children}</main>
      </div>
      <StatusBar />
    </div>
  );
}
