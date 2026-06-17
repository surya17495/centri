# Centri — Documentation Index

Centri is a memory-native [OpenCode](https://github.com/anomalyco/opencode) fork plus a
Centri memory core, with Hermes integration. Start at the
[top-level README](../README.md) for an overview, or dive in below.

## Getting started

- [../README.md](../README.md) — overview, quickstart, configuration
- [../FORK-NOTES.md](../FORK-NOTES.md) — what is Centri vs. upstream OpenCode
- [DEPLOY.md](DEPLOY.md) — deployment (systemd services, Caddy, ports)
- [centri-app.md](centri-app.md) — the OpenCode fork delta (`// CENTRI` patches)

## Architecture & memory

- [architecture.md](architecture.md) — component diagram and layers
- [memory-architecture.md](memory-architecture.md) — typed graph, bi-temporal supersession, curation, tiers
- [event-contract.md](event-contract.md) — the event spine contract and event families
- [ingestion-adapters.md](ingestion-adapters.md) — importing OpenCode / Claude Code / Cursor histories

## Hermes integration

- [HERMES-INTEGRATION.md](HERMES-INTEGRATION.md) — running the core as a Hermes `memory.provider`, structured chat ingestion, systemd services

## Benchmarks & status

- [centri-bench.md](centri-bench.md) — head-to-head methodology vs. a real Letta server
- [bench-results/](bench-results/) — raw bench output
- [ROADMAP.md](ROADMAP.md) — phased roadmap and ratified decisions
- [VISION.md](VISION.md) — the why and the end-to-end shape

## Upstream

- [../upstream/opencode/](../upstream/opencode/) — original OpenCode README, translations, and stats, kept for attribution
