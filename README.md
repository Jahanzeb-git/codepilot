# CodePilot — Developer Reference

**CodePilot** is a code-native agentic framework. The LLM writes Python to act — no JSON schemas, no function-calling APIs. This document covers every feature with working code examples.

---

## Installation

```bash
pip install codepilot
```

Required env var before running anything:
```bash
export ANTHROPIC_API_KEY="sk-ant-..."   # or OPENAI_API_KEY, etc.
```

---

## Table of Contents

1. [AgentFile (YAML config)](#1-agentfile)
2. [Basic usage](#2-basic-usage)
3. [Multi-turn execution](#3-multi-turn-execution)
4. [Session persistence](#4-session-persistence)
5. [Resuming a session](#5-resuming-a-session)
6. [Resetting a session](#6-resetting-a-session)
7. [Hooks — full observability](#7-hooks)
8. [Permission gating](#8-permission-gating)
9. [Mid-task message injection](#9-mid-task-message-injection)
10. [Custom tools](#10-custom-tools)
11. [Aborting the agent](#11-aborting-the-agent)
12. [Building a CLI tool](#12-building-a-cli-tool)
13. [Building a web server integration](#13-building-a-web-server-integration)
14. [Full API surface](#14-full-api-surface)

---

## 1. AgentFile

Every Runtime is driven by a YAML config file. Paths in the file are resolved relative to the file's own location — not the caller's CWD — so the agent works correctly when installed as a global CLI tool.

```yaml
# agent.yaml
agent:
  name: "BackendEngineer"
  role: "Expert Python backend engineer specialising in FastAPI and PostgreSQL."

  # Either a raw string or a path to a .md file (resolved relative to this YAML)
  system_prompt: "./prompts/instructions.md"

  model:
    provider: "anthropic"           # "anthropic" | "openai" | "together"
    name: "claude-opus-4-5"
    api_key_env: "ANTHROPIC_API_KEY"
    temperature: 0.2
    max_tokens: 8096

  runtime:
    work_dir: "./workspace"         # where the agent reads/writes files
    max_steps: 30                   # hard cap on agentic steps per run()
    unsafe_mode: false              # true = allow writes outside work_dir
    allowed_imports:                # stdlib modules allowed in the control block
      - "re"
      - "json"
      - "math"
      - "datetime"
      - "pathlib"

  tools:
    - name: "write_file"
      enabled: true
      config:
        require_permission: false   # true = ask user before every file write

    - name: "read_file"
      enabled: true

    - name: "run_command"
      enabled: true
      config:
        timeout: 60                 # seconds before command is killed
        require_permission: true    # true = ask user before every shell command

    - name: "ask_user"
      enabled: true
```

**Supported providers:**

| `provider` | `name` examples | `api_key_env` |
|---|---|---|
| `anthropic` | `claude-opus-4-5`, `claude-sonnet-4-5` | `ANTHROPIC_API_KEY` |
| `openai` | `gpt-4o`, `gpt-4-turbo` | `OPENAI_API_KEY` |
| `together` | `mistralai/Mixtral-8x7B-Instruct-v0.1` | `TOGETHER_API_KEY` |

---

## 2. Basic Usage

```python
from codepilot import Runtime

runtime = Runtime("agent.yaml")
summary = runtime.run("Create a FastAPI hello-world server in main.py")
print(summary)  # what the agent reported in done()
```

`run()` is **blocking** — it returns when the agent calls `done()`, hits `max_steps`, or is aborted. The return value is the summary string passed to `done()`, or `None` if the loop ended for any other reason.

---

## 3. Multi-turn Execution

Call `run()` multiple times on the same `Runtime` instance. Each call appends to the shared conversation history. The LLM sees every prior task, every file it wrote, and every command it ran — so it won't re-create existing files or hallucinate about prior work.

```python
from codepilot import Runtime

runtime = Runtime("agent.yaml")

# Turn 1
runtime.run("Create a FastAPI app with a /items GET endpoint")

# Turn 2 — agent has full context of what it built in turn 1
runtime.run("Now add a POST /items endpoint with Pydantic validation")

# Turn 3 — agent knows the full codebase it has built across both turns
runtime.run("Add pytest tests for both endpoints")
```

**The key point:** these are not isolated calls. The message history grows with each `run()`. The agent in turn 3 has seen everything from turns 1 and 2 — it knows the exact files it created and what's in them.

---

## 4. Session Persistence

Session backends are **independent** — you choose one at construction time.

| Backend | Storage | Survives restart | Config needed |
|---|---|---|---|
| `"memory"` (default) | RAM only | ❌ | None |
| `"file"` | `~/.codepilot/sessions/` | ✅ | `session_id` |

### In-memory (default)

History lives in RAM. Zero I/O, zero config. Ideal for a while-loop CLI where you want continuity within a run but don't need history to survive a process restart.

```python
runtime = Runtime("agent.yaml")                          # memory, id="backendeng..."
runtime = Runtime("agent.yaml", session="memory")       # explicit, same thing
runtime = Runtime("agent.yaml", session="memory", session_id="my-session")
```

### File-backed

History is serialised to `~/.codepilot/sessions/<session_id>.json` after every completed `run()`. On Windows this is `%USERPROFILE%\.codepilot\sessions\`. The directory is created automatically and requires no elevated permissions.

```python
# Session id defaults to the agent name (lowercased, spaces → hyphens)
runtime = Runtime("agent.yaml", session="file")

# Explicit session id — more predictable
runtime = Runtime("agent.yaml", session="file", session_id="ecommerce-api")

# Custom session directory (override default ~/.codepilot/sessions/)
from pathlib import Path
runtime = Runtime(
    "agent.yaml",
    session="file",
    session_id="ecommerce-api",
    session_dir=Path("/data/codepilot-sessions"),
)
```

The session file format:

```json
{
  "session_id": "ecommerce-api",
  "agent_name": "BackendEngineer",
  "created_at": 1712345678.0,
  "updated_at": 1712349999.0,
  "messages": [ ... ]
}
```

---

## 5. Resuming a Session

Pass the same `session_id` to a file-backed Runtime and the previous conversation is automatically loaded. The LLM picks up exactly where it left off.

```python
# Session A — first run (process 1)
runtime = Runtime("agent.yaml", session="file", session_id="ecommerce-api")
runtime.run("Create the products and orders FastAPI endpoints")
# Process exits — session saved to ~/.codepilot/sessions/ecommerce-api.json

# -------- later, new process --------

# Session A — resumed (process 2)
runtime = Runtime("agent.yaml", session="file", session_id="ecommerce-api")
# runtime.messages is already populated with the full prior conversation
runtime.run("Add database migrations using Alembic")
# Agent knows exactly what files it created in the previous session
```

### Listing saved sessions

The `FileSession` backend exposes a `list_sessions()` method for building a session picker in a UI or CLI:

```python
from codepilot import FileSession

fs = FileSession(session_id="_", agent_name="_")   # dummy instance just to call list_sessions
for s in fs.list_sessions():
    print(f"{s['session_id']:30} {s['messages']:4} messages  updated {s['updated_at']}")
```

Or inspect a specific session's metadata without loading all messages:

```python
from codepilot import FileSession

fs = FileSession(session_id="ecommerce-api", agent_name="BackendEngineer")
meta = fs.metadata()
if meta:
    print(f"Session exists. Last updated: {meta['updated_at']}")
    print(f"Saved at: {fs.path}")
else:
    print("No saved session — will start fresh")
```

---

## 6. Resetting a Session

Wipes all history — clears in-memory messages and deletes the file if using the file backend. The next `run()` starts completely fresh.

```python
runtime = Runtime("agent.yaml", session="file", session_id="ecommerce-api")

# ... some runs ...

runtime.reset()  # wipe everything
runtime.run("Start over — build a GraphQL API instead")
```

---

## 7. Hooks

Hooks are the observability system. Every significant runtime event fires a hook. Register handlers to receive them in your application.

All hook decorators replace the built-in default handler (which prints to stdout with emoji). The built-in defaults mean the library is useful out of the box with zero hook configuration.

```python
from codepilot import (
    Runtime,
    on_think,
    on_tool_call,
    on_tool_result,
    on_ask_user,
    on_finish,
    on_user_message_queued,
    on_user_message_injected,
    EventType,
)

runtime = Runtime("agent.yaml")


@on_think(runtime)
def handle_think(message: str, **_):
    """Fires every time the agent calls think("...")."""
    print(f"[Agent] {message}")


@on_tool_call(runtime)
def handle_tool_call(tool: str, args: dict, **_):
    """Fires before every tool executes."""
    print(f"[→ {tool}] {args}")


@on_tool_result(runtime)
def handle_tool_result(tool: str, result: str, **_):
    """Fires after every tool returns."""
    print(f"[← {tool}] {result[:120]}")


@on_ask_user(runtime)
def handle_ask(question: str, **_):
    """Fires when the agent calls ask_user(). Separate from the answer flow."""
    print(f"\n❓ {question}")


@on_finish(runtime)
def handle_finish(summary: str, **_):
    """Fires when the agent calls done()."""
    print(f"\n✅ {summary}")


@on_user_message_queued(runtime)
def handle_queued(message: str, **_):
    """Fires immediately when send_message() is called (not yet in context)."""
    print(f"[Queued] {message}")


@on_user_message_injected(runtime)
def handle_injected(message: str, **_):
    """Fires when a queued message enters the LLM's context window."""
    print(f"[Injected] {message}")


runtime.run("Refactor the database module to use async SQLAlchemy")
```

### Manual hook registration (no decorator)

```python
from codepilot import EventType

runtime.hooks.register(EventType.THINK, lambda message, **_: print(message))
runtime.hooks.register(EventType.FINISH, lambda summary, **_: save_to_db(summary))
```

### Removing the default handler

```python
# Replace default with your own (decorator does this automatically)
runtime.hooks.clear(EventType.THINK)
runtime.hooks.register(EventType.THINK, my_handler)
```

### Full event reference

| Event | Keyword args | When it fires |
|---|---|---|
| `START` | `task` | `run()` is called |
| `STEP` | `step`, `max_steps` | Each agentic step begins |
| `THINK` | `message` | Agent calls `think()` |
| `TOOL_CALL` | `tool`, `args` | Before any tool executes |
| `TOOL_RESULT` | `tool`, `result` | After any tool returns |
| `ASK_USER` | `question` | Agent calls `ask_user()` |
| `PERMISSION_REQUEST` | `tool`, `description` | Tool with `require_permission: true` fires |
| `SECURITY_ERROR` | `error` | AST validation rejects the control block |
| `RUNTIME_ERROR` | `error` | `exec()` throws an exception |
| `FINISH` | `summary` | Agent calls `done()` |
| `MAX_STEPS` | — | Loop exits because `max_steps` was reached |
| `USER_MESSAGE_QUEUED` | `message` | `send_message()` called |
| `USER_MESSAGE_INJECTED` | `message` | Queued message enters LLM context |
| `SESSION_RESET` | — | `reset()` is called |

---

## 8. Permission Gating

Any tool with `require_permission: true` in the AgentFile fires a `PERMISSION_REQUEST` hook before executing. Your handler returns `True` to approve or `False` to deny. If no handler is registered, the runtime falls back to a CLI `y/N` prompt.

```python
from codepilot import Runtime, on_permission_request

runtime = Runtime("agent.yaml")


@on_permission_request(runtime)
def gate(tool: str, description: str, **_) -> bool:
    """
    tool        — "write_file" | "run_command" | "ask_user"
    description — human-readable description of the specific operation
    Return True to approve, False to deny.
    """
    print(f"\n⚠️  [{tool}] {description}")
    return input("Approve? [y/N]: ").strip().lower() in ("y", "yes")


runtime.run("Deploy the application")
```

**Programmatic approval (e.g. in a web app):**

```python
@on_permission_request(runtime)
def auto_approve_reads_deny_writes(tool: str, description: str, **_) -> bool:
    if tool == "read_file":
        return True
    if tool == "run_command" and description.startswith("Execute: python -m pytest"):
        return True
    return False  # deny everything else
```

---

## 9. Mid-task Message Injection

`runtime.run()` is blocking and runs on the calling thread. From any other thread, call `runtime.send_message()` to inject a message into the running agent. The message is:

1. Queued immediately (non-blocking, thread-safe)
2. Tagged `[USER MESSAGE]` — distinct from `[USER INPUT]` (the original task)
3. Injected into the LLM context at the next step boundary
4. The agent is **never** interrupted mid-step

```python
import threading
from codepilot import Runtime, on_think, on_user_message_injected

runtime = Runtime("agent.yaml")


@on_think(runtime)
def display(message: str, **_):
    print(f"Agent: {message}")


@on_user_message_injected(runtime)
def confirmed(message: str, **_):
    print(f"[Your message is now in context]: {message}")


def run_agent():
    runtime.run("Create a utility module with five string helper functions")


agent_thread = threading.Thread(target=run_agent)
agent_thread.start()

# Inject a message while the agent is working
import time
time.sleep(5)
runtime.send_message("Also add type hints to every function")

agent_thread.join()
```

---

## 10. Custom Tools

Register any callable as a tool. Its docstring is automatically pulled into the system prompt so the agent knows when and how to use it.

**Important:** `exec()` discards return values. If your tool produces output the agent should see, you must explicitly call `runtime._append_execution(result)`.

```python
from codepilot import Runtime

runtime = Runtime("agent.yaml")


def web_search(query: str):
    """
    Search the web for current information and return a summary.
    Use for library documentation, recent API changes, error lookups,
    or anything the codebase snapshot can't answer.
    """
    # Your search implementation
    result = my_search_api(query)
    runtime._append_execution(f"[web_search] {result}")


def send_slack(channel: str, message: str):
    """
    Send a message to a Slack channel.
    Use after completing a task to notify the team.
    channel should be the channel name without #, e.g. 'deployments'.
    """
    slack_client.chat_postMessage(channel=f"#{channel}", text=message)
    runtime._append_execution(f"[send_slack] Message sent to #{channel}.")


runtime.register_tool("web_search", web_search)
runtime.register_tool("send_slack", send_slack)

runtime.run("Research the latest SQLAlchemy 2.0 async API and implement a connection pool")
```

### Overriding a built-in tool

```python
def safe_run_command(command: str, timeout: int = None, background: bool = False):
    """
    Run a shell command. Restricted to read-only operations in this environment.
    Never import subprocess or os directly — always use this tool.
    """
    if any(cmd in command for cmd in ["rm", "del", "format", ">", "sudo"]):
        runtime._append_execution(f"[run_command] Blocked: '{command}' is not permitted.")
        return
    # call original or implement your own
    runtime._shell_tools.run_command(command, timeout=timeout, background=background)


runtime.register_tool("run_command", safe_run_command, replace=True)
```

---

## 11. Aborting the Agent

```python
import threading

runtime = Runtime("agent.yaml")

agent_thread = threading.Thread(
    target=runtime.run,
    args=("Build a complete e-commerce backend",)
)
agent_thread.start()

# From anywhere — stops after the current step completes (never mid-step)
runtime.abort()
agent_thread.join()
```

---

## 12. Building a CLI Tool

The recommended pattern for a conversational CLI — in-memory session, while-loop, `reset` command:

```python
import sys
from codepilot import Runtime, on_think, on_finish, on_ask_user, EventType

runtime = Runtime("agent.yaml", session="memory")


@on_think(runtime)
def show_thinking(message: str, **_):
    print(f"\n  💭 {message}")


@on_finish(runtime)
def show_done(summary: str, **_):
    print(f"\n✅ {summary}\n")


@on_ask_user(runtime)
def show_question(question: str, **_):
    print(f"\n❓ {question}")


print("CodePilot CLI — type 'reset' to clear history, 'quit' to exit.\n")

while True:
    try:
        task = input("You: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nGoodbye.")
        sys.exit(0)

    if not task:
        continue

    if task.lower() == "quit":
        sys.exit(0)

    if task.lower() == "reset":
        runtime.reset()
        print("History cleared. Starting fresh.\n")
        continue

    runtime.run(task)
```

### File-backed CLI (survives restarts, named sessions)

```python
import sys
import argparse
from codepilot import Runtime, FileSession, on_think, on_finish

parser = argparse.ArgumentParser()
parser.add_argument("--session", default=None, help="Session ID to resume")
parser.add_argument("--list", action="store_true", help="List saved sessions")
args = parser.parse_args()

if args.list:
    fs = FileSession(session_id="_", agent_name="_")
    sessions = fs.list_sessions()
    if not sessions:
        print("No saved sessions.")
    for s in sessions:
        print(f"  {s['session_id']:30} {s['messages']:4} messages")
    sys.exit(0)

session_id = args.session or "default"
runtime = Runtime("agent.yaml", session="file", session_id=session_id)

# Inform user if resuming
fs = FileSession(session_id=session_id, agent_name="")
if fs.exists():
    meta = fs.metadata()
    print(f"Resuming session '{session_id}' ({len(runtime.messages)} messages in history)\n")
else:
    print(f"Starting new session '{session_id}'\n")


@on_think(runtime)
def thinking(message: str, **_):
    print(f"  💭 {message}")


@on_finish(runtime)
def done(summary: str, **_):
    print(f"\n✅ {summary}\n")


while True:
    try:
        task = input("You: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nSession saved. Goodbye.")
        sys.exit(0)

    if not task:
        continue
    if task.lower() in ("reset", "clear"):
        runtime.reset()
        print("Session cleared.\n")
        continue
    if task.lower() in ("quit", "exit"):
        sys.exit(0)

    runtime.run(task)
```

```bash
# Usage:
python cli.py                              # new default session
python cli.py --session ecommerce-api      # resume named session
python cli.py --list                       # show all saved sessions
```

---

## 13. Building a Web Server Integration

FastAPI example with WebSocket streaming of hook events and a mid-task injection endpoint:

```python
import asyncio
import threading
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from codepilot import Runtime, EventType

app = FastAPI()

# One runtime per session (in production: store in a session map keyed by session_id)
runtime = Runtime("agent.yaml", session="file", session_id="web-session")

# Bridge between sync hooks and async WebSocket
_event_queue: asyncio.Queue = asyncio.Queue()


def _push(event: dict):
    """Thread-safe push from sync hook into async queue."""
    asyncio.get_event_loop().call_soon_threadsafe(_event_queue.put_nowait, event)


runtime.hooks.register(EventType.THINK,
    lambda message, **_: _push({"type": "think", "message": message}))

runtime.hooks.register(EventType.TOOL_CALL,
    lambda tool, args, **_: _push({"type": "tool_call", "tool": tool, "args": args}))

runtime.hooks.register(EventType.TOOL_RESULT,
    lambda tool, result, **_: _push({"type": "tool_result", "tool": tool, "result": result[:300]}))

runtime.hooks.register(EventType.FINISH,
    lambda summary, **_: _push({"type": "finish", "summary": summary}))

runtime.hooks.register(EventType.RUNTIME_ERROR,
    lambda error, **_: _push({"type": "error", "error": error}))


@app.post("/run")
def start_task(task: str):
    """Start a new task. Non-blocking — agent runs in background thread."""
    threading.Thread(target=runtime.run, args=(task,), daemon=True).start()
    return {"status": "started"}


@app.post("/message")
def inject_message(message: str):
    """Inject a mid-task message. Returns immediately."""
    runtime.send_message(message)
    return {"status": "queued"}


@app.post("/reset")
def reset_session():
    """Wipe conversation history and start fresh."""
    runtime.reset()
    return {"status": "reset"}


@app.websocket("/events")
async def stream_events(websocket: WebSocket):
    """Stream all hook events to the frontend as JSON."""
    await websocket.accept()
    try:
        while True:
            event = await _event_queue.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
```

---

## 14. Full API Surface

### `Runtime`

```python
Runtime(
    agent_file: str,              # path to agent.yaml
    session: str = "memory",      # "memory" | "file"
    session_id: str = None,       # defaults to agent name, slugified
    session_dir: Path = None,     # override ~/.codepilot/sessions/
)

runtime.run(task: str) -> Optional[str]
    # Blocking. Appends to history. Returns done() summary or None.

runtime.send_message(message: str)
    # Thread-safe. Non-blocking. Tagged [USER MESSAGE] in context.

runtime.reset()
    # Wipes messages + session file. Next run() is a blank slate.

runtime.abort()
    # Sets abort flag. Loop stops after current step.

runtime.register_tool(name: str, func: callable, replace: bool = False)
    # Add custom tool. Docstring injected into system prompt automatically.

runtime.messages           # List[Dict] — full conversation history
runtime.session            # BaseSession — current session backend instance
runtime.hooks              # HookSystem — register/emit events manually
runtime.registry           # ToolRegistry — inspect registered tools
```

### `FileSession`

```python
FileSession(session_id, agent_name, session_dir=None)

.load() -> List[Dict]        # load messages from disk
.save(messages)              # persist messages to disk (atomic write)
.reset()                     # delete session file
.exists() -> bool            # True if file exists on disk
.metadata() -> Optional[Dict]  # session metadata without messages
.list_sessions() -> List[Dict] # all sessions in the session directory
.path -> Path                # full path to the session file
.session_id -> str
```

### `InMemorySession`

```python
InMemorySession(session_id="default")

.load() -> List[Dict]
.save(messages)
.reset()
.session_id -> str
```

### `create_session`

```python
create_session(
    backend: str = "memory",     # "memory" | "file"
    session_id: str = "default",
    agent_name: str = "agent",
    session_dir: Path = None,
) -> BaseSession
```

---

*CodePilot — code-native agents, zero JSON, full context.*