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

import json
import os
import platform
import re
import socket
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
    import pexpect.fdpexpect


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
            cwd=work_dir, encoding="utf-8", codec_errors="replace", echo=True,
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
        except (pexpect.TIMEOUT, pexpect.EOF, OSError):
            return ""

    def is_alive(self) -> bool:
        return self._process.isalive()

    def terminate(self):
        """Forcefully terminate the process."""
        if self._process.isalive():
            self._process.close(force=True)


class _SocketPtyBackend:
    """
    POSIX PTY backend that connects to a running MuxServer via Unix Domain Socket.

    Exposes the identical interface as _PosixPtyBackend so TerminalSession
    requires zero changes. The MuxServer owns the real bash PTY; this class
    is purely a client that sends keystrokes and receives VT100 byte output.

    On connect, the MuxServer sends exactly one JSON handshake line:
        {"pid": <int>, "cols": <int>, "rows": <int>}\n
    We consume that line to learn the bash PID (used by _read_until for
    /proc/<pid>/cwd lookups). All subsequent bytes on the socket are raw
    PTY output.
    """

    def __init__(self, socket_path: str, cols: int, rows: int):
        # Keep a reference to the socket object on self so Python's garbage
        # collector never closes the underlying file descriptor while pexpect
        # is still using it.
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(socket_path)

        # Read the handshake line (terminated by '\n') to extract bash PID.
        # We use a raw blocking read here because the handshake is tiny and
        # arrives immediately after connect.
        raw = b""
        while b"\n" not in raw:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError(
                    f"MuxServer at {socket_path} closed the connection "
                    "before sending the handshake."
                )
            raw += chunk

        handshake_line, remainder = raw.split(b"\n", 1)
        handshake = json.loads(handshake_line.decode())
        self.pid: int = handshake["pid"]

        # The server sends the handshake and then the scrollback replay with
        # two sendall() calls. The kernel may coalesce them into one recv, so
        # bytes after the handshake newline are real PTY output (typically the
        # startup prompt). Keep them and deliver them before reading the
        # socket; discarding them loses shell-integration markers.
        self._pending: bytes = remainder

        # Identify this client as the agent's own control channel. Without
        # this hello, the mux server cannot distinguish an agent command from
        # a human typing into the same shared PTY, which caused false
        # "user just ran a command" notifications.
        try:
            self._sock.sendall(b'{"cp_role": "agent"}\n')
        except OSError:
            pass

        # Wrap the socket's file descriptor in pexpect.fdpexpect so the
        # rest of TerminalSession can use the identical sendline/expect API.
        # We do NOT call setwinsize() here — that is meaningless on a socket
        # fd. Window size is managed by MuxServer directly on master_fd.
        self._process = pexpect.fdpexpect.fdspawn(
            self._sock.fileno(),
            encoding="utf-8",
            codec_errors="replace",
            timeout=30,
            maxread=65536,
        )

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
        # Bytes captured alongside the handshake are older than anything still
        # in the socket, so deliver them first.
        if self._pending:
            chunk = self._pending[:size]
            self._pending = self._pending[size:]
            return chunk.decode("utf-8", "replace")
        try:
            return self._process.read_nonblocking(size=size, timeout=timeout)
        except (pexpect.TIMEOUT, pexpect.EOF, OSError):
            return ""

    def is_alive(self) -> bool:
        """Returns True if the underlying socket is still connected."""
        try:
            # MSG_DONTWAIT + MSG_PEEK: returns 0 bytes on clean close, raises on error.
            data = self._sock.recv(1, socket.MSG_DONTWAIT | socket.MSG_PEEK)
            return len(data) > 0
        except BlockingIOError:
            return True   # No data ready, but socket is alive
        except OSError:
            return False

    def terminate(self):
        """Disconnect from the MuxServer and stop it if we own it."""
        try:
            self._sock.close()
        except OSError:
            pass
        # If this backend started the MuxServer (always true on POSIX), stop
        # it cleanly so bash is terminated and the socket file is removed.
        mux = getattr(self, "_mux_server", None)
        if mux is not None:
            mux.stop()


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


def _create_backend(session_id: str, work_dir: str, cols: int, rows: int, shell: str = None):
    """
    Factory: return the correct PTY backend for the current OS.

    On POSIX (Linux / macOS):
        Always starts a MuxServer daemon thread for this session. The MuxServer
        owns the real bash PTY and exposes it via a session-scoped Unix Domain
        Socket. _SocketPtyBackend connects to that socket so pexpect talks to
        bash through the multiplexer. Any additional client (xterm.js, VS Code)
        can connect to the same socket and share the exact same session.

        Socket path per session: /tmp/codepilot_<session_id>.sock

    On Windows:
        ConPTY via pywinpty is used directly. Multiplexing is not supported.
    """
    import threading
    from ..core.multiplexer import MuxServer

    shell = shell or DEFAULT_SHELL
    if IS_WINDOWS:
        return _WindowsPtyBackend(work_dir, cols, rows, shell=shell)

    # Each session gets its own MuxServer and socket path so multiple
    # concurrent terminal sessions remain fully independent.
    socket_path = f"/tmp/codepilot_{session_id}.sock"

    server = MuxServer(
        socket_path=socket_path,
        cols=cols,
        rows=rows,
        work_dir=work_dir,
    )
    server.start()  # forks bash, binds socket — non-blocking

    # serve() blocks in the selector event loop, so we run it in a daemon
    # thread. Daemon threads are killed automatically when the main process
    # exits, so no explicit cleanup is needed for normal shutdown.
    mux_thread = threading.Thread(
        target=server.serve,
        name=f"codepilot-mux-{session_id}",
        daemon=True,
    )
    mux_thread.start()

    # Give the event loop one tick to reach sel.select() and be ready to
    # accept the first client connection.
    time.sleep(0.1)

    backend = _SocketPtyBackend(socket_path, cols, rows)
    # Store server reference on the backend so:
    # (a) GC does not destroy the server while the backend is alive.
    # (b) terminate() can call server.stop() for a clean shutdown.
    # The backend already identified itself as the agent's control channel
    # via the cp_role hello in its constructor.
    backend._mux_server = server
    return backend


# ----------------------------------------------------------------------
# Echo fallback
# ----------------------------------------------------------------------

_PROMPT_SUFFIXES = ("$ ", "# ", "> ", "% ")

# Shell options that can terminate or pollute a *persistent* bash session if
# they survive an execute call. ``set -e`` exits the shell on the first failed
# command; ``set -u`` aborts the current command list; ``set -x`` traces every
# later command, including the shell-integration prompt hook.
_BASH_OPTION_RESET = "set +e +u +x +v +f +n; set +o pipefail 2>/dev/null"

# Commands that can change shell state in ways a persistent session must not
# inherit: ``set`` can enable errexit/nounset/xtrace, ``source``/``eval`` can
# do so indirectly, ``trap``/``exec`` can replace or later kill the shell, and
# assigning PS0-PS4/PROMPT_COMMAND would break completion detection. Such
# commands run in a contained subshell, and their state is bridged back only
# on success. The match is anchored at a command position so quoted mentions
# like ``git commit -m 'set -x'`` are not affected.
_CONTAINED_BASH_RE = re.compile(
    r"(?m)(?:^|[;\n&|({]|\b(?:then|do|else)\b)\s*"
    r"(?:(?:export\s+)?(?:PROMPT_COMMAND|PS[0-4])\+?="
    r"|set\s+(?:-{1,2}|\+)|source\s|[.]\s|eval\s|trap\s|exec\s)"
)

# xtrace lines emitted by the containment wrapper itself. They are noise from
# the plumbing, not from the user's command, so they never reach the model.
_WRAPPER_TRACE_RE = re.compile(
    r"(?m)^\++ (?:__cp_\w+.*|set \+x|unset __cp_cmd_\w+)\n?"
)

# eval() prefixes parser errors with "bash: eval: line N:" and repeats the
# offending command line. Normalize that to the interactive-shell message the
# model expects, and drop the echoed command (it is already in the tool call).
_EVAL_ECHO_RE = re.compile(r"(?m)^bash: eval: line \d+: `.*'[^\n]*\n?")
_EVAL_PREFIX_RE = re.compile(r"(?m)^bash: eval: line \d+: ")


def _heredoc_delimiter(command: str) -> str:
    """Pick a heredoc terminator that cannot collide with the command text."""
    delim = f"__CP_CMD_{uuid.uuid4().hex[:12]}__"
    while delim in command:
        delim += "X"
    return delim


def _eval_wrapper(command: str, rc: str, cmd_var: str) -> str:
    """
    Return wrapper lines that run *command* via ``eval`` of a heredoc variable.

    The command is passed as heredoc *data*, so the outer shell never parses
    it. A syntax error therefore surfaces as ``eval`` returning 2 instead of
    leaving bash waiting for more input on PS2 — which would swallow every
    later command in the persistent session. It also means the command is
    parsed with the session's real options (for example a previously enabled
    ``extglob``), unlike a separate ``bash -n`` check.
    """
    delim = _heredoc_delimiter(command)
    return (
        f"{cmd_var}=$(command cat <<'{delim}'\n"
        f"{command}\n"
        f"{delim}\n"
        ")\n"
        f'eval "${cmd_var}"\n'
        f"{rc}=$?\n"
        # Turn off xtrace before the wrapper's own tail so its bookkeeping
        # never appears in the model-visible output.
        "set +x\n"
        f"unset {cmd_var}\n"
    )


def _strip_command_echo(output: str, command: str) -> str:
    """
    Remove a leading command echo when the shell did not emit OSC 633;C.

    This is a conservative fallback for shells without PS0 support (or where
    the user overrode PS0). It only strips when the output begins with the
    exact command text followed by a newline, or when a single prompt line
    ends with the exact command. Anything ambiguous is left untouched: real
    output is never guessed away.
    """
    if not output or not command:
        return output

    normalized = output.replace("\r\n", "\n").replace("\r", "\n")
    cmd = command.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not cmd:
        return output

    if normalized.startswith(cmd):
        rest = normalized[len(cmd):]
        if rest == "" or rest.startswith("\n"):
            return rest.lstrip("\n")

    first, _, rest = normalized.partition("\n")
    if first.endswith(cmd):
        prefix = first[: len(first) - len(cmd)].rstrip()
        if any(prefix.endswith(suffix.rstrip()) for suffix in _PROMPT_SUFFIXES):
            return rest

    return output


# ======================================================================
#  TerminalSession — wraps a single persistent process (cross-platform)
# ======================================================================

class TerminalSession:
    """A single persistent terminal process managed via the OS-appropriate PTY backend."""

    def __init__(self, session_id: str, work_dir: str, shell: str = None, on_user_command=None):
        self.session_id = session_id
        self.shell = shell or DEFAULT_SHELL
        self.on_user_command = on_user_command
        self._marker = f"__CP_{uuid.uuid4().hex[:8]}__"
        self._rc_pattern = re.compile(
            rf'{re.escape(self._marker)}_RC_(\d+)'
        )
        self._cwd_pattern = re.compile(
            rf'{re.escape(self._marker)}_CWD_([^\r\n]*)'
        )
        self.agent_active_command = False

        # Human-input tracking. The notification feature must fire only for a
        # command a human actually submitted to the *main* terminal. Output
        # alone is never proof of human activity: an agent command can produce
        # output after its completion marker (multi-line input, background
        # children, straggling PTY bytes). The multiplexer tags the agent's own
        # client, and only keystrokes from other clients reach _on_human_input.
        self.human_input_active = False
        self.human_input_pending = False

        self._backend = _create_backend(self.session_id, work_dir, DEFAULT_COLS, DEFAULT_ROWS, shell=self.shell)
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
        self._pending_script: Optional[str] = None  # scratch file for this command
        self._startup_time: float = time.time()  # grace period for bash init
        self._needs_startup_drain: bool = True   # discard shell-startup prompt bytes once
        
        import threading
        self._lock = threading.Lock()

        # Wire human keystroke detection only for the runtime-initialised
        # ``main`` session. Agent-created and human-created sessions never
        # notify the agent about "user commands". The callback is installed
        # after _lock/_screen exist because it may run on the mux thread.
        mux = getattr(self._backend, "_mux_server", None)
        if mux is not None and self.on_user_command is not None and self.session_id == "main":
            mux.set_human_input_callback(self._on_human_input)

        self._bg_thread = threading.Thread(target=self._bg_listener, daemon=True)
        self._bg_thread.start()

    def _on_human_input(self, data: bytes):
        """Record keystrokes from a non-agent (human) mux client.

        Called on the multiplexer thread *before* the bytes are forwarded to
        bash. We only treat a newline/Enter as a command submission; moving
        the cursor or typing characters must not queue a notification.
        """
        submitted = b"\r" in data or b"\n" in data
        with self._lock:
            if self.agent_active_command:
                # The agent owns the PTY right now. Its output must never be
                # disturbed (or attributed to a human) because someone typed
                # concurrently. The keystrokes are still forwarded to bash.
                return
            if not self.human_input_active:
                # Start the notification window exactly at the first human
                # keystroke. Clear the rendered agent output so the queued
                # text contains the human command and its result only, not
                # stale agent output still visible on screen. Shell-integration
                # counters/cwd are preserved by clear_rendered().
                self.human_input_active = True
                self._screen.clear_rendered()
                # Any completion flag still set belongs to output that
                # preceded this human command; never let it fire the
                # notification for the new command.
                self._screen.command_finished = False
            if submitted:
                self.human_input_pending = True

    def _bg_listener(self):
        """Background thread to read socket when agent is idle and detect human commands.

        Grace period: ignores the first 3 seconds after session creation to
        skip bash startup noise (rcfile sourcing, PS1/PROMPT_COMMAND setup,
        OSC 633 shell integration init). Without this, the initial
        __cp_command_finished() from PROMPT_COMMAND fires a false
        'user ran a command' notification.

        Output alone is never treated as human activity. A notification is
        emitted only when the multiplexer saw a non-agent client submit a
        command (``human_input_pending``) AND this is the runtime-initialised
        ``main`` session. Agent commands are therefore structurally unable to
        trigger this path, even if their bytes arrive after completion.
        """
        import time
        while True:
            if not self._backend.is_alive():
                break

            with self._lock:
                if not getattr(self, 'agent_active_command', False) and "bash" in self.shell:
                    # Read chunks directly into screen (timeout=0 prevents blocking inside lock)
                    chunk = self._backend.read_nonblocking(size=65536, timeout=0)
                    if chunk:
                        self._screen.feed(chunk)
                        if getattr(self._screen, 'command_finished', False):
                            self.cwd = self._screen.cwd or self.cwd
                            self._screen.command_finished = False
                            # Skip notifications during startup grace period —
                            # bash init (rcfile, PROMPT_COMMAND) fires OSC 633;D
                            # which looks like a completed command.
                            if time.time() - self._startup_time < 3.0:
                                self.human_input_active = False
                                self.human_input_pending = False
                                self._screen.reset_delta()
                            elif (
                                self.session_id == "main"
                                and self.human_input_pending
                                and self.on_user_command
                            ):
                                output = self._screen.delta_snapshot().strip()
                                self.human_input_active = False
                                self.human_input_pending = False
                                if output:
                                    self.on_user_command(self.session_id, output)
                            else:
                                # Agent-owned or untracked output: never notify.
                                # Advance the watermark so the next genuine human
                                # command starts from a clean delta.
                                self._screen.reset_delta()
            time.sleep(0.05)

    def _build_full_cmd(self, command: str) -> str:
        """Return the exact text to type into the shell for *command*."""
        if "bash" in self.shell:
            # Never type the protection wrapper into the PTY. The shared UI
            # xterm shows every echoed byte, so a wrapper looks like garbage
            # the agent did not ask for. Write the command body to a private
            # script instead: bash echoes only the short trigger, and the
            # script erases that line before producing any output.
            #
            # A sourced file is also parse-safe: an incomplete quote or `if`
            # hits EOF in the file and returns to the prompt, instead of
            # leaving the interactive shell waiting on PS2 and swallowing
            # every later command.
            if _CONTAINED_BASH_RE.search(command):
                return self._script_command(self._wrap_contained_bash(command))
            return self._script_command(self._wrap_guarded_bash(command))
        elif self.shell == "powershell":
            return (
                f'Write-Host "{self._marker}_START"; '
                f'{command}; $__cp_rc=$LASTEXITCODE; '
                f'Write-Host "{self._marker}_CWD_$(Get-Location)"; '
                f'Write-Host "{self._marker}_RC_$__cp_rc"'
            )
        else:
            return (
                f'echo {self._marker}_START& '
                f'{command} & set __cp_rc=%ERRORLEVEL% & '
                f'echo {self._marker}_CWD_%CD% & '
                f'echo {self._marker}_RC_%__cp_rc%'
            )

    def _wrap_guarded_bash(self, command: str) -> str:
        """
        Run a command in the current shell with option hygiene.

        The reset lines run before and after the command so ``set -e/-u/-x``
        and ``pipefail`` cannot leak into the next execute call. The command is
        passed to ``eval`` as heredoc data, so a syntax error is reported as a
        normal failed result instead of leaving bash on PS2. The whole thing is
        one brace group, so bash emits a single OSC 633;C/D pair.
        """
        tag = self._marker.strip("_")
        rc = f"__cp_rc_{tag}"
        cmd_var = f"__cp_cmd_{tag}"
        return (
            "{\n"
            f"{_BASH_OPTION_RESET}\n"
            f"{_eval_wrapper(command, rc, cmd_var)}"
            f"{_BASH_OPTION_RESET}\n"
            f"(exit ${rc})\n"
            "}"
        )

    def _wrap_contained_bash(self, command: str) -> str:
        """
        Run a command that changes shell options in a contained subshell.

        A command like ``set -e; make`` must abort at the first failure, but
        it must not abort the *persistent* shell. A subshell gives exactly
        that semantics. On success, its working directory, variables, exported
        environment, aliases, and functions are copied back into the session
        so the usual persistence guarantees still hold. On failure the state
        is deliberately not copied back: a failed script should not leave
        half-applied ``cd``/``export`` changes behind.
        """
        tag = self._marker.strip("_")
        state = f"__cp_state_{tag}"
        dump = f"__cp_dump_{tag}"
        var = f"__cp_v_{tag}"
        decl = f"__cp_d_{tag}"
        flags = f"__cp_fl_{tag}"
        fn = f"__cp_f_{tag}"
        rc = f"__cp_rc_{tag}"
        cmd_var = f"__cp_cmd_{tag}"
        return (
            "{\n"
            f"{_BASH_OPTION_RESET}\n"
            f"{state}=$(mktemp /tmp/cp_state.XXXXXX 2>/dev/null) || {state}=/tmp/cp_state_$$_{tag}\n"
            "(\n"
            f"{dump}() {{\n"
            "set +e +u +x\n"
            "IFS=$' \\t\\n'\n"
            "{\n"
            "printf 'cd -- %q\\n' \"$PWD\"\n"
            f"for {var} in $(compgen -v); do\n"
            f"case \"${var}\" in __cp_*|BASH*|FUNCNAME|GROUPS|DIRSTACK|PIPESTATUS|LINENO|RANDOM|SRANDOM|SECONDS|EPOCH*|HISTCMD|OPTIND|OPTARG|COMP_*|MAIL*|PS1|PS2|PS4|PROMPT_COMMAND|PWD|OLDPWD|SHLVL|_|IFS) continue ;; esac\n"
            f"{decl}=$(declare -p \"${var}\" 2>/dev/null) || continue\n"
            # Readonly variables (EUID, UID, PPID, HOSTTYPE, ...) cannot be
            # re-declared, and their values never change anyway.
            f"{flags}=${{{decl}#declare -}}; {flags}=${{{flags}%% *}}\n"
            f"case \"${flags}\" in *r*) continue ;; esac\n"
            f"printf '%s\\n' \"${decl}\"\n"
            "done\n"
            "alias -p\n"
            f"for {fn} in $(compgen -A function); do\n"
            f"case \"${fn}\" in __cp_*) continue ;; esac\n"
            f"declare -f \"${fn}\"\n"
            "done\n"
            f"}} > \"${state}\" 2>/dev/null\n"
            "}\n"
            f"trap {dump} EXIT\n"
            f"{_eval_wrapper(command, rc, cmd_var)}"
            f"(exit ${rc})\n"
            ")\n"
            f"{rc}=$?\n"
            f"if [ \"${rc}\" -eq 0 ] && [ -s \"${state}\" ]; then\n"
            f". \"${state}\" >/dev/null 2>&1\n"
            "fi\n"
            f"rm -f \"${state}\"\n"
            f"(exit ${rc})\n"
            "}"
        )

    def _script_command(self, body: str) -> str:
        """
        Write *body* to a private script and return a short source command.

        Sourcing a file keeps the PTY stream clean: bash echoes only the
        trigger, and the script erases even that line before producing output.
        The script's own text is never echoed, so the shared xterm shows the
        command's output instead of the safety wrapper.
        """
        tag = self._marker.strip("_").removeprefix("CP_")
        path = f"/tmp/cp_{tag}_{self.command_tag}.sh"
        erase = "printf '\\033[1A\\033[2K\\r'\n"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(erase + body + "\n")
            self._pending_script = path
            return f". {path}"
        except OSError:
            # Correctness first: if the scratch file cannot be written, fall
            # back to typing the wrapper.
            return body

    def _cleanup_script(self) -> None:
        """Remove this command's scratch script, if any."""
        path = self._pending_script
        self._pending_script = None
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _drain_pending(self, timeout: float = 2.0) -> None:
        """Consume the shell's startup prompt before the first command.

        A fresh shell emits PROMPT_COMMAND's OSC 633;D and its initial prompt
        asynchronously. If those bytes are read during the first real command,
        the stale D can be mistaken for the command finishing. Wait for the
        prompt-end marker (or the startup D), then clear the completed flag so
        the first command starts from a clean lifecycle state.

        This runs once per session, before the first command, so later output
        is never discarded.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self._backend.read_nonblocking(size=65536, timeout=0.05)
            if chunk:
                self._screen.feed(chunk)
                if self._screen.osc_633_b_count > 0 or self._screen.command_finished:
                    break
            elif self._screen.osc_633_b_count > 0:
                break
            else:
                time.sleep(0.01)
        self._screen.command_finished = False

    def run(self, command: str, timeout: int):
        """Execute a command. Returns (output, completed, return_code)."""
        self.command_tag += 1
        self.command_text = command
        self.command_output_buffer = ""
        self.status = "running"
        self.return_code = None
        self.last_used = time.time()
        self._cleanup_script()

        with self._lock:
            self.agent_active_command = True
            # An agent command takes ownership of the terminal. Discard any
            # half-typed human input state so a stale flag can never attach
            # this command's completion to a human notification.
            self.human_input_active = False
            self.human_input_pending = False
            # A fresh shell may still have startup prompt bytes buffered.
            # Drain them once so a stale OSC 633;D cannot be mistaken for
            # this command's completion.
            if self._needs_startup_drain:
                self._drain_pending()
                self._needs_startup_drain = False
            # Reset the virtual screen so previous command output doesn't bleed in
            self._screen.reset()
            self._screen.begin_agent_command()
            self._bytes_fed = 0
            c_before = self._screen.osc_633_c_count
            full_cmd = self._build_full_cmd(command)
            try:
                self._backend.sendline(full_cmd)
            except BaseException:
                # The shell may have died (e.g. a prior ``set -e`` failure).
                # Never leave the session thinking an agent command is still
                # in flight.
                self.agent_active_command = False
                self._cleanup_script()
                raise

        if "bash" not in self.shell:
            # Discard the PTY command echo. Wait for the START marker and ignore
            # everything before it.
            start_pat = re.compile(rf"{re.escape(self._marker)}_START\s*\r?\n")
            try:
                self._backend.expect(start_pat, timeout=2)
                self._bytes_fed = 0
            except Exception:
                pass
        else:
            # Wait a tiny bit for the command echo to start if needed, but VirtualScreen handles it
            pass

        try:
            output, completed, rc = self._read_until(timeout, delta=False)
        except BaseException:
            # Keep the ownership flag consistent if reading fails.
            with self._lock:
                self.agent_active_command = False
            raise
        finally:
            # bash has already opened/parsed the script; removing the path is
            # safe even if the command itself is still running.
            self._cleanup_script()

        # Fallback for shells that do not emit OSC 633;C (no PS0 support or a
        # user override). Only used when the marker was genuinely absent.
        # Compare against the exact text sent to the PTY, which may be a
        # brace-wrapped multi-line block rather than the raw command.
        if "bash" in self.shell and self._screen.osc_633_c_count == c_before:
            output = _strip_command_echo(output, full_cmd)

        self.command_output_buffer = output

        # Sync the delta watermark to the current screen state.
        # run() uses snapshot() which never advances _delta_mark — without this,
        # the first delta_snapshot() in a subsequent send_text() would start from
        # position 0 and replay the entire screen (including already-seen output).
        self._screen.reset_delta()

        if completed:
            self.status = "completed"
            self.return_code = rc
            with self._lock:
                self.agent_active_command = False
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
            with self._lock:
                self.agent_active_command = False
        return delta, completed, rc

    def send_text(self, text: str, timeout: int):
        """Send raw input text. Returns (new_output, completed, return_code).
        Text is sent exactly as-is — no newline is appended. Include \n explicitly
        for line-ending text (e.g. 'answer\n'). Control sequences (\x03, \x1b[B)
        and multi-keystroke runs (\x1b[B * 20) work correctly because no \r\n
        is injected after them.
        """
        self.last_used = time.time()
        with self._lock:
            # Agent interaction owns the terminal for the duration of the
            # read. Keep the background listener out of the PTY and discard
            # any half-typed human input state so this output is never
            # attributed to a human notification.
            was_active = self.agent_active_command
            self.agent_active_command = True
            self.human_input_active = False
            self.human_input_pending = False
            self._backend.send(text)   # raw send — no \r\n appended

        # Full snapshot — shows the complete current terminal state.
        # Delta is wrong here: TUIs need full screen (header + content + footer),
        # and interactive prompts need complete context. Token deduplication is
        # already handled at a higher level by _collapse_previous_outputs().
        try:
            output, completed, rc = self._read_until(timeout, delta=False)
        finally:
            if not was_active:
                with self._lock:
                    self.agent_active_command = False
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
            text = self._extract_cwd_marker(text)
            text = _EVAL_ECHO_RE.sub("", text)
            text = _EVAL_PREFIX_RE.sub("bash: ", text)
            # Remove xtrace noise emitted by the containment plumbing itself.
            return _WRAPPER_TRACE_RE.sub("", text)

        if "bash" in self.shell:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                chunk = self._backend.read_nonblocking(size=65536, timeout=0.1)
                if chunk:
                    self._screen.feed(chunk)
                    if self._screen.command_finished:
                        self.cwd = self._screen.cwd or self.cwd
                        # Reset the flag so _bg_listener doesn't falsely trigger
                        # when it reads the straggling PS1 prompt bytes.
                        self._screen.command_finished = False
                        return _render(), True, self._screen.osc_633_d_rc
                else:
                    if not self._backend.is_alive():
                        return _render(), True, -1
                    # A held pre-C D becomes the completion signal only when
                    # the shell never emits C (no PS0 support).
                    if self._screen.promote_pending_d():
                        self.cwd = self._screen.cwd or self.cwd
                        self._screen.command_finished = False
                        return _render(), True, self._screen.osc_633_d_rc
                    time.sleep(0.05)
            
            # Timeout
            try:
                proc_cwd = os.readlink(f"/proc/{self._backend.pid}/cwd")
                if proc_cwd:
                    self.cwd = proc_cwd
            except (OSError, AttributeError):
                pass
            return _render(), False, None

        # Windows/CMD legacy expect fallback
        before, match = self._backend.expect(self._rc_pattern, timeout)

        if before:
            # Skip bytes already fed in a previous (timed-out) read
            new_bytes = before[self._bytes_fed:]
            self._bytes_fed += len(new_bytes)
            if new_bytes:
                self._screen.feed(new_bytes)

        if match is None:
            # Timeout
            try:
                proc_cwd = os.readlink(f"/proc/{self._backend.pid}/cwd")
                if proc_cwd:
                    self.cwd = proc_cwd
            except (OSError, AttributeError):
                pass
            output = _render()
            return output, False, None
        elif match == "EOF":
            output = _render()
            return output, True, -1
        else:
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

    def __init__(self, runtime: "Runtime", default_session_id: str = "main",
                 control_socket: bool = True):
        self.runtime = runtime
        self._default_session_id = default_session_id
        self._control_enabled = control_socket
        self._sessions: Dict[str, TerminalSession] = {}
        self._session_order: List[str] = []          # most recent first
        self._output_history: Dict[tuple, List[str]] = {}  # (sid, cmd_tag) -> [result strings]

        self._control_socket_path = "/tmp/codepilot_control.sock"
        self._control_clients = []
        self._control_server = None
        self._control_running = False
        self._control_ino = None
        if control_socket:
            self._start_control_server()

    def _start_control_server(self):
        import threading
        import json

        def _serve():
            if not self._control_running:
                return
            try:
                os.unlink(self._control_socket_path)
            except OSError:
                pass
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(self._control_socket_path)
            os.chmod(self._control_socket_path, 0o600)
            server.listen(5)
            server.settimeout(0.5)
            self._control_server = server
            self._control_ino = os.stat(self._control_socket_path).st_ino
            if not self._control_running:
                # close() ran while this thread was binding.
                try:
                    if os.stat(self._control_socket_path).st_ino == self._control_ino:
                        os.unlink(self._control_socket_path)
                except OSError:
                    pass
                server.close()
                return

            while self._control_running:
                try:
                    conn, _ = server.accept()
                    self._control_clients.append(conn)
                    
                    # Immediately notify the new client about all existing terminals!
                    # This prevents race conditions if the UI connects *after* Python started.
                    for sid in list(self._sessions.keys()):
                        try:
                            conn.sendall((json.dumps({
                                "event": "terminal_created",
                                "session_id": sid,
                                "socket_path": f"/tmp/codepilot_{sid}.sock"
                            }) + "\n").encode('utf-8'))
                        except Exception:
                            pass
                            
                    def _handle_client(c):
                        try:
                            while True:
                                data = c.recv(4096)
                                if not data:
                                    break
                                try:
                                    event = json.loads(data.decode('utf-8'))
                                    if event.get("event") == "human_terminal_created":
                                        sid = event.get("session_id")
                                        if sid and sid not in self._sessions:
                                            work_dir = self.runtime.config.runtime.work_dir
                                            session = TerminalSession(sid, work_dir)
                                            session.owner = "human"
                                            self._sessions[sid] = session
                                            self._session_order.insert(0, sid)
                                    elif event.get("event") == "resize":
                                        sid = event.get("session_id")
                                        cols = event.get("cols", DEFAULT_COLS)
                                        rows = event.get("rows", DEFAULT_ROWS)
                                        session = self._sessions.get(sid)
                                        if session and hasattr(session._backend, "_mux_server"):
                                            session._backend._mux_server.set_window_size(cols, rows)
                                            session._screen.resize(cols, rows)
                                except json.JSONDecodeError:
                                    pass
                        except Exception:
                            pass
                        finally:
                            if c in self._control_clients:
                                self._control_clients.remove(c)
                            try:
                                c.close()
                            except OSError:
                                pass
                                
                    threading.Thread(target=_handle_client, args=(conn,), daemon=True).start()
                except socket.timeout:
                    continue
                except Exception:
                    pass

        self._control_running = True
        t = threading.Thread(target=_serve, daemon=True)
        t.start()

    def _broadcast_control_event(self, event_data: dict):
        import json
        data = (json.dumps(event_data) + "\n").encode('utf-8')
        for c in self._control_clients[:]:
            try:
                c.sendall(data)
            except Exception:
                if c in self._control_clients:
                    self._control_clients.remove(c)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_default_terminal(self, work_dir: str = None):
        """Start the default 'main' terminal session.

        ``work_dir`` lets recovery restart the shell in the directory the
        previous shell was last known to be in.
        """
        work_dir = work_dir or self.runtime.config.runtime.work_dir
        if not os.path.isdir(work_dir):
            work_dir = self.runtime.config.runtime.work_dir
        try:
            def _on_user(sid, output):
                # Bound the injected text: a human command can produce
                # unbounded output, and this notice becomes model context.
                output = self._truncate(output)
                msg = f"[Environment Detection Notification] User just ran a command in terminal {sid}:\n{output}"
                # Inject into the agent's context only. This is internal
                # context, not a UI event: emitting TOOL_RESULT/USER_MESSAGE
                # events made the webapp render a phantom "user command" and
                # "Queued/Injected" notice for the user.
                # Queue as a user message for the next inference cycle rather
                # than appending to _execution_buffer. _append_execution()
                # pollutes the current step's result even when no task is
                # running, causing the LLM to hallucinate about phantom
                # commands on the next run() call.
                self.runtime.send_message(msg, silent=True)
                
            session = TerminalSession(
                self._default_session_id, work_dir,
                on_user_command=_on_user if self._default_session_id == "main" else None,
            )
            session.owner = "agent"
            self._sessions[self._default_session_id] = session
            self._session_order.insert(0, self._default_session_id)
            self._broadcast_control_event({
                "event": "terminal_created",
                "session_id": self._default_session_id,
                "socket_path": f"/tmp/codepilot_{self._default_session_id}.sock"
            })
        except Exception as e:
            self.runtime._append_execution(
                f"[terminal] Warning: Could not start default terminal: {e}"
            )

    def ensure_default_terminal(self):
        """Guarantee the default session for this manager exists and is alive."""
        sid = self._default_session_id
        main = self._sessions.get(sid)
        if main is None or not main.is_alive():
            if main is not None:
                # Close the dead session so its mux server and socket are
                # cleaned up before a new shell claims the same path.
                try:
                    main.close()
                except Exception:
                    pass
                self._sessions.pop(sid, None)
                if sid in self._session_order:
                    self._session_order.remove(sid)
            self.start_default_terminal()

    def _recover_dead_session(
        self, session_id: str, session: "TerminalSession",
        rc: Optional[int], error: Optional[BaseException] = None,
        before_command: bool = False,
    ) -> str:
        """
        Clean up a session whose shell exited and, for ``main``, start a new one.

        Returns a truthful note for the model. The note never claims success:
        the command may have stopped halfway, and its state changes are gone.
        """
        last_cwd = getattr(session, "cwd", None) or self.runtime.config.runtime.work_dir
        try:
            session.close()
        except Exception:
            pass
        self._sessions.pop(session_id, None)
        if session_id in self._session_order:
            self._session_order.remove(session_id)

        if before_command:
            note = (
                f"[terminal:{session_id}] The shell had already exited before "
                "this command could run, so the command did not run."
            )
        else:
            # -1 means the socket closed before a status marker arrived, so
            # the exit code is genuinely unknown.
            status = rc if rc is not None and rc >= 0 else "unknown"
            note = (
                f"[terminal:{session_id}] The shell exited while running this "
                f"command (status {status})."
            )
        if error is not None:
            note += f" Execution error: {error}"
        if session_id == self._default_session_id:
            self.start_default_terminal(work_dir=last_cwd)
            note += (
                f" A fresh '{session_id}' shell was started in {last_cwd}. "
                "Working-directory, environment, alias, and variable changes "
                "made by this command were lost. Re-apply any `cd`/`export` "
                "you still need, then verify the command's effects before "
                "depending on them."
            )
        else:
            note += (
                " The session is no longer available. Create a new terminal "
                "with new_terminal=True if you need to continue there."
            )
        return note

    def get_prompt_info(self) -> str:
        """Generate terminal info for the system prompt."""
        if not self._sessions:
            return "No terminal sessions active."

        lines = []
        for i, sid in enumerate(self._session_order):
            session = self._sessions.get(sid)
            if session and session.is_alive():
                tag = " (most recent)" if i == 0 else ""
                owner_tag = " [Created by User]" if getattr(session, "owner", "agent") == "human" else ""
                lines.append(
                    f'  session_id="{sid}" | shell: {session.shell} | PID: {session.pid} | status: {session.status} | cwd: {session.cwd}{owner_tag}{tag}'
                )
        return "\n".join(lines) if lines else "No terminal sessions active."

    def cleanup_all(self):
        """Terminate all terminal sessions."""
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()
        self._session_order.clear()
        self._output_history.clear()

    def close(self):
        """
        Stop the control server and every terminal session.

        Called when a Runtime is replaced (new/switch session, settings save).
        Closing the shared sockets makes the UI's terminal and control bridges
        hit EOF and reconnect to the replacement runtime, instead of staying
        attached to an orphaned shell that the agent no longer uses.
        """
        self._control_running = False

        server = self._control_server
        self._control_server = None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass

        for client in self._control_clients[:]:
            try:
                client.close()
            except OSError:
                pass
        self._control_clients.clear()

        # Only remove the control socket if it is still the one we bound: a
        # replacement manager may already have rebound this path.
        try:
            if (self._control_ino is not None
                    and os.stat(self._control_socket_path).st_ino == self._control_ino):
                os.unlink(self._control_socket_path)
        except OSError:
            pass

        self.cleanup_all()

    # ------------------------------------------------------------------
    # Tool 1: execute
    # ------------------------------------------------------------------

    def _resolve_session_id(self, session_id: str) -> str:
        """Map the tool's default 'main' to this manager's default session.

        Only sub-agent runtimes use a non-'main' default (so they cannot
        hijack the UI-visible ``/tmp/codepilot_main.sock``).
        """
        if session_id == "main" and self._default_session_id != "main":
            return self._default_session_id
        return session_id

    def execute(
        self,
        session_id: str = "main",
        command: str = "",
        timeout: int = 10,
        new_terminal: bool = False,
        shell: str = None,
    ) -> str:
        """
        Runs a shell command in a persistent virtual terminal. Captures output up to timeout seconds.
        Returns output with status: 'completed' (has return_code) or 'running'
        (timed out — use read_output() to wait more, send_input() for prompts).

        Shell state (cwd, environment, aliases) persists between calls, but
        shell options set with ``set``, and commands that can replace or
        reconfigure the shell (``source``, ``eval``, ``trap``, ``exec``), are
        scoped to this call: they run in a contained subshell so a failure
        cannot kill the persistent session. Their state is carried back only
        when the call succeeds.

        To spawn a background process or new environment, set new_terminal=True AND provide 
        a UNIQUE new session_id (e.g., execute("server", "./run.sh", new_terminal=True)). 
        Do NOT reuse an existing session_id (like "main") when creating a new_terminal.
        
        Optional shell parameter: 'bash' (default on Linux/macOS), 'powershell' (default on Windows), or 'cmd'.
        Inspect [EXECUTION RESULT] before taking a dependent action.
        """
        # Forgiving parameter resolution: handle execute("ls") or execute(command="ls")
        if not command and session_id != "main":
            command = session_id
            session_id = "main"
        session_id = self._resolve_session_id(session_id)
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="execute",
            args={
                "session_id": session_id, "command": command,
                "timeout": timeout, "new_terminal": new_terminal,
                "shell": shell,
            },
            label=f"Running `{command}`",
        )

        # ---- cat Interception ----
        cmd_stripped = command.strip()
        if cmd_stripped.startswith("cat "):
            parts = cmd_stripped.split()
            # Intercept simple single-file cats without pipes or flags
            if len(parts) == 2 and not parts[1].startswith("-"):
                target_file = parts[1]
                
                # Resolve relative to the terminal's cwd
                session = self._sessions.get(session_id)
                if session and getattr(session, 'cwd', None):
                    if not os.path.isabs(target_file):
                        target_file = os.path.normpath(os.path.join(session.cwd, target_file))
                
                view_file_func = self.runtime.registry._tools.get("view_file")
                if view_file_func:
                    warning = (
                        f"[terminal:{session_id}] Warning: 'cat' is not recommended for reading code because PTY line-wrapping "
                        f"corrupts syntax. Automatically redirecting to 'view_file({target_file})'...\n"
                    )
                    self.runtime._append_execution(warning)
                    self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="execute", result=warning)
                    try:
                        view_file_func(target_file)
                        return warning + f"view_file('{target_file}') executed."
                    except Exception:
                        pass # Fallback to normal execution if view_file fails

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
                # Forgiveness path: model used new_terminal=True but forgot to
                # provide a unique session_id (defaulted to an existing one).
                # Derive a semantic ID from the command so the model can
                # recognise and reuse it in subsequent read_output/send_input calls.
                cmd_slug = re.sub(r"[^a-z0-9]+", "_", command.split()[0].lower())[:20].strip("_")
                short_uid = uuid.uuid4().hex[:6]
                auto_id = f"{cmd_slug}_{short_uid}"
                notice = (
                    f"[terminal] Note: session_id='{session_id}' already exists; "
                    f"auto-assigned session_id='{auto_id}' for this new terminal. "
                    f"Use '{auto_id}' in subsequent read_output() / send_input() calls."
                )
                self.runtime._append_execution(notice)
                self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="execute", result=notice)
                session_id = auto_id
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
                # Only the runtime-initialised ``main`` session reports human
                # commands. Agent-created terminals deliberately get no
                # on_user_command callback: their output is always agent-owned
                # and must never masquerade as human activity.
                work_dir = self.runtime.config.runtime.work_dir
                session = TerminalSession(session_id, work_dir, shell=shell)
                session.owner = "agent"
                self._sessions[session_id] = session
                self._broadcast_control_event({
                    "event": "terminal_created",
                    "session_id": session_id,
                    "socket_path": f"/tmp/codepilot_{session_id}.sock"
                })
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
            note = self._recover_dead_session(
                session_id, session, None, before_command=True,
            )
            result = f"[terminal:{session_id}] Error: terminal process was dead.\n{note}"
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(
                EventType.TOOL_RESULT, tool="execute", result=result,
            )
            return result

        # Touch session order
        self._touch_session(session_id)

        # Execute. A command that exits the shell (``exit 3``) or a broken
        # pipe raises/returns with the socket closed; both are handled below
        # so the model gets a truthful result instead of a silent dead session.
        run_error = None
        try:
            output, completed, rc = session.run(command, timeout)
        except Exception as e:
            run_error = e
            output, completed, rc = "", True, None

        # Truncate output
        output = self._truncate(output)

        if not session.is_alive():
            # Format before recovery: _format_output reads the session's cwd
            # and recovery replaces (or removes) the session.
            result = self._format_output(
                session_id, session.command_tag, output,
                "shell exited", rc if rc is not None and rc >= 0 else None,
                session.pid, label=f"$ {command}",
            )
            note = self._recover_dead_session(session_id, session, rc, run_error)
            result = f"{result}\n{note}"
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(
                EventType.TOOL_RESULT, tool="execute", result=result,
            )
            return result

        if run_error is not None:
            result = f"[terminal:{session_id}] Execution error: {run_error}"
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(
                EventType.TOOL_RESULT, tool="execute", result=result,
            )
            return result

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
        
        [CRITICAL CONSTRAINT]
        DO NOT call this on a completed session repeatedly unless you expect new output.
        """
        session_id = self._resolve_session_id(session_id)
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
        session_id = self._resolve_session_id(session_id)
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="send_input",
            args={"session_id": session_id, "text": text, "timeout": timeout},
            label=f"Sending input to console [{session_id}]",
        )

        # ---- Permission gate ----
        tool_cfg = self.runtime._tool_config("send_input")
        if tool_cfg.get("require_permission", False):
            perm = self.runtime.hooks.emit(
                EventType.PERMISSION_REQUEST, tool="send_input",
                description=f"Send input to session {session_id}: {text!r}",
            )
            approved = bool(perm) if perm is not None else (
                input(f"\n[Permission] Send input: {text!r}\nApprove? [y/N]: ").strip().lower() in ("y", "yes")
            )
            if not approved:
                result = f"[terminal:{session_id}] Permission denied: {text!r}"
                self.runtime._append_execution(result)
                self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="send_input", result=result)
                return result

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
        Hard-kills a terminal session. Use only as a last resort when 
        Ctrl+C (`send_input(session_id, "\\x03")`) fails.
        
        [CRITICAL CONSTRAINT]
        This destroys the session entirely. The session_id becomes unusable.
        """
        session_id = self._resolve_session_id(session_id)
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
