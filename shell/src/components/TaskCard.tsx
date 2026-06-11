import { useState } from "react";
import type { TaskCard as TaskCardData } from "../types";

const STATUS_META: Record<string, { badge: string; bar?: string }> = {
  running: { badge: "bg-accent/15 text-accent", bar: "bg-accent" },
  completed: { badge: "bg-emerald-500/15 text-emerald-400" },
  failed: { badge: "bg-rose-500/15 text-rose-400" },
  cancelled: { badge: "bg-surface-3 text-ink-dim" },
};

export function TaskCard({ card }: { card: TaskCardData }) {
  const [expanded, setExpanded] = useState(card.status === "running");
  const latest = card.progress[card.progress.length - 1];
  const meta = STATUS_META[card.status] ?? { badge: "bg-surface-3 text-ink-dim" };
  const percent =
    typeof latest?.percent === "number" ? Math.min(100, latest.percent) : null;

  return (
    <div className="overflow-hidden rounded-xl border border-line bg-surface-1 shadow-card">
      {card.status === "running" && (
        <div className="h-0.5 w-full bg-surface-2">
          <div
            className={`h-full ${meta.bar ?? "bg-accent"} transition-all duration-500`}
            style={{ width: `${percent ?? 100}%` }}
          />
        </div>
      )}
      <div className="p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="truncate text-sm font-medium text-ink">{card.description}</div>
            {latest && !expanded && (
              <div className="mt-1 truncate text-xs text-ink-dim">{latest.summary}</div>
            )}
          </div>
          <span
            className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${meta.badge}`}
          >
            {card.status}
          </span>
        </div>

        {card.progress.length > 0 && (
          <button
            onClick={() => setExpanded((e) => !e)}
            className="mt-2.5 inline-flex items-center gap-1 text-xs text-ink-dim transition-colors hover:text-ink"
          >
            <svg
              width="10"
              height="10"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.5"
              className={`transition-transform ${expanded ? "rotate-90" : ""}`}
            >
              <path d="M9 18l6-6-6-6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            {expanded ? "Hide detail" : `Show detail (${card.progress.length})`}
          </button>
        )}

        {expanded && card.progress.length > 0 && (
          <ol className="mt-2.5 space-y-1.5 border-l border-line pl-3.5">
            {card.progress.map((p, i) => (
              <li key={i} className="font-mono text-[11px] leading-relaxed text-ink-dim">
                {typeof p.percent === "number" && (
                  <span className="mr-1.5 tabular-nums text-accent">{p.percent}%</span>
                )}
                {p.summary}
              </li>
            ))}
          </ol>
        )}

        {card.artifacts.length > 0 && (
          <div className="mt-3 space-y-1">
            {card.artifacts.map((a, i) => (
              <div
                key={i}
                className="flex items-center gap-2 rounded-lg border border-line bg-surface-2 px-2.5 py-1.5 text-xs"
              >
                <span className="rounded bg-surface-3 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-ink-dim">
                  {a.type}
                </span>
                <span className="truncate text-ink">{a.title}</span>
                {a.summary && <span className="truncate text-ink-dim">— {a.summary}</span>}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
