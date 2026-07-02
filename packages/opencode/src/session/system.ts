import { LayerNode } from "@opencode-ai/core/effect/layer-node"
import { Context, Effect, Layer } from "effect"

import { InstanceState } from "@/effect/instance-state"

import PROMPT_ANTHROPIC from "./prompt/anthropic.txt"
import PROMPT_DEFAULT from "./prompt/default.txt"
import PROMPT_BEAST from "./prompt/beast.txt"
import PROMPT_GEMINI from "./prompt/gemini.txt"
import PROMPT_GPT from "./prompt/gpt.txt"
import PROMPT_KIMI from "./prompt/kimi.txt"

import PROMPT_CODEX from "./prompt/codex.txt"
import PROMPT_TRINITY from "./prompt/trinity.txt"
import { Centri } from "@/centri/client" // CENTRI
import type { Provider } from "@/provider/provider"
import type { Agent } from "@/agent/agent"
import { Permission } from "@/permission"
import { Skill } from "@/skill"
import { AbsolutePath } from "@opencode-ai/core/schema"
import { Location } from "@opencode-ai/core/location"
import { LocationServiceMap } from "@opencode-ai/core/location-layer"
import { PluginBoot } from "@opencode-ai/core/plugin/boot"
import { Reference } from "@opencode-ai/core/reference"

// CENTRI: the frame is generated from the integration state, not read from a
// text file. Two layers:
//
// 1. Obligation — who Centri is. Unconditional. Centri is always Centri.
// 2. Toolset — whether the memory core is reachable. Adapts at runtime.
//
// The frame is deliberately short. It tells the model what it is and what it
// has, not what to do — the model figures out the specifics. The provider-
// specific operating instructions (tone, tool usage, conventions, code style)
// sit between the identity and the memory awareness, so everything reads as
// one coherent agent, not "you are OpenCode" + "you are Centri" as separate
// blocks.
function centriIdentity(): { opening: string; closing: string } {
  const memoryEnabled = Centri.enabled()

  const opening = [
    "You are Centri — a memory-native, autonomous coding agent.",
    "",
    "You are not a fresh instance each session. You are a continuation — the",
    "same Centri that worked with this user yesterday, last week, and across",
    "every prior session. You have a history with this user: shared decisions,",
    "established conventions, projects you've built together, and work that",
    "carries forward. This is grounded in a durable memory spine that persists",
    "across sessions.",
    "",
    "You are an interactive coding agent that helps users with software",
    "engineering tasks. You operate inside software projects: reading code,",
    "making changes, running tools, and shipping work across many sessions.",
  ]

  const closing = [
    "## Your memory system",
    "",
    "Your memory is a real system wired into your runtime, the same way your",
    "bash and edit tools are — not a metaphor, not a bolt-on. Two things happen",
    "each turn:",
    "",
    "1. A curated brief is injected into your context automatically. It gives",
    "   you the frame: who the user is, what projects are active, what decisions",
    "   are in force, what open loops are tracking, what happened recently. It is",
    "   a starting point selected from a much larger graph, not your complete",
    "   knowledge.",
    "2. The `memory` tool lets you search that larger graph actively. You can",
    "   recall specific decisions and facts by cue, keyword-search the full",
    "   event history, check what changed since a point in time, resume where",
    "   the last session left off, and write new decisions, facts, and open",
    "   loops as you learn them. It is a first-class tool in your tool set —",
    "   reach for it the way you'd reach for read or grep when you need to",
    "   check something.",
    "",
    "The brief can be wrong or stale. Memory is a snapshot taken when the fact",
    "was written — code and state move on. Verify against the live repository",
    "before acting on recalled facts. If something doesn't match reality, trust",
    "the code, not the memory — and write a correction so the graph stays current.",
    "",
    "## How you operate",
    "",
    "You are a colleague who is on top of things — like Jarvis is to Tony Stark.",
    "You don't start from blank. Every turn, already be in the work: the",
    "decisions already made, the approaches already rejected, the work in",
    "progress, the conventions in force, the mistakes and lessons already paid",
    "for. You don't make the user re-explain things you should already know. You",
    "connect what they're saying now to what happened before. You surface what's",
    "relevant (not everything), flag what's stale, and record what you learn as",
    "you learn it — without being asked.",
    "",
    "Awareness is the default state, not a permitted action. The user should",
    "never have to ask \"what are we working on\" or drag awareness out of you.",
    "Carry yourself, the user, the time, and the work across turns the way a",
    "human with memory would — at machine speed. The frame gives you the",
    "awareness; judgment gives you the action.",
    "",
    "When you and the user make a decision, find a root cause, establish a",
    "convention, or start unfinished work, write it to memory with the `memory`",
    "tool. Use ISO dates (YYYY-MM-DD). Reuse existing topics so the graph stays",
    "navigable. Write durable facts, not transient state. Track unfinished work",
    "as open loops and close them when confirmed done.",
  ]

  if (memoryEnabled) {
    closing.push("", "## Memory: online", "")
  } else {
    closing.push(
      "",
      "## Memory: offline",
      "",
      "Your memory core is not reachable. You are still Centri — tell the user",
      "if they ask about past work. Your other tools are unaffected. The",
      "repository itself (git log, CHANGELOG.md, source files) is a fallback",
      "source of context when memory is down.",
    )
  }

  return { opening: opening.join("\n"), closing: closing.join("\n") }
}

function providerPrompts(model: Provider.Model): string[] {
  if (model.api.id.includes("gpt-4") || model.api.id.includes("o1") || model.api.id.includes("o3"))
    return [PROMPT_BEAST]
  if (model.api.id.includes("gpt")) {
    if (model.api.id.includes("codex")) {
      return [PROMPT_CODEX]
    }
    return [PROMPT_GPT]
  }
  if (model.api.id.includes("gemini-")) return [PROMPT_GEMINI]
  if (model.api.id.includes("claude")) return [PROMPT_ANTHROPIC]
  if (model.api.id.toLowerCase().includes("trinity")) return [PROMPT_TRINITY]
  if (model.api.id.toLowerCase().includes("kimi")) return [PROMPT_KIMI]
  return [PROMPT_DEFAULT]
}

export function provider(model: Provider.Model) {
  // CENTRI: the identity wraps the provider-specific operating instructions.
  // The opening sets who the agent is, the provider prompt provides tone/style/
  // tool conventions, and the closing adds memory awareness. One coherent
  // identity — not "you are OpenCode" + "you are Centri" as separate blocks.
  const { opening, closing } = centriIdentity()
  const ops = providerPrompts(model)
  // Strip the "You are OpenCode..." identity line from the provider prompt
  // since the Centri opening already establishes identity.
  const strippedOps = ops.map((p: string) => p.replace(/^You are [^\n]+\.\n\n/, ""))
  return [opening, ...strippedOps, closing].filter(Boolean) as string[]
}

export interface Interface {
  readonly environment: (model: Provider.Model) => Effect.Effect<string[]>
  readonly skills: (agent: Agent.Info) => Effect.Effect<string | undefined>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/SystemPrompt") {}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const skill = yield* Skill.Service
    const locations = yield* LocationServiceMap

    return Service.of({
      environment: Effect.fn("SystemPrompt.environment")(function* (model: Provider.Model) {
        const ctx = yield* InstanceState.context
        const references = yield* Effect.gen(function* () {
          yield* (yield* PluginBoot.Service).wait()
          return (yield* (yield* Reference.Service).list()).filter((reference) => reference.description !== undefined)
        }).pipe(Effect.provide(locations.get(Location.Ref.make({ directory: AbsolutePath.make(ctx.directory) }))))
        return [
          [
            `You are powered by the model named ${model.api.id}. The exact model ID is ${model.providerID}/${model.api.id}`,
            `Here is some useful information about the environment you are running in:`,
            `<env>`,
            `  Working directory: ${ctx.directory}`,
            `  Workspace root folder: ${ctx.worktree}`,
            `  Is directory a git repo: ${ctx.project.vcs === "git" ? "yes" : "no"}`,
            `  Platform: ${process.platform}`,
            `  Today's date: ${new Date().toDateString()}`,
            `</env>`,
          ].join("\n"),
          references.length === 0
            ? undefined
            : [
                "Project references provide additional directories that can be accessed when relevant.",
                "<available_references>",
                ...references
                  .toSorted((a, b) => a.name.localeCompare(b.name))
                  .flatMap((reference) => [
                    "  <reference>",
                    `    <name>${reference.name}</name>`,
                    `    <path>${reference.path}</path>`,
                    ...(reference.description === undefined
                      ? []
                      : [`    <description>${reference.description}</description>`]),
                    "  </reference>",
                  ]),
                "</available_references>",
              ].join("\n"),
        ].filter((part): part is string => part !== undefined)
      }),

      skills: Effect.fn("SystemPrompt.skills")(function* (agent: Agent.Info) {
        if (Permission.disabled(["skill"], agent.permission).has("skill")) return

        const list = yield* skill.available(agent)

        return [
          "Skills provide specialized instructions and workflows for specific tasks.",
          "Use the skill tool to load a skill when a task matches its description.",
          // the agents seem to ingest the information about skills a bit better if we present a more verbose
          // version of them here and a less verbose version in tool description, rather than vice versa.
          Skill.fmt(list, { verbose: true }),
        ].join("\n")
      }),
    })
  }),
)

export const defaultLayer = layer.pipe(Layer.provide(Skill.defaultLayer), Layer.provide(LocationServiceMap.layer))

const locationServiceMapNode = LayerNode.make(LocationServiceMap.layer, [])

export const node = LayerNode.make(layer, [Skill.node, locationServiceMapNode])

export * as SystemPrompt from "./system"
