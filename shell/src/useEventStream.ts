import { useCallback, useEffect, useRef, useState } from "react";
import { api, wsUrl } from "./api";
import type {
  ApprovalCard,
  CentriEvent,
  ConnectionState,
  ProgressLine,
  StatusResponse,
  TaskCard,
  TimelineItem,
} from "./types";

const MAX_BACKOFF_MS = 15_000;
const BASE_BACKOFF_MS = 500;

// Reducer-ish aggregation of the raw event stream into render-ready timeline
// items. Tasks and approvals fold into cards keyed by id; narration and
// unrecognized events fall through as standalone items so nothing is silently
// dropped.
interface AggregateState {
  order: string[];
  items: Map<string, TimelineItem>;
  tasks: Map<string, TaskCard>;
  approvals: Map<string, ApprovalCard>;
}

function emptyState(): AggregateState {
  return { order: [], items: new Map(), tasks: new Map(), approvals: new Map() };
}

function tsOf(ev: CentriEvent): string {
  return ev.ts || new Date().toISOString();
}

function eventId(ev: CentriEvent, index: number): string {
  return (ev.id as string) || `${ev.type}:${ev.ts ?? index}:${index}`;
}

function upsert(state: AggregateState, key: string, item: TimelineItem): void {
  if (!state.items.has(key)) {
    state.order.push(key);
  }
  state.items.set(key, item);
}

function applyEvent(state: AggregateState, ev: CentriEvent, index: number): AggregateState {
  const ts = tsOf(ev);
  const type = ev.type || "";

  // Narration / coordinator responses become plain timeline text.
  if (type === "narrate" || type === "coordinator.response" || type === "user.utterance") {
    const text =
      ev.text ?? ev.summary ?? ev.message ?? (ev.payload?.text as string) ?? "";
    if (text) {
      const key = eventId(ev, index);
      upsert(state, key, {
        kind: "narration",
        id: key,
        ts,
        text,
        role: type === "user.utterance" ? "user" : "assistant",
      });
    }
    return state;
  }

  // Task lifecycle + progress fold into a single card per task_id.
  if (type.startsWith("task.") || type.startsWith("hand.")) {
    const taskId = (ev.task_id as string) || (ev.payload?.task_id as string) || "";
    if (taskId) {
      const existing = state.tasks.get(taskId);
      const card: TaskCard = existing
        ? { ...existing, progress: [...existing.progress], artifacts: [...existing.artifacts] }
        : {
            taskId,
            description:
              (ev.payload?.description as string) ?? ev.summary ?? ev.title ?? "Task",
            status: "running",
            progress: [],
            artifacts: [],
            updatedAt: ts,
          };

      card.updatedAt = ts;

      if (type === "task.started") {
        card.status = "running";
        card.description =
          (ev.payload?.description as string) ?? ev.summary ?? card.description;
      } else if (type === "task.completed") {
        card.status = "completed";
      } else if (type === "task.failed") {
        card.status = "failed";
      } else if (type === "task.cancelled") {
        card.status = "cancelled";
      }

      const summary =
        ev.summary ?? ev.message ?? (ev.payload?.summary as string) ?? "";
      if (summary && (type.endsWith(".progress") || type.endsWith(".updated") || type.startsWith("hand."))) {
        const line: ProgressLine = {
          ts,
          summary,
          percent: typeof ev.percent === "number" ? ev.percent : (ev.payload?.percent as number),
        };
        card.progress.push(line);
      }

      state.tasks.set(taskId, card);
      upsert(state, `task:${taskId}`, { kind: "task", id: `task:${taskId}`, ts, card });
    }
    return state;
  }

  // Artifacts attach to their task card if present, else stand alone.
  if (type === "artifact.created") {
    const taskId = (ev.task_id as string) || "";
    const title = ev.title ?? (ev.payload?.title as string) ?? "Artifact";
    const artType = (ev.payload?.type as string) ?? "file";
    const summary = ev.summary ?? (ev.payload?.summary as string);
    const existing = taskId ? state.tasks.get(taskId) : undefined;
    if (existing) {
      const card: TaskCard = {
        ...existing,
        artifacts: [...existing.artifacts, { title, type: artType, summary }],
        updatedAt: ts,
      };
      state.tasks.set(taskId, card);
      upsert(state, `task:${taskId}`, { kind: "task", id: `task:${taskId}`, ts, card });
    } else {
      const key = eventId(ev, index);
      upsert(state, key, { kind: "event", id: key, ts, event: ev });
    }
    return state;
  }

  // Approvals fold into a card keyed by approval_id; resolution updates it.
  if (type === "approval.requested" || type === "approval.resolved") {
    const approvalId =
      (ev.approval_id as string) || (ev.payload?.approval_id as string) || "";
    if (approvalId) {
      const existing = state.approvals.get(approvalId);
      const card: ApprovalCard = existing
        ? { ...existing }
        : {
            approvalId,
            taskId: (ev.task_id as string) || undefined,
            label: ev.label ?? (ev.payload?.label as string) ?? ev.action ?? "Approval required",
            detail: ev.summary ?? (ev.payload?.detail as string),
            risk: ev.risk ?? (ev.payload?.risk as string) ?? "medium",
          };
      if (type === "approval.resolved") {
        const decision =
          (ev.payload?.decision as string) ?? (ev.status as string) ?? (ev.action as string) ?? "";
        card.resolved = decision === "approved" || decision === "allow" ? "approved" : "rejected";
      }
      state.approvals.set(approvalId, card);
      upsert(state, `approval:${approvalId}`, {
        kind: "approval",
        id: `approval:${approvalId}`,
        ts,
        card,
      });
    }
    return state;
  }

  // Everything else: keep it visible as a raw event row.
  const key = eventId(ev, index);
  upsert(state, key, { kind: "event", id: key, ts, event: ev });
  return state;
}

export interface EventStream {
  connection: ConnectionState;
  timeline: TimelineItem[];
  status: StatusResponse | null;
  refreshStatus: () => void;
  resolveApproval: (approvalId: string, decision: "approve" | "reject") => Promise<void>;
}

export function useEventStream(): EventStream {
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [version, setVersion] = useState(0);
  const [status, setStatus] = useState<StatusResponse | null>(null);

  const stateRef = useRef<AggregateState>(emptyState());
  const counterRef = useRef(0);
  const wsRef = useRef<WebSocket | null>(null);
  const backoffRef = useRef(BASE_BACKOFF_MS);
  const retryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closedRef = useRef(false);

  const bump = useCallback(() => setVersion((v) => v + 1), []);

  const ingest = useCallback(
    (ev: CentriEvent) => {
      applyEvent(stateRef.current, ev, counterRef.current++);
      bump();
    },
    [bump],
  );

  const refreshStatus = useCallback(() => {
    api
      .status()
      .then(setStatus)
      .catch(() => {
        /* surfaced via connection dot; status simply stays stale */
      });
  }, []);

  const connect = useCallback(() => {
    if (closedRef.current) return;
    setConnection("connecting");

    // Neutralize any previous socket before opening a new one. Without this,
    // a reconnect racing a still-open socket (e.g. StrictMode remount) leaves
    // two live connections feeding the same timeline — every event twice.
    const prev = wsRef.current;
    if (prev) {
      prev.onopen = null;
      prev.onmessage = null;
      prev.onerror = null;
      prev.onclose = null;
      try {
        prev.close();
      } catch {
        /* already closed */
      }
      wsRef.current = null;
    }

    let ws: WebSocket;
    try {
      ws = new WebSocket(wsUrl());
    } catch {
      scheduleReconnect();
      return;
    }
    wsRef.current = ws;

    ws.onopen = () => {
      backoffRef.current = BASE_BACKOFF_MS;
      setConnection("online");
      refreshStatus();
      // Hydrate recent history so a fresh connection isn't blank.
      api
        .recentEvents(100)
        .then((res) => {
          // /events returns newest-first; replay oldest-first so the
          // timeline reads chronologically.
          for (const raw of [...res.events].reverse()) {
            applyEvent(stateRef.current, raw as CentriEvent, counterRef.current++);
          }
          bump();
        })
        .catch(() => {
          /* history is best-effort */
        });
    };

    ws.onmessage = (msg) => {
      try {
        ingest(JSON.parse(msg.data as string) as CentriEvent);
      } catch {
        /* ignore malformed frames */
      }
    };

    ws.onerror = () => {
      ws.close();
    };

    ws.onclose = () => {
      wsRef.current = null;
      if (closedRef.current) return;
      setConnection("offline");
      scheduleReconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ingest, refreshStatus, bump]);

  const scheduleReconnect = useCallback(() => {
    if (closedRef.current) return;
    if (retryTimer.current) clearTimeout(retryTimer.current);
    const delay = backoffRef.current;
    backoffRef.current = Math.min(backoffRef.current * 2, MAX_BACKOFF_MS);
    retryTimer.current = setTimeout(connect, delay);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connect]);

  useEffect(() => {
    closedRef.current = false;
    connect();
    return () => {
      closedRef.current = true;
      if (retryTimer.current) clearTimeout(retryTimer.current);
      wsRef.current?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const resolveApproval = useCallback(
    async (approvalId: string, decision: "approve" | "reject") => {
      if (decision === "approve") await api.approve(approvalId);
      else await api.reject(approvalId);
      // Optimistically reflect resolution; the resolved event will confirm.
      const card = stateRef.current.approvals.get(approvalId);
      if (card) {
        const updated: ApprovalCard = {
          ...card,
          resolved: decision === "approve" ? "approved" : "rejected",
        };
        stateRef.current.approvals.set(approvalId, updated);
        stateRef.current.items.set(`approval:${approvalId}`, {
          kind: "approval",
          id: `approval:${approvalId}`,
          ts: new Date().toISOString(),
          card: updated,
        });
        bump();
      }
    },
    [bump],
  );

  // Rebuild the ordered list whenever the version bumps.
  const state = stateRef.current;
  void version;
  const timeline = state.order
    .map((k) => state.items.get(k))
    .filter((x): x is TimelineItem => Boolean(x));

  return { connection, timeline, status, refreshStatus, resolveApproval };
}
