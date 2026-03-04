# CodePilot — Developer Reference

**CodePilot** is a code-native agentic framework for Python. The LLM writes executable code to act — no JSON schemas, no function-calling APIs, no tool wrappers. This document covers every feature with working code examples.

**Version:** `0.4.0`

> **Linux only.** Both the shell tools (`execute`, `read_output`, `send_input`, `send_signal`, `kill_shell`) and `semantic_search` require **Linux**. They rely on `pexpect` and `grepai` — deploy your agent in a Linux container.
>
> **Docker tip:** Pre-install `grepai` in your image so it's not downloaded on every container start:
> ```dockerfile
> RUN curl -sSL https://raw.githubusercontent.com/yoanbernabeu/grepai/main/install.sh | sh
> ```

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
13. [Shell tools](#13-shell-tools)
14. [Workspace change detection](#14-workspace-change-detection)
15. [Chat mode](#15-chat-mode)
16. [Custom tools](#16-custom-tools)
17. [Aborting the agent](#17-aborting-the-agent)
18. [Building a CLI tool](#18-building-a-cli-tool)
19. [Building a web server integration](#19-building-a-web-server-integration)
20. [Full API surface](#20-full-api-surface)

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
# Write the module, then verify it runs
write_file("utils.py")
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

    - name: "execute"
      enabled: true
      config:
        require_permission: true    # true = ask user before every shell command
        max_output_chars: 10000     # truncate long command output

    - name: "read_output"
      enabled: true

    - name: "send_input"
      enabled: true

    - name: "send_signal"
      enabled: true

    - name: "kill_shell"
      enabled: true

    - name: "ask_user"
      enabled: true

    - name: "semantic_search"
      enabled: true
      config:
        # VoyageAI API key env var — REQUIRED for semantic search to work.
        # Get a free key at https://www.voyageai.com/
        api_key_env: "VOYAGE_API_KEY"

        # Embedding model — voyage-code-3 is purpose-built for code search
        model: "voyage-code-3"

        # VoyageAI uses an OpenAI-compatible API — this is the default endpoint
        base_url: "https://api.voyageai.com/v1"

        # Provider name passed to grepai internals (leave as "openai" — it's
        # just the protocol name, not the vendor).
        provider: "openai"

        # Maximum results returned per search (default: 5)
        top_k: 5

        # Max seconds to wait for a grepai command (default: 60)
        timeout: 60

        # Truncate output to prevent context overflow (default: 8000 chars)
        max_output_chars: 8000
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

History is serialised to `~/.codepilot/sessions/<session_id>.json` after every `run()`. Directory is created automatically.

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
def handle_tool_call(tool: str, args: dict, label: str = "", **_):
    """Fires before every tool executes.
    `label` is a human-readable description (e.g. "Running `pytest tests/`").
    Falls back to args dump if label is not set.
    """
    display = label if label else str(args)
    print(f"\n⚙️  [{tool}] {display}")


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
| `TOOL_CALL` | `tool`, `args`, `label` | Before any tool executes |
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

The `execute` tool (and optionally `write_file`) supports `require_permission: true` in the AgentFile. When enabled, a `PERMISSION_REQUEST` hook fires before the tool runs. Return `True` to approve, `False` to deny. Falls back to a CLI `y/N` prompt if no handler is registered.

```python
from codepilot import Runtime, on_permission_request

runtime = Runtime("agent.yaml")


@on_permission_request(runtime)
def gate(tool: str, description: str, **_) -> bool:
    """
    tool        — "write_file" | "execute"
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
    if tool == "execute" and "pytest" in description:
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

### Multi-edit (multiple non-contiguous edits in one file)

Use `mode='multi_edit'` with `edits=[(start1, end1), (start2, end2)]` to fix multiple ranges in one file without line-number drift. The runtime applies edits bottom-to-top automatically. One Payload Block per tuple, in order.

```
```codepilot
# Fix L42-48 (error handling) and L55 (regex) in one step — no drift
write_file("routes/profile.py", mode="multi_edit", edits=[(42, 48), (55, 55)])
```

```python
# ... replacement for L42-48 ...
```

```python
# ... replacement for L55 ...
```
```

### Multiple file reads

Any number of `read_file()` calls per step — no limit.

```python
# LLM control block:
read_file("config.py")
read_file("utils.py")
read_file("tests/test_config.py")
```

---

## 13. Shell Tools

The agent has a **persistent, non-blocking shell session system** powered by pexpect. Commands never hang the agent — output is captured up to a timeout and returned immediately.

> **Linux/macOS only.** pexpect requires POSIX. Deploy in a Linux container.

A default shell session (`"main"`) starts automatically when the Runtime is created. Its PID and status are shown in the agent's system prompt every step.

### execute — run a command

Runs a command, waits up to `timeout` seconds, returns whatever output is available.

```python
# LLM control block:

# status: completed → command finished within timeout (includes return_code)
execute("main", "pytest tests/ -v", 30)

# status: running → timeout hit, process still alive
execute("main", "pip install -r requirements.txt", 10)

# Spin up a server on its own shell, in one step
execute("server", "uvicorn app.main:app --host 0.0.0.0 --port 8000", 4, new_shell=True)
```

### read_output — wait for more output

Called after `execute` returned `status: running`. Waits up to `timeout` seconds for new output.

- **New output available:** returns only the new delta (non-overlapping with previous output).
- **No new output (command already done):** returns the complete accumulated output and collapses previous outputs in the context to save tokens.

```python
# LLM control block:
read_output("main", 30)   # wait up to 30 more seconds
```

### send_input — interact with prompts

Sends text to an interactive command waiting for user input.

```python
# LLM control block:
send_input("main", "yes\n", 5)    # confirm a CLI prompt
send_input("main", "admin\n", 5)  # enter a username
```

### send_signal — interrupt or stop

```python
# Interrupt foreground process (Ctrl+C) — shell survives
send_signal("server", "SIGINT")

# Terminate or kill the shell process entirely
send_signal("server", "SIGTERM")
send_signal("server", "SIGKILL")
```

### kill_shell — destroy a session

```python
kill_shell("server")   # terminates the process, removes the session
```

### Full example: server + test

```python
# Step 1 — LLM control block:
# Start server on its own shell, verify startup logs within 4s
execute("server", "uvicorn app.main:app --port 8000", 4, new_shell=True)

# Step 2 — LLM control block (after seeing server startup logs):
# Run tests against the live server from main shell
execute("main", "pytest tests/test_api.py -v", 30)

# Step 3 — LLM control block (after tests pass):
# Shut server down cleanly
send_signal("server", "SIGINT")
```

### Context deduplication

When `read_output()` returns in full-mode (the command is already done, no new data), it automatically **removes the earlier outputs** for that command from the conversation history and returns one complete, consolidated result. This keeps the agent context lean on long-running tasks.

---

## 14. Workspace Change Detection

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

## 15. Chat Mode

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
Directory: `./workspace` — OS: Linux 5.15 — Time: `2026-03-01 17:00:00`
```

This allows the agent to reason about time-sensitive tasks, deadlines, log timestamps, and how much time has elapsed between steps.

---

## 16. Custom Tools

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
def safe_execute(session_id: str, command: str, timeout: int = 10, new_shell: bool = False):
    """
    Run a shell command. Restricted to read-only operations in this environment.
    Never import subprocess or os directly — always use this tool.
    """
    blocked = ["rm", "del", "format", ">", "sudo", "pip install"]
    if any(cmd in command for cmd in blocked):
        runtime._append_execution(f"[execute] Blocked: '{command}' is not permitted.")
        return
    runtime._shell_manager.execute(session_id, command, timeout, new_shell)


runtime.register_tool("execute", safe_execute, replace=True)
```

---

## 17. Aborting the Agent

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

## 18. Building a CLI Tool

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

## 19. Building a Web Server Integration

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

# Tool activity — label gives a clean human-readable status string
runtime.hooks.register(EventType.TOOL_CALL,
    lambda tool, args, label="", **_: _push({
        "type": "tool_call", "tool": tool,
        "label": label or tool,           # e.g. "Running `pytest tests/`"
    }))

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

## 20. Full API Surface

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
| `'multi_edit'` | `edits=[(s1,e1), (s2,e2)]`. Runtime applies bottom-to-top. | 1 per file per step |

Content always comes from the next payload block — never pass it as a string argument.

#### `read_file(path, start_line=1, end_line=None)`

Returns file content with 1-indexed line numbers. Multiple calls per step are allowed.

#### `execute(session_id, command, timeout=10, new_shell=False)`

Runs a command on a persistent shell session. Returns captured output up to `timeout` seconds.

| Parameter | Description |
|---|---|
| `session_id` | Shell session to use. `"main"` always exists. |
| `command` | Shell command string. |
| `timeout` | Seconds to wait. Output captured on timeout. |
| `new_shell` | `True` = create and use a new shell in one step. |

Result includes `status: completed` (done, has `return_code`) or `status: running` (timed out, process alive).

#### `read_output(session_id, timeout=5)`

Read new output from the latest command. Returns delta (new content only) or full accumulated output if the command is already done. Full-mode collapses previous outputs from context automatically.

#### `send_input(session_id, text, timeout=5)`

Send text to an interactive command waiting for input. Returns new output after sending.

#### `send_signal(session_id, signal='SIGINT')`

Send `SIGINT` (Ctrl+C, shell survives), `SIGTERM`, or `SIGKILL` to the shell session.

#### `kill_shell(session_id)`

Terminate and remove a shell session entirely.

#### `ask_user(question)`

Pauses execution and prompts the user for input. Fires the `ASK_USER` hook.

#### `semantic_search(query, mode='search', depth=2, top_k=5)`

Semanticaly searches the codebase using the `voyage-code-3` embedding model via [grepai](https://github.com/yoanbernabeu/grepai). Finds code by concept — not text match.

**Requires** `VOYAGE_API_KEY` set in environment and `api_key_env: "VOYAGE_API_KEY"` in the AgentFile config.

**First call is slow** (~30-120s): grepai auto-installs if missing, indexes the entire `work_dir`, then searches. Subsequent calls are fast.

| `mode` | What it does |
|---|---|
| `'search'` | Find files/functions matching a natural language concept |
| `'trace_callers'` | Find every place that calls a given function/method |
| `'trace_callees'` | Find everything a function calls internally |
| `'trace_graph'` | Full dependency tree up to `depth` levels — use before modifying code with wide blast radius |

**Environment setup:**

```bash
export VOYAGE_API_KEY="pa-..."
```

**How the API key flows:**
grepai internally reads `OPENAI_API_KEY`. The runtime automatically aliases your `VOYAGE_API_KEY` → `OPENAI_API_KEY` at subprocess launch — you never need to rename your env var.

**grepai index location:** `~/.codepilot/grepai/<hash>/` — entirely outside your project. No `.grepai/` directory is created in your codebase.

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

*CodePilot v0.4.0 — code-native agents, zero JSON, full context.*