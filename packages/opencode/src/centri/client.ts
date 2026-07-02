// CENTRI: the ONLY module that speaks HTTP to the Centri core (memory plane).
// Everything here FAILS OPEN — a dead, slow, or misconfigured backend must
// never block or crash the agent loop. No throws escape; recall returns
// undefined, importEvents is fire-and-forget.
//
// Config is via env vars (see contracts/bridge-api.md):
//   CENTRI_URL    base URL of the core, e.g. http://127.0.0.1:8000
//   CENTRI_TOKEN  bearer token (maps to core's CENTRI_AUTH_TOKEN)

const RECALL_TIMEOUT_MS = Number(process.env["CENTRI_RECALL_TIMEOUT_MS"] ?? 3000)
const SEARCH_TIMEOUT_MS = Number(process.env["CENTRI_SEARCH_TIMEOUT_MS"] ?? 5000)
const IMPORT_TIMEOUT_MS = 5000
const FLUSH_INTERVAL_MS = 2000
const FLUSH_MAX_EVENTS = 50

export type RecallItem = {
  text: string
  score?: number
  score_breakdown?: Record<string, unknown>
  source_event_id?: string
  kind?: "decision" | "fact" | "open_loop" | "convention" | string
}

export type RecallResult = {
  markdown: string
  items: RecallItem[]
  ambient_items?: RecallItem[]
  policy_version?: string
  graph_hwm?: string
  elapsed_ms?: number
}

export type RecallOptions = {
  threadID?: string
  repoID?: string
  budgetTokens?: number
  signal?: AbortSignal
}

export type Envelope = {
  type: string
  ts: string
  source: string
  thread_id?: string
  repo_id?: string
  payload: Record<string, unknown> & { event_uid: string }
}

function baseUrl() {
  const raw = process.env["CENTRI_URL"]?.trim()
  if (!raw) return undefined
  return raw.endsWith("/") ? raw.slice(0, -1) : raw
}

function token() {
  return process.env["CENTRI_TOKEN"]?.trim() || undefined
}

export function enabled() {
  return baseUrl() !== undefined
}

function headers() {
  const result: Record<string, string> = { "content-type": "application/json" }
  const t = token()
  if (t) result["authorization"] = `Bearer ${t}`
  return result
}

// Ambient standing-context layer, fetched by OpenCode's native instruction URL
// machinery at session start. The instruction fetcher can't set headers, so the
// token rides as a query param (contract §3).
export function ambientUrl() {
  const base = baseUrl()
  if (!base) return undefined
  const t = token()
  const url = `${base}/memory/ambient.md`
  return t ? `${url}?token=${encodeURIComponent(t)}` : url
}

// Read path. Per-turn cued brief. Returns undefined on any failure, disabled
// backend, timeout, or non-2xx — the turn proceeds with no brief.
export async function recall(cue: string, opts?: RecallOptions): Promise<RecallResult | undefined> {
  const base = baseUrl()
  if (!base || !cue.trim()) return undefined

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), RECALL_TIMEOUT_MS)
  if (opts?.signal) opts.signal.addEventListener("abort", () => controller.abort(), { once: true })

  try {
    const res = await fetch(`${base}/memory/recall`, {
      method: "POST",
      headers: headers(),
      signal: controller.signal,
      body: JSON.stringify({
        cue,
        thread_id: opts?.threadID,
        repo_id: opts?.repoID,
        budget_tokens: opts?.budgetTokens ?? 5000,
        format: "markdown+items",
      }),
    })
    if (!res.ok) return undefined
    const data = (await res.json()) as RecallResult
    if (!data || typeof data.markdown !== "string") return undefined
    return data
  } catch {
    return undefined
  } finally {
    clearTimeout(timer)
  }
}

// Read path. Full-text search over the event spine (FTS5). Returns undefined
// on any failure — fail-open, never blocks the agent.
export type SearchResult = {
  event_id: string
  type: string
  ts: string
  payload: Record<string, unknown>
  snippet?: string
}

export async function search(query: string, opts?: { limit?: number }): Promise<SearchResult[] | undefined> {
  const base = baseUrl()
  if (!base || !query.trim()) return undefined

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), SEARCH_TIMEOUT_MS)

  try {
    const res = await fetch(`${base}/memory/search`, {
      method: "POST",
      headers: headers(),
      signal: controller.signal,
      body: JSON.stringify({ query, limit: opts?.limit ?? 20 }),
    })
    if (!res.ok) return undefined
    return (await res.json()) as SearchResult[]
  } catch {
    return undefined
  } finally {
    clearTimeout(timer)
  }
}

// Read path. Temporal "what changed since X" narrative. Returns undefined on
// failure — fail-open.
export type SinceResult = {
  available: boolean
  reason?: string
  narrative?: string
  events?: Record<string, unknown>[]
}

export async function since(when: string, opts?: { repoID?: string }): Promise<SinceResult | undefined> {
  const base = baseUrl()
  if (!base) return undefined

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), SEARCH_TIMEOUT_MS)
  const params = new URLSearchParams({ since: when })
  if (opts?.repoID) params.set("repo_id", opts.repoID)

  try {
    const res = await fetch(`${base}/memory/since?${params}`, {
      method: "GET",
      headers: headers(),
      signal: controller.signal,
    })
    if (!res.ok) return undefined
    return (await res.json()) as SinceResult
  } catch {
    return undefined
  } finally {
    clearTimeout(timer)
  }
}

// Read path. "Where did we leave off" resume view. Returns undefined on failure.
export type WhereLeftOffResult = {
  available: boolean
  reason?: string
  narrative?: string
  [key: string]: unknown
}

export async function whereLeftOff(opts?: { repoID?: string }): Promise<WhereLeftOffResult | undefined> {
  const base = baseUrl()
  if (!base) return undefined

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), SEARCH_TIMEOUT_MS)
  const params = new URLSearchParams()
  if (opts?.repoID) params.set("repo_id", opts.repoID)

  try {
    const res = await fetch(`${base}/memory/where-left-off${params.size ? `?${params}` : ""}`, {
      method: "GET",
      headers: headers(),
      signal: controller.signal,
    })
    if (!res.ok) return undefined
    return (await res.json()) as WhereLeftOffResult
  } catch {
    return undefined
  } finally {
    clearTimeout(timer)
  }
}

// Write path. Emits an event with explicit synthesis hints so the deterministic
// consolidation tier picks it up immediately on the next 30s tick — no LLM call
// needed. The hint structure matches what Consolidator.consume_events expects.
export type MemoryWrite =
  | { kind: "decision"; topic: string; statement: string; stance?: "adopted" | "rejected"; rationale?: string; tags?: string[] }
  | { kind: "fact"; topic: string; statement: string; tags?: string[] }
  | { kind: "open_loop"; intent: string; cue?: string; tags?: string[] }
  | { kind: "loop_resolution"; intent: string; resolution?: "done" | "parked" }

export function writeMemory(
  write: MemoryWrite,
  opts?: { threadID?: string; repoID?: string },
): void {
  if (!enabled()) return
  const eventUID = `mem-write-${Date.now()}-${Math.random().toString(36).slice(2)}`
  const ts = new Date().toISOString()
  const payload: Record<string, unknown> & { event_uid: string } = { event_uid: eventUID }
  payload[write.kind] = write
  const envelope: Envelope = {
    type: `centri_app.memory_write`,
    ts,
    source: "centri-app",
    thread_id: opts?.threadID,
    repo_id: opts?.repoID,
    payload: payload as Record<string, unknown> & { event_uid: string },
  }
  importEvents(envelope)
}

// Write path. Batched, fire-and-forget. Events accumulate and flush every 2s or
// when 50 are queued, whichever comes first. A failed flush drops the batch
// rather than retrying forever (events are the source of truth on the core's
// side, and dropping a memory event must never wedge the agent).
let queue: Envelope[] = []
let flushTimer: ReturnType<typeof setTimeout> | undefined

export function importEvents(batch: Envelope | Envelope[]) {
  if (!enabled()) return
  const items = Array.isArray(batch) ? batch : [batch]
  if (items.length === 0) return
  queue.push(...items)
  if (queue.length >= FLUSH_MAX_EVENTS) {
    void flush()
    return
  }
  if (!flushTimer) {
    flushTimer = setTimeout(() => void flush(), FLUSH_INTERVAL_MS)
    // Don't keep the process alive solely for a pending flush.
    if (typeof flushTimer === "object" && "unref" in flushTimer) flushTimer.unref()
  }
}

export async function flush() {
  if (flushTimer) {
    clearTimeout(flushTimer)
    flushTimer = undefined
  }
  const base = baseUrl()
  if (!base || queue.length === 0) {
    queue = []
    return
  }
  const events = queue
  queue = []

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), IMPORT_TIMEOUT_MS)
  try {
    await fetch(`${base}/events/import`, {
      method: "POST",
      headers: headers(),
      signal: controller.signal,
      body: JSON.stringify({ events }),
    })
  } catch {
    // Drop the batch. Fail-open: never retry-loop, never throw.
  } finally {
    clearTimeout(timer)
  }
}

export * as Centri from "./client"
