import { useState } from "react";
import { useEventStream } from "./useEventStream";
import { StatusStrip } from "./components/StatusStrip";
import { Timeline } from "./components/Timeline";
import { CommandBar } from "./components/CommandBar";
import { SettingsPanel } from "./components/SettingsPanel";

export default function App() {
  const { connection, timeline, status, refreshStatus, resolveApproval } = useEventStream();
  const [settingsOpen, setSettingsOpen] = useState(false);

  return (
    <div className="flex h-full flex-col bg-transparent">
      <StatusStrip
        connection={connection}
        status={status}
        onOpenSettings={() => setSettingsOpen(true)}
      />

      <main className="min-h-0 flex-1">
        <Timeline items={timeline} onResolve={resolveApproval} />
      </main>

      <CommandBar />

      {settingsOpen && (
        <SettingsPanel
          status={status}
          onClose={() => setSettingsOpen(false)}
          onSaved={() => {
            refreshStatus();
          }}
        />
      )}
    </div>
  );
}
