# Contributing to CodePilot

Thanks for contributing to CodePilot.

CodePilot is an embeddable autonomous agent runtime for software engineering tasks. Contributions are welcome across runtime behavior, developer experience, documentation, examples, tests, and ecosystem tooling.

## Before You Start

- Open an issue first for large features, API changes, or architectural changes.
- Keep changes focused. Small pull requests are easier to review and merge.
- If behavior changes, update documentation in the same pull request.
- If the change is user-facing, include a short explanation of the motivation and impact.

## Repository Overview

These are the main areas of the codebase:

- `codepilot/engine/runtime.py` - `Runtime` and `AsyncRuntime`, including the main agent loop
- `codepilot/engine/provider.py` - provider integrations and model calls
- `codepilot/core/block_parser.py` - parsing `codepilot`, payload, and `completion` blocks
- `codepilot/core/prompt.py` - prompt rendering and dynamic/static instruction assembly
- `codepilot/core/memory.py` - context stress, archive/reveal flows, and summarization support
- `codepilot/core/vt.py` - VT100/VT102 virtual screen rendering
- `codepilot/tools/shell.py` - stateful shell session management via PTY and `pexpect`
- `codepilot/tools/filesystem.py` - file read, write, and edit operations
- `codepilot/tools/semantic.py` - semantic code search integration
- `codepilot/prompts/static_instructions.j2` - cacheable system behavior contract
- `codepilot/prompts/dynamic_instructions.j2` - per-step runtime state prompt

## Local Setup

Create a virtual environment and install the package in editable mode:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

Optional extras:

```bash
pip install -e .[db]
```

## Development Guidelines

- Prefer backward-compatible changes unless a breaking change is explicitly intended.
- Keep the public API ergonomic and stable.
- Preserve deterministic runtime behavior where possible.
- Keep provider and runtime I/O asynchronous through the stack.
- Avoid introducing unnecessary complexity into prompt contracts or parser logic.
- Do not mix unrelated refactors into a focused contribution.

## Documentation Expectations

Update documentation when behavior changes:

- `README.md` for user-facing behavior, positioning, or examples
- docstrings in touched modules when public behavior changes
- prompt comments or inline comments when runtime rules are clarified

## Validation

At minimum, run:

```bash
python -m compileall codepilot
python -c "from codepilot import Runtime, AsyncRuntime; print('OK')"
```

If tests exist for your area, run them and include the command in the pull request description.

## Pull Request Process

1. Create a branch for your change.
2. Make the smallest coherent change that solves the problem.
3. Run validation locally.
4. Update docs if behavior changed.
5. Open a pull request with a clear description of what changed, why it changed, and how you validated it.

## Commit Guidance

Clear commit messages help maintainability. Prefer messages like:

- `docs: clarify runtime completion behavior`
- `fix: preserve payload block ordering in parser`
- `feat: add hook for shell session lifecycle`

## Reporting Bugs

When opening a bug report, include:

- CodePilot version
- Python version
- operating system
- provider and model, if relevant
- minimal reproduction steps
- expected behavior
- actual behavior
- logs, stack traces, or screenshots if available

## Security

If you believe you found a security issue, do not open a public issue first. Follow the reporting guidance in [SECURITY.md](SECURITY.md).

## Release Process

PyPI publishing is handled by `.github/workflows/publish.yml`.

When cutting a release, update version references together:

- `pyproject.toml`
- `codepilot/__init__.py`
- `README.md`

Then create and push a Git tag matching `v*`.
