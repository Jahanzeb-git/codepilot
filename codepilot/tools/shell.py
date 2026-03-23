"""
Pexpect-based shell session management for the CodePilot agentic runtime.

Provides 5 tools: execute, read_output, send_input, send_signal, kill_shell.
Requires POSIX (Linux/macOS). Not supported on Windows.
"""

import os
import re
import signal as sig_module
import time
import uuid
from typing import Optional, Dict, List, TYPE_CHECKING

import pexpect

from ..core.ansi import strip_ansi
from ..engine.hooks import EventType

if TYPE_CHECKING:
    from ..engine.runtime import Runtime


# ======================================================================
#  ShellSession — wraps a single persistent bash process
# ======================================================================

class ShellSession:
    """A single persistent bash process managed via pexpect."""

    def __init__(self, session_id: str, work_dir: str):
        self.session_id = session_id
        self._marker = f"__CP_{uuid.uuid4().hex[:8]}__"
        self._rc_pattern = re.compile(
            rf'{re.escape(self._marker)}_RC_(\d+)'
        )
        self._cwd_pattern = re.compile(
            rf'^{re.escape(self._marker)}_CWD_(.*)$', re.MULTILINE
        )

        self.process = pexpect.spawn(
            '/bin/bash', ['--norc', '--noprofile'],
            cwd=work_dir, encoding='utf-8', echo=False,
            timeout=30, maxread=65536,
        )
        self.pid = self.process.pid

        # Clean environment: suppress prompt and echo
        self.process.sendline('export PS1="" PS2=""')
        self.process.sendline('stty -echo 2>/dev/null || true')
        time.sleep(0.5)
        try:
            self.process.read_nonblocking(size=65536, timeout=1)
        except (pexpect.TIMEOUT, pexpect.EOF):
            pass

        # Per-command state
        self.command_tag: int = 0
        self.command_text: str = ""
        self.command_output_buffer: str = ""
        self.status: str = "idle"
        self.return_code: Optional[int] = None
        self.last_used: float = time.time()
        self.cwd: str = work_dir

    def run(self, command: str, timeout: int):
        """Execute a command. Returns (output, completed, return_code)."""
        self.command_tag += 1
        self.command_text = command
        self.command_output_buffer = ""
        self.status = "running"
        self.return_code = None
        self.last_used = time.time()

        # Append RC echo so we can detect completion + exit code
        full_cmd = (
            f'{command}; __cp_rc=$?; '
            f'printf "{self._marker}_CWD_%s\\n" "$PWD"; '
            f'echo "{self._marker}_RC_${{__cp_rc}}"'
        )
        self.process.sendline(full_cmd)

        output, completed, rc = self._read_until(timeout)
        self.command_output_buffer = output
        if completed:
            self.status = "completed"
            self.return_code = rc
        return output, completed, rc

    def read_new(self, timeout: int):
        """Read new output. Returns (delta, completed, return_code).
        Empty delta with completed=True signals full-mode re-read."""
        self.last_used = time.time()

        if self.status == "completed":
            # Brief check for any straggling data
            try:
                leftover = self.process.read_nonblocking(size=65536, timeout=0.5)
                leftover = strip_ansi(leftover).strip()
                if leftover:
                    self.command_output_buffer += leftover
                    return leftover, True, self.return_code
            except (pexpect.TIMEOUT, pexpect.EOF):
                pass
            # No new data — signal full-mode
            return "", True, self.return_code

        # Command still running — wait for more
        delta, completed, rc = self._read_until(timeout)
        if delta:
            self.command_output_buffer += delta
        if completed:
            self.status = "completed"
            self.return_code = rc
        return delta, completed, rc

    def send_text(self, text: str, timeout: int):
        """Send input text. Returns (new_output, completed, return_code)."""
        self.last_used = time.time()
        self.process.sendline(text)

        delta, completed, rc = self._read_until(timeout)
        if delta:
            self.command_output_buffer += delta
        if completed:
            self.status = "completed"
            self.return_code = rc
        return delta, completed, rc

    def send_sig(self, signal_name: str):
        """Send a signal. SIGINT goes to foreground process (shell survives).
        SIGTERM/SIGKILL terminate the shell process entirely."""
        if signal_name == "SIGINT":
            self.process.sendintr()
            # Wait briefly for command to exit
            try:
                self.process.expect(self._rc_pattern, timeout=3)
                output = strip_ansi(self.process.before).strip()
                if output:
                    self.command_output_buffer += output
                self.status = "completed"
                self.return_code = int(self.process.match.group(1))
            except (pexpect.TIMEOUT, pexpect.EOF):
                pass
        elif signal_name == "SIGTERM":
            self.process.kill(sig_module.SIGTERM)
            self.status = "completed"
            self.return_code = -1
        elif signal_name == "SIGKILL":
            self.process.kill(sig_module.SIGKILL)
            self.status = "completed"
            self.return_code = -1

    def is_alive(self) -> bool:
        return self.process.isalive()

    def close(self):
        """Terminate the shell process."""
        if self.process.isalive():
            self.process.close(force=True)

    def _read_until(self, timeout: int):
        """Read until RC marker or timeout. Returns (output, completed, rc)."""
        try:
            self.process.expect(self._rc_pattern, timeout=timeout)
            output = self._extract_cwd_marker(
                strip_ansi(self.process.before).strip('\r\n')
            )
            rc = int(self.process.match.group(1))
            return output, True, rc
        except pexpect.TIMEOUT:
            output = self._extract_cwd_marker(
                strip_ansi(self.process.before).strip('\r\n')
            )
            return output, False, None
        except pexpect.EOF:
            output = self._extract_cwd_marker(
                strip_ansi(self.process.before).strip('\r\n')
            )
            return output, True, -1

    def _extract_cwd_marker(self, text: str) -> str:
        """Update current shell cwd from marker lines and remove marker from output."""
        matches = list(self._cwd_pattern.finditer(text))
        if matches:
            self.cwd = matches[-1].group(1).strip() or self.cwd
            text = self._cwd_pattern.sub("", text)
            text = text.strip("\r\n")
        return text


# ======================================================================
#  ShellManager — session lifecycle + agent tool methods
# ======================================================================

class ShellManager:
    """Manages shell sessions and provides the 5 agent tool methods."""

    def __init__(self, runtime: "Runtime"):
        self.runtime = runtime
        self._sessions: Dict[str, ShellSession] = {}
        self._session_order: List[str] = []          # most recent first
        self._output_history: Dict[tuple, List[str]] = {}  # (sid, cmd_tag) -> [result strings]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_default_shell(self):
        """Start the default 'main' shell session."""
        work_dir = self.runtime.config.runtime.work_dir
        try:
            session = ShellSession("main", work_dir)
            self._sessions["main"] = session
            self._session_order.insert(0, "main")
        except Exception as e:
            self.runtime._append_execution(
                f"[shell] Warning: Could not start default shell: {e}"
            )

    def ensure_default_shell(self):
        """Guarantee the default 'main' session exists and is alive."""
        main = self._sessions.get("main")
        if main is None or not main.is_alive():
            if main is not None:
                self._sessions.pop("main", None)
                if "main" in self._session_order:
                    self._session_order.remove("main")
            self.start_default_shell()

    def get_prompt_info(self) -> str:
        """Generate shell info for the system prompt."""
        if not self._sessions:
            return "No shell sessions active."

        lines = []
        for i, sid in enumerate(self._session_order):
            session = self._sessions.get(sid)
            if session and session.is_alive():
                tag = " (most recent)" if i == 0 else ""
                lines.append(
                    f'  "{sid}" | PID: {session.pid} | {session.status} | cwd: {session.cwd}{tag}'
                )
        return "\n".join(lines) if lines else "No shell sessions active."

    def cleanup_all(self):
        """Terminate all shell sessions."""
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()
        self._session_order.clear()
        self._output_history.clear()

    # ------------------------------------------------------------------
    # Tool 1: execute
    # ------------------------------------------------------------------

    def execute(
        self,
        session_id: str,
        command: str,
        timeout: int = 10,
        new_shell: bool = False,
    ) -> str:
        """
        Run a command on a shell session. Captures output up to timeout seconds.
        Returns output with status: 'completed' (has return_code) or 'running'
        (timed out — use read_output() to wait more, send_input() for prompts).
        For a new shell session: set new_shell=True with a unique session_id.
        Never import subprocess or os — always use this tool.
        """
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="execute",
            args={
                "session_id": session_id, "command": command,
                "timeout": timeout, "new_shell": new_shell,
            },
            label=f"Running `{command}`",
        )

        # ---- Permission gate ----
        tool_cfg = self.runtime._tool_config("execute")
        if tool_cfg.get("require_permission", False):
            perm = self.runtime.hooks.emit(
                EventType.PERMISSION_REQUEST, tool="execute",
                description=f"Execute: {command}",
            )
            approved = bool(perm) if perm is not None else (
                input(f"\n[Permission] Run: {command}\nApprove? [y/N]: ").strip().lower() in ("y", "yes")
            )
            if not approved:
                result = f"[shell:{session_id}] Permission denied: {command}"
                self.runtime._append_execution(result)
                self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="execute", result=result)
                return result

        # Create new shell if requested
        if new_shell:
            if session_id in self._sessions:
                result = f"[shell:{session_id}] Error: session '{session_id}' already exists."
                self.runtime._append_execution(result)
                self.runtime.hooks.emit(
                    EventType.TOOL_RESULT, tool="execute", result=result,
                )
                return result
            try:
                work_dir = self.runtime.config.runtime.work_dir
                session = ShellSession(session_id, work_dir)
                self._sessions[session_id] = session
            except Exception as e:
                result = f"[shell:{session_id}] Error creating shell: {e}"
                self.runtime._append_execution(result)
                self.runtime.hooks.emit(
                    EventType.TOOL_RESULT, tool="execute", result=result,
                )
                return result

        # Validate session
        session = self._sessions.get(session_id)
        if session is None:
            result = (
                f"[shell:{session_id}] Error: session '{session_id}' not found. "
                "Use new_shell=True to create it."
            )
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(
                EventType.TOOL_RESULT, tool="execute", result=result,
            )
            return result

        if not session.is_alive():
            result = f"[shell:{session_id}] Error: shell process is dead."
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(
                EventType.TOOL_RESULT, tool="execute", result=result,
            )
            return result

        # Touch session order
        self._touch_session(session_id)

        # Execute
        try:
            output, completed, rc = session.run(command, timeout)
        except Exception as e:
            result = f"[shell:{session_id}] Execution error: {e}"
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(
                EventType.TOOL_RESULT, tool="execute", result=result,
            )
            return result

        # Truncate output
        output = self._truncate(output)

        result = self._format_output(
            session_id, session.command_tag, output,
            session.status, session.return_code, session.pid,
            label=f"$ {command}",
        )

        # Track for context deduplication
        key = (session_id, session.command_tag)
        self._output_history[key] = [result]

        self.runtime._append_execution(result)
        self.runtime.hooks.emit(
            EventType.TOOL_RESULT, tool="execute", result=result,
        )
        return result

    # ------------------------------------------------------------------
    # Tool 2: read_output
    # ------------------------------------------------------------------

    def read_output(self, session_id: str, timeout: int = 5) -> str:
        """
        Read output from the latest command on a shell session.
        New output available: returns only new content (non-overlapping).
        No new output (command already done): returns complete output and
        clears previous outputs from context to save tokens.
        """
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="read_output",
            args={"session_id": session_id, "timeout": timeout},
            label=f"Reading console output [{session_id}]",
        )

        session = self._sessions.get(session_id)
        if session is None:
            result = f"[shell:{session_id}] Error: session not found."
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(
                EventType.TOOL_RESULT, tool="read_output", result=result,
            )
            return result

        self._touch_session(session_id)

        try:
            delta, completed, rc = session.read_new(timeout)
        except Exception as e:
            result = f"[shell:{session_id}] Read error: {e}"
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(
                EventType.TOOL_RESULT, tool="read_output", result=result,
            )
            return result

        if delta:
            # DELTA MODE — new output, non-overlapping
            delta = self._truncate(delta)
            result = self._format_output(
                session_id, session.command_tag, delta,
                session.status, session.return_code, session.pid,
                label="(continued output)",
            )
            # Track for deduplication
            key = (session_id, session.command_tag)
            self._output_history.setdefault(key, []).append(result)
        else:
            # FULL MODE — no new data, agent re-reading
            self._collapse_previous_outputs(session_id, session.command_tag)
            full_output = self._truncate(session.command_output_buffer)
            result = self._format_output(
                session_id, session.command_tag, full_output,
                session.status, session.return_code, session.pid,
                label="(complete output)",
            )
            # Reset history with fresh full output
            key = (session_id, session.command_tag)
            self._output_history[key] = [result]

        self.runtime._append_execution(result)
        self.runtime.hooks.emit(
            EventType.TOOL_RESULT, tool="read_output", result=result,
        )
        return result

    # ------------------------------------------------------------------
    # Tool 3: send_input
    # ------------------------------------------------------------------

    def send_input(self, session_id: str, text: str, timeout: int = 5) -> str:
        """
        Send text to an interactive command waiting for input.
        Returns new output captured after sending, within timeout.
        """
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="send_input",
            args={"session_id": session_id, "text": text, "timeout": timeout},
            label=f"Sending input to console [{session_id}]",
        )

        session = self._sessions.get(session_id)
        if session is None:
            result = f"[shell:{session_id}] Error: session not found."
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(
                EventType.TOOL_RESULT, tool="send_input", result=result,
            )
            return result

        self._touch_session(session_id)

        try:
            delta, completed, rc = session.send_text(text, timeout)
        except Exception as e:
            result = f"[shell:{session_id}] Send error: {e}"
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(
                EventType.TOOL_RESULT, tool="send_input", result=result,
            )
            return result

        delta = self._truncate(delta)
        result = self._format_output(
            session_id, session.command_tag, delta,
            session.status, session.return_code, session.pid,
            label=f'(after input: "{text}")',
        )

        # Track for deduplication
        key = (session_id, session.command_tag)
        self._output_history.setdefault(key, []).append(result)

        self.runtime._append_execution(result)
        self.runtime.hooks.emit(
            EventType.TOOL_RESULT, tool="send_input", result=result,
        )
        return result

    # ------------------------------------------------------------------
    # Tool 4: send_signal
    # ------------------------------------------------------------------

    def send_signal(self, session_id: str, signal: str = "SIGINT") -> str:
        """
        Send a signal to the foreground process in a shell.
        'SIGINT' = Ctrl+C (shell survives). 'SIGTERM'/'SIGKILL' = destroy shell.
        """
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="send_signal",
            args={"session_id": session_id, "signal": signal},
            label=f"Sending {signal} to [{session_id}]",
        )

        session = self._sessions.get(session_id)
        if session is None:
            result = f"[shell:{session_id}] Error: session not found."
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(
                EventType.TOOL_RESULT, tool="send_signal", result=result,
            )
            return result

        if signal not in ("SIGINT", "SIGTERM", "SIGKILL"):
            result = (
                f"[shell:{session_id}] Error: unknown signal '{signal}'. "
                "Use 'SIGINT', 'SIGTERM', or 'SIGKILL'."
            )
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(
                EventType.TOOL_RESULT, tool="send_signal", result=result,
            )
            return result

        session.send_sig(signal)

        if signal == "SIGINT":
            result = (
                f"[shell:{session_id}] SIGINT sent — foreground process interrupted. "
                f"Shell is still alive (status: {session.status})."
            )
        else:
            result = (
                f"[shell:{session_id}] {signal} sent — shell process terminated. "
                "Session is no longer usable."
            )
            # Remove dead session
            self._sessions.pop(session_id, None)
            if session_id in self._session_order:
                self._session_order.remove(session_id)

        self.runtime._append_execution(result)
        self.runtime.hooks.emit(
            EventType.TOOL_RESULT, tool="send_signal", result=result,
        )
        return result

    # ------------------------------------------------------------------
    # Tool 5: kill_shell
    # ------------------------------------------------------------------

    def kill_shell(self, session_id: str) -> str:
        """
        Destroy a shell session entirely. Kills the process and removes it.
        """
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="kill_shell",
            args={"session_id": session_id},
            label=f"Killing shell session [{session_id}]",
        )

        session = self._sessions.pop(session_id, None)
        if session is None:
            result = f"[shell:{session_id}] Error: session not found."
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(
                EventType.TOOL_RESULT, tool="kill_shell", result=result,
            )
            return result

        session.close()
        if session_id in self._session_order:
            self._session_order.remove(session_id)

        result = f"[shell:{session_id}] Session destroyed (PID {session.pid} killed)."

        self.runtime._append_execution(result)
        self.runtime.hooks.emit(
            EventType.TOOL_RESULT, tool="kill_shell", result=result,
        )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _touch_session(self, session_id: str):
        """Move session to front of the order (most recently used)."""
        if session_id in self._session_order:
            self._session_order.remove(session_id)
        self._session_order.insert(0, session_id)

    def _truncate(self, text: str) -> str:
        """Cap output length to prevent context pollution."""
        tool_cfg = self.runtime._tool_config("execute")
        max_chars = tool_cfg.get("max_output_chars", 10000)
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[Output truncated at {max_chars} chars]"
        return text

    def _format_output(
        self, session_id: str, cmd_tag: int, output: str,
        status: str, rc: Optional[int], pid: int, label: str = "",
    ) -> str:
        """Format tool output with session tag and metadata."""
        tag = f"[shell:{session_id}:cmd{cmd_tag}]"
        header = f"{tag} {label}" if label else tag

        rc_str = f" | return_code: {rc}" if rc is not None else ""
        footer = f"[status: {status}{rc_str} | pid: {pid} | cwd: {self._sessions[session_id].cwd}]"

        if output.strip():
            return f"{header}\n{output}\n{footer}"
        return f"{header}\n{footer}"

    def _collapse_previous_outputs(self, session_id: str, command_tag: int):
        """Replace previous shell outputs for this command in conversation
        history with a short notice, to save tokens on full re-read."""
        key = (session_id, command_tag)
        tag = f"[shell:{session_id}:cmd{command_tag}]"
        collapse = f"{tag} Output removed to save tokens - You just read output again!"

        previous_outputs = self._output_history.get(key, [])
        if not previous_outputs:
            return

        for msg in self.runtime.messages:
            if msg["role"] != "user":
                continue
            for old_output in previous_outputs:
                if old_output in msg["content"]:
                    msg["content"] = msg["content"].replace(old_output, collapse)

        # Clear history since outputs are now collapsed
        self._output_history[key] = []
