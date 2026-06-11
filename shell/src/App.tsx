import { useCallback, useEffect, useState } from "react";
import { useEventStream } from "./useEventStream";
import { api } from "./api";
import { StatusStrip } from "./components/StatusStrip";
import { ThreadSidebar } from "./components/ThreadSidebar";
import { Timeline } from "./components/Timeline";
import { CommandBar } from "./components/CommandBar";
import { SettingsPanel } from "./components/SettingsPanel";
import type { Thread } from "./types";

export default function App() {
  const [threads, setThreads] = useState<Thread[]>([]);
  const [activeThreadId, setActiveThreadId] = useState<string | null>(null);
  const { connection, timeline, status, refreshStatus, resolveApproval } =
    useEventStream(activeThreadId);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const refreshThreads = useCallback(() => {
    api
      .threads()
      .then((res) => setThreads(res.threads))
      .catch(() => {
        /* sidebar stays as-is; surfaced via connection dot */
      });
  }, []);

  useEffect(() => {
    refreshThreads();
  }, [refreshThreads]);

  const newThread = useCallback(async () => {
    try {
      const { thread } = await api.createThread();
      setThreads((prev) => [thread, ...prev]);
      setActiveThreadId(thread.id);
    } catch {
      /* ignore; user can retry */
    }
  }, []);

  return (
    <div className="flex h-full flex-col bg-transparent">
      <StatusStrip
        connection={connection}
        status={status}
        onOpenSettings={() => setSettingsOpen(true)}
      />

      <div className="flex min-h-0 flex-1">
        <ThreadSidebar
          threads={threads}
          activeThreadId={activeThreadId}
          onSelect={setActiveThreadId}
          onNew={newThread}
        />
        <main className="min-h-0 flex-1">
          <Timeline items={timeline} onResolve={resolveApproval} />
        </main>
      </div>

      <CommandBar threadId={activeThreadId} onSent={refreshThreads} />

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
