# Centri Hermes plugin

A deployable copy of the Hermes `MemoryProvider` plugin that backs onto the
Centri core's cognitive event-spine API. See
[`docs/HERMES-INTEGRATION.md`](../../docs/HERMES-INTEGRATION.md) for the full
guide.

## Files

- `__init__.py` — `CentriMemoryProvider` + the `centri_retain` / `centri_recall`
  / `centri_reflect` tools. stdlib HTTP only (no `requests`/`httpx` dependency).
- `plugin.yaml` — Hermes plugin manifest (`hooks: on_memory_write,
  on_session_switch`).

## Install

Hermes loads plugins from `~/.hermes/plugins/<name>/`. Symlink (stays in sync
with `git pull`) or copy:

```bash
# from repo root
mkdir -p ~/.hermes/plugins
ln -s "$PWD/deploy/hermes-plugin/centri" ~/.hermes/plugins/centri
# or: cp -r deploy/hermes-plugin/centri ~/.hermes/plugins/centri
```

The `MemoryProvider` base class is imported from the Hermes agent package
(default `~/.hermes/hermes-agent`); override with `export HERMES_AGENT_REPO=…`
if your layout differs.

## Configure

In Hermes' `config.yaml`, set `memory.provider: centri` and point it at the
core:

```yaml
memory:
  provider: centri
  centri:
    api_base: http://127.0.0.1:8760
    auth_token: <CENTRI_AUTH_TOKEN>
```

`auth_token` must equal the core's `CENTRI_AUTH_TOKEN`. Equivalent env vars
(checked after the config file): `CENTRI_API_BASE`, `CENTRI_AUTH_TOKEN`.

Then **restart Hermes** (plugins load at startup):

```bash
systemctl restart hermes   # or however Hermes is launched
```

The plugin hot-reloads its *config* on every call (`_load_config()`), so edits
to `api_base` / `auth_token` take effect on the next turn without a restart —
but edited/new plugin *files* require a restart.

## Verify

```bash
# core is up (unauthenticated)
curl -fsS http://127.0.0.1:8760/health

# the plugin's is_available() hits the same endpoint
# a cued recall (what prefetch() and centri_recall do):
curl -fsS -X POST http://127.0.0.1:8760/memory/recall \
  -H "Authorization: Bearer $CENTRI_AUTH_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"cue":"where did we leave off","format":"markdown+items"}'
```
