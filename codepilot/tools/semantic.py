"""
File: semantic.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description:
Semantic code search tools for the CodePilot agentic runtime.

grepai lifecycle (verified against actual CLI behaviour):
  1. Write <work_dir>/.grepai/config.yaml directly (no 'grepai init' needed).
     The API key is a LITERAL value — grepai does NOT expand ${VAR} syntax.
  2. grepai watch --background  — fire-and-forget; daemon manages its own PID.
  3. Readiness: poll ~/.local/state/grepai/logs/grepai-watch.log for
     the line "[RUNNING] <work_dir> - steady".
  4. grepai search "query" --json --compact --limit N  (cwd = work_dir).
  5. Watcher runs indefinitely; we do NOT stop it on agent exit.

State machine:
  UNCONFIGURED → tool misconfigured, hidden from LLM
  DOWNLOADING  → binary install in progress
  WRITING_CFG  → creating .grepai/config.yaml
  INDEXING     → grepai watch --background started, initial scan running
  READY        → log shows [RUNNING], searches return results

Copyright (c) 2026 Jahanzeb Ahmed. Licensed under the MIT License.
"""

import json
import os
import platform
import stat
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING

from ..engine.hooks import EventType

if TYPE_CHECKING:
    from ..engine.runtime import Runtime


# ---------------------------------------------------------------------------
#  grepai config template — matches the format produced by `grepai init`
#  (verified from user's working omniroot-agent setup).
#  4-space YAML indentation exactly as grepai expects.
# ---------------------------------------------------------------------------
_CONFIG_TEMPLATE = """\
version: 1
embedder:
    provider: {provider}
    model: {model}
    endpoint: {endpoint}
    api_key: "{api_key}"
    parallelism: {parallelism}
store:
    backend: gob
chunking:
    size: 512
    overlap: 50
watch:
    debounce_ms: 500
    rpg_persist_interval_ms: 1000
    rpg_derived_debounce_ms: 300
    rpg_full_reconcile_interval_sec: 300
    rpg_max_dirty_files_per_batch: 128
search:
    boost:
        enabled: true
        penalties:
            - pattern: /tests/
              factor: 0.5
            - pattern: /test/
              factor: 0.5
            - pattern: __tests__
              factor: 0.5
            - pattern: _test.
              factor: 0.5
            - pattern: .test.
              factor: 0.5
            - pattern: .spec.
              factor: 0.5
            - pattern: test_
              factor: 0.5
            - pattern: /mocks/
              factor: 0.4
            - pattern: /mock/
              factor: 0.4
            - pattern: .mock.
              factor: 0.4
            - pattern: /fixtures/
              factor: 0.4
            - pattern: /testdata/
              factor: 0.4
            - pattern: /generated/
              factor: 0.4
            - pattern: .generated.
              factor: 0.4
            - pattern: .gen.
              factor: 0.4
            - pattern: .md
              factor: 0.6
            - pattern: /docs/
              factor: 0.6
        bonuses:
            - pattern: /src/
              factor: 1.1
            - pattern: /lib/
              factor: 1.1
            - pattern: /app/
              factor: 1.1
    hybrid:
        enabled: false
        k: 60
trace:
    mode: fast
    enabled_languages:
        - .go
        - .js
        - .ts
        - .jsx
        - .tsx
        - .py
        - .php
        - .lua
        - .c
        - .h
        - .cpp
        - .hpp
        - .cc
        - .cxx
        - .rs
        - .zig
        - .cs
        - .java
        - .fs
        - .fsx
        - .fsi
        - .pas
        - .dpr
    exclude_patterns:
        - '*_test.go'
        - '*.spec.ts'
        - '*.spec.js'
        - '*.test.ts'
        - '*.test.js'
        - __tests__/*
rpg:
    enabled: false
    feature_mode: local
    drift_threshold: 0.35
    max_traversal_depth: 3
    llm_provider: ollama
    llm_endpoint: http://localhost:11434/v1
    llm_timeout_ms: 8000
    feature_group_strategy: sample
update:
    check_on_startup: false
ignore:
    - .git
    - .grepai
    - node_modules
    - vendor
    - bin
    - dist
    - __pycache__
    - .venv
    - venv
    - .idea
    - .vscode
    - target
    - .zig-cache
    - zig-out
    - qdrant_storage
"""

# ---------------------------------------------------------------------------
#  Supported providers
# ---------------------------------------------------------------------------
_LOCAL_PROVIDERS = {"ollama", "lmstudio", "synthetic"}
_CLOUD_PROVIDERS = {"openai", "openrouter"}
_ALL_PROVIDERS   = _LOCAL_PROVIDERS | _CLOUD_PROVIDERS

_KNOWN_MODELS: dict[str, list[str]] = {
    "ollama":    ["nomic-embed-text", "nomic-embed-text-v2-moe", "bge-m3", "mxbai-embed-large"],
    "lmstudio":  ["text-embedding-nomic-embed-text-v1.5", "bge-small-en-v1.5"],
    "openai":    ["text-embedding-3-small", "text-embedding-3-large", "voyage-code-3"],
    "openrouter": [],
    "synthetic": [],
}

# ---------------------------------------------------------------------------
#  Binary download — GitHub Releases
# ---------------------------------------------------------------------------
_GREPAI_VERSION      = "0.35.0"
_GREPAI_RELEASE_BASE = (
    f"https://github.com/yoanbernabeu/grepai/releases/download/v{_GREPAI_VERSION}"
)
_ASSET_MAP: dict[tuple[str, str], str] = {
    ("Linux",   "x86_64"):  f"grepai_{_GREPAI_VERSION}_linux_amd64.tar.gz",
    ("Linux",   "aarch64"): f"grepai_{_GREPAI_VERSION}_linux_arm64.tar.gz",
    ("Linux",   "arm64"):   f"grepai_{_GREPAI_VERSION}_linux_arm64.tar.gz",
    ("Darwin",  "x86_64"):  f"grepai_{_GREPAI_VERSION}_darwin_amd64.tar.gz",
    ("Darwin",  "arm64"):   f"grepai_{_GREPAI_VERSION}_darwin_arm64.tar.gz",
    ("Windows", "AMD64"):   f"grepai_{_GREPAI_VERSION}_windows_amd64.zip",
    ("Windows", "x86_64"):  f"grepai_{_GREPAI_VERSION}_windows_amd64.zip",
    ("Windows", "ARM64"):   f"grepai_{_GREPAI_VERSION}_windows_arm64.zip",
}


# ---------------------------------------------------------------------------
#  Internal state machine
# ---------------------------------------------------------------------------
class _State(Enum):
    UNCONFIGURED = auto()
    DOWNLOADING  = auto()
    WRITING_CFG  = auto()
    INDEXING     = auto()
    READY        = auto()


class SemanticConfigError(RuntimeError):
    """Raised when semantic_search is misconfigured. Caught by the runtime."""


class SemanticTools:
    def __init__(self, runtime: "Runtime"):
        self.runtime      = runtime
        self._binary_path = ""
        self._work_dir    = ""
        self._state       = _State.UNCONFIGURED
        self._state_since = 0.0
        self._env: dict   = {}

    # ------------------------------------------------------------------
    #  Public lifecycle
    # ------------------------------------------------------------------

    def validate_config(self):
        """Pre-flight check at runtime startup. Raises SemanticConfigError
        if misconfigured so the runtime hides the tool from the LLM."""
        tool_cfg = self.runtime._tool_config("semantic_search")
        provider = tool_cfg.get("provider", "openai").lower().strip()
        model    = tool_cfg.get("model",    "voyage-code-3").strip()

        if provider not in _ALL_PROVIDERS:
            raise SemanticConfigError(
                f"semantic_search misconfigured: unknown provider '{provider}'. "
                f"Supported: {sorted(_ALL_PROVIDERS)}"
            )

        known = _KNOWN_MODELS.get(provider, [])
        if known and model not in known:
            self.runtime._append_execution(
                f"[semantic_search] Warning: model '{model}' not in known list "
                f"for '{provider}' {known}. Proceeding anyway."
            )

        if provider in _CLOUD_PROVIDERS:
            api_key_env = tool_cfg.get("api_key_env", "VOYAGE_API_KEY")
            if not os.environ.get(api_key_env):
                raise SemanticConfigError(
                    f"semantic_search misconfigured: env var '{api_key_env}' not set."
                )

        # Build env — alias user's key to OPENAI_API_KEY for grepai's openai provider
        self._work_dir  = self.runtime.config.runtime.work_dir
        api_key_env     = tool_cfg.get("api_key_env", "VOYAGE_API_KEY")
        self._env       = os.environ.copy()
        if api_key_env in self._env:
            self._env["OPENAI_API_KEY"] = self._env[api_key_env]

        # Kick off setup eagerly — non-fatal if it fails
        try:
            self._bootstrap()
        except Exception as exc:
            self.runtime._append_execution(
                f"[semantic_search] Warning: background setup failed: {exc}"
            )

    def cleanup(self):
        """No-op — the grepai watch daemon runs indefinitely and is NOT
        stopped on agent exit. It continues indexing file changes between
        sessions, making subsequent starts instant."""
        pass

    # ------------------------------------------------------------------
    #  Log file location (platform-specific, from grepai docs)
    # ------------------------------------------------------------------

    def _log_file(self) -> Path:
        system = platform.system()
        if system == "Darwin":
            return Path.home() / "Library" / "Logs" / "grepai" / "grepai-watch.log"
        if system == "Windows":
            base = os.environ.get("LOCALAPPDATA", str(Path.home()))
            return Path(base) / "grepai" / "logs" / "grepai-watch.log"
        # Linux — respects XDG_STATE_HOME
        xdg = os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
        return Path(xdg) / "grepai" / "logs" / "grepai-watch.log"

    # ------------------------------------------------------------------
    #  Binary management
    # ------------------------------------------------------------------

    @staticmethod
    def _managed_bin_dir() -> Path:
        d = Path.home() / ".codepilot" / "bin"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _binary_name() -> str:
        return "grepai.exe" if platform.system() == "Windows" else "grepai"

    def _managed_binary_path(self) -> Path:
        return self._managed_bin_dir() / self._binary_name()

    def _resolve_binary(self) -> str:
        dest = self._managed_binary_path()
        if dest.exists():
            return str(dest)
        return self._download_grepai(dest)

    def _download_grepai(self, dest: Path) -> str:
        system  = platform.system()
        machine = platform.machine()
        asset   = _ASSET_MAP.get((system, machine))
        if asset is None:
            raise RuntimeError(
                f"[semantic_search] Unsupported platform {system}/{machine}. "
                f"Install grepai manually at {dest}"
            )

        url = f"{_GREPAI_RELEASE_BASE}/{asset}"
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="semantic_search",
            args={"action": "downloading", "url": url},
            label=f"Downloading grepai v{_GREPAI_VERSION} ({system}/{machine})…",
        )

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.parent / "grepai_download.tmp"

        try:
            with urllib.request.urlopen(url, timeout=600) as resp:
                with open(tmp, "wb") as fh:
                    while chunk := resp.read(65536):
                        fh.write(chunk)

            if tmp.stat().st_size < 100_000:
                raise RuntimeError("Downloaded archive too small — retry.")

            if asset.endswith(".zip"):
                with zipfile.ZipFile(str(tmp), "r") as zf:
                    members = [m for m in zf.namelist()
                               if m in ("grepai", "grepai.exe")
                               or m.endswith("/grepai") or m.endswith("/grepai.exe")]
                    if not members:
                        raise RuntimeError(f"grepai not found in {asset}")
                    zf.extract(members[0], path=str(dest.parent))
                    (dest.parent / members[0]).rename(dest)
            else:
                with tarfile.open(str(tmp), "r:gz") as tar:
                    members = [m for m in tar.getmembers()
                               if m.name in ("grepai", "grepai.exe")
                               or m.name.endswith("/grepai") or m.name.endswith("/grepai.exe")]
                    if not members:
                        raise RuntimeError(f"grepai not found in {asset}")
                    tar.extract(members[0], path=str(dest.parent))
                    (dest.parent / members[0].name).rename(dest)

            dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        except urllib.error.URLError as exc:
            raise RuntimeError(f"Download failed from {url}: {exc}") from exc
        except OSError as exc:
            raise RuntimeError(f"Failed to install binary to {dest}: {exc}") from exc
        finally:
            tmp.unlink(missing_ok=True)

        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="semantic_search",
            args={"action": "installed", "path": str(dest)},
            label=f"grepai installed at {dest}",
        )
        return str(dest)

    # ------------------------------------------------------------------
    #  grepai subprocess helper
    # ------------------------------------------------------------------

    def _run(self, args: list, **kwargs) -> subprocess.CompletedProcess:
        cmd = [self._binary_path] + args
        kwargs.setdefault("cwd",   self._work_dir)
        kwargs.setdefault("env",   self._env)
        kwargs.setdefault("stdin", subprocess.DEVNULL)
        return subprocess.run(cmd, **kwargs)

    # ------------------------------------------------------------------
    #  Bootstrap (called eagerly from validate_config)
    # ------------------------------------------------------------------

    def _bootstrap(self):
        """Full setup pipeline: binary → write config → start watcher.

        Each step is idempotent:
        - Binary present? Skip download.
        - .grepai/config.yaml present? Skip write.
        - Daemon running? Skip watch start.
        """
        tool_cfg = self.runtime._tool_config("semantic_search")

        # 1. Binary
        self._set_state(_State.DOWNLOADING)
        self._binary_path = self._resolve_binary()

        # 2. Write .grepai/config.yaml directly — NO grepai init.
        #    grepai does NOT expand ${VAR} syntax in config; key must be literal.
        config_path = Path(self._work_dir) / ".grepai" / "config.yaml"
        if not config_path.exists():
            self._set_state(_State.WRITING_CFG)
            provider    = tool_cfg.get("provider", "openai")
            model       = tool_cfg.get("model",    "voyage-code-3")
            endpoint    = tool_cfg.get("base_url", "https://api.voyageai.com/v1")
            api_key     = self._env.get("OPENAI_API_KEY", "")
            parallelism = tool_cfg.get("parallelism", 1)

            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                _CONFIG_TEMPLATE.format(
                    provider=provider,
                    model=model,
                    endpoint=endpoint,
                    api_key=api_key,
                    parallelism=parallelism,
                )
            )
            self.runtime.hooks.emit(
                EventType.TOOL_CALL, tool="semantic_search",
                args={"action": "config written", "path": str(config_path)},
                label=f"Wrote grepai config → {config_path}",
            )

        # 3. Check if daemon already running
        status_r = self._run(
            ["watch", "--status"],
            capture_output=True, text=True, timeout=10,
        )
        if "status: running" in status_r.stdout.lower():
            self._set_state(_State.READY)
            return

        # 4. Start grepai watch --background (fire-and-forget via Popen).
        #    The daemon manages its own PID file and logs to the OS log dir.
        #    We poll the log file for "[RUNNING]" to detect readiness.
        self._set_state(_State.INDEXING)
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="semantic_search",
            args={"action": "watch --background", "work_dir": self._work_dir},
            label="Starting grepai background indexer…",
        )
        subprocess.Popen(
            [self._binary_path, "watch", "--background"],
            cwd=self._work_dir, env=self._env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        # Do NOT wait. _check_ready() polls the log file for [RUNNING].

    def _set_state(self, new_state: _State):
        self._state       = new_state
        self._state_since = time.time()

    # ------------------------------------------------------------------
    #  Readiness detection — poll log file for [RUNNING] marker
    # ------------------------------------------------------------------

    def _check_ready(self) -> bool:
        """True when the log file shows [RUNNING] for this project.

        Log line format (verified):
          [grepai-watch] 2026/05/04 17:06:18 [RUNNING] /path/to/project - steady

        Fallback: grepai status --no-ui with 'Files indexed: N' check.
        """
        if self._state == _State.READY:
            return True
        if not self._binary_path:
            return False

        # Primary: log file
        log = self._log_file()
        if log.exists():
            try:
                text = log.read_text(errors="replace")
                if f"[RUNNING] {self._work_dir}" in text:
                    self._set_state(_State.READY)
                    return True
                # Error on this project — don't mark ready
                if f"[ERROR] {self._work_dir}" in text:
                    return False
            except OSError:
                pass

        # Fallback: status command (format verified: "Files indexed: N")
        try:
            r = self._run(
                ["status", "--no-ui"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and "Files indexed: 0" not in r.stdout:
                self._set_state(_State.READY)
                return True
        except Exception:
            pass

        return False

    def _status_message(self) -> str:
        """Informative message returned to the LLM while not ready."""
        elapsed = int(time.time() - self._state_since)
        m, s    = divmod(elapsed, 60)
        elapsed_str = f"{m}m {s}s" if m else f"{s}s"

        if self._state in (_State.DOWNLOADING, _State.UNCONFIGURED):
            return (
                f"[semantic_search] Downloading grepai binary (elapsed: {elapsed_str}). "
                "Please retry in a moment."
            )
        if self._state == _State.WRITING_CFG:
            return (
                f"[semantic_search] Writing index config (elapsed: {elapsed_str}). "
                "Please retry in a few seconds."
            )
        # INDEXING state
        return (
            f"[semantic_search] Initial codebase indexing is in progress "
            f"(elapsed: {elapsed_str}). "
            "This is a one-time cost — subsequent agent starts will be instant. "
            "Use find() or shell commands in the meantime. "
            "Retry this search in a moment."
        )

    # ------------------------------------------------------------------
    #  Tool entry point
    # ------------------------------------------------------------------

    def semantic_search(
        self,
        query: str,
        mode: str = "search",
        depth: int = 2,
        top_k: int = 5,
    ) -> str:
        """
        Semantically search the codebase or trace function dependencies using
        the configured embedding model. Finds code by concept — not text match
        — so you land on exactly the right file and line without grepping.

        Modes:
          'search'        — find code matching a natural language concept.
          'trace_callers' — find every call-site of a function/method.
          'trace_callees' — find everything a function/method calls.
          'trace_graph'   — full dependency tree up to `depth` levels deep.

        Tips:
          - Be descriptive: "user login validation" > "login"
          - Describe intent: "error handling in API layer" > "try except"

        Use `top_k` to limit results (default 5).
        """
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="semantic_search",
            args={"query": query, "mode": mode},
            label=f"Semantic search: {query!r}",
        )

        if not self._check_ready():
            result = self._status_message()
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="semantic_search", result=result)
            return result

        tool_cfg    = self.runtime._tool_config("semantic_search")
        timeout     = tool_cfg.get("timeout",          60)
        max_results = tool_cfg.get("max_results",      top_k)
        max_chars   = tool_cfg.get("max_output_chars", 8000)

        if mode == "search":
            cmd = ["search", query, "--json", "--compact", "--limit", str(max_results)]
        elif mode == "trace_callers":
            cmd = ["trace", "callers", query]
        elif mode == "trace_callees":
            cmd = ["trace", "callees", query]
        elif mode == "trace_graph":
            cmd = ["trace", "graph", query, "--depth", str(depth), "--json"]
        else:
            result = (
                f"[semantic_search] Invalid mode '{mode}'. "
                "Valid: 'search', 'trace_callers', 'trace_callees', 'trace_graph'."
            )
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="semantic_search", result=result)
            return result

        try:
            r = self._run(cmd, capture_output=True, text=True, timeout=timeout)

            if r.returncode == 0:
                output = r.stdout.strip()

                if "--json" in cmd:
                    try:
                        data = json.loads(output)
                        if isinstance(data, list):
                            # Empty result while daemon running = still indexing
                            if len(data) == 0:
                                # Check if files are actually indexed yet
                                sr = self._run(
                                    ["status", "--no-ui"],
                                    capture_output=True, text=True, timeout=10,
                                )
                                if sr.returncode == 0 and "Files indexed: 0" in sr.stdout:
                                    result = self._status_message()
                                    self.runtime._append_execution(result)
                                    self.runtime.hooks.emit(
                                        EventType.TOOL_RESULT,
                                        tool="semantic_search", result=result,
                                    )
                                    return result

                            total  = len(data)
                            data   = data[:max_results]
                            output = json.dumps(data, indent=2)
                            if total > max_results:
                                output += f"\n\n[Showing top {max_results} of {total} results]"
                    except json.JSONDecodeError:
                        pass

                if len(output) > max_chars:
                    output = output[:max_chars] + f"\n\n[Truncated at {max_chars} chars]"

                result = f"=== Semantic Search ({mode}) ===\n{output}"
            else:
                result = (
                    f"=== Semantic Search Error ({mode}) ===\n"
                    f"Exit code: {r.returncode}\n{r.stderr.strip()}"
                )

        except subprocess.TimeoutExpired:
            result = f"[semantic_search] Timed out after {timeout}s."
        except Exception as exc:
            result = f"[semantic_search] Unexpected error: {exc}"

        self.runtime._append_execution(result)
        self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="semantic_search", result=result)
        return result
