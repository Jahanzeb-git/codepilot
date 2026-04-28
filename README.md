<div align="center">

<img src="assets/codepilot.png" alt="CodePilot logo" width="720" />

### Embeddable Autonomous Agent Framework for Software Engineering

[![PyPI version](https://img.shields.io/pypi/v/codepilot-ai)](https://pypi.org/project/codepilot-ai/)
[![Python](https://img.shields.io/pypi/pyversions/codepilot-ai)](https://pypi.org/project/codepilot-ai/)
[![License](https://img.shields.io/badge/license-MIT-black)](LICENSE)
[![GitHub](https://img.shields.io/badge/GitHub-codepilot-black?logo=github)](https://github.com/Jahanzeb-git/codepilot)

**Embeddable Autonomous Agent (EAA)** • **Code-as-Interface Runtime** • **Python Library** • **MIT Licensed**

```bash
pip install codepilot-ai
```
</div>

**CodePilot** is an **Embeddable Autonomous Agent (EAA)** framework for software engineering tasks.
It is distributed as a Python library so you can embed an autonomous agent directly into your own systems: DevOps pipelines, web backends, internal tools, CLI workflows, and other software components.

Instead of forcing the model through brittle JSON schemas or generic function-calling wrappers, CodePilot uses a **code-as-interface** runtime: the model streams natural language to the user, writes executable Python in a `codepilot` block, side-loads file payloads when needed, and explicitly terminates with a `completion` block.

**What CodePilot is not:** not a chatbot UI, not a generic "AI agent" wrapper, and not another hosted coding assistant. It is a library-first runtime for embedding autonomous software agents into your own application stack.

**Version:** `0.8.9`

> **Cross-Platform:** CodePilot runs on **Linux**, **macOS**, and **Windows 10 1809+** (ConPTY required). Linux and macOS use `pexpect` for PTY management; Windows uses `pywinpty`. All terminal tools — including TUI applications, interactive REPLs, and raw control sequences (`Ctrl+C`, `Ctrl+D`, arrow keys) — work identically across all three platforms.

---

## Getting Started

Install the library:

```bash
pip install codepilot-ai
```

Set your LLM provider key before running anything:

```bash
# Pick one
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
export DASHSCOPE_API_KEY="..."
```

Create an `agent.yaml`:

```yaml
agent:
  name: "CodePilot"
  role: "Autonomous software engineering agent."

  model:
    provider: "anthropic"
    name: "claude-sonnet-4-5"
    api_key_env: "ANTHROPIC_API_KEY"

  runtime:
    work_dir: "./workspace"
    max_steps: 20

  tools:
    - name: "read_file"
      enabled: true
    - name: "write_file"
      enabled: true
    - name: "execute"
      enabled: true
    - name: "read_output"
      enabled: true
```

Run it synchronously:

```python
from codepilot import Runtime

runtime = Runtime("agent.yaml")
summary = runtime.run("Fix the nginx config")
print(summary)
```

Run it asynchronously:

```python
import asyncio

from codepilot import AsyncRuntime

runtime = AsyncRuntime("agent.yaml")

async def main():
    summary = await runtime.run("Fix the nginx config")
    print(summary)

if __name__ == "__main__":
    asyncio.run(main())
```

If you want the full config surface and runtime behavior details, keep reading.

---

## Table of Contents

1. [How it works](#1-how-it-works)
2. [AgentFile (YAML config)](#2-agentfile)
3. [Basic usage](#3-basic-usage)
4. [Streaming](#4-streaming)
5. [Multi-turn execution](#5-multi-turn-execution)
6. [Session persistence](#6-session-persistence)
7. [Context memory management](#7-context-memory-management)
8. [Resuming a session](#8-resuming-a-session)
9. [Resetting a session](#9-resetting-a-session)
10. [Hooks â€” full observability](#10-hooks)
11. [Permission gating](#11-permission-gating)
12. [Mid-task message injection](#12-mid-task-message-injection)
13. [Multi-operation steps](#13-multi-operation-steps)
14. [Shell tools](#14-shell-tools)
15. [Completion block](#15-completion-block)
16. [Workspace change detection](#16-workspace-change-detection)
17. [Chat mode](#17-chat-mode)
18. [Custom tools](#18-custom-tools)
19. [Aborting the agent](#19-aborting-the-agent)
20. [Building a CLI tool](#20-building-a-cli-tool)
21. [Building a web server integration](#21-building-a-web-server-integration)
22. [Full API surface](#22-full-api-surface)

---

## 1. How It Works

CodePilot uses a **code-as-interface** paradigm. Instead of the LLM describing actions in JSON, it writes Python code that the runtime executes directly.

Each agent step:

1. **LLM receives** the system prompt (refreshed every step) + full conversation history
2. **LLM writes** a natural language reasoning paragraph (streamed to user in real time), then a ` ```codepilot ` block (Python code)
3. **Runtime executes** the code block in a sandboxed environment with bound tool functions
4. **Execution result** is appended to conversation history as `[EXECUTION RESULT]`
5. **Repeat** until the agent emits a ` ```completion ` block, hits `max_steps`, or is aborted

### The three block types

**Control Block** (` ```codepilot `) the only block the runtime executes. Regular ` ```python ` blocks are display-only markdown the agent uses freely in explanations.

**Payload Blocks** (` ```python `, ` ```js `, etc. after a codepilot block) file content consumed by `write_file()` in order. Never executed.

**Completion Block** (` ```completion `) natural text that streams directly to the user in real time. Its presence marks the task complete andthe agentic loop terminates after this step. Can be combined with the codepilot block and payload blocks in a single agentic step.

### Response shapes

**Action step (more work needed):**
````
Alright, let me read the file first to get the line numbers.

```codepilot
# Reading before editing â€” exact line numbers required.
read_file("routes/profile.py", start_line=35, end_line=65)
```
````

**Single-step task (action + completion in one step):**
````
Got it updating the timeout value.

```codepilot
# Simple single-line edit, no read needed we know the line.
write_file("config.py", start_line=12, end_line=12, mode="edit")
```

```python
TIMEOUT = 30
```

```completion
Done. Updated TIMEOUT to 30s in config.py on line 12.
```
````

**Chat/explanation (no execution, entire response streams):**
````
Sure! Here's how the config loader handles missing files:

```python
# Display block never executed
def load(path: str) -> dict:
    if not os.path.exists(path):
        return {}   # returns empty dict as default
    with open(path) as f:
        return json.load(f)
```

The fallback is an empty dict, so callers always get a valid dict no None checks needed.
````

---

## 2. AgentFile

Every Runtime is driven by a YAML config. Paths are resolved relative to the YAML file's location not the caller's CWD.

```yaml
# agent.yaml
agent:
  name: "BackendEngineer"
  role: "Expert Python backend engineer specialising in FastAPI and PostgreSQL."

  # Either a raw string or a path to a .md file (resolved relative to this YAML)
  system_prompt: "./prompts/instructions.md"

  model:
    provider: "alibaba"             # "anthropic" | "openai" | "alibaba"
    name: "qwen-max"
    api_key_env: "DASHSCOPE_API_KEY"
    temperature: 0.2
    max_tokens: 8096
    thinking:                       # Anthropic only: extended reasoning
      enabled: false
      budget_tokens: 8000

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

    - name: "find"
      enabled: true

    - name: "semantic_search"
      enabled: true
      config:
        # VoyageAI API key env var - REQUIRED for semantic search to work.
        # Get a free key at https://www.voyageai.com/
        api_key_env: "VOYAGE_API_KEY"

        # Embedding model - voyage-code-3 is purpose-built for code search
        model: "voyage-code-3"

        # VoyageAI uses an OpenAI-compatible API - this is the default endpoint
        base_url: "https://api.voyageai.com/v1"

        # Provider name passed to grepai internals (leave as "openai"
        # it's the protocol name, not the vendor)
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
| `alibaba` | `qwen-max`, `qwen-plus`, `qwen-turbo` | `DASHSCOPE_API_KEY` |

---

## 3. Basic Usage

**Sync Usage**
```python
from codepilot import Runtime

runtime = Runtime("agent.yaml")
summary = runtime.run("Fix the nginx config")
print(summary)  # the text the agent put in the completion block, or None
```
**Async Usage**
```python
import asyncio

from codepilot import AsyncRuntime

runtime = AsyncRuntime("agent.yaml")
async def main():
    summary = await runtime.run("Fix the nginx config")
    print(summary)

if __name__ == "__main__":
    asyncio.run(main())
```

`run()` returns when the agent emits a completion block, hits `max_steps`, or is aborted. The return value is the completion block text, or `None` if the loop ended for any other reason.

---

## 4. Streaming

Enable streaming to receive the agent's reasoning text token-by-token, in real time, *before* any code executes. This dramatically improves perceived responsiveness.

```python
from codepilot import Runtime, on_stream

runtime = Runtime("agent.yaml", stream=True)


@on_stream(runtime)
def handle_stream(text: str, **_):
    """Fires with each chunk of streamed text."""
    print(text, end="", flush=True)


runtime.run("Diagnose the CI pipeline for the latest failure happened and stage the fix.")
```

### What gets streamed

The runtime streams in two windows per step:

1. **Pre-fence text** everything before the ` ```codepilot ` block. This is the agent's reasoning paragraph and any display ` ```python ` blocks used in explanations. Streams in real time as the LLM generates it.

2. **Completion block** the ` ```completion ` block content, when the task is done. Streams in real time directly to the user. The loop terminates after this.

Everything between the two windows (the codepilot block, payload blocks) is buffered silently while tools execute.

For **chat/question responses** (no `codepilot` block at all), the **entire response** streams token-by-token in real time and the loop exits cleanly.

### Non-streaming mode

Without `stream=True`, the full response is emitted as a single `STREAM` event when inference completes. The `on_stream` hook still fires you see the complete text at once rather than token-by-token.

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
from codepilot import AsyncRuntime

runtime = AsyncRuntime("agent.yaml")

# Turn 1
runtime.run("Create a FastAPI app with a /items GET endpoint")

# Turn 2 agent has full context of what it built in turn 1
runtime.run("Now add a POST /items endpoint with Pydantic validation")

# Turn 3 agent knows the full codebase it has built
runtime.run("Add pytest tests for both endpoints")
```

---

## 6. Session Persistence

Session backends are chosen at construction time.

| Backend | Storage | Survives restart | Best for |
|---|---|---|---|
| `"memory"` (default) | RAM only | Scripts, one-off tasks |
| `"file"` | `~/.codepilot/sessions/` | CLI tools, local dev |
| `"db"` | Any SQL database | Web apps, containers, multi-user |

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

### Database-backed

Persists history to any SQLAlchemy-compatible database. The `codepilot_sessions` table is created automatically no migration scripts needed.

```bash
# Install the db extras
pip install codepilot-ai[db]          # SQLite or PostgreSQL
pip install psycopg2-binary           # PostgreSQL driver only
```

```python
# SQLite simple, zero-config, great for local persistence
runtime = Runtime(
    "agent.yaml",
    session="db",
    session_id="user-42",
    db_url="sqlite:///./codepilot.db",
)

# PostgreSQL for containers, Cloud Run, multi-user apps
import os
runtime = Runtime(
    "agent.yaml",
    session="db",
    session_id=f"user-{user_id}",
    db_url=os.environ["DATABASE_URL"],
)
```

**Persistence behaviour:**

| Moment | What happens |
|---|---|
| `Runtime(...)` construction | One `SELECT` loads prior messages for the session_id, or `[]` for new sessions |
| Each `run()` call | All agentic steps run **fully in-memory** â€” zero DB I/O during inference |
| `run()` completes | One atomic `UPSERT` full messages list written to DB |
| New `Runtime(...)` same `session_id` | One `SELECT` session fully restored |
| `runtime.reset()` | `DELETE` row clean slate |

**Listing all sessions:**

```python
from codepilot import DatabaseSession

ds = DatabaseSession(session_id="_", db_url="sqlite:///./codepilot.db")
for s in ds.list_sessions():
    print(f"{s['session_id']:30} {s['messages']:4} messages")
```

---

## 7. Context Memory Management

CodePilot uses agent-driven context control with a global safety net.

### How it works

1. The agent can explicitly archive finished tasks using `archive_context(...)`. The original task messages are stored internally and replaced with `[ARCHIVED TASK N]` plus your summary.
2. The agent can restore any archived task using `reveal_context(N)`.
3. A global safety net runs at the start of each `run()`: if context exceeds `global_summary_threshold * max_context_tokens`, older history is collapsed into one `[GLOBAL SUMMARY]` message.

This gives precise control during normal operation, while still preventing overflow in very long sessions.

### Configuration

Add a `memory:` block to `agent.yaml` (all optional):

```yaml
agent:
  memory:
    # Context window size for stress tracking and safety-net triggering
    max_context_tokens: 120000

    # Trigger global summary when usage crosses this fraction
    global_summary_threshold: 0.9

    # Max tokens for generated [GLOBAL SUMMARY] content
    global_summary_max_tokens: 500
```

### What the LLM sees in long sessions

```
[GLOBAL SUMMARY]            <- oldest history compressed by safety net
[ARCHIVED TASK 3]           <- explicit archive summary created by agent
[ARCHIVED TASK 4]           <- explicit archive summary created by agent
[Task 5][USER INPUT] ...    <- active task, raw
```

### Context tools

Agent uses these from the control block:

```python
# Archive one task
archive_context(task=3, summary="Implemented auth middleware and passing tests.")

# Backward-compatible argument name
archive_context(position=4, summary="Added user routes and validation.")

# Archive multiple tasks in one call
archive_context(
    task=(1, 2),
    summary=[
        "Initialized FastAPI project layout.",
        "Added SQLAlchemy models for users and sessions."
    ]
)

# Reveal archived task content
reveal_context(3)

# List archived tasks with token savings
list_archived_context()
```

---

## 8. Resuming a Session

Pass the same `session_id` to a new file-backed Runtime and the prior conversation loads automatically.

```python
# Process 1
runtime = Runtime("agent.yaml", session="file", session_id="ecommerce-api")
runtime.run("Create the products and orders FastAPI endpoints")
# Process exit and session saved

# -------- later, new process --------

# Process 2 picks up exactly where process 1 left off
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
    print("No saved session, will start fresh")
```

---

## 8. Resetting a Session

Wipes all history and deletes the session file (if file-backed). The next `run()` starts completely fresh.

```python
runtime = Runtime("agent.yaml", session="file", session_id="ecommerce-api")

# ... some runs ...

runtime.reset()
runtime.run("Start over build a GraphQL API instead")
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
    """Fires for each text chunk both pre-fence reasoning and completion block content."""
    print(text, end="", flush=True)


@on_tool_call(runtime)
def handle_tool_call(tool: str, args: dict, label: str = "", **_):
    """Fires before every tool executes.
    `label` is a human-readable description (e.g. "Running `pytest tests/`").
    Falls back to args dump if label is not set.
    """
    display = label if label else str(args)
    print(f"\nâš™ï¸  [{tool}] {display}")


@on_tool_result(runtime)
def handle_tool_result(tool: str, result: str, **_):
    """Fires after every tool returns."""
    print(f"{result[:200]}")


@on_ask_user(runtime)
def handle_ask(question: str, **_):
    """Fires when the agent calls ask_user()."""
    print(f"\n{question}")


@on_finish(runtime)
def handle_finish(summary: str, **_):
    """Fires when the task completes (completion block detected)."""
    print(f"\n{summary}\n")


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
| `STREAM` | `text` | Chunk of streamed text (pre-fence reasoning or completion block content) |
| `TOOL_CALL` | `tool`, `args`, `label` | Before any tool executes |
| `TOOL_RESULT` | `tool`, `result` | After any tool returns |
| `ASK_USER` | `question` | Agent calls `ask_user()` |
| `PERMISSION_REQUEST` | `tool`, `description` | Tool with `require_permission: true` fires |
| `SECURITY_ERROR` | `error` | AST validation rejects the control block |
| `RUNTIME_ERROR` | `error` | `exec()` throws an exception |
| `FINISH` | `summary` | Task complete completion block detected |
| `MAX_STEPS` | - | Loop exits because `max_steps` was reached |
| `USER_MESSAGE_QUEUED` | `message` | `send_message()` called |
| `USER_MESSAGE_INJECTED` | `message` | Queued message enters LLM context |
| `SESSION_RESET` | - | `reset()` called |

---

## 10. Permission Gating

The `execute` tool (and optionally `write_file`) supports `require_permission: true` in the AgentFile. When enabled, a `PERMISSION_REQUEST` hook fires before the tool runs. Return `True` to approve, `False` to deny. Falls back to a CLI `y/N` prompt if no handler is registered.

```python
from codepilot import Runtime, on_permission_request

runtime = Runtime("agent.yaml")


@on_permission_request(runtime)
def gate(tool: str, description: str, **_) -> bool:
    """
    tool: "write_file" | "execute"
    description:  human-readable description of the specific operation
    Return True to approve, False to deny.
    """
    print(f"\n[{tool}] {description}")
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
2. Tagged `[USER MESSAGE]` â€” distinct from `[USER INPUT]` (the original task)
3. Injected into the LLM context at the **next step boundary** never mid-step

```python
import time
from codepilot import AsyncRuntime, on_stream, on_user_message_injected

runtime = AsyncRuntime("agent.yaml", stream=True)


@on_stream(runtime)
def show(text: str, **_):
    print(text, end="", flush=True)


@on_user_message_injected(runtime)
def confirmed(message: str, **_):
    print(f"\n[Your message is now in context]: {message}")


async def run_agent():
    await runtime.run("Create a utility module with five string helper functions")
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
# Two new files order of write_file() matches order of payload blocks below.
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

````
```codepilot
# Fix L42-48 (error handling) and L55 (regex) in one step no drift
write_file("routes/profile.py", mode="multi_edit", edits=[(42, 48), (55, 55)])
```

```python
# ... replacement for L42-48 ...
```

```python
# ... replacement for L55 ...
```
````

### Multiple file reads

Any number of `read_file()` calls per step - no limit.

```python
# LLM control block:
read_file("config.py")
read_file("utils.py")
read_file("tests/test_config.py")
```

---

## 13. Shell Tools

The agent has a **persistent, non-blocking shell session system** powered by pexpect. Commands never hang the agent, output is captured up to a timeout and returned immediately.

> **Linux/macOS only.** pexpect requires POSIX. Deploy in a Linux container.

A default shell session (`"main"`) starts automatically when the Runtime is created and persists across `run()` calls on the same Runtime instance. Its PID, status, and current working directory are shown in the system prompt every step.

### execute -run a command

Runs a command, waits up to `timeout` seconds, returns whatever output is available.

```python
# LLM control block:

# status: completed at command finished within timeout (includes return_code)
execute("main", "pytest tests/ -v", 30)

# status: running timeout hit, process still alive
execute("main", "pip install -r requirements.txt", 10)

# Spin up a server on its own shell, in one step
execute("server", "uvicorn app.main:app --host 0.0.0.0 --port 8000", 4, new_shell=True)
```

### read_output -wait for more output

Called after `execute` returned `status: running`. Waits up to `timeout` seconds for new output.

- **New output available:** returns only the new delta (non-overlapping with previous output).
- **No new output (command already done):** returns the complete accumulated output and collapses previous outputs in the context to save tokens.

```python
# LLM control block:
read_output("main", 30)   # wait up to 30 more seconds
```

### send_input - interact with prompts

Sends text to an interactive command waiting for user input.

```python
# LLM control block:
send_input("main", "yes\n", 5)    # confirm a CLI prompt
send_input("main", "admin\n", 5)  # enter a username
```

### send_signal -interrupt or stop

```python
# Interrupt foreground process (Ctrl+C) â€” shell survives
send_signal("server", "SIGINT")

# Terminate or kill the shell process entirely
send_signal("server", "SIGTERM")
send_signal("server", "SIGKILL")
```

### kill_shell - destroy a session

```python
kill_shell("server")   # terminates the process, removes the session
```

### Full example: server + test

```python
# Step 1 LLM control block:
# Start server on its own shell, verify startup logs within 4s
execute("server", "uvicorn app.main:app --port 8000", 4, new_shell=True)

# Step 2 LLM control block (after seeing server startup logs):
# Run tests against the live server from main shell
execute("main", "pytest tests/test_api.py -v", 30)

# Step 3 LLM control block (after tests pass):
# Shut server down cleanly then use a completion block to finish
send_signal("server", "SIGINT")
```

### Context deduplication

When `read_output()` returns in full-mode (the command is already done, no new data), it automatically **removes the earlier outputs** for that command from the conversation history and returns one complete, consolidated result. This keeps the agent context lean on long-running tasks.

---

## 14. Completion Block

The ` ```completion ` block is how the agent signals a task is done. Its content is natural text that **streams directly to the user in real time** token by token just like the pre-fence reasoning. When the runtime detects it, the agentic loop terminates after the current step.

### Why it exists

- **No wasted step** `done()` required a dedicated agentic step just to call it. The completion block can be combined with the action step, saving a full LLM inference call on simple tasks.
- **Real-time streaming** the completion text reaches the user as the LLM generates it, not after.
- **Natural** the agent just writes its closing message as plain text inside the fence, rather than constructing a Python string argument.

### Separate final step (multi-step tasks)

After tests pass and all work is verified:

````
All green both fixes are solid.

```completion
Fixed the 500 on profile email update: two bugs squashed.
(1) `routes/profile.py:L42` bare DB write had no error handling; wrapped in try/except,
now returns a proper 400 on failure.
(2) `utils/validators.py:L18` email regex was rejecting `+` aliases; pattern updated.
All tests pass. You're good to go.
```
````

### Same-step completion (simple tasks)

For simple tasks, combine everything in one agentic step:

````
Updating the timeout value.

```codepilot
write_file("config.py", start_line=12, end_line=12, mode="edit")
```

```python
TIMEOUT = 30
```

```completion
Done updated TIMEOUT from 10 to 30 seconds in config.py:L12.
```
````

### Receiving it in your app

The completion block fires the `FINISH` hook with its text as `summary`:

```python
@on_finish(runtime)
def handle_finish(summary: str, **_):
    print(f"\n{summary}\n")
    save_to_database(summary)   # or send a notification, etc.

summary = runtime.run("Fix the login bug")
# summary == the completion block text, or None if loop ended another way
```

---

## 15. Workspace Change Detection

The runtime automatically detects when **you** modify files in the workspace between agent steps. If you edit a file while the agent is working, it will be notified at the start of the next step with exact line numbers of what changed.

**What the agent sees in its context:**

```
[ENVIRONMENT CHANGE] 2026-02-21 16:30:12

Modified: main.py
Changed lines: 1-4, 47
Created: .env (3 lines)
Deleted: old_config.py
```

The agent is then instructed to re-read affected files before editing because its cached line numbers become stale.

**How it works:**

- Tracking is **opt-in by file** only files the agent has touched (read or written) are watched
- Detection is **snapshot-based** no background daemon, no file watchers, zero overhead between steps
- Snapshots are taken at the end of each step and compared at the start of the next
- Diff limits: 30 changed lines reported per file, 100 total across all files

No configuration is required - this is always on.

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
    print(f"\n{summary}")


# Agent answers with natural markdown no code executed, streams fully
runtime.run("How does the config loader handle missing files?")

# Agent takes action executes code, ends with completion block
runtime.run("Add a fallback default value to the config loader")
```

The agent freely uses ` ```python ` blocks to display code examples in its explanations - they are **never** executed. Only ` ```codepilot ` blocks execute.

### Step awareness

The agent's system prompt is refreshed every step with the current timestamp, OS, working directory, and a live step counter with progressive urgency:

```
# Steps 1-9 of 30 neutral
Agentic step 3 / 30

# Steps 10-22 of 30 mild signal
Agentic step 12 / 30 40% agentic steps consumed!

# Steps 23-26 of 30 approaching
Agentic step 24 / 30 80% agentic steps consumed. Approaching step limit!

# Steps 27-30 of 30 urgent
Agentic step 28 / 30 93% agentic steps consumed! Hard Limit Near!
```

This allows the agent to reason about time, deadlines, and to self-regulate efficiency as it approaches the configured `max_steps` limit.

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
def safe_execute(session_id: str, command: str, timeout: int = 10, new_shell: bool = False):
    """
    Run a shell command. Restricted to read-only operations in this environment.
    Never import subprocess or os directly â€” always use this tool.
    """
    blocked = ["rm", "del", "format", ">", "sudo", "pip install"]
    if any(cmd in command for cmd in blocked):
        runtime._append_execution(f"[execute] Blocked: '{command}' is not permitted.")
        return
    runtime._shell_manager.execute(session_id, command, timeout, new_shell)


runtime.register_tool("execute", safe_execute, replace=True)
```

---

## 18. Aborting the Agent

```python
import asyncio
from codepilot import AsyncRuntime

runtime = AsyncRuntime("agent.yaml")

agent_task = asyncio.create_task(
    runtime.run("Build a complete e-commerce backend")
)

# From anywhere stops after the current step completes (never mid-step)
runtime.abort()
agent_task.join()
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
    print(f"\n{summary}\n")


@on_ask_user(runtime)
def show_question(question: str, **_):
    print(f"\n{question}")


print("CodePilot CLI type 'reset' to clear history, 'quit' to exit.\n")

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
    print(f"\nâœ… {summary}\n")


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


# Stream reasoning text and completion block content token by token
runtime.hooks.register(EventType.STREAM,
    lambda text, **_: _push({"type": "stream", "text": text}))

# Tool activity label gives a clean human-readable status string
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
    """Start a new task. Non-blocking â€” agent runs in background thread."""
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
    session: str = "memory",      # "memory" | "file" | "db"
    session_id: str = None,       # defaults to agent name, slugified
    session_dir: Path = None,     # override ~/.codepilot/sessions/
    stream: bool = False,         # True = token-by-token streaming
    db_url: Optional[str] = None, # database URL for database session (can be SQLite, PostgreSQL, MySQL, etc.)
)

# AsyncRuntime() also exists for async operations. Expects same arguments.

runtime.run(task: str) -> Optional[str]
    # Blocking. Appends to history. Returns completion block text or None.

runtime.send_message(message: str)
    # Thread-safe. Non-blocking. Tagged [USER MESSAGE] in context.

runtime.reset()
    # Wipes messages + session file. Next run() is a blank slate.

runtime.abort()
    # Sets abort flag. Loop stops after current step.

runtime.register_tool(name: str, func: callable, replace: bool = False)
    # Add custom tool. Docstring injected into system prompt automatically.

runtime.messages           # List[Dict] - full conversation history
runtime.session            # BaseSession - current session backend instance
runtime.hooks              # HookSystem - register/emit events manually
runtime.registry           # ToolRegistry - inspect registered tools
```

### Hook decorators

```python
from codepilot import (
    on_stream,                  # STREAM - pre-fence reasoning text or completion block content
    on_tool_call,               # TOOL_CALL - before any tool executes
    on_tool_result,             # TOOL_RESULT - after any tool returns
    on_ask_user,                # ASK_USER - agent called ask_user()
    on_finish,                  # FINISH - task complete (completion block detected)
    on_permission_request,      # PERMISSION_REQUEST - awaiting approval
    on_user_message_queued,     # USER_MESSAGE_QUEUED - send_message() called
    on_user_message_injected,   # USER_MESSAGE_INJECTED - message in context
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

Content always comes from the next payload block â€” never pass it as a string argument.

#### `read_file(path, start_line=1, end_line=None)`

Returns file content with 1-indexed line numbers. Multiple calls per step are allowed.

#### `execute(session_id, command, timeout=10, new_shell=False)`

Runs a command on a persistent shell session. Returns captured output up to `timeout` seconds.

| Parameter | Description |
|---|---|
| `session_id` | Shell session to use. `"main"` exists by default (recreated after `reset()`). |
| `command` | Shell command string. |
| `timeout` | Seconds to wait. Output captured on timeout. |
| `new_shell` | `True` = create and use a new shell in one step. |

Result includes `status: completed` (done, has `return_code`) or `status: running` (timed out, process alive).

Command results also include the shell `cwd`, so the agent can track directory changes across steps and runs.

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

#### `archive_context(position=None, summary=None, task=None)`

Archive completed task context with your summary. `task` is an alias for `position`. Pass only one of them.

#### `reveal_context(position)`

Restore a previously archived task's full original context.

#### `list_archived_context()`

List archived tasks with summary previews and estimated token savings.

#### `find(pattern, scope='codebase', target=None, include=None, max_results=50)`

Text / regex search across a file, multiple files, or the entire workspace. Results are returned as `file:line:matched_line` â€” one match per line.

Uses **ripgrep** (`rg`) when available â€” fast and honours `.gitignore` automatically (ignores `node_modules`, build artifacts, lock files). Falls back to a pure-Python implementation when `rg` is not installed.

| Parameter | Description |
|---|---|
| `pattern` | Regex pattern. Escape special chars: `r'validate_email\('` |
| `scope` | `'file'` / `'files'` / `'codebase'` |
| `target` | File path (str) or list of paths â€” required for `scope='file'/'files'` |
| `include` | Glob filter for `scope='codebase'`. e.g. `'*.py'`, `'tests/**'` |
| `max_results` | Cap on returned matches (default 50) |

```python
# LLM control block examples:
find(pattern=r'validate_email\(', scope='file', target='routes/profile.py')
find(pattern='TODO:', scope='files', target=['routes/profile.py', 'utils/validators.py'])
find(pattern=r'class \w+Handler', scope='codebase', include='*.py')
find(pattern='import torch', scope='codebase', include='tests/**')
```

**Install ripgrep** for best performance (optional Python fallback is always available):
```bash
apt-get install ripgrep      # Debian/Ubuntu
brew install ripgrep          # macOS
```

#### `semantic_search(query, mode='search', depth=2, top_k=5)`

Semantically searches the codebase using the `voyage-code-3` embedding model via [grepai](https://github.com/yoanbernabeu/grepai). Finds code by concept not text match. Use when you don't know which file or function to look at. Use `find()` when you know the exact symbol or string.

**Requires** `VOYAGE_API_KEY` set in environment and `api_key_env: "VOYAGE_API_KEY"` in the AgentFile config.

**First call is slow** (~30-120s): grepai auto-installs if missing, indexes the entire `work_dir`, then searches. Subsequent calls are fast.

| `mode` | What it does |
|---|---|
| `'search'` | Find files/functions matching a natural language concept |
| `'trace_callers'` | Find every place that calls a given function/method |
| `'trace_callees'` | Find everything a function calls internally |
| `'trace_graph'` | Full dependency tree up to `depth` levels â€” use before modifying code with wide blast radius |

**Environment setup:**

```bash
export VOYAGE_API_KEY="pa-..."
```

**How the API key flows:**
grepai internally reads `OPENAI_API_KEY`. The runtime automatically aliases your `VOYAGE_API_KEY` â†’ `OPENAI_API_KEY` at subprocess launch â€” you never need to rename your env var.

**grepai index location:** `~/.codepilot/grepai/<hash>/` â€” entirely outside your project. No `.grepai/` directory is created in your codebase.

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

*CodePilot-ai v0.8.9 — MIT License — [GitHub](https://github.com/Jahanzeb-git/codepilot) — [PyPI](https://pypi.org/project/codepilot-ai/) — Built by [Jahanzeb Ahmed](https://github.com/Jahanzeb-git)*
