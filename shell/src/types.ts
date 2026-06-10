// Shapes mirrored from the CENTRI event contract (docs/event-contract.md) and the
// FastAPI /status + /utterance responses. Kept intentionally loose — the event
// envelope carries arbitrary payloads, so unknown fields are tolerated.

export interface CentriEvent {
  type: string;
  ts?: string;
  source?: string;
  task_id?: string | null;
  thread_id?: string | null;
  repo_id?: string | null;
  payload?: Record<string, unknown>;
  // Convenience top-level fields some publishers mirror from payload.
  summary?: string;
  text?: string;
  status?: string;
  percent?: number;
  approval_id?: string;
  label?: string;
  risk?: string;
  action?: string;
  message?: string;
  title?: string;
  [key: string]: unknown;
}

export interface HandCapability {
  name: string;
  risk: string;
  configured: boolean;
  healthy: boolean;
  detail: string;
}

export interface StatusResponse {
  status: string;
  version: string;
  pending_approvals: number;
  running_tasks: number;
  active_threads: number;
  hands: HandCapability[];
  role_models: Record<string, string>;
}

export interface UtteranceResponse {
  response_type: string;
  message: string;
  data: Record<string, unknown>;
}

export type ConnectionState = "connecting" | "online" | "offline";

// A task card aggregated from the event stream.
export interface TaskCard {
  taskId: string;
  description: string;
  status: string;
  progress: ProgressLine[];
  artifacts: ArtifactLine[];
  updatedAt: string;
}

export interface ProgressLine {
  ts: string;
  summary: string;
  percent?: number;
}

export interface ArtifactLine {
  title: string;
  type: string;
  summary?: string;
}

export interface ApprovalCard {
  approvalId: string;
  taskId?: string;
  label: string;
  detail?: string;
  risk: string;
  resolved?: "approved" | "rejected";
}

// A flat, render-ready timeline item.
export type TimelineItem =
  | { kind: "narration"; id: string; ts: string; text: string }
  | { kind: "task"; id: string; ts: string; card: TaskCard }
  | { kind: "approval"; id: string; ts: string; card: ApprovalCard }
  | { kind: "event"; id: string; ts: string; event: CentriEvent };
