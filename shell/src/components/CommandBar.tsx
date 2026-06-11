import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { api } from "../api";

export function CommandBar() {
  const [text, setText] = useState("");
  const [inFlight, setInFlight] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

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
    setInFlight(true);
    setError(null);
    try {
      await api.utterance(trimmed);
      setText("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to send");
    } finally {
      setInFlight(false);
      inputRef.current?.focus();
    }
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
    <div className="shrink-0 px-4 pb-5 pt-2">
      <form onSubmit={onSubmit} className="mx-auto w-full max-w-2xl">
        {error && (
          <div className="mb-2 animate-rise-in rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-1.5 text-xs text-rose-300">
            {error}
          </div>
        )}
        <div className="flex items-center gap-2 rounded-2xl bg-surface-1 px-4 py-3 shadow-composer transition-shadow focus-within:shadow-[0_0_0_1px_rgba(124,124,244,0.5),0_8px_24px_rgba(0,0,0,0.45)]">
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
          <kbd className="hidden rounded border border-line bg-surface-2 px-1.5 py-0.5 font-mono text-[10px] text-ink-faint sm:block">
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
