import type { StatusResponse, Thread, UtteranceResponse } from "./types";

const STORAGE_KEY = "centri.backendUrl";
const TOKEN_KEY = "centri.authToken";
const DEFAULT_BACKEND = "http://127.0.0.1:8760";

export function getBackendUrl(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) || DEFAULT_BACKEND;
  } catch {
    return DEFAULT_BACKEND;
  }
}

export function setBackendUrl(url: string): void {
  try {
    localStorage.setItem(STORAGE_KEY, url.replace(/\/$/, ""));
  } catch {
    /* ignore storage failures (private mode, etc.) */
  }
}

export function getAuthToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

export function setAuthToken(token: string): void {
  try {
    if (token) {
      localStorage.setItem(TOKEN_KEY, token);
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

  utterance: (text: string, threadId?: string | null) =>
    req<UtteranceResponse>("/utterance", {
      method: "POST",
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

  cancelTask: (taskId: string) =>
    req<Record<string, unknown>>(`/tasks/${taskId}/cancel`, { method: "POST" }),

  recentEvents: (limit = 50, threadId?: string | null) =>
    req<{ events: unknown[] }>(
      `/events?limit=${limit}${threadId ? `&thread_id=${encodeURIComponent(threadId)}` : ""}`,
    ),

  connectAccount: (provider: string, apiKey: string) =>
    req<Record<string, unknown>>(`/accounts/${provider}/connect`, {
      method: "POST",
      body: JSON.stringify({ api_key: apiKey }),
    }),
};
