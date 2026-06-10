import { useState, type FormEvent, type KeyboardEvent } from "react";
import { api } from "../api";

export function CommandBar() {
  const [text, setText] = useState("");
  const [inFlight, setInFlight] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
    <form onSubmit={onSubmit} className="border-t border-surface-2 bg-surface-1 px-4 py-3">
      {error && <div className="mb-2 text-xs text-rose-400">{error}</div>}
      <div className="flex items-center gap-2 rounded-xl border border-surface-2 bg-surface-0 px-3 py-2 focus-within:border-accent">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={inFlight}
          placeholder="Ask CENTRI to do something…"
          aria-label="Command input"
          className="flex-1 bg-transparent text-sm text-ink placeholder:text-ink-faint focus:outline-none disabled:opacity-60"
        />
        <button
          type="submit"
          disabled={inFlight || !text.trim()}
          className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent-hover disabled:opacity-40"
        >
          {inFlight ? "Sending…" : "Send"}
        </button>
      </div>
    </form>
  );
}
