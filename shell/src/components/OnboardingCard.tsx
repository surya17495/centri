import { useMemo } from "react";
import type { BootstrapProgress } from "../useEventStream";
import type { DiscoverResponse } from "../types";

const AGENT_LABEL: Record<string, string> = {
  opencode: "OpenCode",
  claude_code: "Claude Code",
  cursor: "Cursor",
};

function agentLabel(agent: string): string {
  return AGENT_LABEL[agent] ?? agent;
}

// "Found N OpenCode messages, M Claude Code sessions, K Cursor chats" — one
// clause per available source, summing counts per agent.
function describeFindings(discover: DiscoverResponse): string {
  const byAgent = new Map<string, number>();
  for (const s of discover.sources) {
    if (!s.available) continue;
    byAgent.set(s.agent, (byAgent.get(s.agent) ?? 0) + (s.count ?? 0));
  }
  const parts = [...byAgent.entries()].map(
    ([agent, count]) => `${count.toLocaleString()} ${agentLabel(agent)} message${count === 1 ? "" : "s"}`,
  );
  if (parts.length === 0) return "No prior coding-agent history found.";
  if (parts.length === 1) return `Found ${parts[0]}`;
  return `Found ${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

export function OnboardingCard({
  discover,
  bootstrap,
  importing,
  onImport,
  onDismiss,
}: {
  discover: DiscoverResponse;
  bootstrap: BootstrapProgress | null;
  importing: boolean;
  onImport: () => void;
  onDismiss: () => void;
}) {
  const findings = useMemo(() => describeFindings(discover), [discover]);
  const running = importing && bootstrap?.phase !== "completed";
  const done = bootstrap?.phase === "completed";

  return (
    <div className="glass overflow-hidden rounded-xl border border-accent/20">
      {running && (
        <div className="h-0.5 w-full overflow-hidden bg-white/[0.06]">
          <div className="h-full w-1/3 animate-pulse bg-accent" />
        </div>
      )}
      <div className="p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="text-sm font-semibold text-ink">Import your memory</div>
            <p className="mt-1 text-xs leading-relaxed text-ink-dim">
              {done
                ? `Imported ${bootstrap?.imported.toLocaleString() ?? 0} message${
                    bootstrap?.imported === 1 ? "" : "s"
                  } into memory. CENTRI now remembers your prior coding sessions.`
                : `${findings} — import into memory?`}
            </p>
            {running && bootstrap?.lastSummary && (
              <p className="mt-1.5 truncate font-mono text-[11px] text-accent" aria-live="polite">
                {bootstrap.lastSummary}
              </p>
            )}
          </div>
          {!done && (
            <button
              onClick={onDismiss}
              aria-label="Dismiss import"
              className="shrink-0 rounded-lg p-1 text-ink-dim transition-colors hover:bg-white/[0.06] hover:text-ink"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M18 6 6 18M6 6l12 12" strokeLinecap="round" />
              </svg>
            </button>
          )}
        </div>

        {!done && (
          <div className="mt-3 flex items-center gap-2">
            <button
              onClick={onImport}
              disabled={running}
              className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {running ? "Importing…" : "Import into memory"}
            </button>
            <button
              onClick={onDismiss}
              disabled={running}
              className="rounded-lg px-3 py-1.5 text-xs font-medium text-ink-dim transition-colors hover:text-ink disabled:opacity-60"
            >
              Skip
            </button>
          </div>
        )}

        {done && (
          <button
            onClick={onDismiss}
            className="mt-3 rounded-lg px-3 py-1.5 text-xs font-medium text-ink-dim transition-colors hover:text-ink"
          >
            Dismiss
          </button>
        )}
      </div>
    </div>
  );
}
