import { useEffect, useRef } from "react";
import type { TimelineItem } from "../types";
import { TaskCard } from "./TaskCard";
import { ApprovalCard } from "./ApprovalCard";

function RawEvent({ item }: { item: Extract<TimelineItem, { kind: "event" }> }) {
  const { event } = item;
  const label = event.summary ?? event.message ?? event.title ?? event.type;
  return (
    <div className="flex items-baseline gap-2 text-xs text-ink-dim">
      <span className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[10px] text-ink-faint">
        {event.type}
      </span>
      <span className="truncate">{label}</span>
    </div>
  );
}

export function Timeline({
  items,
  onResolve,
}: {
  items: TimelineItem[];
  onResolve: (id: string, decision: "approve" | "reject") => Promise<void>;
}) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ behavior: "smooth" });
  }, [items.length]);

  if (items.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-ink-faint">
        No activity yet — send a message to get started.
      </div>
    );
  }

  return (
    <div className="scrollbar-thin flex flex-col gap-3 overflow-y-auto px-4 py-6">
      {items.map((item) => {
        switch (item.kind) {
          case "narration":
            return (
              <div key={item.id} className="max-w-2xl text-sm leading-relaxed text-ink">
                {item.text}
              </div>
            );
          case "task":
            return <TaskCard key={item.id} card={item.card} />;
          case "approval":
            return <ApprovalCard key={item.id} card={item.card} onResolve={onResolve} />;
          case "event":
            return <RawEvent key={item.id} item={item} />;
          default:
            return null;
        }
      })}
      <div ref={endRef} />
    </div>
  );
}
