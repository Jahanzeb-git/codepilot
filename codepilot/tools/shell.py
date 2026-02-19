import subprocess
from typing import TYPE_CHECKING

from ..engine.hooks import EventType

if TYPE_CHECKING:
    from ..engine.runtime import Runtime


class ShellTools:

    def __init__(self, runtime: "Runtime"):
        self.runtime = runtime

    def run_command(self, command: str, timeout: int = None, background: bool = False) -> str:
        """
        Run a shell command in the workspace directory.
        Returns a string with STDOUT, STDERR, and Return Code.
        A non-zero Return Code means failure — read the error and fix it before proceeding.
        Use background=True to start long-running processes (servers, watchers) without blocking.
        Never import subprocess or os directly — always use this tool.
        """
        tool_cfg         = self.runtime._tool_config("run_command")
        effective_timeout = timeout or tool_cfg.get("timeout", 30)

        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="run_command",
            args={"command": command, "timeout": effective_timeout, "background": background},
        )

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

        work_dir = self.runtime.config.runtime.work_dir

        if background:
            proc = subprocess.Popen(
                command, shell=True, cwd=work_dir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            result = f"[run_command] Started in background. PID: {proc.pid}"
        else:
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
