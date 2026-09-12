# Codepilot Web Client

A from-scratch, dependency-light Agentic IDE frontend. No React/Vue, no UI
kit, no icon font — hand-rolled DOM + a ~40-line pub/sub store. The only
heavy dependency is Monaco itself, and it's trimmed to only the languages
this IDE actually supports (see `src/core/monacoSetup.ts`).

## Answers to the open questions from the brief

**Where does code get written?** Directly to disk, in this cloned repo,
as real files — under `webapp/`, alongside the existing `client/` (old
code-server extension, left untouched) and `runner/` (Rust server + agent).
Nothing here is a markdown transcript or a Claude-side artifact; it's a
normal npm project you build and ship like any other frontend.

**How are files served to the browser?** Static files from disk via
`tower_http::services::ServeDir` — which `runner/server/src/routes.rs`
*already* used (`STATIC_PATH`, default `./frontend/dist`). I didn't
introduce `rust-embed`: `ServeDir` is already minimal (no extra binary
size, streams from disk instead of holding the whole bundle in memory),
and keeping the frontend as loose files means you can `docker cp` an
updated `dist/` into a running container for a quick fix without
recompiling the Rust binary. The Dockerfile now builds this webapp in its
own stage and copies `dist/` to `/opt/codepilot/frontend/dist` (outside
`/workspace`, so a B2-restored volume never shadows the UI), and sets
`STATIC_PATH` accordingly.

**Server-side changes.** None to the Rust server's logic — `routes.rs`,
`fs.rs`, `agent.rs`, `state.rs` were already a clean, complete, minimal
implementation of exactly the fs + terminal + agent bridge this client
needs. The only edit was to `Dockerfile`, adding a frontend build stage.

## Architecture

```
src/
  core/
    types.ts          — protocol types, mirrored 1:1 from routes.rs / agent_server.py
    controlSocket.ts   — /ws/control client (fs CRUD + live fs_events)
    agentSocket.ts      — /ws/agent client (NDJSON framing over the raw byte bridge)
    monacoSetup.ts      — selective Monaco import + worker wiring
  state/store.ts        — tiny observable Store<T>, localStorage-backed settings
  ui/
    icons.ts             — inline SVG icon set
    explorer/explorer.ts — file tree: real fs-backed CRUD, live sync, context menu, inline rename
    editor/editor.ts      — Monaco tabs, dirty tracking, ⌘S save, external-change reload
    agent/agent.ts          — chat: streaming text/thinking, tool call cards, permission/ask_user prompts, sessions
    settings/settings.ts     — consolidated modal: Editor+Theme, Agent Model, Tools, Advanced
  main.ts                — sidebar (Explorer / Agent / Settings) + wiring
```

**Why no framework.** The whole UI is event-driven DOM updates keyed off
two WebSocket streams; a VDOM diffing framework buys nothing here and
costs bundle size + a runtime. `Store<T>` is the entire "state
management" story — `get/set/subscribe`.

**Themes.** Exactly two, `dark` and `light`, matching Monaco's own
`vs-dark`/`vs`. `src/styles/theme.css` defines CSS variables sampled from
those two Monaco palettes, so the sidebar/explorer/agent/settings chrome
and the editor always agree — flip the toggle in Settings → Editor and
`monaco.editor.setTheme()` and `data-theme` on `<html>` update together.

**Real-time explorer sync.** The tree never polls. `fs.rs`'s `notify`
watcher already broadcasts `file_created` / `file_deleted` / `file_changed`
to every `/ws/control` client; `Explorer` subscribes and patches only the
affected node's children — so an agent tool call that writes a file shows
up in the tree the moment the watcher fires, no refresh needed. The open
editor tab for that file is refreshed the same way (`agent.ts` fires
`onFileWritten`, `editor.ts` reloads from disk *if the tab has no unsaved
local edits* — never clobbers your typing).

**Known protocol gap — sessions.** `agent_server.py` has no `list_sessions`
op (sessions live as files in `~/.codepilot/sessions`, outside the
`/workspace` root the fs socket exposes). The session switcher therefore
tracks known session ids in `localStorage` rather than fetching a live
list. If you want a real list, the cleanest fix is a small addition to
`agent_server.py`: an `event_type: "list_sessions"` handler that
`os.listdir(SESSIONS_DIR)`s and emits `{"type":"session_list","sessions":[...]}`
— a few lines, deliberately left out here since it's server logic, not client.

**Known protocol gap — rename/move.** There's no `rename` op on
`/ws/control`. File rename does read+create+write+delete; directory
rename recursively rebuilds the tree the same way (see
`Explorer.moveDirectory`). Fine for normal workspace-sized renames; if you
routinely rename huge trees, a native `rename_path` op in `fs.rs` (a
single `tokio::fs::rename` call) would be a trivial, much cheaper addition.

## Local development

Requires the Rust server running locally (talking to a real `/workspace`
and, optionally, a real `agent_server.py` on `/tmp/agent_runtime.sock` for
the agent panel to do anything):

```bash
# Terminal 1 — the server, from runner/server/
WORKSPACE_PATH=/tmp/codepilot-test-workspace \
STATIC_PATH=../../webapp-unused \
cargo run

# Terminal 2 — the webapp, from webapp/
npm install
npm run dev        # http://localhost:5173, proxies /ws/* to :8080
```

`STATIC_PATH` above points somewhere that doesn't exist on purpose — in
dev you're serving the webapp from Vite (`npm run dev`), not from the Rust
server, so the Rust server's own static serving is irrelevant; only its
`/ws/*` endpoints matter and `vite.config.ts` proxies to them.

Without `agent_server.py` running, `/ws/agent` will still connect (Rust
side accepts the browser connection regardless) but `agent.rs`'s bridge to
`/tmp/agent_runtime.sock` will keep retrying — the agent panel will just
sit idle, which is fine for explorer/editor development.

## Production build

```bash
npm run build      # tsc --noEmit type-check, then vite build -> ../runner/frontend/dist
```

This is what the updated `runner/Dockerfile` runs automatically — you
don't need to build it by hand before `docker build`.

## Deploying

Nothing changes about the Fly.io Machines / proxy architecture you
described — this client only talks to `/ws/control`, `/ws/terminal`
(unused here, reserved for a future terminal panel), `/ws/agent`, and
static `GET /`, all of which the existing FastAPI proxy already forwards
verbatim over the private network by cookie-selected `machine_id`. Build:

```bash
cd runner
docker build -t codepilot-runner .
```

Push wherever your Fly.io Machines API deploy step already pulls the
image from — nothing in that pipeline needs to know the frontend changed;
it's baked into the same image at `/opt/codepilot/frontend/dist`.

## Testing

**Fastest smoke test (no Fly.io needed):** run the server against a throwaway
local directory and open it directly, bypassing the FastAPI proxy entirely:

```bash
mkdir -p /tmp/codepilot-test-workspace
cd runner/server
WORKSPACE_PATH=/tmp/codepilot-test-workspace \
STATIC_PATH=../../webapp/dist-or-wherever-you-built \
cargo run
# open http://localhost:8080
```

Checklist:
- Explorer: create a file/folder via the toolbar and via right-click,
  rename, delete. Confirm each shows up instantly (it's not polling).
- From another terminal, `touch /tmp/codepilot-test-workspace/x.txt` —
  confirm the explorer updates without any UI interaction (proves the
  `notify` watcher → `/ws/control` → `Explorer` path works end-to-end).
- Editor: open a file, edit it, `⌘S`/Ctrl+S, confirm the dot→tab-close
  swap and that the byte content on disk actually changed (`cat` it).
- Settings: toggle theme — confirm the editor *and* the chrome repaint
  together, not just one.
- Agent: only testable with `agent_server.py` actually running and a
  real `/tmp/agent_runtime.sock` — see `runner/entrypoint.sh` for how
  it's normally launched. Easiest is a full `docker build` + `docker run`
  of the whole image locally with a `-v` mount for `/workspace`, since
  the agent runtime, its Unix socket, and the Rust bridge are all wired
  together by the entrypoint script, not independently runnable.

**Full-stack local test:**
```bash
docker build -t codepilot-runner runner/
docker run -p 8080:8080 -v /tmp/codepilot-test-workspace:/workspace \
  -e DASHSCOPE_API_KEY=... \
  codepilot-runner
# open http://localhost:8080 — explorer, editor, and agent all live.
```
