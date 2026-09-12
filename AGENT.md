# AGENT.md — CodePilot Contributor Guide for AI Agents

This file tells an AI agent everything it needs to work inside this repository as a **contributor or developer**. If you are an AI agent that has been asked to fix a bug, add a feature, write tests, or otherwise modify the codebase, start here.

---

## Repository Overview

**Package name:** `codepilot-ai`  
**Import name:** `codepilot`  
**Current version:** see `pyproject.toml` → `[project] version`  
**Language:** Python ≥ 3.9  
**License:** MIT

CodePilot is a Python library for embedding autonomous software-engineering agents into other products. It is **not** a standalone CLI agent — it is a runtime that developers import and embed. The library owns: model inference, a Code-as-Interface execution protocol, file editing, terminal control, session persistence, and an observable hook system.

---

## Repository Layout

```
codepilot/               # main Python package
  __init__.py            # public API surface — re-exports only
  core/
    agent_file.py        # AgentConfig / AgentFile: YAML schema & loader (Pydantic v2)
    conflict_protocol.py # SEARCH/REPLACE block parser (the core edit primitive)
    session.py           # session backends: InMemorySession, FileSession, DatabaseSession, AsyncDatabaseSession
    memory.py            # MemoryManager: context stress, archival, summarisation
    prompt.py            # prompt renderer (Jinja2, static + dynamic instructions)
    model_profiles.py    # verified context-window profiles keyed by (provider, model)
    mcp.py               # Model Context Protocol client
    mcp_registry.py      # MCP tool registry
    mcp_store.py         # MCP tool persistence
    multiplexer.py       # Unix socket multiplexer for terminal sharing
    vt.py                # VT100/VT102 virtual screen renderer
    watcher.py           # filesystem watcher
    intent_gate.py       # intent / safety gate
    ansi.py              # ANSI escape stripping helpers
    context.py           # context utilities
  engine/
    runtime.py           # Runtime, AsyncRuntime — the main agent loop (largest file)
    provider.py          # LLM provider adapters (Anthropic, OpenAI, Gemini, DeepSeek, Alibaba, ExperientialLabs)
    hooks.py             # HookSystem + EventType enum + decorator helpers
    eval_trace.py        # eval trace utilities
  tools/
    filesystem.py        # view_file, list_dir, find_file, write_file, edit_file
    terminal.py          # execute, read_output, send_input, terminate_terminal (PTY/ConPTY)
    search.py            # grep/ripgrep search
    semantic.py          # semantic code search
    subagent.py          # spawn_subagent, await_subagent
    mcp_tool.py          # MCP tool wrapper
    context.py           # context injection tool
    interaction.py       # ask_user tool
    internal.py          # internal/hidden tools
    registry.py          # tool registry
  prompts/
    static_instructions.j2   # cacheable system-prompt contract (rarely changes)
    dynamic_instructions.j2  # per-step runtime state injected before each LLM call

cloud/                   # CodePilot Workspaces (hosted cloud IDE — separate concern)
  machine/runner/        # Docker image booted per user (Firecracker microVM)
  backend/               # FastAPI control plane
  distribution/          # self-host install scripts (install.sh, codepilot-workspace)

examples/                # ready-to-run agent.yaml files
tests/                   # test suite
evals/                   # evaluation harness
docsite/                 # GitHub Pages documentation source
```

---

## Architecture: Code-as-Interface Protocol

The agent does **not** use JSON function calls. Instead it produces fenced markdown blocks that the runtime parses and executes. Two block types matter:

### 1. Tool-call block (ephemeral `codepilot.py` script)

```
codepilot.py
<<<<<<< SEARCH
=======
execute("main", "pytest tests/ -v", timeout=30)
view_file("codepilot/engine/runtime.py")
>>>>>>> REPLACE
```

The SEARCH section is always **empty** for tool calls. The runtime executes the REPLACE body as a Python snippet with tool functions in scope.

### 2. File-edit block (SEARCH/REPLACE diff)

```
codepilot/core/session.py
<<<<<<< SEARCH
    def load(self) -> List[Dict]:
        return list(self._messages)
=======
    def load(self) -> List[Dict]:
        """Return a copy of the stored message list."""
        return list(self._messages)
>>>>>>> REPLACE
```

SEARCH must uniquely match exactly one location in the file. Zero-match or multi-match is rejected. Leave SEARCH empty for full-file creation or complete rewrites.

### Task completion signal

```python
# Inside a codepilot.py block:
task(finish=True)
```

---

## Available Tools (in scope inside `codepilot.py` blocks)

| Tool | Signature | Notes |
|------|-----------|-------|
| `execute` | `execute(terminal, cmd, timeout, new_terminal=False)` | Run shell command in named PTY session |
| `read_output` | `read_output(terminal, timeout)` | Read from a running terminal |
| `send_input` | `send_input(terminal, text, timeout)` | Send stdin to terminal |
| `terminate_terminal` | `terminate_terminal(terminal)` | Kill named terminal session |
| `view_file` | `view_file(path, start_line=None, end_line=None)` | Read file contents |
| `list_dir` | `list_dir(path)` | List directory entries |
| `find_file` | `find_file(pattern, directory=".")` | Find files by glob/name |
| `search` | `search(pattern, path, include=None)` | ripgrep search |
| `semantic_search` | `semantic_search(query, top_k=5)` | Semantic code search |
| `task` | `task(finish=True)` | Signal task completion |
| `ask_user` | `ask_user(question)` | Prompt user for input |
| `spawn_subagent` | `spawn_subagent(config, task)` | Spawn a sub-agent |
| `await_subagent` | `await_subagent(agent_id)` | Wait for sub-agent result |

---

## Development Setup

```bash
# 1. Clone and enter the repo
git clone https://github.com/Jahanzeb-git/codepilot.git
cd codepilot

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate      # Linux/macOS
# .venv\Scripts\activate       # Windows

# 3. Install in editable mode
pip install -U pip
pip install -e .

# Optional extras
pip install -e .[db]     # SQLAlchemy session backends
pip install -e .[evals]  # eval harness

# 4. Verify the install is healthy
python -m compileall codepilot
python -c "from codepilot import Runtime, AsyncRuntime; print('OK')"
```

---

## Testing

```bash
# Syntax / import sanity (always run this)
python -m compileall codepilot
python -c "from codepilot import Runtime, AsyncRuntime; print('OK')"

# Run the full test suite
pytest tests/ -v

# Run a specific test file
pytest tests/test_session.py -v

# Run with coverage
pytest tests/ --cov=codepilot --cov-report=term-missing
```

There is also an evaluation harness in `evals/`. The GitHub Actions workflow `.github/workflows/eval.yml` shows how it is invoked in CI.

---

## Code Style and Constraints

- **Python ≥ 3.9.** No walrus operator in 3.8-incompatible positions, no `match`/`case`.
- **Pydantic v2** for all config schemas (`codepilot/core/agent_file.py`).
- **Async throughout the engine layer.** `provider.py` and `runtime.py` are async. Sync `Runtime` wraps `AsyncRuntime` with `asyncio.run()`. Do not introduce blocking I/O inside `async def` functions without `asyncio.to_thread`.
- **No new top-level dependencies** without discussion. Core deps are: `pydantic`, `openai`, `anthropic`, `python-dotenv`, `PyYAML`, `rich`, `jinja2`, `pexpect`/`pywinpty`, `tiktoken`.
- **Backward-compatible public API.** `codepilot/__init__.py` is the public surface. Do not remove or rename exports without a major version bump.
- **One concern per file.** Do not mix runtime logic into tool implementations or vice versa.
- **Comment the "why", not the "what".** Inline comments explain design decisions; docstrings document public interfaces.

---

## Key Invariants — Do Not Break These

1. **SEARCH/REPLACE uniqueness**: `conflict_protocol.py` rejects ambiguous SEARCH sections. Do not relax this — it is a safety property.
2. **work_dir sandbox**: file tools constrain writes to `runtime.work_dir` unless `unsafe_mode: true`. Do not bypass this.
3. **Session atomicity**: `FileSession` uses atomic temp-file rename. `DatabaseSession` uses transactions. Any new persistence code must be crash-safe.
4. **Engine → Core → Tools dependency order**: `engine/` imports from `core/` and `tools/`. `core/` imports from nowhere inside the package. `tools/` may import from `core/`. Never import `engine/` from `core/` or `tools/`.
5. **`__init__.py` is re-exports only**: no logic lives there.
6. **Model profiles are exact**: `model_profiles.py` lists verified (provider, model) pairs. Do not add guessed or approximate profiles. Unknown models must set `memory.max_context_tokens` explicitly in their `agent.yaml`.

---

## Adding a New LLM Provider

1. Add entries to `_PROFILES` in `codepilot/core/model_profiles.py` for every model you want auto-resolved.
2. Add a provider adapter in `codepilot/engine/provider.py`. Follow the pattern of the existing adapters — they all implement `generate()` and `generate_stream()` coroutines.
3. Add the provider string to the docstring of `ModelConfig.provider` in `codepilot/core/agent_file.py`.
4. Add an example YAML in `examples/`.
5. Run the validation checks listed under **Testing**.

---

## Adding a New Tool

1. Implement the tool function in the appropriate file under `codepilot/tools/`.
2. Register it in `codepilot/tools/registry.py`.
3. Add a `ToolConfig` entry to the agent.yaml schema examples (the tool will be disabled by default unless listed and `enabled: true`).
4. Update the tool table in this file and in `README.md` if it is user-facing.

---

## Modifying Prompts

- `static_instructions.j2` — the system prompt rendered once per session. Changes here affect every model interaction. Be extremely conservative; any new constraint must be unambiguous to the model.
- `dynamic_instructions.j2` — injected at each step with runtime state (tool results, task list, context stress). Keep it short and structured; it is re-rendered every step and consumes context tokens.

---

## Version Bumps

When cutting a release, update **all three** of these atomically:

1. `pyproject.toml` → `[project] version`
2. `codepilot/__init__.py` → `__version__`
3. `README.md` → the `**Version:**` line

Then create and push a Git tag matching `v*`. PyPI publishing is handled by `.github/workflows/publish.yml`.

---

## Pull Request Checklist

- [ ] `python -m compileall codepilot` passes with no errors
- [ ] `python -c "from codepilot import Runtime, AsyncRuntime; print('OK')"` passes
- [ ] `pytest tests/ -v` passes (or failing tests are explicitly noted and justified)
- [ ] Public API surface in `codepilot/__init__.py` is unchanged (or the change is intentional and documented)
- [ ] `README.md` updated if user-facing behavior changed
- [ ] Docstrings updated in touched modules if public behavior changed
- [ ] No new hard dependencies added without discussion
- [ ] Commit messages follow the `type: description` pattern (`fix:`, `feat:`, `docs:`, `refactor:`, `test:`)

---

## File Encoding and Formatting

- All source files: **UTF-8**, Unix line endings (`\n`).
- No tabs — 4-space indentation throughout.
- Max line length: 100 characters (soft), 120 (hard).

---

## Cloud Infrastructure (read-only for library contributors)

The `cloud/` directory contains CodePilot Workspaces — a hosted cloud IDE built on top of the library. Library contributors generally do not need to touch it. It is a separate concern from the `codepilot-ai` Python package.

- `cloud/machine/runner/` — the Docker image booted per user workspace
- `cloud/backend/` — FastAPI control plane
- `cloud/distribution/` — self-host install scripts

If you are working on the cloud layer specifically, refer to `cloud/README_CLOUD.md`.
