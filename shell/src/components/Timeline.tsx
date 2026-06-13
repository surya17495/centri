import { useEffect, useRef, useState } from "react";
import type { StatusResponse, TimelineItem } from "../types";
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

function shouldHideEvent(item: TimelineItem): boolean {
  if (item.kind !== "event") return false;
  const type = item.event.type ?? "";
  return (
    type.startsWith("curation.") ||
    type === "memory.recall" ||
    type === "context.updated" ||
    type.startsWith("brief.") ||
    type === "ingest.opencode.message" ||
    type === "ingest.hermes.message" ||
    type === "ingest.mempalace.message"
  );
}

function UserMessage({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] rounded-2xl rounded-br-md border border-accent/25 bg-accent/[0.13] px-4 py-2.5 text-sm leading-relaxed text-ink shadow-[inset_0_1px_0_rgba(255,255,255,0.07),0_4px_16px_rgba(0,0,0,0.25)] backdrop-blur-md">
        {text}
      </div>
    </div>
  );
}

function AssistantMessage({ text }: { text: string }) {
  const looksLikeToolDump = /<bash>|<\/bash>|^\s*(find|grep|curl|python|npm|git|docker)\b/m.test(text);
  const [expanded, setExpanded] = useState(!looksLikeToolDump);
  return (
    <div className="flex gap-3">
      <span className="mt-0.5 shrink-0 text-ink-faint">
        <Logo size={16} />
      </span>
      <div className="max-w-[85%] text-sm leading-relaxed text-ink">
        {looksLikeToolDump && !expanded ? (
          <div className="rounded-xl border border-white/[0.08] bg-white/[0.035] px-3 py-2.5">
            <div className="text-ink">Ran a background tool step.</div>
            <button
              onClick={() => setExpanded(true)}
              className="mt-1 text-xs text-ink-dim underline decoration-white/20 underline-offset-4 hover:text-ink"
            >
              Show command output
            </button>
          </div>
        ) : (
          <>
            {text}
            {looksLikeToolDump && (
              <button
                onClick={() => setExpanded(false)}
                className="ml-2 text-xs text-ink-dim underline decoration-white/20 underline-offset-4 hover:text-ink"
              >
                Hide output
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function ActivityCard({ status }: { status: StatusResponse | null | undefined }) {
  const running = status?.running_tasks ?? 0;
  const pending = status?.pending_approvals ?? 0;
  if (running === 0 && pending === 0) return null;
  const waiting = pending > 0;
  return (
    <div className="animate-rise-in pl-7">
      <div className="glass rounded-xl border border-accent/15 p-4">
        <div className="flex items-center gap-2 text-sm font-medium text-ink">
          <span className={`h-2 w-2 rounded-full ${waiting ? "bg-amber-400" : "bg-accent animate-pulse"}`} aria-hidden />
          {waiting ? "Waiting for your approval" : "Working in the background"}
        </div>
        <p className="mt-1.5 text-xs leading-relaxed text-ink-dim">
          {running > 0 && `${running} task${running === 1 ? " is" : "s are"} running. `}
          {pending > 0 && `${pending} approval${pending === 1 ? " is" : "s are"} pending. `}
          Updates stream here as they complete; you can keep typing while CENTRI works.
        </p>
      </div>
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
            className="glass-chip rounded-full px-3.5 py-1.5 text-[12px] text-ink-dim transition-colors hover:bg-white/[0.08] hover:text-ink"
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
  status,
}: {
  items: TimelineItem[];
  onResolve: (id: string, decision: "approve" | "reject") => Promise<void>;
  status?: StatusResponse | null;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  const visibleItems = items.filter((item) => !shouldHideEvent(item));
  const hasActivity = (status?.running_tasks ?? 0) > 0 || (status?.pending_approvals ?? 0) > 0;

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ behavior: "smooth" });
  }, [items.length]);

  if (visibleItems.length === 0 && !hasActivity) {
    return <EmptyState />;
  }

  return (
    <div className="scrollbar-thin h-full overflow-y-auto">
      <div className="mx-auto flex w-full max-w-2xl flex-col gap-4 px-4 py-8">
        {visibleItems.map((item) => {
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
        <ActivityCard status={status} />
        <div ref={endRef} />
      </div>
    </div>
  );
}
