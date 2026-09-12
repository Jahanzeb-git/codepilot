# SKILL.md — Using `codepilot-ai` as a Library

This file teaches an AI agent how to use the `codepilot-ai` Python package in an application. If you have been asked to embed an autonomous agent into a product, integrate CodePilot into an API, build a CLI tool powered by an agent, or otherwise *use* the library (not contribute to it), this is your reference.

> **For contribution / development work** see [`AGENT.md`](AGENT.md) instead.

---

## Installation

### From PyPI (recommended)

```bash
pip install codepilot-ai
```

With the optional database session backend:

```bash
pip install "codepilot-ai[db]"    # adds SQLAlchemy
```

### From the GHCR workspace image (self-hosted IDE)

CodePilot ships a ready-to-run workspace Docker image at:

```
ghcr.io/jahanzeb-git/codepilot-workspace:latest
```

This image runs a full browser IDE (code-server + agent runtime) in a container. Use it when you want a user-facing agent IDE rather than an embedded library.

**One-line install of the `codepilot-workspace` CLI launcher:**

```bash
curl -fsSL https://raw.githubusercontent.com/Jahanzeb-git/codepilot/main/cloud/distribution/install.sh | bash
```

**Launch from any project directory:**

```bash
cd my-project
codepilot-workspace
```

The launcher will:
1. Pull `ghcr.io/jahanzeb-git/codepilot-workspace:latest` on first run (subsequent starts reuse the cached image).
2. Mount your current directory as `/workspace` inside the container.
3. Persist agent config and sessions to `~/.codepilot/` on the host.
4. Open the IDE in your browser at `http://localhost:8080`.

**Environment variables for the workspace launcher:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `CODEPILOT_WORKSPACE_DIR` | current directory | Project directory to mount |
| `CODEPILOT_PORT` | `8080` | Port to expose the IDE on |

```bash
# Different project directory
CODEPILOT_WORKSPACE_DIR=/path/to/project codepilot-workspace

# Different port
CODEPILOT_PORT=9090 codepilot-workspace

# Stop the running workspace
docker stop codepilot-workspace
```

---

## Quick Start: Minimal Agent

### 1. Create an `agent.yaml`

```yaml
name: MyAgent
role: Autonomous software engineering agent.

model:
  provider: anthropic
  name: claude-sonnet-5
  api_key_env: ANTHROPIC_API_KEY   # name of the env var holding the key

runtime:
  work_dir: ./workspace            # agent file operations are sandboxed here
  max_steps: 20
  unsafe_mode: false

tools:
  - name: execute
    enabled: true
  - name: view_file
    enabled: true
  - name: list_dir
    enabled: true
  - name: find_file
    enabled: true
  - name: task
    enabled: true
```

### 2. Run the agent

```python
from codepilot import Runtime

runtime = Runtime("agent.yaml")
summary = runtime.run("Inspect the project and fix the failing tests.")
print(summary)
```

---

## Supported Providers

| Provider key | Example model name | Notes |
|---|---|---|
| `anthropic` | `claude-sonnet-5`, `claude-opus-5`, `claude-haiku-4-5`, `claude-fable-5` | Extended thinking supported |
| `openai` | `gpt-4o` | — |
| `gemini` | `gemini-3.5-flash` | Set `api_key_env: GEMINI_API_KEY` |
| `deepseek` | `deepseek-chat`, `deepseek-reasoner`, `deepseek-v4-pro` | Reasoning supported |
| `alibaba` | `qwen-max`, `deepseek-v4-flash`, `deepseek-v4-pro` | Alibaba Model Studio |
| `experientiallabs` | `claude-sonnet-5`, `claude-fable-5`, `kimi-k3` | OpenAI-compatible gateway |

**For any provider not listed**, you must set `memory.max_context_tokens` explicitly in your `agent.yaml`:

```yaml
model:
  provider: my-custom-provider
  name: my-model
  api_key_env: MY_API_KEY

memory:
  max_context_tokens: 128000   # required for unrecognised models
```

---

## Provider Configuration Examples

### Anthropic with extended thinking

```yaml
model:
  provider: anthropic
  name: claude-sonnet-5
  api_key_env: ANTHROPIC_API_KEY
  thinking:
    enabled: true
    budget_tokens: 10000    # max tokens for internal reasoning
```

### Gemini

```yaml
model:
  provider: gemini
  name: gemini-3.5-flash
  api_key_env: GEMINI_API_KEY
```

### DeepSeek with reasoning

```yaml
model:
  provider: deepseek
  name: deepseek-v4-pro
  api_key_env: DEEPSEEK_API_KEY
  thinking:
    enabled: true
    reasoning_effort: high    # "low" | "medium" | "high" | "max"
```

### Experiential Labs (OpenAI-compatible gateway)

```yaml
model:
  provider: experientiallabs
  name: claude-sonnet-5
  api_key_env: EXPERIENTIAL_API_KEY
```

```yaml
model:
  provider: experientiallabs
  name: kimi-k3
  api_key_env: EXPERIENTIAL_API_KEY
  thinking:
    enabled: true
    reasoning_effort: high
```

---

## Full `agent.yaml` Reference

```yaml
name: "AgentName"            # required
role: "Senior Engineer"      # optional: injected into the system prompt

model:
  provider: anthropic        # required: see provider table above
  name: claude-sonnet-5      # required: exact model identifier
  api_key_env: ANTHROPIC_API_KEY   # env var name (default: OPENAI_API_KEY)
  temperature: 0.0           # 0.0–2.0 (default: 0.0)
  max_tokens: 8192           # max output tokens; auto-set for known models
  thinking:
    enabled: false           # enable extended thinking / reasoning
    budget_tokens: 8000      # Anthropic: max internal reasoning tokens
    reasoning_effort: high   # OpenAI/Gemini/DeepSeek/Kimi: "low"|"medium"|"high"

runtime:
  work_dir: "."              # workspace root; file ops are sandboxed here
  max_steps: 20              # hard step limit (default: 20)
  unsafe_mode: false         # allow writes outside work_dir (default: false)

memory:
  max_context_tokens: 200000          # model context window (auto for known models)
  context_stress_trigger: 0.78        # start archiving at 78% context usage
  context_stress_multiplier: 1.0
  context_safety_margin_tokens: 1024

tools:
  - name: execute            # shell command execution (PTY)
    enabled: true
    config:
      require_permission: false   # set true to gate every shell call
  - name: view_file
    enabled: true
  - name: list_dir
    enabled: true
  - name: find_file
    enabled: true
  - name: task
    enabled: true
  - name: semantic_search
    enabled: false
  - name: mcp
    enabled: false
    config:
      servers:
        - name: tavily
          url: https://mcp.tavily.com/mcp/?tavilyApiKey=...
          api_key_env: TAVILY_API_KEY
  - name: sub_agents
    enabled: false
    config:
      max_steps: 20

# Optional: load a system prompt from an external file
# system_prompt: my_system_prompt.md
```

---

## Runtime API

### `Runtime` (synchronous)

```python
from codepilot import Runtime

runtime = Runtime(
    "agent.yaml",
    stream=True,                    # stream pre-block reasoning text (default: False)
    session="memory",               # "memory" | "file" | "db" (default: "memory")
    session_id="my-project",        # used by "file" and "db" backends
    db_url="sqlite:///codepilot.db",# used by "db" backend
)

summary = runtime.run("Refactor the database module.")
print(summary)

# Run again in the same session (history is preserved)
summary2 = runtime.run("Now add unit tests for what you just wrote.")

# Inject a message mid-task (queued for next step)
runtime.send_message("Focus on the auth module only.")

# Reset session history
runtime.reset()
```

### `AsyncRuntime` (async)

```python
from codepilot import AsyncRuntime
from sqlalchemy.ext.asyncio import create_async_engine

engine = create_async_engine("postgresql+asyncpg://user:pass@host/db")

runtime = AsyncRuntime(
    "agent.yaml",
    session="db",
    db=engine,
    stream=True,
)

summary = await runtime.run("Build a REST API for user registration.")
```

### Passing a pre-built session object

```python
from codepilot import Runtime, FileSession

session = FileSession(session_id="my-project", agent_name="MyAgent")
runtime = Runtime("agent.yaml", session=session)
summary = runtime.run("Fix the linting errors.")
```

---

## Session Backends

### In-memory (default — ephemeral)

```python
Runtime("agent.yaml")
# or explicitly:
Runtime("agent.yaml", session="memory")
```

History is lost when the process exits. Good for one-shot CLI tools.

### File-backed (JSON on disk)

```python
Runtime("agent.yaml", session="file", session_id="my-project")
# Sessions saved to ~/.codepilot/sessions/my-project.json
```

Automatically resumes previous conversation on next run.

### Database (SQLAlchemy)

```python
Runtime(
    "agent.yaml",
    session="db",
    session_id="user-123",
    db_url="postgresql://user:pass@host/dbname",
)
```

For async web apps, pass the engine directly (no second pool):

```python
from sqlalchemy.ext.asyncio import create_async_engine
from codepilot import AsyncRuntime

engine = create_async_engine("postgresql+asyncpg://user:pass@host/db",
                             pool_size=5, max_overflow=10, pool_pre_ping=True)

runtime = AsyncRuntime("agent.yaml", session="db", db=engine, session_id="user-123")
summary = await runtime.run("...")
```

> **Important:** A SQLAlchemy engine is a per-process object. Different processes or containers should each create their own engine even if all point to the same Postgres database.

---

## Hooks and Observability

Hooks are the integration point for UIs, logging, and approval flows. They fire on every observable runtime event.

### Decorator pattern

```python
from codepilot import Runtime, on_stream, on_tool_call, on_tool_result, on_finish, on_permission_request

runtime = Runtime("agent.yaml", stream=True)

@on_stream(runtime)
def handle_stream(text: str, **_):
    print(text, end="", flush=True)

@on_tool_call(runtime)
def handle_tool_call(tool: str, args: dict, label: str = "", **_):
    print(f"\n[{tool}] {label or args}")

@on_tool_result(runtime)
def handle_tool_result(tool: str, result: str, **_):
    print(f"  → {result[:200]}")

@on_finish(runtime)
def handle_finish(summary: str, **_):
    print(f"\n✓ Done: {summary}")

@on_permission_request(runtime)
def handle_permission(tool: str, description: str, **_) -> bool:
    answer = input(f"Allow {tool} ({description})? [y/N] ")
    return answer.strip().lower() == "y"

runtime.run("Create a FastAPI server with user authentication.")
```

### Low-level `hooks.register`

```python
from codepilot import Runtime, EventType

runtime = Runtime("agent.yaml", stream=True)

runtime.hooks.register(
    EventType.STREAM,
    lambda text, **_: send_to_websocket({"type": "stream", "text": text}),
)

runtime.hooks.register(
    EventType.TOOL_CALL,
    lambda tool, args, label="", **_: send_to_websocket({
        "type": "tool_call", "tool": tool, "label": label,
    }),
)

runtime.hooks.register(
    EventType.FINISH,
    lambda summary, **_: send_to_websocket({"type": "done", "summary": summary}),
)
```

### All EventTypes

| EventType | When it fires | Key kwargs |
|-----------|--------------|-----------|
| `START` | Agent loop begins | — |
| `STEP` | Each new agentic step | `step: int` |
| `STREAM` | Pre-block reasoning text chunk | `text: str` |
| `THINKING_STREAM` | Model chain-of-thought chunk | `thinking: str` |
| `TOOL_CALL` | Tool about to execute | `tool: str`, `args: dict`, `label: str` |
| `TOOL_RESULT` | Tool returned | `tool: str`, `result: str` |
| `ASK_USER` | Agent is asking a question | `question: str` |
| `PERMISSION_REQUEST` | Shell command needs approval | `tool: str`, `description: str` → return `bool` |
| `SECURITY_ERROR` | AST validation rejected code | `error: str` |
| `RUNTIME_ERROR` | Execution error | `error: str` |
| `FINISH` | Task completed | `summary: str` |
| `MAX_STEPS` | Step limit reached | — |
| `USER_MESSAGE_QUEUED` | `send_message()` called | `message: str` |
| `USER_MESSAGE_INJECTED` | Queued message entered context | `message: str` |
| `CONTEXT_MAINTENANCE_START` | Context cleanup turn starting | `stress_pct: int` |
| `CONTEXT_DROP` | Context was reduced | `before_pct: int`, `after_pct: int`, `tokens_saved: int` |
| `SUBAGENT_SPAWN` | Sub-agent spawned | `agent_id: int`, `task_summary: str` |
| `SUBAGENT_MESSAGE` | Sub-agent sent a message | `agent_id: int`, `message: str` → return reply `str` or `None` |
| `SUBAGENT_FINISH` | Sub-agent completed | `agent_id: int`, `summary: str`, `elapsed_seconds: float` |
| `LLM_RESPONSE` | Raw model generation (before execution) | `step: int`, `response: str` |

---

## FastAPI Integration Pattern

```python
from fastapi import FastAPI, WebSocket
from codepilot import AsyncRuntime, EventType
from sqlalchemy.ext.asyncio import create_async_engine

app = FastAPI()
engine = create_async_engine("postgresql+asyncpg://user:pass@host/db")

@app.websocket("/ws/agent/{session_id}")
async def agent_ws(ws: WebSocket, session_id: str):
    await ws.accept()

    runtime = AsyncRuntime(
        "agent.yaml",
        session="db",
        db=engine,
        session_id=session_id,
        stream=True,
    )

    runtime.hooks.register(
        EventType.STREAM,
        lambda text, **_: ws.send_json({"type": "stream", "text": text}),
    )
    runtime.hooks.register(
        EventType.TOOL_CALL,
        lambda tool, args, label="", **_: ws.send_json({
            "type": "tool_call", "tool": tool, "label": label,
        }),
    )
    runtime.hooks.register(
        EventType.FINISH,
        lambda summary, **_: ws.send_json({"type": "done", "summary": summary}),
    )

    data = await ws.receive_json()
    summary = await runtime.run(data["task"])
    await ws.send_json({"type": "summary", "text": summary})
```

---

## Loading Config Without Running

Useful when you want to inspect or validate an `agent.yaml` without starting the runtime:

```python
from codepilot import AgentConfig

config = AgentConfig.load("agent.yaml")
print(config.name)
print(config.model.provider)
print(config.model.name)
print(config.runtime.work_dir)
print(config.memory.max_context_tokens)
```

---

## External System Prompt File

The `system_prompt` field in `agent.yaml` can be a path to a `.md`, `.txt`, or `.j2` file:

```yaml
name: MyAgent
role: Backend Engineer
system_prompt: prompts/my_system_prompt.md   # relative to the agent.yaml location

model:
  provider: anthropic
  name: claude-sonnet-5
  api_key_env: ANTHROPIC_API_KEY
```

The file is loaded and inlined at `AgentConfig.load()` time — the runtime never sees the path, only the text content.

---

## MCP (Model Context Protocol) Tools

```yaml
tools:
  - name: mcp
    enabled: true
    config:
      servers:
        - name: tavily
          url: https://mcp.tavily.com/mcp/?tavilyApiKey=your_key
          api_key_env: TAVILY_API_KEY
          api_key_param: tavilyApiKey
        - name: my-internal-mcp
          url: http://localhost:9000/mcp
```

MCP tools are automatically discovered from the configured servers and made available to the agent at runtime.

---

## Sub-Agent Usage

```yaml
tools:
  - name: sub_agents
    enabled: true
    config:
      max_steps: 15   # max steps per spawned sub-agent
```

Sub-agents are spawned by the main agent using `spawn_subagent()` and `await_subagent()` inside its tool-call blocks. The main agent waits for each sub-agent to complete before continuing. Hook events (`SUBAGENT_SPAWN`, `SUBAGENT_MESSAGE`, `SUBAGENT_FINISH`) let the host application observe sub-agent lifecycle.

---

## Semantic Search (optional)

```yaml
tools:
  - name: semantic_search
    enabled: true
```

Requires a vector store to be configured. Refer to the documentation site for setup instructions: **https://Jahanzeb-git.github.io/codepilot/**

---

## Session Management API

```python
from codepilot import FileSession

session = FileSession(session_id="my-project")

# Check if a previous session exists
if session.exists():
    print("Resuming previous session")

# List all saved sessions
for s in session.list_sessions():
    print(s["session_id"], s["messages"], s["updated_at"])

# Get metadata without loading messages
meta = session.metadata()

# Reset / delete the session
session.reset()
```

---

## Common Patterns

### One-shot CLI agent

```python
import sys
from codepilot import Runtime

runtime = Runtime("agent.yaml")
task = " ".join(sys.argv[1:]) or "Inspect the repository and report what you find."
print(runtime.run(task))
```

### Streaming CLI loop

```python
from codepilot import Runtime, on_stream, on_finish

runtime = Runtime("agent.yaml", stream=True, session="file", session_id="dev")

@on_stream(runtime)
def stream(text: str, **_):
    print(text, end="", flush=True)

@on_finish(runtime)
def done(summary: str, **_):
    print(f"\n\n[Done] {summary}\n")

while True:
    task = input("Task> ").strip()
    if task in ("exit", "quit"):
        break
    runtime.run(task)
```

### Background CI repair agent

```python
from codepilot import Runtime

runtime = Runtime(
    "agent.yaml",
    session="db",
    session_id=f"ci-repair-{build_id}",
    db_url=DATABASE_URL,
)

# Gate all shell commands — CI must approve before execution
@runtime.hooks.register(EventType.PERMISSION_REQUEST)
def approve(tool, description, **_):
    return True   # auto-approve in CI; log for audit

summary = runtime.run(
    f"The CI build {build_id} failed. The logs are attached. "
    f"Diagnose and fix the root cause.\n\n{ci_logs}"
)
send_slack_notification(f"CI repair summary: {summary}")
```

---

## Docs and Support

- Full documentation: **https://Jahanzeb-git.github.io/codepilot/**
- PyPI: **https://pypi.org/project/codepilot-ai/**
- GitHub: **https://github.com/Jahanzeb-git/codepilot**
- Issues: **https://github.com/Jahanzeb-git/codepilot/issues**
