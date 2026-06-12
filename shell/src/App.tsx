import { useCallback, useEffect, useState } from "react";
import { useEventStream } from "./useEventStream";
import { api } from "./api";
import { StatusStrip } from "./components/StatusStrip";
import { ThreadSidebar } from "./components/ThreadSidebar";
import { Timeline } from "./components/Timeline";
import { CommandBar } from "./components/CommandBar";
import { SettingsPanel } from "./components/SettingsPanel";
import { OnboardingCard } from "./components/OnboardingCard";
import type { DiscoverResponse, Thread } from "./types";

export default function App() {
  const [threads, setThreads] = useState<Thread[]>([]);
  const [activeThreadId, setActiveThreadId] = useState<string | null>(null);
  const { connection, timeline, status, bootstrap, refreshStatus, resolveApproval } =
    useEventStream(activeThreadId);
  const [settingsOpen, setSettingsOpen] = useState(false);

  // First-run import: discover the user's coding-agent histories on launch and
  // offer a one-click import. "Should we show it?" derives from the backend
  // bootstrapped flag (not localStorage); dismissal is per-session.
  const [discover, setDiscover] = useState<DiscoverResponse | null>(null);
  const [dismissedOnboarding, setDismissedOnboarding] = useState(false);
  const [importing, setImporting] = useState(false);

  const refreshDiscover = useCallback(() => {
    api
      .discover()
      .then(setDiscover)
      .catch(() => {
        /* discovery is best-effort; no card if it fails */
      });
  }, []);

  useEffect(() => {
    refreshDiscover();
  }, [refreshDiscover]);

  const startImport = useCallback(async () => {
    setImporting(true);
    try {
      await api.bootstrap();
    } catch {
      /* progress (or absence) is reflected via the event stream */
    } finally {
      refreshDiscover();
    }
  }, [refreshDiscover]);

  const reimport = useCallback(() => {
    setDismissedOnboarding(false);
    setImporting(false);
    refreshDiscover();
  }, [refreshDiscover]);

  const showOnboarding =
    !dismissedOnboarding &&
    discover !== null &&
    !discover.bootstrapped;

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
        <main className="flex min-h-0 flex-1 flex-col">
          {showOnboarding && discover && (
            <div className="shrink-0 pt-4">
              <div className="mx-auto w-full max-w-2xl px-4">
                <OnboardingCard
                  discover={discover}
                  bootstrap={bootstrap}
                  importing={importing}
                  onImport={startImport}
                  onDismiss={() => setDismissedOnboarding(true)}
                />
              </div>
            </div>
          )}
          <div className="min-h-0 flex-1">
            <Timeline items={timeline} onResolve={resolveApproval} />
          </div>
          <CommandBar threadId={activeThreadId} onSent={refreshThreads} />
        </main>
      </div>

      {settingsOpen && (
        <SettingsPanel
          status={status}
          discover={discover}
          onReimport={() => {
            reimport();
            setSettingsOpen(false);
          }}
          onClose={() => setSettingsOpen(false)}
          onSaved={() => {
            refreshStatus();
          }}
        />
      )}
    </div>
  );
}
