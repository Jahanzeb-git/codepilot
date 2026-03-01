import subprocess
import sys
import json
import os
import shutil
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

    def _run_grepai(self, args: list, **kwargs) -> subprocess.CompletedProcess:
        """
        Run a grepai subprocess with proper Windows shell handling.
        Centralizes the sys.platform check to avoid inconsistency.
        """
        cmd = [self._cmd_base] + args
        if sys.platform == "win32":
            kwargs["args"] = " ".join(f'"{p}"' if " " in p else p for p in cmd)
            kwargs["shell"] = True
        else:
            kwargs["args"] = cmd
        return subprocess.run(**kwargs)

    def _install_grepai_if_missing(self) -> str:
        """
        Dynamically detects OS and executes the canonical installation command for grepai.
        Returns the executable name assuming it installs to system PATH.
        """
        if shutil.which("grepai"):
            return "grepai"

        # On Windows, grepai.ps1 usually drops it in AppData\Local\Programs\grepai
        if sys.platform == "win32":
            appdata = os.environ.get("LOCALAPPDATA")
            if appdata:
                win_path = Path(appdata) / "Programs" / "grepai" / "grepai.exe"
                if win_path.exists():
                    return str(win_path)

        self.runtime.hooks.emit(EventType.TOOL_CALL, tool="semantic_search", args={"action": "installing grepai"})

        try:
            if sys.platform == "win32":
                cmd = ["powershell", "-Command", "irm https://raw.githubusercontent.com/yoanbernabeu/grepai/main/install.ps1 | iex"]
                subprocess.run(cmd, check=True)

                appdata = os.environ.get("LOCALAPPDATA")
                if appdata:
                    win_path = Path(appdata) / "Programs" / "grepai" / "grepai.exe"
                    if win_path.exists():
                        self.runtime.hooks.emit(EventType.TOOL_CALL, tool="semantic_search", args={"action": "successfully installed grepai to AppData"})
                        return str(win_path)
            else:
                subprocess.run(
                    ["sh", "-c", "curl -sSL https://raw.githubusercontent.com/yoanbernabeu/grepai/main/install.sh | sh"],
                    check=True
                )

            self.runtime.hooks.emit(EventType.TOOL_CALL, tool="semantic_search", args={"action": "successfully installed grepai"})
            return "grepai"
        except Exception as e:
            self.runtime._append_execution(f"[semantic_search] Failed to install grepai: {e}")
            return "grepai"

    def _ensure_setup(self):
        """
        Ensure grepai is installed, initialized, configured, and indexing.
        """
        if self._setup_done:
            return

        tool_cfg = self.runtime._tool_config("semantic_search")
        work_dir = self.runtime.config.runtime.work_dir

        # 1. Install via shell depending on OS
        self._cmd_base = self._install_grepai_if_missing()

        # Default configurations
        provider = tool_cfg.get("provider", "openai")
        model = tool_cfg.get("model", "voyage-code-3")
        base_url = tool_cfg.get("base_url", "https://api.voyageai.com/v1")
        api_key_env = tool_cfg.get("api_key_env", "VOYAGE_API_KEY")

        grepai_dir = Path(work_dir) / ".grepai"
        config_path = grepai_dir / "config.yaml"

        # 2. Run grepai init and write config ONLY if config doesn't already exist
        if not config_path.exists():
            try:
                self._run_grepai(
                    ["init"],
                    cwd=work_dir, capture_output=True, text=True,
                )
            except Exception as e:
                self.runtime._append_execution(f"[semantic_search] Failed to run grepai init: {e}")

            # 3. Write config.yaml with proper structure
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
                grepai_dir.mkdir(parents=True, exist_ok=True)
                config_path.write_text(config_content)
            except Exception as e:
                self.runtime._append_execution(f"[semantic_search] Warning: Could not write {config_path}: {e}")

        # 4. Start watching to index
        self.runtime.hooks.emit(EventType.TOOL_CALL, tool="semantic_search", args={"action": "starting grepai watch"})
        try:
            env = os.environ.copy()
            if api_key_env in env:
                env["OPENAI_API_KEY"] = env[api_key_env]

            if sys.platform == "win32":
                popen_cmd = f'"{self._cmd_base}" watch --no-ui'
                self._watch_process = subprocess.Popen(
                    popen_cmd, shell=True,
                    cwd=work_dir, env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                self._watch_process = subprocess.Popen(
                    [self._cmd_base, "watch", "--no-ui"],
                    cwd=work_dir, env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            self._wait_for_index(work_dir, env, timeout=120)
        except Exception as e:
            self.runtime._append_execution(f"[semantic_search] Failed to start grepai watch: {e}")

        self._setup_done = True

    def _wait_for_index(self, work_dir: str, env: dict, timeout: int = 120):
        """
        Poll `grepai status` until at least one file is indexed or timeout expires.
        This ensures the initial embedding pass is complete before the first search.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            try:
                r = self._run_grepai(
                    ["status", "--no-ui"],
                    cwd=work_dir, env=env,
                    capture_output=True, text=True, timeout=10,
                )
                if "Files indexed: 0" not in r.stdout:
                    return
            except Exception:
                pass
        self.runtime._append_execution(
            "[semantic_search] Warning: grepai index not ready after "
            f"{timeout}s — search may return partial results."
        )

    def semantic_search(self, query: str, mode: str = "search", depth: int = 2, top_k: int = 5) -> str:
        """
        Semantically search the codebase or trace function dependencies using the
        voyage-code-3 embedding model. Finds code by concept — not text match — so
        you land on exactly the right file and line without reading through files linearly.
        Modes:
          'search'        — find code matching a natural language concept.
          'trace_callers' — find every call-site of a function/method.
          'trace_callees' — find everything a function/method calls internally.
          'trace_graph'   — full dependency tree up to `depth` levels deep. Use this
                            before fixing a function that might break its dependents —
                            it reveals the blast radius so you don't introduce regressions.
        Use `top_k` to control how many results you get back (default 5).
        DO NOT use this tool when you already know the exact file path and lines — just `read_file`
        instead. Also skip it for tiny files you can read in one shot or for simple
        text matching where `run_command` with grep is faster.
        """
        self._ensure_setup()

        tool_cfg = self.runtime._tool_config("semantic_search")
        timeout = tool_cfg.get("timeout", 60)
        max_results = tool_cfg.get("max_results", top_k)
        max_chars = tool_cfg.get("max_output_chars", 8000)

        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="semantic_search",
            args={"query": query, "mode": mode, "depth": depth, "top_k": top_k},
        )

        work_dir = self.runtime.config.runtime.work_dir

        # Inject API key: grepai's openai provider reads OPENAI_API_KEY, so alias it.
        env = os.environ.copy()
        api_key_env = tool_cfg.get("api_key_env", "VOYAGE_API_KEY")
        if api_key_env in env:
            env["OPENAI_API_KEY"] = env[api_key_env]

        if mode == "search":
            cmd_args = ["search", query, "--json"]
        elif mode == "trace_callers":
            cmd_args = ["trace", "callers", query]
        elif mode == "trace_callees":
            cmd_args = ["trace", "callees", query]
        elif mode == "trace_graph":
            cmd_args = ["trace", "graph", query, "--depth", str(depth), "--json"]
        else:
            result = f"[semantic_search] Error: Invalid mode '{mode}'. Use 'search', 'trace_callers', 'trace_callees', or 'trace_graph'."
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="semantic_search", result=result)
            return result

        try:
            r = self._run_grepai(
                cmd_args,
                cwd=work_dir, env=env,
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
                                output += f"\n\n[Showing top {max_results} of {total} results]"
                    except json.JSONDecodeError:
                        pass

                # Character-level safety cap
                if len(output) > max_chars:
                    output = output[:max_chars] + f"\n\n[Output truncated at {max_chars} chars]"

                result = f"=== Semantic Search Results ({mode}) ===\n{output}"
            else:
                result = (
                    f"=== Semantic Search Error ({mode}) ===\n"
                    f"Return Code: {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
                )
        except subprocess.TimeoutExpired:
            cmd = [self._cmd_base] + cmd_args
            result = f"[semantic_search] Command `{' '.join(cmd)}` timed out after {timeout}s."
        except Exception as e:
            result = f"[semantic_search] Execution failed: {e}"

        self.runtime._append_execution(result)
        self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="semantic_search", result=result)
        return result
