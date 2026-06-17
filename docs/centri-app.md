# Centri app shell (Track B notes)

How the OpenCode app shell (`packages/`, TypeScript/Bun) was made memory-native
against the Centri core (`core/`, Python — Track A). Goal: smallest possible
diff, all changes concentrated in NEW files, inline patches marked `// CENTRI`,
every memory call FAILS OPEN (a dead/slow core never blocks or crashes the agent
loop).

## Files touched

New files:
- `packages/opencode/src/centri/client.ts` — the ONLY module that speaks HTTP to
  the core. Config, bearer auth, timeouts, batching, fail-open. Exports `Centri`.
- `packages/opencode/src/centri/tap.ts` — write-path tap. One idempotent listener
  on the process-global `GlobalBus`; maps runtime events → `centri_app.*`
  envelopes → `Centri.importEvents`. Exports `CentriTap`.
- `packages/opencode/src/session/prompt/centri.txt` — Centri identity prompt.

Inline patches (each marked `// CENTRI`, minimal):
- `packages/opencode/src/server/server.ts` — import `CentriTap`; call
  `CentriTap.install()` at the top of `listenEffect` (server boot).
- `packages/opencode/src/session/prompt.ts` — import `Centri`; in `runLoop`, on
  the first LLM call of a user turn (`step === 1`), build a cue and append the
  recalled markdown to that turn's `system[]`. Plus a `centriCue()` helper.
- `packages/opencode/src/session/system.ts` — import `PROMPT_CENTRI`; append it
  as an ADDITIONAL system entry for every provider (provider-native prompts are
  preserved, not replaced).
- `packages/opencode/src/session/instruction.ts` — automatically appends
  `Centri.ambientUrl()` to OpenCode's native remote instruction URL list when
  `CENTRI_URL` is set.

Rebrand (user-visible product name only — no package/binary renames):
- `packages/app/index.html` — `<title>` → Centri
- `packages/app/src/desktop-menu.ts` — macOS app menu label → Centri
- `packages/app/src/components/windows-app-menu.tsx` — Windows menu heading → Centri
- `packages/desktop/src/main/index.ts` — `APP_NAMES` + dev `setName` → Centri
- `packages/desktop/src/main/windows.ts` — window `title` → Centri

## Environment variables

| Var            | Purpose                                                      |
|----------------|--------------------------------------------------------------|
| `CENTRI_URL`   | Base URL of the Centri core, e.g. `http://127.0.0.1:8000`. When unset, the whole integration is disabled (no recall, no event import) — the app behaves exactly like upstream OpenCode. |
| `CENTRI_TOKEN` | Bearer token. Sent as `Authorization: Bearer <token>` on `/memory/recall` and `/events/import`, and as `?token=` on the ambient instruction URL (the instruction fetcher can't set headers). Maps to the core's `CENTRI_AUTH_TOKEN`. |

The contract also allows a `"centri"` block in `opencode.json`. We deliberately
read **env vars only** to keep the diff minimal and avoid touching/relaxing the
strict config schema (a low-risk choice for the demo; can be added later).

## Endpoints used (see `contracts/bridge-api.md`)

- `POST /memory/recall` — read path. 3s timeout (configurable via `CENTRI_RECALL_TIMEOUT_MS`), fail-open → no brief.
- `POST /events/import` — write path. Batched (flush every 2s or 50 events),
  fire-and-forget, 5s timeout. Drops a failed batch rather than retry-looping.
- `GET /memory/ambient.md?token=` — ambient layer. `Centri.ambientUrl()` builds
  the URL; `instruction.system()` appends it to the native remote-instruction
  fetch list when the core is configured.

## How per-turn injection works

1. A user sends a message → `SessionPrompt.prompt` → `runLoop`.
2. On the first model call of that turn (`step === 1`), `centriCue(msgs)`
   builds a cue string: the user's visible text (non-synthetic, non-ignored text
   parts) plus any attached file names (`Active files: a.ts, b.ts`), plus the
   last 6 conversation turns (user+assistant, truncated to 500 chars each) for
   anaphora resolution — so "where did we leave off?" or "is that still
   broken?" can match against prior session context.
3. `Centri.recall(cue, { threadID: sessionID })` POSTs to `/memory/recall` with
   a 3s timeout (configurable via `CENTRI_RECALL_TIMEOUT_MS`). The core runs
   its pure `curate()` and returns `{ markdown, items, ... }`. No LLM at read
   time.
4. If a non-empty `markdown` brief comes back, it's pushed onto this turn's
   `system[]` array (after env/instructions/skills, before structured-output
   prompt). It is NOT persisted as a message — it's a per-turn system entry.
5. Any failure (disabled core, timeout, non-2xx, malformed body) → `recall`
   returns `undefined` → the turn proceeds untouched. Fail-open.

The write path runs independently: `CentriTap` listens to every `GlobalBus`
event and ships envelopes for session lifecycle (`session.created|updated|idle`),
message text (`message.updated`, plus text-part updates), tool execution
(`tool.execute`, collapsed onto the terminal tool-part transition), and
permissions (`permission.asked|replied`). `thread_id` = session id;
`payload.event_uid` = the bus event id (the core dedupes on
`(source, payload.event_uid)`).

## Running the web app against a Centri core

```bash
# 1. Start the Centri core (Track A; see core/ + HANDOFF.md) on, say, :8000.

# 2. Point the shell at it and launch the web UI from repo root:
export CENTRI_URL=http://127.0.0.1:8000
export CENTRI_TOKEN=<same value as the core's CENTRI_AUTH_TOKEN>
bun install                       # first time only
bun run packages/opencode/src/index.ts web     # or: opencode web
```

With `CENTRI_URL` unset, everything still runs — the integration just no-ops.

## Demo script (acceptance, per contract)

1. `export CENTRI_URL=... CENTRI_TOKEN=...`, start core, launch the web app.
2. Work a real session: ask it to make a small change, run a tool or two, reply
   to a permission prompt. The tap streams these to the core as `centri_app.*`
   events (verify on the core: `accepted > 0` from `/events/import`).
3. Kill the app / end the session.
4. Reopen the app, start a session, ask: **"where did we leave off?"**
5. The first turn's `system[]` gets a brief assembled from the spine — with
   receipts (`source_event_id` on each item) — and the model answers from it.
   No LLM ran at read time; the brief came from the core's `curate()`.

## Verification status (be honest)

Sandbox-verified:
- `bun install` at repo root — succeeds (needed `patches/` materialized in the
  sparse checkout first).
- `bun run typecheck` (`tsgo --noEmit`) in `packages/opencode` — **zero type
  errors in any Centri file or any file we patched**. There is one PRE-EXISTING
  error in `packages/opencode/src/bus/global.ts` (an upstream EventEmitter
  generics mismatch in the base merge commit); it contains no Centri code and is
  unrelated to this work.

Needs a real machine / live core (NOT verified in sandbox):
- End-to-end recall and event import against a running Centri core (no core was
  available in the sandbox).
- The full demo script above (requires both the core and a browser).
- Desktop (Electron) packaging with the rebranded names.
- Ambient instruction-URL wiring is implemented; verify the fetched ambient text
  appears in a live model request when `CENTRI_URL` is set.
