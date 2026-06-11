import type { ConnectionState, StatusResponse } from "../types";
import { Logo } from "./Logo";

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

function Pill({
  children,
  title,
}: {
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title}
      className="glass-chip inline-flex h-6 items-center gap-1.5 rounded-full px-2.5 text-[11px] font-medium text-ink-dim"
    >
      {children}
    </span>
  );
}

export function StatusStrip({
  connection,
  status,
  onOpenSettings,
}: {
  connection: ConnectionState;
  status: StatusResponse | null;
  onOpenSettings: () => void;
}) {
  const activeHand =
    status?.hands.find((h) => h.configured && h.healthy) ?? status?.hands[0];
  const configuredModels = Object.values(status?.role_models ?? {}).filter(
    (m) => m.configured,
  ).length;
  const busy =
    (status?.running_tasks ?? 0) > 0 || (status?.pending_approvals ?? 0) > 0;

  return (
    <header className="flex h-12 shrink-0 items-center justify-between border-b border-white/[0.08] bg-[rgba(12,12,18,0.5)] px-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.05)] backdrop-blur-2xl">
      <div className="flex items-center gap-2.5">
        <span className="text-ink">
          <Logo size={18} />
        </span>
        <span className="text-[13px] font-semibold tracking-[0.18em] text-ink">
          CENTRI
        </span>
        <span className="ml-2 hidden items-center gap-1.5 sm:inline-flex">
          <span className={`h-1.5 w-1.5 rounded-full ${DOT[connection]}`} aria-hidden />
          <span className="text-[11px] text-ink-dim">{DOT_LABEL[connection]}</span>
        </span>
      </div>

      <div className="flex items-center gap-2">
        {activeHand && (
          <Pill title={activeHand.detail}>
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                activeHand.healthy ? "bg-emerald-400" : "bg-ink-faint"
              }`}
              aria-hidden
            />
            <span className="text-ink">{activeHand.name}</span>
          </Pill>
        )}

        {status && busy && (
          <Pill>
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" aria-hidden />
            {status.running_tasks} running
            {status.pending_approvals > 0 && ` · ${status.pending_approvals} pending`}
          </Pill>
        )}

        {configuredModels > 0 && (
          <button
            onClick={onOpenSettings}
            className="glass-chip hidden h-6 items-center rounded-full px-2.5 text-[11px] font-medium text-ink-dim transition-colors hover:text-ink sm:inline-flex"
            title="View model roles in settings"
          >
            {configuredModels} models
          </button>
        )}

        <button
          onClick={onOpenSettings}
          aria-label="Settings"
          className="rounded-lg p-1.5 text-ink-dim transition-colors hover:bg-white/[0.06] hover:text-ink"
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
        </button>
      </div>
    </header>
  );
}
