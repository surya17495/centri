// CENTRI: write-path tap. Subscribes once to the process-global GlobalBus and
// maps OpenCode runtime events into Centri envelopes (centri_app.* family),
// then hands them to the batching client. Pure listener: it never throws into
// the bus and never blocks the agent loop (importEvents is fire-and-forget).
//
// Wired at server boot via install(). Idempotent — safe to call repeatedly.

import { GlobalBus, type GlobalEvent } from "@/bus/global"
import { Centri, type Envelope } from "./client"

let installed = false

export function install() {
  if (installed) return
  if (!Centri.enabled()) return
  installed = true
  GlobalBus.on("event", handle)
}

function handle(event: GlobalEvent) {
  const envelope = toEnvelope(event)
  if (envelope) Centri.importEvents(envelope)
}

// Maps a GlobalBus event to a centri_app.* envelope, or undefined to skip.
// GlobalBus payloads look like { id, type, properties } (see event-v2-bridge.ts).
function toEnvelope(event: GlobalEvent): Envelope | undefined {
  const payload = event.payload
  if (!payload || typeof payload !== "object") return undefined
  const type = payload.type as string | undefined
  if (!type) return undefined
  const props = (payload.properties ?? {}) as Record<string, unknown>
  const eventUID = (payload.id as string | undefined) ?? `evt_${Date.now()}_${Math.random().toString(36).slice(2)}`
  const ts = new Date().toISOString()

  const mapped = mapType(type, props)
  if (!mapped) return undefined

  return {
    type: `centri_app.${mapped.type}`,
    ts,
    source: "centri-app",
    thread_id: mapped.threadID,
    repo_id: mapped.repoID,
    payload: { ...mapped.payload, event_uid: eventUID },
  }
}

// Translate an OpenCode event type + properties into the contract's family
// (session.*, message.updated, tool.execute, permission.*). Returns undefined
// for events we don't ingest.
function mapType(
  type: string,
  props: Record<string, unknown>,
): { type: string; threadID?: string; repoID?: string; payload: Record<string, unknown> } | undefined {
  const sessionID = (props.sessionID ?? props.session_id) as string | undefined
  const info = (props.info ?? {}) as Record<string, unknown>
  // Session directory is the project root — pass it through as repo_id so the
  // core can resolve a project and scope the event.
  const directory = (info.directory ?? info.dir ?? info.path) as string | undefined

  switch (type) {
    case "session.created":
      return { type: "session.created", threadID: sessionID, repoID: directory, payload: { info: props.info } }
    case "session.updated":
      return { type: "session.updated", threadID: sessionID, repoID: directory, payload: { info: props.info } }
    case "session.deleted":
      return { type: "session.idle", threadID: sessionID, payload: { reason: "deleted" } }
    case "session.idle":
      return { type: "session.idle", threadID: sessionID, payload: {} }

    case "message.updated":
      return { type: "message.updated", threadID: sessionID, payload: messageText(props.info) }

    // Tool execute before/after collapses onto the tool part lifecycle: a tool
    // part transitions running -> completed/error within message.part.updated.
    case "message.part.updated": {
      const part = props.part as Record<string, unknown> | undefined
      if (!part) return undefined
      if (part.type === "text") {
        return {
          type: "message.updated",
          threadID: sessionID,
          payload: { text: part.text, part_id: part.id, message_id: part.messageID },
        }
      }
      if (part.type === "tool") return toolPart(sessionID, part)
      return undefined
    }

    case "permission.asked":
      return { type: "permission.asked", threadID: sessionID, payload: { ...props } }
    case "permission.replied":
      return { type: "permission.replied", threadID: sessionID, payload: { ...props } }

    default:
      return undefined
  }
}

function messageText(info: unknown): Record<string, unknown> {
  if (!info || typeof info !== "object") return { info }
  const message = info as Record<string, unknown>
  return { role: message.role, message_id: message.id, info }
}

function toolPart(sessionID: string | undefined, part: Record<string, unknown>) {
  const state = (part.state ?? {}) as Record<string, unknown>
  const status = state.status as string | undefined
  // Only emit on terminal transitions to collapse before/after into one
  // envelope; "running" updates stream too frequently to be useful memory.
  if (status !== "completed" && status !== "error") return undefined
  return {
    type: "tool.execute",
    threadID: sessionID,
    payload: {
      tool: part.tool,
      call_id: part.callID,
      status,
      input: state.input,
      output: state.output ?? state.error,
      title: state.title,
      message_id: part.messageID,
    },
  }
}

export * as CentriTap from "./tap"
