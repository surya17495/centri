import type {
  BootstrapResult,
  DiscoverResponse,
  StatusResponse,
  PendingApproval,
  Thread,
  UtteranceResponse,
} from "./types";

const STORAGE_KEY = "centri.backendUrl";
const TOKEN_KEY = "centri.authToken";
const CORE_PORT = "8760";
const SHELL_PORT = "8761";

// When the shell is served over http(s) (e.g. the Docker web deployment on
// :8761), the core is almost always the same host on :8760 — so default to
// that instead of hardcoding 127.0.0.1, which points at the *viewer's*
// machine, not the server. Tauri (tauri:// origin) falls back to localhost.
function defaultBackend(): string {
  try {
    const { protocol, hostname } = window.location;
    if (protocol === "http:" || protocol === "https:") {
      return `http://${hostname}:${CORE_PORT}`;
    }
  } catch {
    /* non-browser environment (tests) */
  }
  return "http://127.0.0.1:8760";
}

export function normalizeBackendUrl(raw: string): string {
  const trimmed = raw.trim().replace(/\/+$/, "");
  if (!trimmed) return "";

  const candidate = /^https?:\/\//i.test(trimmed) ? trimmed : `http://${trimmed}`;
  try {
    const parsed = new URL(candidate);
    if (parsed.port === SHELL_PORT) {
      parsed.port = CORE_PORT;
    }
    parsed.pathname = "";
    parsed.search = "";
    parsed.hash = "";
    return parsed.toString().replace(/\/+$/, "");
  } catch {
    return trimmed;
  }
}

export function cleanAuthToken(raw: string): string {
  const token = raw.trim();
  // A common settings mistake is pasting the backend URL into the token field;
  // never persist that as a bearer token or WS query token.
  return /^https?:\/\//i.test(token) ? "" : token;
}

export function getBackendUrl(): string {
  try {
    return normalizeBackendUrl(localStorage.getItem(STORAGE_KEY) || defaultBackend());
  } catch {
    return defaultBackend();
  }
}

export function setBackendUrl(url: string): void {
  try {
    const cleaned = normalizeBackendUrl(url);
    if (cleaned) {
      localStorage.setItem(STORAGE_KEY, cleaned);
    } else {
      localStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    /* ignore storage failures (private mode, etc.) */
  }
}

export function getAuthToken(): string {
  try {
    return cleanAuthToken(localStorage.getItem(TOKEN_KEY) || "");
  } catch {
    return "";
  }
}

export function setAuthToken(token: string): void {
  try {
    const cleaned = cleanAuthToken(token);
    if (cleaned) {
      localStorage.setItem(TOKEN_KEY, cleaned);
    } else {
      localStorage.removeItem(TOKEN_KEY);
    }
  } catch {
    /* ignore storage failures (private mode, etc.) */
  }
}

export function wsUrl(): string {
  const base = getBackendUrl().replace(/^http/, "ws") + "/events/stream";
  const token = getAuthToken();
  // Browsers cannot attach headers to WebSocket handshakes; the core accepts
  // the bearer token as a query parameter instead.
  return token ? `${base}?token=${encodeURIComponent(token)}` : base;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getAuthToken();
  const res = await fetch(getBackendUrl() + path, {
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    ...init,
  });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export const api = {
  status: () => req<StatusResponse>("/status"),

  utterance: (text: string, threadId?: string | null, signal?: AbortSignal) =>
    req<UtteranceResponse>("/utterance", {
      method: "POST",
      signal,
      body: JSON.stringify({
        text,
        user_id: "local",
        source: "desktop_text",
        ...(threadId ? { thread_id: threadId } : {}),
      }),
    }),

  threads: () => req<{ threads: Thread[] }>("/threads"),

  createThread: (title?: string) =>
    req<{ thread: Thread }>("/threads", {
      method: "POST",
      body: JSON.stringify({ title: title ?? "New chat" }),
    }),

  approve: (approvalId: string) =>
    req<Record<string, unknown>>(`/approvals/${approvalId}/approve`, { method: "POST" }),

  reject: (approvalId: string) =>
    req<Record<string, unknown>>(`/approvals/${approvalId}/reject`, { method: "POST" }),

  approvals: () => req<{ approvals: PendingApproval[] }>("/approvals"),

  cancelTask: (taskId: string) =>
    req<Record<string, unknown>>(`/tasks/${taskId}/cancel`, { method: "POST" }),

  recentEvents: (limit = 50, threadId?: string | null) =>
    req<{ events: unknown[] }>(
      `/events?limit=${limit}${threadId ? `&thread_id=${encodeURIComponent(threadId)}` : ""}`,
    ),

  discover: () => req<DiscoverResponse>("/ingest/discover"),

  bootstrap: () =>
    req<BootstrapResult>("/ingest/bootstrap", {
      method: "POST",
      body: JSON.stringify({}),
    }),

  connectAccount: (provider: string, apiKey: string) =>
    req<Record<string, unknown>>(`/accounts/${provider}/connect`, {
      method: "POST",
      body: JSON.stringify({ api_key: apiKey }),
    }),

  getSettingsOverrides: () => req<{ overrides: Record<string, string> }>("/settings/overrides"),

  updateSettingsOverrides: (settings: Record<string, string>) =>
    req<{ status: string; overrides: Record<string, string> }>("/settings/overrides", {
      method: "POST",
      body: JSON.stringify({ settings }),
    }),
};
