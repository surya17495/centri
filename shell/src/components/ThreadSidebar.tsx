import type { Thread } from "../types";

// Minimal chat-thread rail: list / new / switch. Threads scope only the chat
// timeline; CENTRI's memory stays global across all of them (the point of 3b.2).
export function ThreadSidebar({
  threads,
  activeThreadId,
  onSelect,
  onNew,
}: {
  threads: Thread[];
  activeThreadId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
}) {
  return (
    <aside className="flex h-full w-52 shrink-0 flex-col border-r border-white/[0.08] bg-[rgba(12,12,18,0.4)] backdrop-blur-2xl">
      <div className="flex items-center justify-between px-3 py-2.5">
        <span className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-dim">
          Threads
        </span>
        <button
          onClick={onNew}
          aria-label="New thread"
          className="glass-chip grid h-6 w-6 place-items-center rounded-lg text-ink-dim transition-colors hover:text-ink"
          title="New thread"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M12 5v14M5 12h14" strokeLinecap="round" />
          </svg>
        </button>
      </div>

      <nav className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {threads.length === 0 ? (
          <p className="px-2 py-1 text-[11px] text-ink-faint">No threads yet</p>
        ) : (
          <ul className="flex flex-col gap-0.5">
            {threads.map((t) => {
              const active = t.id === activeThreadId;
              return (
                <li key={t.id}>
                  <button
                    onClick={() => onSelect(t.id)}
                    aria-current={active ? "true" : undefined}
                    className={`w-full truncate rounded-lg px-2.5 py-1.5 text-left text-[12px] transition-colors ${
                      active
                        ? "bg-accent/20 text-ink"
                        : "text-ink-dim hover:bg-white/[0.05] hover:text-ink"
                    }`}
                    title={t.title}
                  >
                    {t.title || "Untitled"}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </nav>
    </aside>
  );
}
