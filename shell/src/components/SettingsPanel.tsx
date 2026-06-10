import { useState } from "react";
import { api, getBackendUrl, setBackendUrl } from "../api";
import type { StatusResponse } from "../types";

export function SettingsPanel({
  status,
  onClose,
  onSaved,
}: {
  status: StatusResponse | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [backend, setBackend] = useState(getBackendUrl());
  const [provider, setProvider] = useState("anthropic");
  const [apiKey, setApiKey] = useState("");
  const [keyStatus, setKeyStatus] = useState<string | null>(null);

  function saveBackend() {
    setBackendUrl(backend);
    onSaved();
  }

  async function connectKey() {
    if (!apiKey.trim()) return;
    setKeyStatus("Connecting…");
    try {
      await api.connectAccount(provider, apiKey.trim());
      setKeyStatus(`Connected ${provider}`);
      setApiKey("");
      onSaved();
    } catch (e) {
      setKeyStatus(e instanceof Error ? e.message : "Failed");
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 p-4 pt-16"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-2xl border border-surface-2 bg-surface-1 p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-ink">Settings</h2>
          <button onClick={onClose} aria-label="Close settings" className="text-ink-dim hover:text-ink">
            ✕
          </button>
        </div>

        <section className="mt-5">
          <label className="text-xs font-medium text-ink-dim">Backend URL</label>
          <div className="mt-1.5 flex gap-2">
            <input
              value={backend}
              onChange={(e) => setBackend(e.target.value)}
              className="flex-1 rounded-lg border border-surface-2 bg-surface-0 px-2.5 py-1.5 text-xs text-ink focus:border-accent focus:outline-none"
            />
            <button
              onClick={saveBackend}
              className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent-hover"
            >
              Save
            </button>
          </div>
        </section>

        <section className="mt-5">
          <label className="text-xs font-medium text-ink-dim">API key (BYOK)</label>
          <div className="mt-1.5 flex gap-2">
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="rounded-lg border border-surface-2 bg-surface-0 px-2 py-1.5 text-xs text-ink focus:border-accent focus:outline-none"
            >
              <option value="anthropic">Anthropic</option>
              <option value="openai">OpenAI</option>
              <option value="openrouter">OpenRouter</option>
            </select>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-…"
              className="flex-1 rounded-lg border border-surface-2 bg-surface-0 px-2.5 py-1.5 text-xs text-ink focus:border-accent focus:outline-none"
            />
            <button
              onClick={connectKey}
              className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent-hover"
            >
              Connect
            </button>
          </div>
          {keyStatus && <div className="mt-1.5 text-xs text-ink-dim">{keyStatus}</div>}
        </section>

        <section className="mt-5">
          <div className="text-xs font-medium text-ink-dim">Hands</div>
          <div className="mt-1.5 space-y-1">
            {(status?.hands ?? []).map((h) => (
              <div key={h.name} className="flex items-center justify-between text-xs">
                <span className="text-ink">{h.name}</span>
                <span className={h.healthy ? "text-emerald-400" : "text-ink-faint"}>
                  {h.configured ? (h.healthy ? "healthy" : "unhealthy") : "not configured"}
                </span>
              </div>
            ))}
            {!status && <div className="text-xs text-ink-faint">Connect to backend to view hands.</div>}
          </div>
        </section>

        <section className="mt-5">
          <div className="text-xs font-medium text-ink-dim">Model roles</div>
          <div className="mt-1.5 space-y-1">
            {Object.entries(status?.role_models ?? {}).map(([role, model]) => (
              <div key={role} className="flex items-center justify-between text-xs">
                <span className="text-ink-dim">{role}</span>
                <span className="text-ink">{model}</span>
              </div>
            ))}
            {(!status || Object.keys(status.role_models).length === 0) && (
              <div className="text-xs text-ink-faint">No role mapping reported.</div>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
