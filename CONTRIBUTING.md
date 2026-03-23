# Contributing to CodePilot

Thanks for contributing to CodePilot.

## Scope

This repository publishes the `codepilot-ai` package to PyPI through GitHub Actions when a tag matching `v*` is pushed.

## Local Setup

1. Clone the repository.
2. Create and activate a virtual environment.
3. Install dependencies.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\\Scripts\\Activate.ps1
pip install -U pip
pip install -e .
pip install -e .[db]
```

## Project Conventions

1. Keep runtime behavior deterministic and backward-compatible unless a breaking change is intentional.
2. Prefer small, focused commits with clear messages.
3. Update docs in the same PR when behavior changes.
4. Keep public API ergonomic (`Runtime`, hooks, tools, session backends).

## Validation Before PR

Run at least:

```bash
python -m compileall codepilot
```

If you add tests, run them and include the command/output in your PR description.

## Documentation Requirements

When changing behavior, update:

1. `README.md` for user-facing behavior and examples.
2. Docstrings in touched modules.
3. Any examples affected by the change.

## Release Process (PyPI)

`publish.yml` is triggered by pushing a Git tag like `v0.8.4`.

### 1) Bump version

Update all version surfaces together:

1. `pyproject.toml` -> `[project].version`
2. `codepilot/__init__.py` -> `__version__`
3. `README.md` -> top version badge/line

### 2) Commit and push

```bash
git add pyproject.toml codepilot/__init__.py README.md
git commit -m "release: v0.8.4"
git push origin main
```

### 3) Tag and push tag

```bash
git tag v0.8.4
git push origin v0.8.4
```

### 4) Verify release

1. Check GitHub Actions `Publish to PyPI` workflow succeeds.
2. Confirm package/version on PyPI.
3. Sanity-install:

```bash
pip install -U codepilot-ai==0.8.4
```

## Pull Request Checklist

1. Behavior change is clear and justified.
2. Backward compatibility considered.
3. README/examples updated.
4. Version changes included if this is a release PR.
5. Local validation completed.
