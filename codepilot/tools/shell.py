import subprocess
from typing import List, Tuple, TYPE_CHECKING

from ..engine.hooks import EventType

if TYPE_CHECKING:
    from ..engine.runtime import Runtime


class ShellTools:

    def __init__(self, runtime: "Runtime"):
        self.runtime = runtime
        self._parallel_batch: List[Tuple[str, int]] = []

    # ------------------------------------------------------------------
    # Per-step lifecycle
    # ------------------------------------------------------------------

    def reset_step(self):
        """Clear any leftover parallel batch state from the previous step."""
        self._parallel_batch = []

    # ------------------------------------------------------------------
    # Public tool
    # ------------------------------------------------------------------

    def run_command(
        self,
        command: str,
        timeout: int = None,
        background: bool = False,
        execution: str = "inline",
    ) -> str:
        """
        Run a shell command in the workspace directory.
        Returns STDOUT, STDERR, and Return Code.
        A non-zero Return Code means failure — read the error and fix it.
        Never import subprocess or os directly — always use this tool.

        execution='inline'   — run immediately, wait for result (default).
        execution='parallel' — queue command; all parallel commands execute
                               simultaneously after the control block finishes.
                               Use for independent tasks (e.g. two test suites).
        background=True      — start long-running processes without blocking.
        """
        tool_cfg          = self.runtime._tool_config("run_command")
        effective_timeout = timeout or tool_cfg.get("timeout", 30)

        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="run_command",
            args={
                "command": command, "timeout": effective_timeout,
                "background": background, "execution": execution,
            },
        )

        # ---- Permission gate ----
        if tool_cfg.get("require_permission", False):
            perm = self.runtime.hooks.emit(
                EventType.PERMISSION_REQUEST, tool="run_command",
                description=f"Execute: {command}",
            )
            approved = bool(perm) if perm is not None else (
                input(f"\n[Permission] Run: {command}\nApprove? [y/N]: ").strip().lower() in ("y", "yes")
            )
            if not approved:
                result = f"[run_command] Permission denied: {command}"
                self.runtime._append_execution(result)
                self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="run_command", result=result)
                return result

        # ---- Parallel: queue for batch execution after the control block ----
        if execution == "parallel":
            self._parallel_batch.append((command, effective_timeout))
            result = f"[run_command] Queued for parallel execution: `{command}`"
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="run_command", result=result)
            return result

        # ---- Background: fire & forget ----
        work_dir = self.runtime.config.runtime.work_dir

        if background:
            proc = subprocess.Popen(
                command, shell=True, cwd=work_dir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            result = f"[run_command] Started in background. PID: {proc.pid}"
        else:
            # ---- Inline: run & wait ----
            try:
                r = subprocess.run(
                    command, shell=True, cwd=work_dir,
                    capture_output=True, text=True, timeout=effective_timeout,
                )
                result = (
                    f"[run_command] `{command}`\n"
                    f"STDOUT:\n{r.stdout}"
                    f"STDERR:\n{r.stderr}"
                    f"Return Code: {r.returncode}"
                )
            except subprocess.TimeoutExpired:
                result = f"[run_command] `{command}` timed out after {effective_timeout}s."

        self.runtime._append_execution(result)
        self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="run_command", result=result)
        return result

    # ------------------------------------------------------------------
    # Parallel batch flush — called by Runtime after exec() completes
    # ------------------------------------------------------------------

    def flush_parallel(self):
        """
        Execute all queued parallel commands simultaneously and collect results.

        Called by the runtime after the control block finishes executing.
        Each command runs in its own subprocess; all start at once and are
        awaited with their respective timeouts.
        """
        if not self._parallel_batch:
            return

        work_dir = self.runtime.config.runtime.work_dir

        # Launch all commands simultaneously
        processes: List[Tuple[str, subprocess.Popen, int]] = []
        for cmd, timeout_val in self._parallel_batch:
            proc = subprocess.Popen(
                cmd, shell=True, cwd=work_dir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            processes.append((cmd, proc, timeout_val))

        # Wait for each and collect results
        for cmd, proc, timeout_val in processes:
            try:
                stdout, stderr = proc.communicate(timeout=timeout_val)
                result = (
                    f"[run_command:parallel] `{cmd}`\n"
                    f"STDOUT:\n{stdout}"
                    f"STDERR:\n{stderr}"
                    f"Return Code: {proc.returncode}"
                )
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()   # drain pipes to avoid zombie
                result = f"[run_command:parallel] `{cmd}` timed out after {timeout_val}s."

            self.runtime._append_execution(result)
            self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="run_command", result=result)

        self._parallel_batch = []
