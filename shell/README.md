# CENTRI desktop shell

A Tauri 2 + React + TypeScript + Vite + Tailwind desktop surface for CENTRI.
Minimal dark UI: an activity timeline (narration, live-streaming task cards,
inline approval cards, artifact summaries), a bottom command bar, a top status
strip (connection / active hand / model roles / settings), and a settings sheet
(backend URL, BYOK API keys, hands, model-role mapping).

It talks to the Python core over REST + a `/events/stream` WebSocket, reconnects
with exponential backoff, and shows offline state honestly.

## Requirements

- **Node** 20+ and **npm** 10+ (for the React frontend — this is all you need to
  develop and verify the UI).
- **Rust** stable + **cargo** (for the Tauri desktop binary only). Install via
  [rustup](https://rustup.rs/). On Linux you also need the usual Tauri system deps
  (`webkit2gtk`, `libayatana-appindicator`, etc.) — see the
  [Tauri prerequisites](https://tauri.app/start/prerequisites/).

## Run in the browser (no Rust needed)

The frontend is a normal Vite app and runs fully in a browser against a running
core backend. This is the fastest dev loop and is what CI verifies.

```bash
cd shell
npm install
npm run dev        # http://localhost:1420
```

Point it at your backend from the in-app settings gear (default
`http://127.0.0.1:8760`), or it will use that default.

Quality gates (run in CI / the sandbox):

```bash
npm run typecheck  # tsc --noEmit
npm run build      # tsc --noEmit && vite build
npm test           # vitest: timeline rendering + approval-card flow
```

## Run as a desktop app (needs Rust)

The Tauri wrapper lives in `src-tauri/`. It hosts the same Vite build in a single
resizable window (960×720, 480px minimum, dark theme) and installs the
global-shortcut plugin (handler wiring is a Phase 1 stub).

```bash
cd shell
npm install
npm run tauri dev      # dev window with hot reload
npm run tauri build    # production bundle
```

> **Sandbox note:** the Rust toolchain is not available in the build sandbox, so
> the Tauri binary is **not** sandbox-verified — `src-tauri/` is scaffolded and
> correct by construction but must be compiled locally. The React frontend it
> hosts *is* fully verified (typecheck, build, tests, browser run).

## Layout

```
shell/
  index.html
  src/
    main.tsx              React entry
    App.tsx               window layout
    api.ts                REST client + WS URL + backend-URL persistence
    types.ts              event/card/timeline types
    useEventStream.ts     WS connect + backoff + REST hydration; event aggregation
    components/
      StatusStrip.tsx     connection dot, active hand health, model roles, gear
      Timeline.tsx        renders the aggregated timeline (narration/task/approval/event)
      TaskCard.tsx        streaming progress + artifacts, expandable detail
      ApprovalCard.tsx    inline approve/reject
      CommandBar.tsx      text input -> POST /utterance
      SettingsPanel.tsx   backend URL, BYOK keys, hands, model roles
  src-tauri/              Tauri 2 wrapper (Cargo.toml, tauri.conf.json, capabilities, Rust)
```
