# Contributing to CodePilot

Thanks for contributing to CodePilot.

## Architecture Overview

Understanding the key layers helps you contribute effectively:

- **`engine/runtime.py`** — `AsyncRuntime` (native async agentic loop) + `Runtime` (thin sync wrapper). The loop: build system prompt → LLM inference → parse `codepilot` block → `exec()` in sandboxed environment → feed result back as `[EXECUTION RESULT]`.
- **`engine/provider.py`** — Async LLM providers (`AsyncOpenAIProvider`, `AsyncAnthropicProvider`, `AlibabaProvider`). All `chat()` and `chat_stream()` methods are `async`.
- **`core/vt.py`** — VT100/VT102 virtual terminal emulator. Converts raw PTY byte streams (ANSI escape codes, carriage returns, etc.) into a clean 2D character grid. The LLM sees only the final rendered snapshot.
- **`core/memory.py`** — Context management. Token counting, per-task breakdown for the stress signal, agent-driven `archive_context` / `reveal_context`, and a global summarization safety net.
- **`core/session.py`** — Session backends: `InMemorySession`, `FileSession`, `DatabaseSession`.
- **`tools/shell.py`** — `ShellManager` wraps `pexpect` to manage persistent PTY shell sessions. Feeds output to `VirtualScreen`.
- **`tools/filesystem.py`** — `write_file`, `read_file` with multi-edit and insert modes.
- **`prompts/system_prompt.j2`** — Jinja2 system prompt template. Static portion is Anthropic prompt-cached with a 1h TTL; dynamic portion (shell state, context stress, codebase snapshot) changes every step.

## Local Setup

1. Clone the repository.
2. Create and activate a virtual environment.
3. Install dependencies.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -U pip
pip install -e .
pip install -e .[db]       # optional: SQLAlchemy database session backend
```

## Project Conventions

1. **All provider and runtime I/O is async.** Keep `async/await` through the stack. Do not introduce synchronous HTTP calls.
2. Keep runtime behavior deterministic and backward-compatible unless a breaking change is intentional.
3. Prefer small, focused commits with clear messages.
4. Update docs in the same PR when behavior changes.
5. Keep the public API ergonomic (`Runtime`, `AsyncRuntime`, hooks, tools, session backends).
6. PTY interaction (pexpect) runs in `asyncio.run_in_executor` — keep individual reads short and bounded.

## Validation Before PR

Run at minimum:

```bash
python -m compileall codepilot
```

Confirm both classes import cleanly:

```bash
python3 -c "from codepilot import Runtime, AsyncRuntime; print('OK')"
```

If you add tests, run them and include the command and output in your PR description.

## Documentation Requirements

When changing behavior, update:

1. `README.md` for user-facing behavior and examples.
2. Docstrings in touched modules.
3. Any examples affected by the change.

## Release Process (PyPI)

`publish.yml` is triggered by pushing a Git tag matching `v*`.

### 1) Bump version

Update all version surfaces together:

1. `pyproject.toml` → `[project].version`
2. `codepilot/__init__.py` → `__version__`
3. `README.md` → top version badge/line

### 2) Commit and push

```bash
git add pyproject.toml codepilot/__init__.py README.md
git commit -m "release: v0.8.7"
git push origin main
```

### 3) Tag and push tag

```bash
git tag v0.8.7
git push origin v0.8.7
```

### 4) Verify release

1. Check GitHub Actions `Publish to PyPI` workflow succeeds.
2. Confirm package/version on PyPI.
3. Sanity-install:

```bash
pip install -U codepilot-ai==0.8.7
```

## Pull Request Checklist

1. Behavior change is clear and justified.
2. Backward compatibility considered (sync `Runtime` wrapper still works).
3. README/examples updated.
4. Version changes included if this is a release PR.
5. Local validation (`compileall` + import check) completed.
