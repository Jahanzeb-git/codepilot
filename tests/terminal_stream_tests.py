"""
Regression tests for live terminal streaming between the agent and the UI.

The UI's xterm is a second client on the same MuxServer Unix socket as the
agent's terminal backend. Two failures used to break that live stream:

1. A replaced runtime was never stopped, so its ``main`` MuxServer survived
   as an orphan. The browser stayed attached to the old shell and never saw
   agent commands, while the control-plane listener stayed on the old
   manager and missed new terminals.
2. MuxServer used a non-blocking ``sendall`` for every client; a burst of
   output that momentarily filled the browser's socket buffer was treated as
   a dead client and silently disconnected, losing the stream until reconnect.

These tests use a real bash PTY, so they are skipped on Windows.

Run with:

    python3 -m pytest tests/terminal_stream_tests.py -q
"""
from __future__ import annotations

import socket
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from codepilot.engine.hooks import EventType, HookSystem  # noqa: E402
from codepilot.tools.terminal import (  # noqa: E402
    IS_WINDOWS,
    TerminalManager,
    TerminalSession,
)

pytestmark = pytest.mark.skipif(IS_WINDOWS, reason="POSIX PTY integration test")


class _DummyRuntime:
    def __init__(self, work_dir: str) -> None:
        self.config = SimpleNamespace(runtime=SimpleNamespace(work_dir=work_dir))
        self.hooks = HookSystem()
        for event in (
            EventType.TOOL_CALL, EventType.TOOL_RESULT, EventType.STREAM,
            EventType.SECURITY_ERROR, EventType.RUNTIME_ERROR,
            EventType.FINISH, EventType.MAX_STEPS,
        ):
            self.hooks.clear(event)
        self.registry = SimpleNamespace(_tools={})
        self.execution_log: list[str] = []

    def _tool_config(self, tool_name: str) -> dict:
        return {"max_output_chars": 10000} if tool_name == "execute" else {}

    def _append_execution(self, text: str) -> None:
        self.execution_log.append(text)


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _connect_ui(session_id: str = "main") -> socket.socket:
    """Connect a second client, as the browser bridge does."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(f"/tmp/codepilot_{session_id}.sock")
    sock.settimeout(2.0)
    raw = b""
    while b"\n" not in raw:
        raw += sock.recv(4096)
    return sock


def _drain(sock: socket.socket, timeout: float = 3.0) -> bytes:
    sock.settimeout(timeout)
    out = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            out += chunk
    except socket.timeout:
        pass
    return out


def test_replaced_manager_closes_old_stream_and_keeps_new_socket(tmp_path):
    runtime_a = _DummyRuntime(str(tmp_path))
    manager_a = TerminalManager(runtime_a)
    manager_b = None
    ui_old = None
    ui_new = None
    try:
        manager_a.start_default_terminal()
        assert _wait_for(lambda: manager_a._control_ino is not None)
        ui_old = _connect_ui()
        old_control_ino = manager_a._control_ino

        # Simulate a runtime re-init: a new manager binds the same paths.
        manager_b = TerminalManager(_DummyRuntime(str(tmp_path)))
        manager_b.start_default_terminal()
        assert _wait_for(
            lambda: manager_b._control_ino is not None
            and manager_b._control_ino != old_control_ino
        )

        # Cleanup of the replaced runtime must not unlink the new sockets...
        manager_a.close()
        assert Path("/tmp/codepilot_main.sock").exists()
        assert Path("/tmp/codepilot_control.sock").exists()

        # ...and must close the browser's old stream so it reconnects.
        ui_old.settimeout(0.5)
        closed = False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                if not ui_old.recv(4096):
                    closed = True
                    break
            except socket.timeout:
                continue
        assert closed, "old UI stream stayed open after manager cleanup"
        ui_old.close()
        ui_old = None

        # A reconnecting browser now reaches the live server.
        ui_new = _connect_ui()
        result = manager_b.execute(session_id="main", command="echo FROM-NEW-RUNTIME", timeout=8)
        assert "FROM-NEW-RUNTIME" in result
        seen = _drain(ui_new, timeout=2.0)
        assert b"FROM-NEW-RUNTIME" in seen
    finally:
        for sock in (ui_old, ui_new):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        manager_a.close()
        if manager_b is not None:
            manager_b.close()
        time.sleep(0.1)


def test_slow_ui_client_is_not_disconnected_by_output_burst():
    session = TerminalSession(f"burst_{uuid.uuid4().hex[:8]}", "/tmp")
    ui = None
    try:
        session.run("echo ready", timeout=5)
        ui = _connect_ui(session.session_id)

        # ~4 MiB, well beyond a Unix socket's default send buffer, produced
        # while the browser is not reading.
        out, completed, rc = session.run(
            "python3 -c \"import sys; sys.stdout.write('x'*4000000)\"",
            timeout=20,
        )
        assert completed and rc == 0
        assert len(out) > 0

        time.sleep(0.5)  # let the mux fill the client's kernel buffer
        seen = _drain(ui, timeout=8.0)
        assert seen, "client was disconnected before delivering its backlog"
        assert seen.count(b"x") >= 3_500_000
    finally:
        if ui is not None:
            try:
                ui.close()
            except OSError:
                pass
        session.close()
        time.sleep(0.1)


def test_isolated_manager_never_binds_main_or_control_sockets(tmp_path):
    """Sub-agent runtimes must not steal the UI-visible sockets."""
    runtime = _DummyRuntime(str(tmp_path))
    manager = TerminalManager(
        runtime, default_session_id="sub_testiso", control_socket=False,
    )
    try:
        manager.start_default_terminal()
        assert manager._control_server is None
        assert "sub_testiso" in manager._sessions

        # The tool's default session id ("main") maps to the private one.
        result = manager.execute(command="echo private-shell", timeout=8)
        assert "[terminal:sub_testiso" in result
        assert "private-shell" in result

        follow_up = manager._resolve_session_id("main")
        assert follow_up == "sub_testiso"
    finally:
        manager.close()
        time.sleep(0.1)


def test_wrapper_bytes_never_reach_the_ui_terminal():
    """The shared xterm must show output, not the agent's safety wrapper."""
    session = TerminalSession(f"clean_{uuid.uuid4().hex[:8]}", "/tmp")
    ui = None
    try:
        session.run("echo ready", timeout=5)
        ui = _connect_ui(session.session_id)

        for command, expected in (
            ("echo visible", b"visible"),
            ("echo a\necho b", b"a\r\nb"),
            ("set -e\necho before\nfalse", b"before"),
        ):
            out, completed, rc = session.run(command, timeout=8)
            assert completed
            assert "__cp_" not in out
            assert "set +e +u" not in out

            raw = _drain(ui, timeout=1.0)
            assert expected in raw, (command, raw[-200:])
            assert b"__cp_" not in raw, raw[-400:]
            assert b"set +e +u" not in raw, raw[-400:]
            # The short source trigger is erased before the output renders.
            assert b"\x1b[1A\x1b[2K\r" in raw

        tag = session._marker.strip("_").removeprefix("CP_")
        assert not list(Path("/tmp").glob(f"cp_{tag}_*.sh"))
    finally:
        if ui is not None:
            try:
                ui.close()
            except OSError:
                pass
        session.close()
        time.sleep(0.1)


def test_runtime_cleanup_closes_terminal_manager():
    from codepilot.engine.runtime import AsyncRuntime, Runtime

    calls = []

    class _StubTerminal:
        def close(self):
            calls.append("terminals")

    runtime = AsyncRuntime.__new__(AsyncRuntime)
    runtime._terminal_manager = _StubTerminal()
    runtime._cancel_cache_timer = lambda: calls.append("timer")

    runtime.cleanup()
    assert calls == ["timer", "terminals"]
    # The sync wrapper used by the runner must expose the same hook.
    assert hasattr(Runtime, "cleanup")
