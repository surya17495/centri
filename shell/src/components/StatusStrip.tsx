import type { ConnectionState, StatusResponse } from "../types";

const DOT: Record<ConnectionState, string> = {
  online: "bg-emerald-400",
  connecting: "bg-amber-400 animate-pulse",
  offline: "bg-rose-500",
};

const DOT_LABEL: Record<ConnectionState, string> = {
  online: "Online",
  connecting: "Connecting…",
  offline: "Offline",
};

export function StatusStrip({
  connection,
  status,
  onOpenSettings,
}: {
  connection: ConnectionState;
  status: StatusResponse | null;
  onOpenSettings: () => void;
}) {
  const activeHand = status?.hands.find((h) => h.configured && h.healthy) ?? status?.hands[0];

  return (
    <header className="flex items-center justify-between border-b border-surface-2 bg-surface-1 px-4 py-2.5">
      <div className="flex items-center gap-4 text-xs">
        <span className="flex items-center gap-1.5">
          <span className={`h-2 w-2 rounded-full ${DOT[connection]}`} aria-hidden />
          <span className="text-ink-dim">{DOT_LABEL[connection]}</span>
        </span>

        {activeHand && (
          <span className="flex items-center gap-1.5 text-ink-dim">
            <span className="text-ink">{activeHand.name}</span>
            <span
              className={
                activeHand.healthy ? "text-emerald-400" : "text-rose-400"
              }
              title={activeHand.detail}
            >
              {activeHand.healthy ? "healthy" : "unavailable"}
            </span>
          </span>
        )}

        {status && (
          <span className="text-ink-faint">
            {status.running_tasks} running · {status.pending_approvals} pending
          </span>
        )}
      </div>

      <div className="flex items-center gap-3">
        {status?.role_models && Object.keys(status.role_models).length > 0 && (
          <span className="hidden gap-2 text-[10px] text-ink-faint sm:flex">
            {Object.entries(status.role_models).map(([role, model]) => (
              <span key={role} title={`${role}: ${model}`}>
                {role}=<span className="text-ink-dim">{model}</span>
              </span>
            ))}
          </span>
        )}
        <button
          onClick={onOpenSettings}
          aria-label="Settings"
          className="rounded-lg p-1.5 text-ink-dim hover:bg-surface-2 hover:text-ink"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
        </button>
      </div>
    </header>
  );
}
