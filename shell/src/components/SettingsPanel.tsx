import { useState } from "react";
import { api, getAuthToken, getBackendUrl, setAuthToken, setBackendUrl } from "../api";
import type { DiscoverResponse, StatusResponse } from "../types";

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[11px] font-semibold uppercase tracking-wider text-ink-faint">
      {children}
    </div>
  );
}

const FIELD =
  "rounded-lg border border-white/[0.09] bg-black/30 px-2.5 py-1.5 text-xs text-ink placeholder:text-ink-faint focus:border-accent focus:outline-none transition-colors";
const PRIMARY_BTN =
  "rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-hover";

export function SettingsPanel({
  status,
  discover,
  onReimport,
  onClose,
  onSaved,
}: {
  status: StatusResponse | null;
  discover?: DiscoverResponse | null;
  onReimport?: () => void;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [backend, setBackend] = useState(getBackendUrl());
  const [token, setToken] = useState(getAuthToken());
  const [provider, setProvider] = useState("anthropic");
  const [apiKey, setApiKey] = useState("");
  const [keyStatus, setKeyStatus] = useState<string | null>(null);

  function saveBackend() {
    setBackendUrl(backend);
    setAuthToken(token.trim());
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
      className="fixed inset-0 z-50 flex justify-end bg-black/55 backdrop-blur-[2px] animate-fade-in"
      onClick={onClose}
    >
      <div
        className="glass-deep scrollbar-thin h-full w-full max-w-sm overflow-y-auto !rounded-none border-y-0 border-r-0 p-6 animate-slide-in-right"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold tracking-tight text-ink">Settings</h2>
          <button
            onClick={onClose}
            aria-label="Close settings"
            className="rounded-lg p-1.5 text-ink-dim transition-colors hover:bg-white/[0.06] hover:text-ink"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M18 6L6 18M6 6l12 12" strokeLinecap="round" />
            </svg>
          </button>
        </div>

        <section className="mt-7">
          <SectionTitle>Backend</SectionTitle>
          <div className="mt-2.5 space-y-2">
            <input
              value={backend}
              onChange={(e) => setBackend(e.target.value)}
              aria-label="Backend URL"
              className={`w-full font-mono ${FIELD}`}
            />
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="Auth token (empty if core has none)"
              aria-label="Auth token"
              className={`w-full font-mono ${FIELD}`}
            />
            <button onClick={saveBackend} className={`w-full ${PRIMARY_BTN}`}>
              Save
            </button>
          </div>
        </section>

        <section className="mt-7">
          <SectionTitle>API key (BYOK)</SectionTitle>
          <div className="mt-2.5 space-y-2">
            <div className="flex gap-2">
              <select
                value={provider}
                onChange={(e) => setProvider(e.target.value)}
                className={`shrink-0 ${FIELD}`}
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
                className={`min-w-0 flex-1 font-mono ${FIELD}`}
              />
            </div>
            <button onClick={connectKey} className={`w-full ${PRIMARY_BTN}`}>
              Connect
            </button>
          </div>
          {keyStatus && (
            <div className="mt-2 text-xs text-ink-dim animate-fade-in">{keyStatus}</div>
          )}
        </section>

        {onReimport && (
          <section className="mt-7">
            <SectionTitle>Memory import</SectionTitle>
            <div className="mt-2.5 space-y-2">
              <p className="text-xs leading-relaxed text-ink-dim">
                {discover?.bootstrapped
                  ? "Coding-agent histories have been imported. Re-run to pick up anything new."
                  : "Import your OpenCode / Claude Code / Cursor history into memory."}
              </p>
              <button onClick={onReimport} className={`w-full ${PRIMARY_BTN}`}>
                {discover?.bootstrapped ? "Re-run import" : "Import now"}
              </button>
            </div>
          </section>
        )}

        <section className="mt-7">
          <SectionTitle>Hands</SectionTitle>
          <div className="mt-2.5 divide-y divide-line rounded-xl border border-white/[0.08] bg-white/[0.03]">
            {(status?.hands ?? []).map((h) => (
              <div
                key={`${h.name}::${h.detail}`}
                title={h.detail}
                className="flex items-center justify-between px-3 py-2 text-xs"
              >
                <span className="font-medium text-ink">{h.name}</span>
                <span
                  className={`inline-flex items-center gap-1.5 ${
                    h.healthy ? "text-emerald-400" : "text-ink-faint"
                  }`}
                >
                  <span
                    className={`h-1.5 w-1.5 rounded-full ${
                      h.healthy ? "bg-emerald-400" : "bg-ink-faint"
                    }`}
                    aria-hidden
                  />
                  {h.configured ? (h.healthy ? "healthy" : "unhealthy") : "not configured"}
                </span>
              </div>
            ))}
            {!status && (
              <div className="px-3 py-2 text-xs text-ink-faint">
                Connect to backend to view hands.
              </div>
            )}
          </div>
        </section>

        <section className="mt-7">
          <SectionTitle>Model roles</SectionTitle>
          <div className="mt-2.5 divide-y divide-line rounded-xl border border-white/[0.08] bg-white/[0.03]">
            {Object.entries(status?.role_models ?? {}).map(([role, info]) => (
              <div key={role} className="flex items-center justify-between gap-3 px-3 py-2 text-xs">
                <span className="shrink-0 text-ink-dim">{role}</span>
                <span
                  className={`truncate text-right font-mono text-[11px] ${
                    info.configured && info.model ? "text-ink" : "text-ink-faint"
                  }`}
                  title={info.via_proxy ? "via proxy" : undefined}
                >
                  {info.configured && info.model ? info.model : "not configured"}
                </span>
              </div>
            ))}
            {(!status || Object.keys(status.role_models).length === 0) && (
              <div className="px-3 py-2 text-xs text-ink-faint">No role mapping reported.</div>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
