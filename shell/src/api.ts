import type { StatusResponse, UtteranceResponse } from "./types";

const STORAGE_KEY = "centri.backendUrl";
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

export function wsUrl(): string {
  const base = getBackendUrl();
  return base.replace(/^http/, "ws") + "/events/stream";
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(getBackendUrl() + path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export const api = {
  status: () => req<StatusResponse>("/status"),

  utterance: (text: string) =>
    req<UtteranceResponse>("/utterance", {
      method: "POST",
      body: JSON.stringify({ text, user_id: "local", source: "desktop_text" }),
    }),

  approve: (approvalId: string) =>
    req<Record<string, unknown>>(`/approvals/${approvalId}/approve`, { method: "POST" }),

  reject: (approvalId: string) =>
    req<Record<string, unknown>>(`/approvals/${approvalId}/reject`, { method: "POST" }),

  cancelTask: (taskId: string) =>
    req<Record<string, unknown>>(`/tasks/${taskId}/cancel`, { method: "POST" }),

  recentEvents: (limit = 50) =>
    req<{ events: unknown[] }>(`/events?limit=${limit}`),

  connectAccount: (provider: string, apiKey: string) =>
    req<Record<string, unknown>>(`/accounts/${provider}/connect`, {
      method: "POST",
      body: JSON.stringify({ api_key: apiKey }),
    }),
};
