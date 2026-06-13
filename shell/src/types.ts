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

export interface RoleModelInfo {
  configured: boolean;
  model?: string;
  via_proxy?: boolean;
  api_base?: string | null;
}

export interface StatusResponse {
  status: string;
  version: string;
  pending_approvals: number;
  running_tasks: number;
  active_threads: number;
  hands: HandCapability[];
  role_models: Record<string, RoleModelInfo>;
}

export interface Thread {
  id: string;
  title: string;
  goal?: string;
  status?: string;
  updated_at?: string;
}

export interface UtteranceResponse {
  response_type: string;
  message: string;
  data: Record<string, unknown>;
}

export type ConnectionState = "connecting" | "online" | "offline";

// GET /ingest/discover — what coding-agent histories CENTRI can import.
export interface DiscoveredSource {
  agent: string;
  path: string;
  available: boolean;
  source?: string;
  count?: number;
  reason?: string;
}

export interface DiscoverResponse {
  sources: DiscoveredSource[];
  available_count: number;
  total_messages: number;
  agents: string[];
  // First-run flag derived from the backend (has any source been ingested?).
  bootstrapped?: boolean;
  opencode_providers?: { provider: string; has_key: boolean }[];
}

export interface BootstrapResult {
  imported: number;
  source_count: number;
}

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

export interface PendingApproval {
  id: string;
  task_id?: string | null;
  thread_id?: string | null;
  label: string;
  detail?: string;
  risk: string;
  requested_action?: string;
  requested_at?: string;
  status?: string;
}

// A flat, render-ready timeline item.
export type TimelineItem =
  | { kind: "narration"; id: string; ts: string; text: string; role?: "user" | "assistant" }
  | { kind: "task"; id: string; ts: string; card: TaskCard }
  | { kind: "approval"; id: string; ts: string; card: ApprovalCard }
  | { kind: "event"; id: string; ts: string; event: CentriEvent };
