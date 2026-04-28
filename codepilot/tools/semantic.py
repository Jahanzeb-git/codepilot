"""
File: semantic.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description:
Semantic code search tools for the CodePilot agentic runtime.

Architectural Notes:
Provides the semantic_search tool backed by the grepai engine and the
voyage-code-3 embedding model. Supports search, trace_callers, trace_callees,
and trace_graph modes, enabling the agent to navigate large codebases by
concept rather than exact text match. grepai is auto-installed on first use
and its index lives in ~/.codepilot/grepai/ — never inside the user's project.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

import subprocess
import json
import os
import shutil
import hashlib
import time
from typing import TYPE_CHECKING
from pathlib import Path

from ..engine.hooks import EventType

if TYPE_CHECKING:
    from ..engine.runtime import Runtime


class SemanticTools:
    def __init__(self, runtime: "Runtime"):
        self.runtime = runtime
        self._setup_done = False
        self._watch_process = None
        self._grepai_home: Path = None   # ~/.codepilot/grepai/<hash>/
        self._cmd_base: str = "grepai"

    def cleanup(self):
        """Terminate the background grepai watch process."""
        if self._watch_process and self._watch_process.poll() is None:
            self._watch_process.terminate()
            try:
                self._watch_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._watch_process.kill()
            self._watch_process = None

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass

    # ------------------------------------------------------------------
    #  grepai home: ~/.codepilot/grepai/<8-char hash of work_dir>
    #  Keeps ALL grepai state outside the user's project.
    # ------------------------------------------------------------------

    def _get_grepai_home(self, work_dir: str) -> Path:
        """
        Stable, unique directory for this work_dir's grepai index.
        Lives at ~/.codepilot/grepai/<hash>/ — never inside the user's project.
        """
        digest = hashlib.sha256(work_dir.encode()).hexdigest()[:8]
        home = Path.home() / ".codepilot" / "grepai" / digest
        home.mkdir(parents=True, exist_ok=True)
        return home

    def _run_grepai(self, args: list, **kwargs) -> subprocess.CompletedProcess:
        """Run a grepai subprocess. Always uses grepai_home as cwd."""
        cmd = [self._cmd_base] + args
        kwargs.setdefault("cwd", str(self._grepai_home))
        return subprocess.run(cmd, **kwargs)

    def _install_grepai_if_missing(self) -> str:
        """Install grepai using the appropriate method for the current OS."""
        if shutil.which("grepai"):
            return "grepai"

        import platform as _plat

        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="semantic_search",
            args={"action": "installing grepai"},
            label="Installing grepai...",
        )

        system = _plat.system()
        try:
            if system == "Windows":
                subprocess.run(
                    ["powershell", "-Command",
                     "irm https://raw.githubusercontent.com/yoanbernabeu/grepai/main/install.ps1 | iex"],
                    check=True,
                )
            elif system == "Darwin":
                subprocess.run(
                    ["sh", "-c", "brew install yoanbernabeu/tap/grepai"],
                    check=True,
                )
            else:
                subprocess.run(
                    ["sh", "-c",
                     "curl -sSL https://raw.githubusercontent.com/yoanbernabeu/grepai/main/install.sh | sh"],
                    check=True,
                )
        except Exception as e:
            self.runtime._append_execution(
                f"[semantic_search] Failed to install grepai: {e}"
            )

        return "grepai"

    def _ensure_setup(self):
        """
        Ensure grepai is installed, initialized, configured, and indexing.
        All grepai state lives in ~/.codepilot/grepai/<hash>/ — never in work_dir.
        """
        if self._setup_done:
            return

        tool_cfg = self.runtime._tool_config("semantic_search")
        work_dir = self.runtime.config.runtime.work_dir

        # 1. External grepai home
        self._grepai_home = self._get_grepai_home(work_dir)

        # 2. Install if missing
        self._cmd_base = self._install_grepai_if_missing()

        # 3. Config
        provider    = tool_cfg.get("provider", "openai")
        model       = tool_cfg.get("model", "voyage-code-3")
        base_url    = tool_cfg.get("base_url", "https://api.voyageai.com/v1")
        api_key_env = tool_cfg.get("api_key_env", "VOYAGE_API_KEY")

        config_path = self._grepai_home / ".grepai" / "config.yaml"

        if not config_path.exists():
            try:
                self._run_grepai(["init"], capture_output=True, text=True)
            except Exception as e:
                self.runtime._append_execution(
                    f"[semantic_search] grepai init failed: {e}"
                )

            config_content = (
                f"version: 1\n"
                f"store:\n"
                f"  backend: gob\n"
                f"embedder:\n"
                f"  provider: {provider}\n"
                f"  model: {model}\n"
                f"  endpoint: {base_url}\n"
                f"  parallelism: 1\n"
            )
            try:
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(config_content)
            except Exception as e:
                self.runtime._append_execution(
                    f"[semantic_search] Could not write config: {e}"
                )

        # 4. Start watch daemon — indexes work_dir, stores index in grepai_home
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="semantic_search",
            args={"action": "indexing codebase"},
            label="Indexing codebase for semantic search...",
        )
        try:
            env = os.environ.copy()
            if api_key_env in env:
                env["OPENAI_API_KEY"] = env[api_key_env]

            self._watch_process = subprocess.Popen(
                [self._cmd_base, "watch", "--no-ui", "--path", work_dir],
                cwd=str(self._grepai_home), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._wait_for_index(env, timeout=120)
        except Exception as e:
            self.runtime._append_execution(
                f"[semantic_search] Failed to start grepai watch: {e}"
            )

        self._setup_done = True

    def _wait_for_index(self, env: dict, timeout: int = 120):
        """Poll grepai status until at least one file is indexed."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            try:
                r = self._run_grepai(
                    ["status", "--no-ui"],
                    env=env, capture_output=True, text=True, timeout=10,
                )
                if "Files indexed: 0" not in r.stdout:
                    return
            except Exception:
                pass
        self.runtime._append_execution(
            f"[semantic_search] Index not ready after {timeout}s "
            "— search may return partial results."
        )

    # ------------------------------------------------------------------
    #  Tool entry point
    # ------------------------------------------------------------------

    def semantic_search(
        self, query: str, mode: str = "search",
        depth: int = 2, top_k: int = 5,
    ) -> str:
        """
        Semantically search the codebase or trace function dependencies using the
        voyage-code-3 embedding model. Finds code by concept — not text match — so
        you land on exactly the right file and line without grepping.

        Modes:
          'search'        — find code matching a natural language concept.
          'trace_callers' — find every call-site of a function/method.
          'trace_callees' — find everything a function/method calls internally.
          'trace_graph'   — full dependency tree up to `depth` levels deep. 

        Tips for better queries:
          - Be descriptive: "user login validation" > "login"
          - Use natural language: "where are users saved" > "save user"
          - Describe intent, not syntax: "error handling in API layer"

        Use `top_k` to limit results (default 5).
        Skip this tool when you already know the exact file's content of focus.
        This is the primary tool for context-efficient codebase navigation.
        """
        self._ensure_setup()

        tool_cfg    = self.runtime._tool_config("semantic_search")
        timeout     = tool_cfg.get("timeout", 60)
        max_results = tool_cfg.get("max_results", top_k)
        max_chars   = tool_cfg.get("max_output_chars", 8000)
        work_dir    = self.runtime.config.runtime.work_dir

        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="semantic_search",
            args={"query": query, "mode": mode},
            label=f"Semantic search: {query!r}",
        )

        # API key alias: VOYAGE_API_KEY → OPENAI_API_KEY for grepai
        env = os.environ.copy()
        api_key_env = tool_cfg.get("api_key_env", "VOYAGE_API_KEY")
        if api_key_env in env:
            env["OPENAI_API_KEY"] = env[api_key_env]

        # Build command args per mode
        if mode == "search":
            cmd_args = ["search", query, "--json", "--compact", "--path", work_dir]
        elif mode == "trace_callers":
            cmd_args = ["trace", "callers", query, "--path", work_dir]
        elif mode == "trace_callees":
            cmd_args = ["trace", "callees", query, "--path", work_dir]
        elif mode == "trace_graph":
            cmd_args = [
                "trace", "graph", query,
                "--depth", str(depth), "--json", "--path", work_dir,
            ]
        else:
            result = (
                f"[semantic_search] Invalid mode '{mode}'. "
                "Use 'search', 'trace_callers', 'trace_callees', or 'trace_graph'."
            )
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(
                EventType.TOOL_RESULT, tool="semantic_search", result=result,
            )
            return result

        try:
            r = self._run_grepai(
                cmd_args, env=env,
                capture_output=True, text=True, timeout=timeout,
            )

            if r.returncode == 0:
                output = r.stdout.strip()

                # Truncate JSON results to top_k
                if "--json" in cmd_args:
                    try:
                        data = json.loads(output)
                        if isinstance(data, list):
                            total = len(data)
                            data = data[:max_results]
                            output = json.dumps(data, indent=2)
                            if total > max_results:
                                output += (
                                    f"\n\n[Showing top {max_results} "
                                    f"of {total} results]"
                                )
                    except json.JSONDecodeError:
                        pass

                # Character-level safety cap
                if len(output) > max_chars:
                    output = (
                        output[:max_chars]
                        + f"\n\n[Truncated at {max_chars} chars]"
                    )

                result = f"=== Semantic Search ({mode}) ===\n{output}"
            else:
                result = (
                    f"=== Semantic Search Error ({mode}) ===\n"
                    f"Code: {r.returncode}\n{r.stderr.strip()}"
                )
        except subprocess.TimeoutExpired:
            result = f"[semantic_search] Timed out after {timeout}s."
        except Exception as e:
            result = f"[semantic_search] Failed: {e}"

        self.runtime._append_execution(result)
        self.runtime.hooks.emit(
            EventType.TOOL_RESULT, tool="semantic_search", result=result,
        )
        return result
