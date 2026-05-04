"""
File: terminal.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16
Modified: 2026-04-28

Description:
Cross-platform terminal session management for the CodePilot agentic runtime.

Architectural Notes:
Provides 4 tools: execute, read_output, send_input, terminate_terminal.
Supports Windows (ConPTY via pywinpty), macOS, and Linux (PTY via pexpect).
OS detection happens once at import time. On Windows, build >= 17763
(Windows 10 1809+) is required for ConPTY support.

Both backends produce standard VT100 byte streams consumed by the same
VirtualScreen state machine in vt.py — the emulator is backend-agnostic.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

import os
import platform
import re
import sys
import time
import uuid
from typing import Optional, Dict, List, TYPE_CHECKING

from ..core.vt import VirtualScreen, DEFAULT_COLS, DEFAULT_ROWS
from ..engine.hooks import EventType

if TYPE_CHECKING:
    from ..engine.runtime import Runtime


# ======================================================================
#  OS detection — resolved once at import time
# ======================================================================

IS_WINDOWS = sys.platform == "win32"
DEFAULT_SHELL = "powershell" if IS_WINDOWS else "bash"
VALID_SHELLS = {"bash", "powershell", "cmd"}

if IS_WINDOWS:
    # Verify ConPTY support (Windows 10 build 17763 / version 1809+)
    _win_build = sys.getwindowsversion().build if hasattr(sys, "getwindowsversion") else 0
    if _win_build < 17763:
        raise EnvironmentError(
            f"CodePilot requires Windows 10 build 17763+ for ConPTY support "
            f"(detected build {_win_build}). Please update Windows."
        )
    import winpty  # pywinpty
else:
    import pexpect


# ======================================================================
#  PTY Backends — unified interface over pexpect / pywinpty
# ======================================================================

class _PosixPtyBackend:
    """POSIX PTY backend using pexpect (Linux / macOS)."""

    def __init__(self, work_dir: str, cols: int, rows: int, shell: str = "bash"):
        # bash is the only POSIX shell we spawn; the shell arg is accepted
        # for interface consistency but ignored on POSIX.
        # echo=True: keep PTY echo ON so interactive programs (input(), readline)
        # correctly advance the cursor on newline. Command echo is handled by
        # pre-skipping the echoed bytes in TerminalSession._bytes_fed.
        self._process = pexpect.spawn(
            "/bin/bash", ["--norc", "--noprofile"],
            cwd=work_dir, encoding="utf-8", echo=True,
            timeout=30, maxread=65536,
        )
        self._process.setwinsize(rows, cols)
        self.pid = self._process.pid

        # Suppress prompt only — do NOT disable echo (needed for interactive programs)
        self._process.sendline('export PS1="" PS2=""')
        time.sleep(0.3)
        try:
            self._process.read_nonblocking(size=65536, timeout=1)
        except (pexpect.TIMEOUT, pexpect.EOF):
            pass

    def sendline(self, text: str):
        """Send text followed by a newline (for shell command submission)."""
        self._process.sendline(text)

    def send(self, text: str):
        """Send raw bytes with no newline appended (for control sequences, TUI keys)."""
        self._process.send(text)

    def expect(self, pattern: re.Pattern, timeout: int):
        """Search for pattern in output. Returns (before_text, match) or raises."""
        try:
            self._process.expect(pattern, timeout=timeout)
            return self._process.before, self._process.match
        except pexpect.TIMEOUT:
            return self._process.before, None
        except pexpect.EOF:
            return self._process.before, "EOF"

    def read_nonblocking(self, size: int = 65536, timeout: float = 0.5) -> str:
        try:
            return self._process.read_nonblocking(size=size, timeout=timeout)
        except (pexpect.TIMEOUT, pexpect.EOF):
            return ""

    def is_alive(self) -> bool:
        return self._process.isalive()

    def terminate(self):
        """Forcefully terminate the process."""
        if self._process.isalive():
            self._process.close(force=True)


class _WindowsPtyBackend:
    """Windows ConPTY backend using pywinpty."""

    def __init__(self, work_dir: str, cols: int, rows: int, shell: str = "powershell"):
        # Resolve shell executable
        if shell == "powershell":
            exe = "powershell.exe"
        elif shell == "cmd":
            exe = os.environ.get("COMSPEC", "cmd.exe")
        else:
            exe = os.environ.get("COMSPEC", "cmd.exe")

        self._pty = winpty.PTY(cols, rows)
        self._pty.spawn(exe, cwd=work_dir)
        self.pid = self._pty.pid

        # Per-shell initialization: suppress prompt and echo
        if shell == "powershell":
            self._pty.write("function prompt { '' }\r\n")
            self._pty.write("$ProgressPreference = 'SilentlyContinue'\r\n")
        else:
            # cmd.exe
            self._pty.write("@echo off\r\n")
            self._pty.write("prompt $S\r\n")
        time.sleep(0.5)
        # Drain startup output
        try:
            self._pty.read(65536, blocking=False)
        except Exception:
            pass

    def sendline(self, text: str):
        """Send text followed by CRLF (for shell command submission)."""
        self._pty.write(text + "\r\n")

    def send(self, text: str):
        """Send raw bytes with no newline appended (for control sequences, TUI keys)."""
        self._pty.write(text)

    def expect(self, pattern: re.Pattern, timeout: int):
        """
        Read from ConPTY until pattern is found or timeout expires.
        Returns (before_text, match) or (before_text, None) on timeout,
        or (before_text, "EOF") on process exit.
        """
        accumulated = ""
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            try:
                chunk = self._pty.read(65536, blocking=False)
            except Exception:
                chunk = ""

            if chunk:
                accumulated += chunk
                m = pattern.search(accumulated)
                if m:
                    before = accumulated[:m.start()]
                    return before, m
            else:
                if not self.is_alive():
                    return accumulated, "EOF"
                time.sleep(0.05)

        return accumulated, None

    def read_nonblocking(self, size: int = 65536, timeout: float = 0.5) -> str:
        try:
            data = self._pty.read(size, blocking=False)
            return data if data else ""
        except Exception:
            return ""

    def is_alive(self) -> bool:
        return self._pty.isalive()

    def terminate(self):
        """Forcefully terminate the process."""
        try:
            self._pty.close()
        except Exception:
            pass


def _create_backend(work_dir: str, cols: int, rows: int, shell: str = None):
    """Factory: returns the correct PTY backend for the current OS."""
    shell = shell or DEFAULT_SHELL
    if IS_WINDOWS:
        return _WindowsPtyBackend(work_dir, cols, rows, shell=shell)
    return _PosixPtyBackend(work_dir, cols, rows, shell=shell)


# ======================================================================
#  TerminalSession — wraps a single persistent process (cross-platform)
# ======================================================================

class TerminalSession:
    """A single persistent terminal process managed via the OS-appropriate PTY backend."""

    def __init__(self, session_id: str, work_dir: str, shell: str = None):
        self.session_id = session_id
        self.shell = shell or DEFAULT_SHELL
        self._marker = f"__CP_{uuid.uuid4().hex[:8]}__"
        self._rc_pattern = re.compile(
            rf'{re.escape(self._marker)}_RC_(\d+)'
        )
        self._cwd_pattern = re.compile(
            rf'{re.escape(self._marker)}_CWD_([^\r\n]*)'
        )

        self._backend = _create_backend(work_dir, DEFAULT_COLS, DEFAULT_ROWS, shell=self.shell)
        self.pid = self._backend.pid

        # Virtual terminal emulator — one per session, reset before each command.
        self._screen: VirtualScreen = VirtualScreen(cols=DEFAULT_COLS, rows=DEFAULT_ROWS)

        # Per-command state
        self.command_tag: int = 0
        self.command_text: str = ""
        self.command_output_buffer: str = ""
        self.status: str = "idle"
        self.return_code: Optional[int] = None
        self.last_used: float = time.time()
        self.cwd: str = work_dir
        self._bytes_fed: int = 0   # total raw chars fed to VT screen this command

    def _build_full_cmd(self, command: str) -> str:
        """Wrap user command with RC/CWD markers appropriate for the shell type."""
        if self.shell == "powershell":
            return (
                f'Write-Host "{self._marker}_START"; '
                f'{command}; $__cp_rc=$LASTEXITCODE; '
                f'Write-Host "{self._marker}_CWD_$(Get-Location)"; '
                f'Write-Host "{self._marker}_RC_$__cp_rc"'
            )
        elif self.shell == "cmd":
            return (
                f'echo {self._marker}_START& '
                f'{command} & set __cp_rc=%ERRORLEVEL% & '
                f'echo {self._marker}_CWD_%CD% & '
                f'echo {self._marker}_RC_%__cp_rc%'
            )
        else:
            # bash (POSIX default)
            return (
                f'printf "{self._marker}_START\\n"; '
                f'{command}; __cp_rc=$?; '
                f'printf "{self._marker}_CWD_%s\\n" "$PWD"; '
                f'echo "{self._marker}_RC_${{__cp_rc}}"'
            )

    def run(self, command: str, timeout: int):
        """Execute a command. Returns (output, completed, return_code)."""
        self.command_tag += 1
        self.command_text = command
        self.command_output_buffer = ""
        self.status = "running"
        self.return_code = None
        self.last_used = time.time()

        # Reset the virtual screen so previous command output doesn't bleed in
        self._screen.reset()
        self._bytes_fed = 0

        full_cmd = self._build_full_cmd(command)
        self._backend.sendline(full_cmd)

        # Discard the PTY command echo. Wait for the START marker and ignore
        # everything before it. This is perfectly robust against line-wrapping.
        start_pat = re.compile(rf"{re.escape(self._marker)}_START\s*\r?\n")
        try:
            self._backend.expect(start_pat, timeout=2)
            # Reset _bytes_fed since we effectively flushed the buffer
            self._bytes_fed = 0
        except Exception:
            pass

        output, completed, rc = self._read_until(timeout, delta=False)
        self.command_output_buffer = output

        # Sync the delta watermark to the current screen state.
        # run() uses snapshot() which never advances _delta_mark — without this,
        # the first delta_snapshot() in a subsequent send_text() would start from
        # position 0 and replay the entire screen (including already-seen output).
        self._screen.reset_delta()

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
            leftover = self._backend.read_nonblocking(size=65536, timeout=0.5)
            if leftover:
                self._screen.feed(leftover)
                new_text = self._extract_cwd_marker(self._screen.delta_snapshot())
                if new_text:
                    self.command_output_buffer += "\n" + new_text
                    return new_text, True, self.return_code
            # No new data — signal full-mode (caller will do context collapse + full re-read)
            return "", True, self.return_code

        # Command still running — read delta of new bytes
        delta, completed, rc = self._read_until(timeout, delta=True)
        if delta:
            self.command_output_buffer += delta
        if completed:
            self.status = "completed"
            self.return_code = rc
        return delta, completed, rc

    def send_text(self, text: str, timeout: int):
        """Send raw input text. Returns (new_output, completed, return_code).
        Text is sent exactly as-is — no newline is appended. Include \n explicitly
        for line-ending text (e.g. 'answer\n'). Control sequences (\x03, \x1b[B)
        and multi-keystroke runs (\x1b[B * 20) work correctly because no \r\n
        is injected after them.
        """
        self.last_used = time.time()
        self._backend.send(text)   # raw send — no \r\n appended

        # Full snapshot — shows the complete current terminal state.
        # Delta is wrong here: TUIs need full screen (header + content + footer),
        # and interactive prompts need complete context. Token deduplication is
        # already handled at a higher level by _collapse_previous_outputs().
        output, completed, rc = self._read_until(timeout, delta=False)
        self.command_output_buffer = output

        # Sync delta watermark so any subsequent read_new() starts fresh
        self._screen.reset_delta()

        if completed:
            self.status = "completed"
            self.return_code = rc
        return output, completed, rc

    def is_alive(self) -> bool:
        return self._backend.is_alive()

    def close(self):
        """Terminate the terminal process."""
        self._backend.terminate()

    def _read_until(self, timeout: int, delta: bool = False):
        """
        Read PTY output until the RC marker is found or timeout expires.

        All raw bytes are fed into the virtual screen emulator.
        Returns (output, completed, rc) where *output* is the clean rendered
        text from the virtual screen — either a full snapshot or a delta
        (new content only) depending on the *delta* flag.

        Pexpect buffer carry-over: after a timeout, pexpect keeps unmatched
        bytes in its internal buffer and includes them again in the next
        expect()'s `before`. We track _bytes_fed to skip already-seen bytes,
        so the VT screen only ever receives genuinely new bytes.
        """
        def _render() -> str:
            text = self._screen.delta_snapshot() if delta else self._screen.snapshot()
            return self._extract_cwd_marker(text)

        before, match = self._backend.expect(self._rc_pattern, timeout)

        if before:
            # Skip bytes already fed in a previous (timed-out) read
            new_bytes = before[self._bytes_fed:]
            self._bytes_fed += len(new_bytes)
            if new_bytes:
                self._screen.feed(new_bytes)

        if match is None:
            # Timeout — the RC/CWD marker was never printed (command still running).
            # Read the bash process's real CWD directly from the OS so we always
            # report an accurate directory even for long-running commands like
            # `docker compose up` where the shell already cd'd before forking.
            try:
                proc_cwd = os.readlink(f"/proc/{self._backend.pid}/cwd")
                if proc_cwd:
                    self.cwd = proc_cwd
            except (OSError, AttributeError):
                pass  # /proc not available (macOS/Windows) — keep existing cwd
            output = _render()
            return output, False, None
        elif match == "EOF":
            # Process exited
            output = _render()
            return output, True, -1
        else:
            # RC marker found
            output = _render()
            rc = int(match.group(1))
            return output, True, rc

    def _extract_cwd_marker(self, text: str) -> str:
        """Update current terminal cwd from marker lines and remove marker from output."""
        matches = list(self._cwd_pattern.finditer(text))
        if matches:
            self.cwd = matches[-1].group(1).strip() or self.cwd
            text = self._cwd_pattern.sub("", text)
            text = text.strip("\r\n")
        return text


# ======================================================================
#  TerminalManager — session lifecycle + agent tool methods
# ======================================================================

class TerminalManager:
    """Manages terminal sessions and provides the 4 agent tool methods."""

    def __init__(self, runtime: "Runtime"):
        self.runtime = runtime
        self._sessions: Dict[str, TerminalSession] = {}
        self._session_order: List[str] = []          # most recent first
        self._output_history: Dict[tuple, List[str]] = {}  # (sid, cmd_tag) -> [result strings]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_default_terminal(self):
        """Start the default 'main' terminal session."""
        work_dir = self.runtime.config.runtime.work_dir
        try:
            session = TerminalSession("main", work_dir)
            self._sessions["main"] = session
            self._session_order.insert(0, "main")
        except Exception as e:
            self.runtime._append_execution(
                f"[terminal] Warning: Could not start default terminal: {e}"
            )

    def ensure_default_terminal(self):
        """Guarantee the default 'main' session exists and is alive."""
        main = self._sessions.get("main")
        if main is None or not main.is_alive():
            if main is not None:
                self._sessions.pop("main", None)
                if "main" in self._session_order:
                    self._session_order.remove("main")
            self.start_default_terminal()

    def get_prompt_info(self) -> str:
        """Generate terminal info for the system prompt."""
        if not self._sessions:
            return "No terminal sessions active."

        lines = []
        for i, sid in enumerate(self._session_order):
            session = self._sessions.get(sid)
            if session and session.is_alive():
                tag = " (most recent)" if i == 0 else ""
                lines.append(
                    f'  session_id="{sid}" | shell: {session.shell} | PID: {session.pid} | status: {session.status} | cwd: {session.cwd}{tag}'
                )
        return "\n".join(lines) if lines else "No terminal sessions active."

    def cleanup_all(self):
        """Terminate all terminal sessions."""
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
        new_terminal: bool = False,
        shell: str = None,
    ) -> str:
        """
        Run a command on a terminal session. Captures output up to timeout seconds.
        Returns output with status: 'completed' (has return_code) or 'running'
        (timed out — use read_output() to wait more, send_input() for prompts).
        For a new terminal session: set new_terminal=True with a unique session_id.
        Optional shell parameter (only used with new_terminal=True): 'bash' (default
        on Linux/macOS), 'powershell' (default on Windows), or 'cmd'.
        Write commands in the syntax of the active shell shown in the session list.
        Never import subprocess or os — always use this tool.
        """
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="execute",
            args={
                "session_id": session_id, "command": command,
                "timeout": timeout, "new_terminal": new_terminal,
                "shell": shell,
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
                result = f"[terminal:{session_id}] Permission denied: {command}"
                self.runtime._append_execution(result)
                self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="execute", result=result)
                return result

        # Create new terminal if requested
        if new_terminal:
            if session_id in self._sessions:
                result = f"[terminal:{session_id}] Error: session '{session_id}' already exists."
                self.runtime._append_execution(result)
                self.runtime.hooks.emit(
                    EventType.TOOL_RESULT, tool="execute", result=result,
                )
                return result
            # Validate shell choice
            if shell is not None and shell not in VALID_SHELLS:
                result = (
                    f"[terminal:{session_id}] Error: unknown shell '{shell}'. "
                    f"Valid options: {', '.join(sorted(VALID_SHELLS))}."
                )
                self.runtime._append_execution(result)
                self.runtime.hooks.emit(
                    EventType.TOOL_RESULT, tool="execute", result=result,
                )
                return result
            try:
                work_dir = self.runtime.config.runtime.work_dir
                session = TerminalSession(session_id, work_dir, shell=shell)
                self._sessions[session_id] = session
            except Exception as e:
                result = f"[terminal:{session_id}] Error creating terminal: {e}"
                self.runtime._append_execution(result)
                self.runtime.hooks.emit(
                    EventType.TOOL_RESULT, tool="execute", result=result,
                )
                return result

        # Validate session
        session = self._sessions.get(session_id)
        if session is None:
            result = (
                f"[terminal:{session_id}] Error: session '{session_id}' not found. "
                "Use new_terminal=True to create it."
            )
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(
                EventType.TOOL_RESULT, tool="execute", result=result,
            )
            return result

        if not session.is_alive():
            result = f"[terminal:{session_id}] Error: terminal process is dead."
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
            result = f"[terminal:{session_id}] Execution error: {e}"
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
        Read output from the latest command on a terminal session.
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
            result = f"[terminal:{session_id}] Error: session not found."
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(
                EventType.TOOL_RESULT, tool="read_output", result=result,
            )
            return result

        self._touch_session(session_id)

        try:
            delta, completed, rc = session.read_new(timeout)
        except Exception as e:
            result = f"[terminal:{session_id}] Read error: {e}"
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
        Send raw input to an interactive process. Returns new output after sending.

        Text is sent exactly as-is — include \n explicitly when you want a newline
        (e.g. 'my answer\n'). Do NOT add \n for control sequences or TUI keys.

        Examples:
          'answer\n'           — text answer + Enter (for prompts)
          '\x03'              — Ctrl+C (interrupt foreground process)
          '\x1b[B'            — Down Arrow (one line)
          '\x1b[B' * 20       — 20 Down Arrows at once (fast TUI navigation)
          '\x1b[A'            — Up Arrow
          '\x04'              — Ctrl+D (EOF / exit REPL)
          '\x1b'              — Escape key
        All of these work cross-platform.
        """
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="send_input",
            args={"session_id": session_id, "text": text, "timeout": timeout},
            label=f"Sending input to console [{session_id}]",
        )

        session = self._sessions.get(session_id)
        if session is None:
            result = f"[terminal:{session_id}] Error: session not found."
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(
                EventType.TOOL_RESULT, tool="send_input", result=result,
            )
            return result

        self._touch_session(session_id)

        # Warn the LLM if plain text input is missing a trailing newline.
        # Control sequences (Ctrl+C, arrows, Ctrl+D, Escape) intentionally
        # omit \n so we only flag printable text that looks like a prompt answer.
        _is_control = text.startswith(("\x03", "\x04", "\x1b")) or (len(text) <= 2 and not text[-1:].isalnum())
        _missing_newline = not _is_control and not text.endswith("\n") and text.isprintable()

        try:
            delta, completed, rc = session.send_text(text, timeout)
        except Exception as e:
            result = f"[terminal:{session_id}] Send error: {e}"
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

        if _missing_newline:
            result += (
                "\n\n\u26a0 WARNING: Your text input did not end with \\n. "
                "Interactive prompts require an explicit \\n to submit "
                "(e.g. 'my answer\\n'). Without it the process is still "
                "waiting for Enter. Add \\n to your text and resend."
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
    # Tool 4: terminate_terminal
    # ------------------------------------------------------------------

    def terminate_terminal(self, session_id: str) -> str:
        """
        Destroy a terminal session entirely. Kills the process and removes it.
        Use this as a last resort when a process is unresponsive to Ctrl+C
        (send_input with "\\x03"). This is a hard kill — the session becomes
        unusable after this call.
        """
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="terminate_terminal",
            args={"session_id": session_id},
            label=f"Terminating terminal session [{session_id}]",
        )

        session = self._sessions.pop(session_id, None)
        if session is None:
            result = f"[terminal:{session_id}] Error: session not found."
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(
                EventType.TOOL_RESULT, tool="terminate_terminal", result=result,
            )
            return result

        session.close()
        if session_id in self._session_order:
            self._session_order.remove(session_id)

        result = f"[terminal:{session_id}] Session destroyed (PID {session.pid} terminated)."

        self.runtime._append_execution(result)
        self.runtime.hooks.emit(
            EventType.TOOL_RESULT, tool="terminate_terminal", result=result,
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
        tag = f"[terminal:{session_id}:cmd{cmd_tag}]"
        # Strip literal newlines from label so it stays on one header line
        safe_label = label.replace("\n", "\\n").replace("\r", "") if label else ""
        header = f"{tag} {safe_label}" if safe_label else tag

        rc_str = f" | return_code: {rc}" if rc is not None else ""
        footer = f"[status: {status}{rc_str} | pid: {pid} | cwd: {self._sessions[session_id].cwd}]"

        if output.strip():
            return f"{header}\n{output}\n{footer}"
        return f"{header}\n{footer}"

    def _collapse_previous_outputs(self, session_id: str, command_tag: int):
        """Replace previous terminal outputs for this command in conversation
        history with a short notice, to save tokens on full re-read."""
        key = (session_id, command_tag)
        tag = f"[terminal:{session_id}:cmd{command_tag}]"
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
