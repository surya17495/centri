import { useState } from "react";
import type { TaskCard as TaskCardData } from "../types";

const STATUS_STYLES: Record<string, string> = {
  running: "bg-accent/15 text-accent",
  completed: "bg-emerald-500/15 text-emerald-400",
  failed: "bg-rose-500/15 text-rose-400",
  cancelled: "bg-surface-3 text-ink-dim",
};

export function TaskCard({ card }: { card: TaskCardData }) {
  const [expanded, setExpanded] = useState(card.status === "running");
  const latest = card.progress[card.progress.length - 1];
  const badge = STATUS_STYLES[card.status] ?? "bg-surface-3 text-ink-dim";

  return (
    <div className="rounded-xl border border-surface-2 bg-surface-1 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-ink">{card.description}</div>
          {latest && !expanded && (
            <div className="mt-1 truncate text-xs text-ink-dim">{latest.summary}</div>
          )}
        </div>
        <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${badge}`}>
          {card.status}
        </span>
      </div>

      {card.progress.length > 0 && (
        <button
          onClick={() => setExpanded((e) => !e)}
          className="mt-2 text-xs text-ink-dim hover:text-ink"
        >
          {expanded ? "Hide detail" : `Show detail (${card.progress.length})`}
        </button>
      )}

      {expanded && card.progress.length > 0 && (
        <ol className="mt-2 space-y-1 border-l border-surface-2 pl-3">
          {card.progress.map((p, i) => (
            <li key={i} className="text-xs text-ink-dim">
              {typeof p.percent === "number" && (
                <span className="mr-1 tabular-nums text-accent">{p.percent}%</span>
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
              className="flex items-center gap-2 rounded-lg bg-surface-2 px-2.5 py-1.5 text-xs"
            >
              <span className="rounded bg-surface-3 px-1.5 py-0.5 text-[10px] uppercase text-ink-dim">
                {a.type}
              </span>
              <span className="truncate text-ink">{a.title}</span>
              {a.summary && <span className="truncate text-ink-dim">— {a.summary}</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
