import { useEffect, useRef } from "react";
import type { TimelineItem } from "../types";
import { TaskCard } from "./TaskCard";
import { ApprovalCard } from "./ApprovalCard";
import { Logo } from "./Logo";

const EVENT_TINT: Record<string, string> = {
  memory: "bg-violet-400/70",
  context: "bg-sky-400/70",
  procedural: "bg-violet-400/70",
  approval: "bg-amber-400/70",
  scheduler: "bg-ink-faint",
};

function RawEvent({ item }: { item: Extract<TimelineItem, { kind: "event" }> }) {
  const { event } = item;
  const label = event.summary ?? event.message ?? event.title ?? event.type;
  const family = (event.type ?? "").split(".")[0];
  const tint = EVENT_TINT[family] ?? "bg-ink-faint";
  return (
    <div className="flex items-center gap-2.5 pl-1 text-[11px] text-ink-faint">
      <span className={`h-1 w-1 shrink-0 rounded-full ${tint}`} aria-hidden />
      <span className="shrink-0 font-mono text-[10px] tracking-tight text-ink-faint/90">
        {event.type}
      </span>
      {label !== event.type && <span className="truncate text-ink-faint">{label}</span>}
    </div>
  );
}

function UserMessage({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] rounded-2xl rounded-br-md border border-line bg-surface-2 px-4 py-2.5 text-sm leading-relaxed text-ink shadow-card">
        {text}
      </div>
    </div>
  );
}

function AssistantMessage({ text }: { text: string }) {
  return (
    <div className="flex gap-3">
      <span className="mt-0.5 shrink-0 text-ink-faint">
        <Logo size={16} />
      </span>
      <div className="max-w-[85%] text-sm leading-relaxed text-ink">{text}</div>
    </div>
  );
}

const SUGGESTIONS = [
  "What's on my plate today?",
  "Please refactor the auth module",
  "Summarize my open loops",
];

function EmptyState() {
  function prefill(text: string) {
    window.dispatchEvent(new CustomEvent("centri:prefill", { detail: text }));
  }
  return (
    <div className="flex h-full flex-col items-center justify-center gap-6 px-6">
      <span className="text-ink-faint animate-fade-in">
        <Logo size={44} />
      </span>
      <div className="text-center animate-rise-in">
        <h1 className="text-lg font-semibold tracking-tight text-ink">
          What should CENTRI do?
        </h1>
        <p className="mt-1.5 text-[13px] text-ink-faint">
          No activity yet — send a message to get started.
        </p>
      </div>
      <div className="flex flex-wrap justify-center gap-2 animate-rise-in">
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            onClick={() => prefill(s)}
            className="rounded-full border border-line bg-surface-1 px-3.5 py-1.5 text-[12px] text-ink-dim transition-colors hover:border-line-strong hover:bg-surface-2 hover:text-ink"
          >
            {s}
          </button>
        ))}
      </div>
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
    return <EmptyState />;
  }

  return (
    <div className="scrollbar-thin h-full overflow-y-auto">
      <div className="mx-auto flex w-full max-w-2xl flex-col gap-4 px-4 py-8">
        {items.map((item) => {
          switch (item.kind) {
            case "narration":
              return (
                <div key={item.id} className="animate-rise-in">
                  {item.role === "user" ? (
                    <UserMessage text={item.text} />
                  ) : (
                    <AssistantMessage text={item.text} />
                  )}
                </div>
              );
            case "task":
              return (
                <div key={item.id} className="animate-rise-in pl-7">
                  <TaskCard card={item.card} />
                </div>
              );
            case "approval":
              return (
                <div key={item.id} className="animate-rise-in pl-7">
                  <ApprovalCard card={item.card} onResolve={onResolve} />
                </div>
              );
            case "event":
              return (
                <div key={item.id} className="animate-fade-in pl-7">
                  <RawEvent item={item} />
                </div>
              );
            default:
              return null;
          }
        })}
        <div ref={endRef} />
      </div>
    </div>
  );
}
