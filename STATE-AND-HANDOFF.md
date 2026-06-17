# Centri — Current State & Handoff
Date: 2026-06-13 00:11 PDT · Repo: github.com/surya17495/centri · HEAD: 2a598b2

## The vision (why this exists)
Every AI agent has amnesia: close the session and it forgets you, your project,
and every decision you made together. Centri is the opposite — an agent with
photographic memory.

  Remembers everything verbatim. Recalls like a person. Verifies like a machine.

- Verbatim: an append-only event spine records every session, message, tool call,
  and approval. Nothing is deleted; facts are superseded, never lost.
- Like a person: a pure, deterministic curate() assembles fresh, budgeted context
  for EVERY turn from a typed memory graph — no LLM at read time, no flat blob.
- Like a machine: every recalled line carries a source_event_id receipt back to
  ground truth, so recall can be proven, not hallucinated.

Strategy: Cursor : VS Code :: Centri : OpenCode. We forked OpenCode (the cleanest
open agent runtime — sessions, streaming, tools, permissions, diffs, UI) into ONE
repo and made memory a first-class part of the runtime, not a plugin. Coding is
the first domain, not the boundary; voice/browser/ambient channels come later on
the same spine. Decision history: a plugin was tried first and abandoned (no
general prompt injection, no UI control, no agent identity) — hence the fork.

## Architecture (as built)
  packages/  — OpenCode v1.17.4 shell = user-facing agent runtime/UI
  core/      — Python FastAPI memory plane (the brain): event spine, memory graph,
               curate(), redaction, bearer auth. ~376 tests passing.
  Bridge     — thin HTTP contract between them (contracts/bridge-api.md).
One repo, one product. Root layout matches upstream, so `git merge` from
sst/opencode stays cheap (upstream kept as the `opencode` remote; pinned v1.17.4).

## What is DONE and pushed to main
Core (memory plane):
- POST /memory/recall — runs Curator.assemble(); returns {markdown, items[] with
  score/score_breakdown/source_event_id/kind, ambient_items[], policy_version,
  graph_hwm, elapsed_ms}. Fails open.            (162df06)
- POST /events/import — idempotent on (source, event_uid); redaction before
  persistence; accepts centri_app.*; {accepted, duplicates, rejected}. (662b960)
- GET /memory/ambient.md — ambient layer as text/plain; auth also via ?token=.
                                                  (92e827a)
- Tests: 376 passed / 1 skipped (+11 new bridge tests). HANDOFF.md updated. (2a598b2)

Fork delta (memory-native shell):
- packages/opencode/src/centri/client.ts — only file that talks HTTP to core;
  config CENTRI_URL + CENTRI_TOKEN; fail-open.    (ac3f62d)
- packages/opencode/src/centri/tap.ts — GlobalBus tap at server boot: session,
  message, tool, permission events → batched → /events/import.  (1fc4f2c)
- Per-turn cued recall injected into the system[] array (// CENTRI marked). (dbcb11e)
- session/prompt/centri.txt — Centri identity, appended for all providers. (03bc960)
- Ambient memory URL is auto-appended to OpenCode's remote instruction loader
  when CENTRI_URL is set.                            (working tree)
- Rebrand "opencode" → "Centri" in the app shell.  (fdc9a99)
- docs/centri-app.md — integration notes.          (d7bd87c)

Also done: build-ship post drafted (short + long), shared with Raghu.

## What is NOT done / NOT verified (honest)
- [ ] bun typecheck / build NOT confirmed on the fork delta. Run before demo:
        cd packages/opencode && bun install && bun run typecheck
- [ ] End-to-end loop NEVER run live. The demo itself is unproven:
        1) start core (uvicorn) with CENTRI_AUTH_TOKEN set
        2) run the Centri app with CENTRI_URL + CENTRI_TOKEN pointing at it
        3) work a coding session, then kill it
        4) reopen, ask "where did we leave off?" → expect a receipted brief
- [ ] Optional UI "Recalled N memories" chip — NOT built (cancelled to save credits).
- [ ] Recall latency vs the 800ms fail-open budget — not measured on real machine.
- [x] Ambient-URL wiring in the native instruction loader; no opencode.json
      edit required. Verify fetched content in a live model request.
- Pre-existing unrelated test failures noted by Track A: test_discover_endpoint_shape,
  test_embed_leaves_already_prefixed_model_unchanged (sandbox-env, not bridge).

## Next session (in priority order)
1. Verify build: bun install + typecheck the opencode package; fix any type errors
   in the // CENTRI patches.
2. Run the end-to-end demo loop above on a real machine; capture the recall output
   with receipts as the demo artifact.
3. Confirm ambient memory text appears in a live model request.
4. If time: the "Recalled N memories" chip (message-v2 memory part + web UI).
5. Stretch later (post-hackathon, from ROADMAP): import onboarding (HAL/OpenCode/
   mempalace), voice channel, non-coding tools.

## Demo script (one minute)
"Every agent forgets you the moment you close the tab. Watch." → work a session,
kill it, reopen, ask "where did we leave off?" → Centri answers from the spine and
shows the receipts. "It didn't summarize. It remembered — and it can prove it."

## Risks / discipline
- Keep the fork delta in NEW files + // CENTRI inline marks so upstream merges stay cheap.
- Memory must always fail open — a dead core must never block the agent loop.
- Push after every deliverable (a prior run was lost to a sandbox timeout with
  nothing pushed — do not repeat).
