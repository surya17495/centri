# Fork Notes

Centri is a fork of the [OpenCode](https://github.com/anomalyco/opencode) project
(MIT-licensed; the upstream license is preserved at
[LICENSE-OPENCODE](LICENSE-OPENCODE)). The original upstream README and its community
translations are kept verbatim under [upstream/opencode/](upstream/opencode/) for
attribution.

## What Centri is

Centri is two halves that share one durable memory, plus a Hermes plugin:

- **Centri core** ([`core/`](core/)) — a Python memory API: an append-only event
  **spine**, a typed **memory graph** with bi-temporal supersession, deterministic
  **curation**, optional **LLM consolidation**, and a REST/WS surface.
- **OpenCode fork** ([`packages/opencode/`](packages/opencode/)) — the TypeScript/Bun
  OpenCode app shell, patched so every turn recalls a brief from the core and every
  runtime event is tapped back into the spine.
- **Hermes plugin** ([`deploy/hermes-plugin/centri/`](deploy/hermes-plugin/centri/)) — a
  deployable `memory.provider` that translates Hermes memory calls into the core's HTTP
  API.

The **event spine is the source of truth**; the memory graph is a derived, re-derivable
index over it. The context window is a cache, not storage.

## What changed in the fork

Centri-specific changes are concentrated in new files and minimal inline patches, each
marked `// CENTRI`. All memory calls **fail open** — a dead or slow core never blocks or
crashes the agent loop.

- [`packages/opencode/src/centri/client.ts`](packages/opencode/src/centri/client.ts) —
  the only module that speaks HTTP to the core (config, bearer auth, batching, fail-open).
- [`packages/opencode/src/centri/tap.ts`](packages/opencode/src/centri/tap.ts) —
  write-path tap mapping runtime events → `centri_app.*` envelopes on the spine.
- Inline patches in `server.ts`, `session/prompt.ts`, and `session/system.ts` recall a
  per-turn brief from the core and append the Centri identity prompt.

See [`docs/centri-app.md`](docs/centri-app.md) for the full patch inventory and
[`docs/HERMES-INTEGRATION.md`](docs/HERMES-INTEGRATION.md) for the integration guide.

## Upstream content kept for reference

[`upstream/opencode/`](upstream/opencode/) holds the original OpenCode README, its
translations, and upstream download stats. They describe upstream OpenCode, not Centri,
and may be out of date relative to this fork.

Most inherited OpenCode CI workflows (`.github/workflows/*.yml`) are inert on this fork:
several are gated to `github.repository == 'anomalyco/opencode'` and
`docs-locale-sync` is disabled (`if: false`). They are retained to keep the merge
surface from upstream small and can be pruned independently.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) is inherited from upstream OpenCode; its sections
(issues, vouch, Discord, `bun dev`) apply to the `packages/opencode` workspace, and its
links point at the upstream OpenCode project. For Centri-core development and deployment,
see the Quickstart in [README.md](README.md) and [`docs/DEPLOY.md`](docs/DEPLOY.md).
