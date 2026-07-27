"""
File: multiplexer.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-05-08

Description:
    PTY multiplexer daemon for CodePilot on Linux and macOS.

Architectural Notes:
    A single bash process is owned via a real PTY (master/slave pair).
    Multiple clients — CodePilot's pexpect backend, a VS Code WebSocket
    bridge, etc. — connect via a Unix Domain Socket and share the same
    shell session bidirectionally.

    Data flow:
        bash (slave PTY end FD 0/1/2)
          ↕  raw VT100 bytes
        master_fd  ← owned exclusively by MuxServer
          ↕  broadcast / collect
        Unix Domain Socket  ← MuxServer listens here
          ↕  per-client connection
        [client 1: CodePilot pexpect.fdpexpect]
        [client 2: VS Code WebSocket bridge]
        ...

    On connect, each client receives exactly one JSON handshake line:
        {"pid": <bash_pid>, "cols": <cols>, "rows": <rows>}\n
    After that, the connection carries raw VT100 bytes in both directions.

    POSIX-only. On Windows, ConPTY via pywinpty is used directly.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

import errno
import fcntl
import json
import logging
import os
import pathlib
import pty
import selectors
import signal
import socket
import struct
import sys
import termios
import time
from typing import Optional

__all__ = ["MuxServer", "DEFAULT_SOCKET_PATH"]

_log = logging.getLogger(__name__)

DEFAULT_SOCKET_PATH = "/tmp/codepilot_shared.sock"
_READ_CHUNK = 65536


# ======================================================================
#  PTY window-size helper
# ======================================================================

def _set_pty_size(master_fd: int, cols: int, rows: int) -> None:
    """
    Set PTY window dimensions via ioctl TIOCSWINSZ.

    The kernel struct winsize layout (from <termios.h>) is:
        unsigned short ws_row, ws_col, ws_xpixel, ws_ypixel

    We set pixel dimensions to 0 — they are irrelevant for headless PTYs.
    Bash and child processes receive SIGWINCH after this call.
    """
    winsz = struct.pack("HHHH", rows, cols, 0, 0)
    try:
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsz)
    except OSError as exc:
        _log.warning("Could not set PTY window size to %dx%d: %s", cols, rows, exc)


# ======================================================================
#  MuxServer
# ======================================================================

class MuxServer:
    """
    PTY multiplexer server.

    Spawns a bash process behind a real PTY, then listens on a Unix Domain
    Socket. Any number of clients can connect and share the same session.

    Typical lifecycle::

        server = MuxServer("/tmp/codepilot_shared.sock", cols=220, rows=50)
        server.start()   # forks bash, binds socket — non-blocking
        server.serve()   # blocks: runs the selector event loop
        # serve() returns when bash exits or stop() is called
        server.stop()    # idempotent cleanup

    Thread-safety: all methods must be called from the same thread that
    called start().
    """

    def __init__(
        self,
        socket_path: str = DEFAULT_SOCKET_PATH,
        cols: int = 220,
        rows: int = 50,
        shell: str = "/bin/bash",
        shell_args: Optional[list] = None,
        work_dir: Optional[str] = None,
    ) -> None:
        self._socket_path = pathlib.Path(socket_path)
        self._cols = cols
        self._rows = rows
        self._shell = shell
        self._shell_args: list = shell_args or ["bash", "--norc", "--noprofile"]
        self._work_dir: str = work_dir or os.getcwd()

        self._master_fd: Optional[int] = None
        self._clients: List[socket.socket] = []
        self._server: Optional[socket.socket] = None
        self._sel: Optional[selectors.DefaultSelector] = None

        self._scrollback: bytes = b""
        self._max_scrollback = 16384  # 16KB

        self._bash_pid: Optional[int] = None
        self._running: bool = False
        self._rcfile_path: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Fork bash behind a PTY and bind the Unix Domain Socket. Non-blocking."""
        self._spawn_bash()
        self._open_server()
        _log.info(
            "MuxServer started — bash PID=%d  socket=%s",
            self._bash_pid, self._socket_path,
        )

    def serve(self) -> None:
        """Block and run the multiplexer event loop until bash exits or stop() is called."""
        self._running = True
        self._sel = selectors.DefaultSelector()
        self._sel.register(self._server, selectors.EVENT_READ, data="accept")
        self._sel.register(self._master_fd, selectors.EVENT_READ, data="pty")
        try:
            self._loop()
        finally:
            self.stop()

    def stop(self) -> None:
        """Cleanly tear down all resources. Safe to call multiple times."""
        self._running = False

        # Close all clients first so we can cleanly unregister them
        for client in self._clients[:]:
            self._close_client(client, log=False)

        if self._sel is not None:
            self._sel.close()
            self._sel = None

        if self._server is not None:
            self._server.close()
            self._server = None

        self._socket_path.unlink(missing_ok=True)

        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

        if self._rcfile_path is not None:
            try:
                os.unlink(self._rcfile_path)
            except OSError:
                pass
            self._rcfile_path = None

        if self._bash_pid is not None:
            try:
                os.waitpid(self._bash_pid, os.WNOHANG)
            except ChildProcessError:
                pass

        _log.info("MuxServer stopped.")

    def set_window_size(self, cols: int, rows: int) -> None:
        """
        Resize the PTY.

        The kernel delivers SIGWINCH to bash and its foreground process group.
        All connected clients will see the updated geometry in the next
        handshake (new connects only; existing clients are not notified).
        """
        if self._master_fd is None:
            return
        self._cols = cols
        self._rows = rows
        _set_pty_size(self._master_fd, cols, rows)

    @property
    def bash_pid(self) -> Optional[int]:
        return self._bash_pid

    # ------------------------------------------------------------------
    # Private: setup
    # ------------------------------------------------------------------

    def _spawn_bash(self) -> None:
        """Open a PTY pair and fork bash into the slave end."""
        master_fd, slave_fd = pty.openpty()
        _set_pty_size(master_fd, self._cols, self._rows)

        pid = os.fork()

        if pid == 0:
            # ── Child process: become bash ──────────────────────────────
            # Create a new session so bash becomes the session leader and
            # the slave PTY becomes its controlling terminal.
            os.setsid()
            os.close(master_fd)

            # Wire standard I/O to the slave end
            os.dup2(slave_fd, 0)   # stdin
            os.dup2(slave_fd, 1)   # stdout
            os.dup2(slave_fd, 2)   # stderr
            if slave_fd > 2:
                os.close(slave_fd)

            # Set working directory
            try:
                os.chdir(self._work_dir)
            except OSError:
                pass

            # Setup OSC 633 shell integration
            if "bash" in self._shell:
                self._rcfile_path = f"/tmp/codepilot_bashrc_{os.getpid()}"
                try:
                    with open(self._rcfile_path, "w") as f:
                        f.write('''\
if [ -f ~/.bashrc ]; then
    source ~/.bashrc
fi

__cp_command_finished() {
    local rc=$?
    printf "\\033]633;D;%s\\007" "$rc"
    printf "\\033]633;P;Cwd=%s\\007" "$PWD"
}

PS1="\\[\\033]633;A\\007\\]${PS1}\\[\\033]633;B\\007\\]"
PS0="\\033]633;C\\007"
PROMPT_COMMAND="__cp_command_finished; $PROMPT_COMMAND"
''')
                    self._shell_args = [self._shell, "--rcfile", self._rcfile_path]
                except OSError as e:
                    _log.error("Could not write rcfile: %s", e)

            SENSITIVES = {"MACHINE_SECRET", "CONTROL_PLANE_URL", "DASHSCOPE_API_KEY", "ALIBABA_API_KEY", "VOYAGE_API_KEY", "TAVILY_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}

            clean_env = {
                k: v for k, v in os.environ.items()
                if k not in SENSITIVES
            }
            os.execve(self._shell, self._shell_args, clean_env)
            # os.execve() never returns on success. If it does, bail.
            sys.exit(127)

        # ── Parent process: hold master_fd ──────────────────────────────
        os.close(slave_fd)
        self._master_fd = master_fd
        self._bash_pid = pid
        
        # We need the parent to also know the rcfile path to clean it up
        if "bash" in self._shell:
            self._rcfile_path = f"/tmp/codepilot_bashrc_{pid}"

        # Reap bash automatically when it exits to avoid zombie processes.
        # This may fail with ValueError if called from a background thread
        # (e.g. asyncio.to_thread). If it does, stop() will still reap the
        # child via waitpid when the master_fd raises EIO.
        try:
            signal.signal(signal.SIGCHLD, self._on_sigchld)
        except ValueError:
            pass

        # Brief pause to let bash finish its startup sequence before any
        # client connects and sees stale initialization bytes.
        time.sleep(0.15)

    def _open_server(self) -> None:
        """Bind and listen on the Unix Domain Socket."""
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._socket_path.unlink(missing_ok=True)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self._socket_path))

        # Restrict socket access to the current user only.
        os.chmod(str(self._socket_path), 0o600)

        server.listen(8)
        server.setblocking(False)
        self._server = server

    # ------------------------------------------------------------------
    # Private: event loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Main selector event loop. Returns when bash exits or _running is False."""
        while self._running:
            try:
                events = self._sel.select(timeout=1.0)
            except OSError:
                break

            for key, _mask in events:
                if key.data == "accept":
                    self._handle_accept()
                elif key.data == "pty":
                    if not self._handle_pty_output():
                        return   # bash exited — EIO on master_fd
                elif key.data == "client":
                    self._handle_client_input(key.fileobj)

    def _handle_accept(self) -> None:
        """Accept a new client and send the JSON handshake line."""
        try:
            client_sock, _ = self._server.accept()
        except OSError:
            return

        client_sock.setblocking(False)

        # The handshake gives clients the bash PID (for /proc/<pid>/cwd
        # lookups) and the current PTY geometry.
        handshake = json.dumps({
            "pid":  self._bash_pid,
            "cols": self._cols,
            "rows": self._rows,
        }) + "\n"

        try:
            client_sock.sendall(handshake.encode())
            if self._scrollback:
                client_sock.sendall(self._scrollback)
        except OSError:
            client_sock.close()
            return

        self._clients.append(client_sock)
        self._sel.register(client_sock, selectors.EVENT_READ, data="client")
        _log.debug("Client connected — total connected: %d", len(self._clients))

    def _handle_pty_output(self) -> bool:
        """
        Read output from bash and broadcast it to every connected client.

        Returns False when the PTY is closed (bash exited), signalling the
        event loop to terminate.

        On Linux, when bash exits and the slave PTY is closed, the next
        os.read() on the master_fd raises OSError with errno EIO (errno 5).
        An empty-bytes return does NOT happen for PTY file descriptors —
        that pattern only applies to regular pipes and sockets.
        """
        try:
            data = os.read(self._master_fd, _READ_CHUNK)
        except OSError as exc:
            if exc.errno == errno.EIO:
                _log.info("Bash exited (EIO on master_fd).")
            else:
                _log.error("Unexpected error reading master_fd: %s", exc)
            self._running = False
            return False

        # Update scrollback buffer
        self._scrollback = (self._scrollback + data)[-self._max_scrollback:]

        for client in self._clients[:]:
            try:
                client.sendall(data)
            except OSError:
                self._close_client(client)

        return True

    def _handle_client_input(self, client_sock: socket.socket) -> None:
        """Forward keystrokes received from a client into bash via master_fd."""
        try:
            data = client_sock.recv(_READ_CHUNK)
        except OSError:
            self._close_client(client_sock)
            return

        if not data:
            # Empty recv() means the client closed the connection cleanly.
            self._close_client(client_sock)
            return

        try:
            os.write(self._master_fd, data)
        except OSError as exc:
            _log.warning("Write to master_fd failed (%s) — bash may have exited.", exc)
            self._running = False

    def _close_client(self, client_sock: socket.socket, *, log: bool = True) -> None:
        """Unregister, remove, and close a client socket."""
        if self._sel is not None:
            try:
                self._sel.unregister(client_sock)
            except (KeyError, ValueError, OSError):
                pass
        if client_sock in self._clients:
            self._clients.remove(client_sock)
        try:
            client_sock.close()
        except OSError:
            pass
        if log:
            _log.debug("Client disconnected — remaining: %d", len(self._clients))

    def _on_sigchld(self, signum: int, frame) -> None:
        """
        SIGCHLD handler: reap bash when it exits.

        Without this, the exited bash process remains as a zombie in the
        process table until the parent (MuxServer) itself exits.
        os.WNOHANG ensures the handler returns immediately if no child has
        actually changed state yet (avoids blocking the event loop).
        """
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == self._bash_pid:
                _log.info("Bash (PID %d) confirmed exited via SIGCHLD.", pid)
                self._running = False
        except ChildProcessError:
            pass


# ======================================================================
#  CLI entry point
# ======================================================================

def main() -> None:
    """Run the multiplexer as a standalone daemon process."""
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="CodePilot PTY multiplexer daemon (Linux/macOS only).",
    )
    parser.add_argument(
        "socket_path",
        nargs="?",
        default=DEFAULT_SOCKET_PATH,
        help=f"Path for the Unix Domain Socket (default: {DEFAULT_SOCKET_PATH}).",
    )
    parser.add_argument("--cols", type=int, default=220, help="PTY width in columns.")
    parser.add_argument("--rows", type=int, default=50,  help="PTY height in rows.")
    parser.add_argument("--work-dir", default=None, help="Working directory for bash.")
    args = parser.parse_args()

    server = MuxServer(
        socket_path=args.socket_path,
        cols=args.cols,
        rows=args.rows,
        work_dir=args.work_dir,
    )
    server.start()

    def _shutdown(signum: int, frame) -> None:
        _log.info("Received signal %d — shutting down.", signum)
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    server.serve()


if __name__ == "__main__":
    main()
