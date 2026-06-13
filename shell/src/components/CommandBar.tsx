import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { api } from "../api";

export function CommandBar({
  threadId = null,
  onSent,
}: {
  threadId?: string | null;
  onSent?: () => void;
} = {}) {
  const [text, setText] = useState("");
  const [inFlight, setInFlight] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!inFlight) {
      setElapsed(0);
      return;
    }
    const started = Date.now();
    const id = window.setInterval(() => {
      setElapsed(Math.max(1, Math.floor((Date.now() - started) / 1000)));
    }, 1000);
    return () => window.clearInterval(id);
  }, [inFlight]);

  // Empty-state suggestion chips prefill the composer.
  useEffect(() => {
    function onPrefill(e: Event) {
      const detail = (e as CustomEvent<string>).detail;
      if (typeof detail === "string") {
        setText(detail);
        inputRef.current?.focus();
      }
    }
    window.addEventListener("centri:prefill", onPrefill);
    return () => window.removeEventListener("centri:prefill", onPrefill);
  }, []);

  async function submit() {
    const trimmed = text.trim();
    if (!trimmed || inFlight) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setInFlight(true);
    setError(null);
    try {
      await api.utterance(trimmed, threadId, controller.signal);
      setText("");
      onSent?.();
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") {
        setError("Stopped waiting. The answer may still appear if the server finishes.");
      } else {
        setError(e instanceof Error ? e.message : "Failed to send");
      }
    } finally {
      setInFlight(false);
      abortRef.current = null;
      inputRef.current?.focus();
    }
  }

  function stopWaiting() {
    abortRef.current?.abort();
    setInFlight(false);
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    void submit();
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void submit();
    }
  }

  return (
    <div className="shrink-0 pb-5 pt-2">
      <form onSubmit={onSubmit} className="mx-auto w-full max-w-2xl px-4">
        {error && (
          <div className="mb-2 animate-rise-in rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-1.5 text-xs text-rose-300">
            {error}
          </div>
        )}
        {inFlight && (
          <div className="mb-2 animate-rise-in rounded-xl border border-accent/25 bg-accent/[0.08] px-3 py-2 text-xs text-ink-dim shadow-[0_12px_40px_rgba(0,0,0,0.28)] backdrop-blur-md">
            <div className="flex items-center justify-between gap-3">
              <div className="flex min-w-0 items-center gap-2">
                <span className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-accent" aria-hidden />
                <span className="truncate text-ink">Generating response</span>
                <span className="font-mono text-[10px] text-ink-faint">{elapsed}s</span>
              </div>
              <button
                type="button"
                onClick={stopWaiting}
                className="shrink-0 rounded-lg border border-white/[0.1] px-2.5 py-1 text-[11px] font-medium text-ink-dim transition-colors hover:bg-white/[0.07] hover:text-ink"
              >
                Stop waiting
              </button>
            </div>
            <div className="mt-1 text-[11px] leading-relaxed text-ink-faint">
              The model is working in the background. You can stop waiting without cancelling server-side work.
            </div>
          </div>
        )}
        <div className="glass-deep flex items-center gap-2 rounded-2xl px-4 py-3 transition-shadow focus-within:shadow-[inset_0_1px_0_rgba(255,255,255,0.09),0_0_0_1px_rgba(124,124,244,0.55),0_16px_48px_rgba(0,0,0,0.5)]">
          <input
            ref={inputRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            disabled={inFlight}
            placeholder="Ask CENTRI to do something…"
            aria-label="Command input"
            className="flex-1 bg-transparent text-sm text-ink placeholder:text-ink-faint focus:outline-none disabled:opacity-60"
          />
          <kbd className="glass-chip hidden rounded px-1.5 py-0.5 font-mono text-[10px] text-ink-faint sm:block">
            ↵
          </kbd>
          <button
            type="submit"
            disabled={inFlight || !text.trim()}
            aria-label="Send"
            className="grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-accent text-white transition-colors hover:bg-accent-hover disabled:bg-surface-3 disabled:text-ink-faint"
          >
            {inFlight ? (
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="animate-spin">
                <path d="M21 12a9 9 0 1 1-6.2-8.56" strokeLinecap="round" />
              </svg>
            ) : (
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                <path d="M12 19V5M5 12l7-7 7 7" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            )}
          </button>
        </div>
      </form>
    </div>
  );
}
