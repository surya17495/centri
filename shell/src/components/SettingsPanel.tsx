import { useState, useEffect } from "react";
import {
  api,
  cleanAuthToken,
  getAuthToken,
  getBackendUrl,
  normalizeBackendUrl,
  setAuthToken,
  setBackendUrl,
} from "../api";
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
  const [backendStatus, setBackendStatus] = useState<string | null>(null);
  const [provider, setProvider] = useState("anthropic");
  const [apiKey, setApiKey] = useState("");
  const [keyStatus, setKeyStatus] = useState<string | null>(null);
  const [roleModels, setRoleModels] = useState<Record<string, string>>({});
  const [saveStatus, setSaveStatus] = useState<string | null>(null);
  const [importStatus, setImportStatus] = useState<string | null>(null);
  // Consolidation + embedding config
  const [consolidationModel, setConsolidationModel] = useState("");
  const [consolidationBatchSize, setConsolidationBatchSize] = useState("8");
  const [embeddingEnabled, setEmbeddingEnabled] = useState(false);
  const [embeddingLocalModel, setEmbeddingLocalModel] = useState("");
  const [memConfigStatus, setMemConfigStatus] = useState<string | null>(null);

  async function handleImport() {
    setImportStatus("Importing…");
    try {
      await api.bootstrap();
      setImportStatus("Import complete");
      setTimeout(() => setImportStatus(null), 4000);
      onSaved();
    } catch (e) {
      setImportStatus(e instanceof Error ? e.message : "Import failed");
    }
  }

  // Load current server-side overrides on open
  useEffect(() => {
    api.getSettingsOverrides().then(({ overrides }) => {
      if (overrides.consolidation_model) setConsolidationModel(String(overrides.consolidation_model));
      if (overrides.consolidation_batch_size) setConsolidationBatchSize(String(overrides.consolidation_batch_size));
      if (overrides.embedding_enabled !== undefined) setEmbeddingEnabled(String(overrides.embedding_enabled) === "true");
      if (overrides.embedding_local_model) setEmbeddingLocalModel(String(overrides.embedding_local_model));
    }).catch(() => {/* non-fatal */});
  }, []);

  useEffect(() => {
    if (status?.role_models) {
      const initial: Record<string, string> = {};
      Object.entries(status.role_models).forEach(([role, info]) => {
        initial[role] = info.model || "";
      });
      setRoleModels(initial);
    }
  }, [status]);

  async function saveModels() {
    setSaveStatus("Saving…");
    try {
      const payload: Record<string, string> = {};
      Object.entries(roleModels).forEach(([role, model]) => {
        payload[`model_${role}`] = model;
      });
      await api.updateSettingsOverrides(payload);
      setSaveStatus("Saved models");
      setTimeout(() => setSaveStatus(null), 3000);
      onSaved();
    } catch (e) {
      setSaveStatus(e instanceof Error ? e.message : "Failed to save models");
    }
  }

  function saveBackend() {
    const nextBackend = normalizeBackendUrl(backend);
    const nextToken = cleanAuthToken(token);
    setBackend(nextBackend || getBackendUrl());

    if (token.trim() && !nextToken) {
      setToken("");
      setAuthToken("");
      setBackendStatus("Token field contains a URL. Paste the auth token, not the backend URL.");
      return;
    }

    setBackendUrl(nextBackend);
    setAuthToken(nextToken);
    onSaved();
    // The WebSocket and any cached fetches still point at the old backend;
    // a reload re-initializes everything against the new URL + token.
    setBackendStatus("Saved — reconnecting…");
    setTimeout(() => window.location.reload(), 400);
  }

  async function saveMemoryConfig() {
    setMemConfigStatus("Saving…");
    try {
      await api.updateSettingsOverrides({
        consolidation_model: consolidationModel,
        consolidation_batch_size: consolidationBatchSize,
        embedding_enabled: embeddingEnabled ? "true" : "false",
        embedding_local_model: embeddingLocalModel,
        // Activate semantic scoring when embeddings are on
        curation_w_embedding_similarity: embeddingEnabled ? "0.35" : "0.0",
      });
      setMemConfigStatus("Saved");
      setTimeout(() => setMemConfigStatus(null), 3000);
      onSaved();
    } catch (e) {
      setMemConfigStatus(e instanceof Error ? e.message : "Failed to save");
    }
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
          <div className="flex items-center justify-between">
            <SectionTitle>Backend</SectionTitle>
            {backendStatus && (
              <span className="text-[10px] text-ink-dim animate-fade-in">{backendStatus}</span>
            )}
          </div>
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
          <div className="flex items-center justify-between">
            <SectionTitle>Memory import</SectionTitle>
            {importStatus && (
              <span className="text-[10px] text-ink-dim animate-fade-in">{importStatus}</span>
            )}
          </div>
          <div className="mt-2.5 space-y-2">
            <p className="text-xs leading-relaxed text-ink-dim">
              {discover?.bootstrapped
                ? "Histories already imported. Re-run to pick up any new sessions."
                : "Import your OpenCode, Claude Code, and Cursor histories into memory."}
            </p>
            <button
              onClick={onReimport ?? handleImport}
              disabled={importStatus === "Importing…"}
              className={`w-full ${PRIMARY_BTN} disabled:opacity-60 disabled:cursor-not-allowed`}
            >
              {importStatus === "Importing…"
                ? "Importing…"
                : discover?.bootstrapped
                ? "Re-run import"
                : "Import now"}
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
          <div className="flex items-center justify-between">
            <SectionTitle>Model roles</SectionTitle>
            {saveStatus && (
              <span className="text-[10px] text-ink-dim animate-fade-in">{saveStatus}</span>
            )}
          </div>
          <div className="mt-2.5 space-y-3 rounded-xl border border-white/[0.08] bg-white/[0.03] p-3">
            {Object.keys(status?.role_models ?? {}).map((role) => (
              <div key={role} className="flex flex-col gap-1.5">
                <label htmlFor={`model-input-${role}`} className="text-[10px] font-medium text-ink-dim uppercase tracking-wider">
                  {role.replace("_", " ")}
                </label>
                <input
                  id={`model-input-${role}`}
                  value={roleModels[role] ?? ""}
                  onChange={(e) =>
                    setRoleModels((prev) => ({ ...prev, [role]: e.target.value }))
                  }
                  placeholder="e.g. pioneer/claude-opus-4-8"
                  className={`w-full font-mono ${FIELD}`}
                />
              </div>
            ))}
            {status && Object.keys(status.role_models).length > 0 && (
              <button onClick={saveModels} className={`w-full mt-2 ${PRIMARY_BTN}`}>
                Save Model Roles
              </button>
            )}
            {(!status || Object.keys(status.role_models).length === 0) && (
              <div className="text-xs text-ink-faint text-center py-2">
                Connect to backend to view and edit model roles.
              </div>
            )}
          </div>
        </section>

        <section className="mt-7">
          <div className="flex items-center justify-between">
            <SectionTitle>Memory config</SectionTitle>
            {memConfigStatus && (
              <span className="text-[10px] text-ink-dim animate-fade-in">{memConfigStatus}</span>
            )}
          </div>
          <div className="mt-2.5 space-y-3 rounded-xl border border-white/[0.08] bg-white/[0.03] p-3">

            {/* Embeddings */}
            <div className="flex flex-col gap-1.5">
              <div className="flex items-center justify-between">
                <label className="text-[10px] font-medium uppercase tracking-wider text-ink-dim">
                  Embeddings
                </label>
                <button
                  role="switch"
                  aria-checked={embeddingEnabled}
                  onClick={() => setEmbeddingEnabled((v) => !v)}
                  className={`relative h-5 w-9 rounded-full transition-colors ${
                    embeddingEnabled ? "bg-accent" : "bg-white/10"
                  }`}
                >
                  <span
                    className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform ${
                      embeddingEnabled ? "translate-x-4" : "translate-x-0.5"
                    }`}
                  />
                </button>
              </div>
              <input
                value={embeddingLocalModel}
                onChange={(e) => setEmbeddingLocalModel(e.target.value)}
                placeholder="BAAI/bge-small-en-v1.5  (fastembed, local)"
                aria-label="Embedding local model"
                className={`w-full font-mono ${FIELD}`}
              />
              <p className="text-[10px] leading-relaxed text-ink-faint">
                Local model runs in-container via fastembed — no API cost. Leave blank to disable.
              </p>
            </div>

            {/* Consolidation */}
            <div className="flex flex-col gap-1.5 border-t border-white/[0.06] pt-3">
              <label className="text-[10px] font-medium uppercase tracking-wider text-ink-dim">
                Consolidation model
              </label>
              <input
                value={consolidationModel}
                onChange={(e) => setConsolidationModel(e.target.value)}
                placeholder="e.g. meta-llama/Llama-3.3-70B-Instruct"
                aria-label="Consolidation model"
                className={`w-full font-mono ${FIELD}`}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label className="text-[10px] font-medium uppercase tracking-wider text-ink-dim">
                Consolidation batch size
              </label>
              <input
                type="number"
                min={1}
                max={100}
                value={consolidationBatchSize}
                onChange={(e) => setConsolidationBatchSize(e.target.value)}
                aria-label="Consolidation batch size"
                className={`w-full font-mono ${FIELD}`}
              />
              <p className="text-[10px] leading-relaxed text-ink-faint">
                Number of unprocessed events that trigger a consolidation run.
              </p>
            </div>

            <button onClick={saveMemoryConfig} className={`w-full mt-1 ${PRIMARY_BTN}`}>
              Save memory config
            </button>
          </div>
        </section>
      </div>
    </div>
  );
}
