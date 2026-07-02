import { Effect, Schema } from "effect"
import * as Tool from "./tool"
import { Centri } from "@/centri/client"
import DESCRIPTION from "./memory.txt"

export const Parameters = Schema.Struct({
  action: Schema.Literals([
    "search", "recall", "where-left-off", "since",
    "write_decision", "write_fact", "write_open_loop", "close_loop",
  ]).annotate({
    description: "What to do. Read: search, recall, where-left-off, since. Write: write_decision, write_fact, write_open_loop, close_loop.",
  }),
  query: Schema.optional(Schema.String).annotate({
    description: "Search query or recall cue. Required for 'search' and 'recall' actions.",
  }),
  when: Schema.optional(Schema.String).annotate({
    description: 'For "since" action: ISO date ("2026-06-10"), "last-session", or empty for everything.',
  }),
  limit: Schema.optional(Schema.Number).annotate({
    description: "Max results for search (default 20). Ignored for other actions.",
  }),
  // Write parameters
  topic: Schema.optional(Schema.String).annotate({
    description: "For write_decision/write_fact: the topic (reuse existing topics when possible).",
  }),
  statement: Schema.optional(Schema.String).annotate({
    description: "For write_decision/write_fact: self-contained statement. ABSOLUTE DATES ONLY (YYYY-MM-DD).",
  }),
  stance: Schema.optional(Schema.Literals(["adopted", "rejected"])).annotate({
    description: "For write_decision: adopted or rejected. Defaults to adopted.",
  }),
  rationale: Schema.optional(Schema.String).annotate({
    description: "For write_decision: why this decision was made.",
  }),
  intent: Schema.optional(Schema.String).annotate({
    description: "For write_open_loop/close_loop: what work is in progress or planned.",
  }),
  cue: Schema.optional(Schema.String).annotate({
    description: "For write_open_loop: keywords that should trigger this loop in recall.",
  }),
  resolution: Schema.optional(Schema.Literals(["done", "parked"])).annotate({
    description: "For close_loop: done or parked. Defaults to done.",
  }),
  tags: Schema.optional(Schema.Array(Schema.String)).annotate({
    description: "Tags for write_decision/write_fact/write_open_loop.",
  }),
})

type MemoryMetadata = {
  available: boolean
  count?: number
  error?: string
  empty?: boolean
  items?: number
  policy_version?: string
  written?: boolean
}

export const MemoryTool = Tool.define(
  "memory",
  Effect.gen(function* () {
    return {
      description: DESCRIPTION,
      parameters: Parameters,
      execute: (params: Schema.Schema.Type<typeof Parameters>, ctx: Tool.Context): Effect.Effect<Tool.ExecuteResult<MemoryMetadata>> =>
        Effect.gen(function* () {
          if (!Centri.enabled()) {
            return {
              title: "Memory unavailable",
              output: "Centri memory core is not configured (CENTRI_URL not set). Proceeding without memory recall.",
              metadata: { available: false } as MemoryMetadata,
            }
          }

          const repoID = (ctx.extra?.directory as string | undefined) ?? undefined

          switch (params.action) {
            // === READ ACTIONS ===
            case "search": {
              if (!params.query?.trim()) {
                return {
                  title: "Memory search — missing query",
                  output: "The 'search' action requires a 'query' parameter.",
                  metadata: { available: true, error: "missing_query" },
                }
              }
              const results = yield* Effect.promise(() =>
                Centri.search(params.query!, { limit: params.limit ?? 20 }),
              )
              if (!results || results.length === 0) {
                return {
                  title: "Memory search — no results",
                  output: `No memory results for "${params.query}".`,
                  metadata: { available: true, count: 0 },
                }
              }
              const formatted = results.map((r, i) => {
                const payloadStr = r.payload ? JSON.stringify(r.payload, null, 2).slice(0, 500) : ""
                return `### ${i + 1}. ${r.type} (${r.ts})\nevent_id: ${r.event_id}${r.snippet ? `\n${r.snippet}` : ""}${payloadStr ? `\n\n\`\`\`json\n${payloadStr}\n\`\`\`` : ""}`
              }).join("\n\n---\n\n")
              return {
                title: `Memory search — ${results.length} result(s)`,
                output: formatted,
                metadata: { available: true, count: results.length },
              }
            }

            case "recall": {
              if (!params.query?.trim()) {
                return {
                  title: "Memory recall — missing cue",
                  output: "The 'recall' action requires a 'query' (cue) parameter.",
                  metadata: { available: true, error: "missing_cue" },
                }
              }
              const brief = yield* Effect.promise(() =>
                Centri.recall(params.query!, { threadID: ctx.sessionID, repoID }),
              )
              if (!brief?.markdown.trim()) {
                return {
                  title: "Memory recall — empty",
                  output: `No recalled context for cue: "${params.query}".`,
                  metadata: { available: true, empty: true },
                }
              }
              return {
                title: "Memory recall",
                output: brief.markdown,
                metadata: {
                  available: true,
                  items: brief.items?.length ?? 0,
                  policy_version: brief.policy_version,
                },
              }
            }

            case "where-left-off": {
              const result = yield* Effect.promise(() =>
                Centri.whereLeftOff({ repoID }),
              )
              if (!result?.available) {
                return {
                  title: "Where left off — unavailable",
                  output: result?.reason ?? "Memory temporal narrative is not available.",
                  metadata: { available: false },
                }
              }
              return {
                title: "Where we left off",
                output: (result.narrative ?? JSON.stringify(result, null, 2)),
                metadata: { available: true },
              }
            }

            case "since": {
              const when = params.when ?? "last-session"
              const result = yield* Effect.promise(() =>
                Centri.since(when, { repoID }),
              )
              if (!result?.available) {
                return {
                  title: `Since ${when} — unavailable`,
                  output: result?.reason ?? "Memory temporal narrative is not available.",
                  metadata: { available: false },
                }
              }
              return {
                title: `Changed since ${when}`,
                output: (result.narrative ?? JSON.stringify(result, null, 2)),
                metadata: { available: true },
              }
            }

            // === WRITE ACTIONS ===
            case "write_decision": {
              if (!params.topic?.trim() || !params.statement?.trim()) {
                return {
                  title: "Memory write — missing fields",
                  output: "write_decision requires 'topic' and 'statement'.",
                  metadata: { available: true, error: "missing_fields" },
                }
              }
              Centri.writeMemory(
                {
                  kind: "decision",
                  topic: params.topic!,
                  statement: params.statement!,
                  stance: params.stance ?? "adopted",
                  rationale: params.rationale,
                  tags: params.tags ? [...params.tags] : undefined,
                },
                { threadID: ctx.sessionID, repoID },
              )
              return {
                title: `Decision written: ${params.topic}`,
                output: `Decision recorded to memory graph (will be consolidated on next tick):\n  topic: ${params.topic}\n  stance: ${params.stance ?? "adopted"}\n  statement: ${params.statement}`,
                metadata: { available: true, written: true },
              }
            }

            case "write_fact": {
              if (!params.topic?.trim() || !params.statement?.trim()) {
                return {
                  title: "Memory write — missing fields",
                  output: "write_fact requires 'topic' and 'statement'.",
                  metadata: { available: true, error: "missing_fields" },
                }
              }
              Centri.writeMemory(
                {
                  kind: "fact",
                  topic: params.topic!,
                  statement: params.statement!,
                  tags: params.tags ? [...params.tags] : undefined,
                },
                { threadID: ctx.sessionID, repoID },
              )
              return {
                title: `Fact written: ${params.topic}`,
                output: `Fact recorded to memory graph (will be consolidated on next tick):\n  topic: ${params.topic}\n  statement: ${params.statement}`,
                metadata: { available: true, written: true },
              }
            }

            case "write_open_loop": {
              if (!params.intent?.trim()) {
                return {
                  title: "Memory write — missing fields",
                  output: "write_open_loop requires 'intent'.",
                  metadata: { available: true, error: "missing_fields" },
                }
              }
              Centri.writeMemory(
                {
                  kind: "open_loop",
                  intent: params.intent!,
                  cue: params.cue,
                  tags: params.tags ? [...params.tags] : undefined,
                },
                { threadID: ctx.sessionID, repoID },
              )
              return {
                title: `Open loop written: ${params.intent!.slice(0, 60)}`,
                output: `Open loop recorded to memory graph (will be consolidated on next tick):\n  intent: ${params.intent}${params.cue ? `\n  cue: ${params.cue}` : ""}`,
                metadata: { available: true, written: true },
              }
            }

            case "close_loop": {
              if (!params.intent?.trim()) {
                return {
                  title: "Memory write — missing fields",
                  output: "close_loop requires 'intent'.",
                  metadata: { available: true, error: "missing_fields" },
                }
              }
              Centri.writeMemory(
                {
                  kind: "loop_resolution",
                  intent: params.intent!,
                  resolution: params.resolution ?? "done",
                },
                { threadID: ctx.sessionID, repoID },
              )
              return {
                title: `Loop closed: ${params.intent!.slice(0, 60)}`,
                output: `Loop resolution recorded (will be consolidated on next tick):\n  intent: ${params.intent}\n  resolution: ${params.resolution ?? "done"}`,
                metadata: { available: true, written: true },
              }
            }

            default:
              return {
                title: "Memory — unknown action",
                output: `Unknown action: ${params.action}.`,
                metadata: { available: true, error: "unknown_action" },
              }
          }
        }).pipe(Effect.orDie),
    }
  }),
)
