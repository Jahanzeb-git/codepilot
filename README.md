# CodePilot — Developer Reference

**CodePilot** is a code-native agentic framework for Python. The LLM writes executable code to act — no JSON schemas, no function-calling APIs, no tool wrappers. This document covers every feature with working code examples.

**Version:** `0.3.0`

---

## Installation

```bash
pip install codepilot-ai
```

Set your LLM provider key before running anything:

```bash
# Pick one
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
export TOGETHER_API_KEY="..."
```

---

## Table of Contents

1. [How it works](#1-how-it-works)
2. [AgentFile (YAML config)](#2-agentfile)
3. [Basic usage](#3-basic-usage)
4. [Streaming](#4-streaming)
5. [Multi-turn execution](#5-multi-turn-execution)
6. [Session persistence](#6-session-persistence)
7. [Resuming a session](#7-resuming-a-session)
8. [Resetting a session](#8-resetting-a-session)
9. [Hooks — full observability](#9-hooks)
10. [Permission gating](#10-permission-gating)
11. [Mid-task message injection](#11-mid-task-message-injection)
12. [Multi-operation steps](#12-multi-operation-steps)
13. [Parallel command execution](#13-parallel-command-execution)
14. [Background commands with timeout](#14-background-commands-with-timeout)
15. [Workspace change detection](#15-workspace-change-detection)
16. [Chat mode](#16-chat-mode)
17. [Custom tools](#17-custom-tools)
18. [Aborting the agent](#18-aborting-the-agent)
19. [Building a CLI tool](#19-building-a-cli-tool)
20. [Building a web server integration](#20-building-a-web-server-integration)
21. [Full API surface](#21-full-api-surface)

---

## 1. How It Works

CodePilot uses a **code-as-interface** paradigm. Instead of the LLM describing actions in JSON, it writes Python code that the runtime executes directly.

Each agent step:

1. **LLM receives** the system prompt + full conversation history
2. **LLM writes** a natural language reasoning paragraph (streamed to user), then a ` ```codepilot ` block (Python code)
3. **Runtime executes** the code block in a sandboxed environment with bound tool functions
4. **Execution result** is appended to conversation history as `[EXECUTION RESULT]`
5. **Repeat** until the agent calls `done()`, hits `max_steps`, or is aborted

### The `codepilot` fence

The ` ```codepilot ` fence is the **only** block the runtime executes. Regular ` ```python ` blocks are display-only markdown — the agent can freely use them for code examples in explanations without risk of execution.

**LLM output for an action:**
````
Alright, I'll create the file and verify it runs.

```codepilot
# Write the module, then immediately verify it imports
write_file("utils.py")
run_command("python -c 'import utils; print(utils.slugify(\"Hello World\"))'")
```

```python
def slugify(text: str) -> str:
    return text.lower().replace(" ", "-")
```
````

**LLM output for a chat/explanation (no execution):**
````
Sure! Here's how the `slugify` function works:

```python
# This is a display block — never executed
def slugify(text: str) -> str:
    return text.lower().replace(" ", "-")
```

Each space is replaced with a hyphen, and the string is lowercased.
````

---

## 2. AgentFile

Every Runtime is driven by a YAML config. Paths are resolved relative to the YAML file's location — not the caller's CWD.

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
        timeout: 60                 # default timeout in seconds
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

## 3. Basic Usage

```python
from codepilot import Runtime

runtime = Runtime("agent.yaml")
summary = runtime.run("Create a FastAPI hello-world server in main.py")
print(summary)  # the string the agent passed to done()
```

`run()` is **blocking** — it returns when the agent calls `done()`, hits `max_steps`, or is aborted. The return value is the `done()` summary string, or `None` if the loop ended for any other reason.

---

## 4. Streaming

Enable streaming to receive the agent's reasoning text token-by-token, in real time, *before* any code executes. This dramatically improves perceived responsiveness.

```python
from codepilot import Runtime, on_stream

runtime = Runtime("agent.yaml", stream=True)


@on_stream(runtime)
def handle_stream(text: str, **_):
    """Fires with each chunk of pre-execution reasoning text."""
    print(text, end="", flush=True)


runtime.run("Refactor the database module to use async SQLAlchemy")
```

### What gets streamed

The runtime streams **everything before the `codepilot` block** — this includes the agent's reasoning paragraph and any display ` ```python ` code blocks it uses in explanations. Once the ` ```codepilot ` fence is detected, streaming pauses while the code block is executed.

For chat/question responses (no `codepilot` block), the **entire response** streams to the user.

### Non-streaming mode

Without `stream=True`, the full response is emitted as a single `STREAM` event when inference completes. The `on_stream` hook still fires — you see the complete reasoning text at once rather than token-by-token.

```python
runtime = Runtime("agent.yaml")   # stream=False by default

@on_stream(runtime)
def show_reasoning(text: str, **_):
    print(f"\n{text}\n")
```

---

## 5. Multi-turn Execution

Call `run()` multiple times on the same `Runtime` instance. Each call appends to the shared conversation history. The LLM sees every prior task, every file it wrote, and every command it ran.

```python
from codepilot import Runtime

runtime = Runtime("agent.yaml")

# Turn 1
runtime.run("Create a FastAPI app with a /items GET endpoint")

# Turn 2 — agent has full context of what it built in turn 1
runtime.run("Now add a POST /items endpoint with Pydantic validation")

# Turn 3 — agent knows the full codebase it has built
runtime.run("Add pytest tests for both endpoints")
```

---

## 6. Session Persistence

Session backends are chosen at construction time.

| Backend | Storage | Survives restart | Config needed |
|---|---|---|---|
| `"memory"` (default) | RAM only | ❌ | None |
| `"file"` | `~/.codepilot/sessions/` | ✅ | `session_id` |

### In-memory (default)

```python
runtime = Runtime("agent.yaml")                          # memory, id = agent name
runtime = Runtime("agent.yaml", session="memory")       # explicit, same thing
runtime = Runtime("agent.yaml", session="memory", session_id="my-session")
```

### File-backed

History is serialised to `~/.codepilot/sessions/<session_id>.json` after every `run()`. On Windows: `%USERPROFILE%\.codepilot\sessions\`. Directory is created automatically.

```python
runtime = Runtime("agent.yaml", session="file")                     # id = agent name
runtime = Runtime("agent.yaml", session="file", session_id="ecommerce-api")

# Custom session directory
from pathlib import Path
runtime = Runtime(
    "agent.yaml",
    session="file",
    session_id="ecommerce-api",
    session_dir=Path("/data/codepilot-sessions"),
)
```

Session file format:

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

## 7. Resuming a Session

Pass the same `session_id` to a new file-backed Runtime and the prior conversation loads automatically.

```python
# Process 1
runtime = Runtime("agent.yaml", session="file", session_id="ecommerce-api")
runtime.run("Create the products and orders FastAPI endpoints")
# Process exits — session saved

# -------- later, new process --------

# Process 2 — picks up exactly where process 1 left off
runtime = Runtime("agent.yaml", session="file", session_id="ecommerce-api")
runtime.run("Add database migrations using Alembic")
```

### Listing saved sessions

```python
from codepilot import FileSession

fs = FileSession(session_id="_", agent_name="_")
for s in fs.list_sessions():
    print(f"{s['session_id']:30} {s['messages']:4} messages  updated {s['updated_at']}")
```

### Inspecting a session without loading messages

```python
from codepilot import FileSession

fs = FileSession(session_id="ecommerce-api", agent_name="BackendEngineer")
meta = fs.metadata()
if meta:
    print(f"Last updated: {meta['updated_at']}")
    print(f"File path: {fs.path}")
else:
    print("No saved session — will start fresh")
```

---

## 8. Resetting a Session

Wipes all history and deletes the session file (if file-backed). The next `run()` starts completely fresh.

```python
runtime = Runtime("agent.yaml", session="file", session_id="ecommerce-api")

# ... some runs ...

runtime.reset()
runtime.run("Start over — build a GraphQL API instead")
```

---

## 9. Hooks

Hooks are the observability system. Every significant runtime event fires a hook. Register handlers to receive them in your application.

All built-in decorators replace the default stdout handler. The defaults work out of the box with zero configuration.

```python
from codepilot import (
    Runtime,
    on_stream,
    on_tool_call,
    on_tool_result,
    on_ask_user,
    on_finish,
    on_user_message_queued,
    on_user_message_injected,
    EventType,
)

runtime = Runtime("agent.yaml", stream=True)


@on_stream(runtime)
def handle_stream(text: str, **_):
    """Fires with each chunk of the agent's reasoning text (before code executes)."""
    print(text, end="", flush=True)


@on_tool_call(runtime)
def handle_tool_call(tool: str, args: dict, **_):
    """Fires before every tool executes."""
    print(f"\n⚙️  [{tool}] {args}")


@on_tool_result(runtime)
def handle_tool_result(tool: str, result: str, **_):
    """Fires after every tool returns."""
    print(f"   ↳ {result[:200]}")


@on_ask_user(runtime)
def handle_ask(question: str, **_):
    """Fires when the agent calls ask_user()."""
    print(f"\n❓ {question}")


@on_finish(runtime)
def handle_finish(summary: str, **_):
    """Fires when the agent calls done()."""
    print(f"\n✅ {summary}\n")


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

### Manual hook registration

```python
from codepilot import EventType

runtime.hooks.register(EventType.STREAM,  lambda text, **_: print(text, end="", flush=True))
runtime.hooks.register(EventType.FINISH,  lambda summary, **_: save_to_db(summary))
```

### Full event reference

| Event | Keyword args | When it fires |
|---|---|---|
| `START` | `task` | `run()` is called |
| `STEP` | `step`, `max_steps` | Each agentic step begins |
| `STREAM` | `text` | Chunk of pre-execution reasoning text |
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
| `SESSION_RESET` | — | `reset()` called |

---

## 10. Permission Gating

Any tool with `require_permission: true` fires a `PERMISSION_REQUEST` hook before executing. Return `True` to approve, `False` to deny. Falls back to a CLI `y/N` prompt if no handler is registered.

```python
from codepilot import Runtime, on_permission_request

runtime = Runtime("agent.yaml")


@on_permission_request(runtime)
def gate(tool: str, description: str, **_) -> bool:
    """
    tool        — "write_file" | "run_command"
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
def auto_gate(tool: str, description: str, **_) -> bool:
    if tool == "read_file":
        return True
    if tool == "run_command" and "pytest" in description:
        return True
    return False   # deny everything else
```

---

## 11. Mid-task Message Injection

`runtime.run()` is blocking and runs on the calling thread. From any other thread, call `runtime.send_message()` to inject a message into the running agent.

1. Queued immediately (non-blocking, thread-safe)
2. Tagged `[USER MESSAGE]` — distinct from `[USER INPUT]` (the original task)
3. Injected into the LLM context at the **next step boundary** — never mid-step

```python
import threading
import time
from codepilot import Runtime, on_stream, on_user_message_injected

runtime = Runtime("agent.yaml", stream=True)


@on_stream(runtime)
def show(text: str, **_):
    print(text, end="", flush=True)


@on_user_message_injected(runtime)
def confirmed(message: str, **_):
    print(f"\n[Your message is now in context]: {message}")


def run_agent():
    runtime.run("Create a utility module with five string helper functions")


agent_thread = threading.Thread(target=run_agent)
agent_thread.start()

time.sleep(5)
runtime.send_message("Also add type hints to every function")

agent_thread.join()
```

---

## 12. Multi-operation Steps

The agent can perform multiple file operations in a single step, reducing round-trips and improving efficiency.

### Multiple file writes

Up to **5 `write_file()` calls** with `mode='w'` or `mode='a'` per step. Each call consumes the next payload block in order.

**LLM output (writes two files in one step):**
````
Alright, both files are independent so I'll write them together.

```codepilot
# Two new files — order of write_file() matches order of payload blocks below.
write_file("config.py")
write_file("utils.py")
```

```python
import json, os

def load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)
```

```python
def slugify(text: str) -> str:
    return text.lower().replace(" ", "-")
```
````

**Limitation:** Only **one edit** (`mode='edit'` or `mode='insert'`) is allowed per step to avoid line-number conflicts. After an edit, the file's line numbers shift and subsequent edits would target wrong lines.

### Multiple file reads

Any number of `read_file()` calls per step — no limit.

```python
# LLM control block:
read_file("config.py")
read_file("utils.py")
read_file("tests/test_config.py")
```

---

## 13. Parallel Command Execution

Run independent shell commands simultaneously using `execution="parallel"`. Parallel commands are queued during the control block and launched concurrently after the block finishes executing.

```python
# LLM control block — these run at the same time:
run_command("pytest tests/test_config.py -v", execution="parallel")
run_command("pytest tests/test_utils.py -v", execution="parallel")
run_command("mypy src/ --strict", execution="parallel")
```

The default is `execution="inline"` which runs commands sequentially, one after another.

```python
# Sequential — waits for each before starting the next:
run_command("pip install -r requirements.txt")           # inline (default)
run_command("python -m pytest tests/ -v")               # inline
```

Mix inline and parallel in the same step — inline commands run first (sequentially), parallel commands run after all inline commands finish:

```python
run_command("pip install -r requirements.txt")          # inline — runs first
run_command("pytest tests/unit/ -v", execution="parallel")    # parallel batch
run_command("pytest tests/integration/ -v", execution="parallel")
```

---

## 14. Background Commands with Timeout

Use `background=True` to run a long-running process without blocking the entire step. Add a `timeout` to have the runtime wait for completion — if the process finishes within the timeout, you get the full output; if it's still running, you get its PID and execution continues.

```python
# Wait up to 30 seconds for the dev server to start, then move on.
# If it finishes in time → full output returned.
# If still running → "still running, PID: 1234" — process stays alive.
run_command("uvicorn main:app --reload", background=True, timeout=30)
```

```python
# Fire and forget — no wait, just a PID back immediately.
run_command("npm run build:watch", background=True)
```

This eliminates the common pattern where agents loop and waste inference steps polling for a background process to complete.

---

## 15. Workspace Change Detection

The runtime automatically detects when **you** modify files in the workspace between agent steps. If you edit a file while the agent is working, it will be notified at the start of the next step with exact line numbers of what changed.

**What the agent sees in its context:**

```
[ENVIRONMENT CHANGE] 2026-02-21 16:30:12

📝 Modified: main.py
  Changed lines: 1-4, 47
📄 Created: .env (3 lines)
🗑️ Deleted: old_config.py
```

The agent is then instructed to re-read affected files before editing — because its cached line numbers may be wrong.

**How it works:**

- Tracking is **opt-in by file** — only files the agent has touched (read or written) are watched
- Detection is **snapshot-based** — no background daemon, no file watchers, zero overhead between steps
- Snapshots are taken at the end of each step and compared at the start of the next
- Diff limits: 30 changed lines reported per file, 100 total across all files

No configuration is required — this is always on.

---

## 16. Chat Mode

The agent can respond to questions and explanations without executing any code. If the LLM produces a response with no ` ```codepilot ` block, the runtime treats it as a conversational reply: the response is fully streamed to the user and the loop exits cleanly.

```python
runtime = Runtime("agent.yaml", stream=True)

@on_stream(runtime)
def show(text: str, **_):
    print(text, end="", flush=True)


@on_finish(runtime)
def done(summary: str, **_):
    print(f"\n✅ {summary}")


# Agent answers with natural markdown — no code executed
runtime.run("How does the config loader handle missing files?")

# Agent takes action — executes code
runtime.run("Add a fallback default value to the config loader")
```

The agent freely uses ` ```python ` blocks to display code examples in its explanations — they are **never** executed. Only ` ```codepilot ` blocks execute.

### Internal clock

The agent's system prompt is refreshed every step with the current timestamp:

```
Directory: `./workspace` — OS: Windows 10 — Time: `2026-02-21 16:30:12`
```

This allows the agent to reason about time-sensitive tasks, deadlines, log timestamps, and how much time has elapsed between steps.

---

## 17. Custom Tools

Register any callable as a tool. Its docstring is automatically pulled into the system prompt so the agent knows when and how to use it.

**Important:** `exec()` discards return values. If your tool produces output the agent should see, explicitly call `runtime._append_execution(result)`.

```python
from codepilot import Runtime

runtime = Runtime("agent.yaml")


def web_search(query: str):
    """
    Search the web for current information and return a summary.
    Use for library documentation, recent API changes, error lookups,
    or anything the codebase snapshot can't answer.
    """
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
def safe_run_command(command: str, timeout: int = None, background: bool = False, execution: str = "inline"):
    """
    Run a shell command. Restricted to read-only operations in this environment.
    Never import subprocess or os directly — always use this tool.
    """
    if any(cmd in command for cmd in ["rm", "del", "format", ">", "sudo"]):
        runtime._append_execution(f"[run_command] Blocked: '{command}' is not permitted.")
        return
    runtime._shell_tools.run_command(command, timeout=timeout, background=background, execution=execution)


runtime.register_tool("run_command", safe_run_command, replace=True)
```

---

## 18. Aborting the Agent

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

## 19. Building a CLI Tool

### Simple conversational CLI

```python
import sys
from codepilot import Runtime, on_stream, on_finish, on_ask_user

runtime = Runtime("agent.yaml", session="memory", stream=True)


@on_stream(runtime)
def show_stream(text: str, **_):
    print(text, end="", flush=True)


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

### File-backed CLI with named sessions

```python
import sys
import argparse
from codepilot import Runtime, FileSession, on_stream, on_finish

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
runtime = Runtime("agent.yaml", session="file", session_id=session_id, stream=True)

fs = FileSession(session_id=session_id, agent_name="")
if fs.exists():
    print(f"Resuming session '{session_id}' ({len(runtime.messages)} messages)\n")
else:
    print(f"Starting new session '{session_id}'\n")


@on_stream(runtime)
def streaming(text: str, **_):
    print(text, end="", flush=True)


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
python cli.py                              # new default session
python cli.py --session ecommerce-api      # resume named session
python cli.py --list                       # show all saved sessions
```

---

## 20. Building a Web Server Integration

FastAPI example with WebSocket streaming (token-by-token to the browser) and mid-task injection:

```python
import asyncio
import threading
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from codepilot import Runtime, EventType

app = FastAPI()

runtime = Runtime("agent.yaml", session="file", session_id="web-session", stream=True)

# Bridge between sync hooks and async WebSocket
_event_queue: asyncio.Queue = asyncio.Queue()


def _push(event: dict):
    """Thread-safe push from sync hook into async queue."""
    asyncio.get_event_loop().call_soon_threadsafe(_event_queue.put_nowait, event)


# Stream reasoning text token by token
runtime.hooks.register(EventType.STREAM,
    lambda text, **_: _push({"type": "stream", "text": text}))

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

## 21. Full API Surface

### `Runtime`

```python
Runtime(
    agent_file: str,              # path to agent.yaml
    session: str = "memory",      # "memory" | "file"
    session_id: str = None,       # defaults to agent name, slugified
    session_dir: Path = None,     # override ~/.codepilot/sessions/
    stream: bool = False,         # True = token-by-token streaming
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

### Hook decorators

```python
from codepilot import (
    on_stream,                  # STREAM — pre-execution reasoning text chunk
    on_tool_call,               # TOOL_CALL — before any tool executes
    on_tool_result,             # TOOL_RESULT — after any tool returns
    on_ask_user,                # ASK_USER — agent called ask_user()
    on_finish,                  # FINISH — agent called done()
    on_permission_request,      # PERMISSION_REQUEST — awaiting approval
    on_user_message_queued,     # USER_MESSAGE_QUEUED — send_message() called
    on_user_message_injected,   # USER_MESSAGE_INJECTED — message in context
)
```

### Built-in tools

#### `write_file(path, start_line=None, end_line=None, after_line=None, mode='w', edits=None)`

| `mode` | Behaviour | Limit |
|---|---|---|
| `'w'` | Create or overwrite the whole file | 5 per step |
| `'a'` | Append to end of file | 5 per step (shared with `'w'`) |
| `'edit'` | Replace lines `start_line` to `end_line` | 1 per file per step |
| `'insert'` | Insert after `after_line` (`0` = top of file) | 1 per file per step |
| `'multi_edit'` | Provide `edits=[(s1, e1), (s2, e2)]`. Bottom-up replacement. | 1 per file per step |

Content always comes from the next payload block — never pass it as a string argument.

#### `read_file(path, start_line=1, end_line=None)`

Returns file content with 1-indexed line numbers. Multiple calls per step are allowed.

#### `run_command(command, timeout=None, background=False, execution='inline')`

| Parameter | Values | Behaviour |
|---|---|---|
| `execution` | `"inline"` (default) | Runs immediately, blocks until done |
| `execution` | `"parallel"` | Queued; all parallel commands launched concurrently after the block |
| `background` | `True` + `timeout=N` | Waits up to N seconds; returns partial output if still running (does not kill) |
| `background` | `True` (no timeout) | Fire and forget; returns PID immediately |

#### `ask_user(question)`

Pauses execution and prompts the user for input. Fires the `ASK_USER` hook.

#### `done(summary)`

Marks the task complete. Fires the `FINISH` hook. `summary` should be a thorough, human-readable description of everything that was accomplished.

### `FileSession`

```python
FileSession(session_id, agent_name, session_dir=None)

.load() -> List[Dict]          # load messages from disk
.save(messages)                # persist messages to disk (atomic write)
.reset()                       # delete session file
.exists() -> bool              # True if file exists on disk
.metadata() -> Optional[Dict]  # session metadata without messages
.list_sessions() -> List[Dict] # all sessions in the session directory
.path -> Path                  # full path to the session file
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

*CodePilot v0.3.0 — code-native agents, zero JSON, full context.*