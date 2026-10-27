"""
Regression tests for the terminal "user ran a command" notification.

The notification exists so the agent can see commands a human typed into the
runtime-initialised ``main`` terminal. It must never fire for the agent's own
``execute`` calls, must never fire for other sessions, and must never be
rendered in the UI.

These tests use a real bash PTY through the MuxServer, so they are skipped on
Windows.

Run with:

    python3 -m pytest tests/terminal_notification_tests.py -q
"""
from __future__ import annotations

import queue
import socket
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from codepilot.tools.terminal import IS_WINDOWS, TerminalSession  # noqa: E402


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _open_human_client(session: TerminalSession) -> socket.socket:
    """Connect a second (non-agent) client to the session's PTY socket."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(f"/tmp/codepilot_{session.session_id}.sock")
    sock.settimeout(2.0)
    raw = b""
    while b"\n" not in raw:
        raw += sock.recv(4096)
    return sock


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX PTY integration test")
def test_agent_multiline_command_never_reports_human_activity():
    """A multi-line agent command must not look like a human command.

    Before the fix, bash emitted one OSC 633 C/D pair per line. run() returned
    after the first line, and the remaining lines were read by the background
    listener and reported as a user command.
    """
    events = []
    session = TerminalSession("main", "/tmp", on_user_command=lambda sid, out: events.append((sid, out)))
    try:
        # Drain shell startup deterministically instead of sleeping 3s.
        out, completed, rc = session.run("echo ready", timeout=5)
        assert completed and rc == 0
        session._startup_time = 0.0

        out, completed, rc = session.run(
            "echo line1\necho line2\necho line3", timeout=5,
        )
        assert completed and rc == 0
        assert out == "line1\nline2\nline3"

        # Give the background listener ample time to misbehave if it can.
        time.sleep(0.8)
        assert events == []
    finally:
        session.close()
        time.sleep(0.1)


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX PTY integration test")
def test_human_command_in_main_is_reported_once_with_its_own_output():
    events = []
    session = TerminalSession("main", "/tmp", on_user_command=lambda sid, out: events.append((sid, out)))
    try:
        out, completed, rc = session.run("echo agent-output", timeout=5)
        assert completed and rc == 0
        session._startup_time = 0.0
        time.sleep(0.2)

        human = _open_human_client(session)
        try:
            human.sendall(b"echo HUMAN-RAN-THIS\r")
            assert _wait_for(lambda: len(events) >= 1), "human command was not reported"
            # No duplicate notification for the same command.
            time.sleep(0.8)
            assert len(events) == 1
            assert events[0][0] == "main"
            assert "HUMAN-RAN-THIS" in events[0][1]
            # Must not replay the agent's earlier output.
            assert "agent-output" not in events[0][1]
        finally:
            human.close()

        # A subsequent agent command must not produce a notification.
        out, completed, rc = session.run("echo agent-after-human", timeout=5)
        assert completed and rc == 0
        time.sleep(0.8)
        assert len(events) == 1
    finally:
        session.close()
        time.sleep(0.1)


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX PTY integration test")
def test_human_command_in_non_main_session_is_never_reported():
    """Even if a non-main session is wired with a callback, it must stay silent."""
    sid = f"notmain_{uuid.uuid4().hex[:8]}"
    events = []
    session = TerminalSession(sid, "/tmp", on_user_command=lambda s, out: events.append((s, out)))
    try:
        out, completed, rc = session.run("echo ready", timeout=5)
        assert completed and rc == 0
        session._startup_time = 0.0
        time.sleep(0.2)

        human = _open_human_client(session)
        try:
            human.sendall(b"echo HUMAN-IN-NON-MAIN\r")
            time.sleep(1.2)
            assert events == []
        finally:
            human.close()
    finally:
        session.close()
        time.sleep(0.1)


def test_silent_send_message_injects_context_without_ui_events():
    """The environment notification enters the model context but emits no UI events."""
    from codepilot.engine.runtime import AsyncRuntime

    class FakeHooks:
        def __init__(self):
            self.events = []

        def emit(self, event, **kwargs):
            self.events.append((event, kwargs))

    rt = AsyncRuntime.__new__(AsyncRuntime)
    rt._message_queue = queue.Queue()
    rt.messages = []
    rt.hooks = FakeHooks()

    rt.send_message("silent internal note", silent=True)
    rt.send_message("visible user note")
    rt._drain_message_queue()

    contents = [m["content"] for m in rt.messages]
    assert any("silent internal note" in c for c in contents)
    assert any("visible user note" in c for c in contents)

    emitted = [kwargs.get("message") for _, kwargs in rt.hooks.events]
    assert "silent internal note" not in emitted
    assert "visible user note" in emitted


# ---------------------------------------------------------------------------
# Client role handshake (unit tests, no PTY needed)
# ---------------------------------------------------------------------------

def _bare_mux():
    """A MuxServer with just the state used by role classification."""
    from codepilot.core.multiplexer import MuxServer

    mux = MuxServer.__new__(MuxServer)
    mux._client_roles = {}
    mux._client_buffers = {}
    return mux


def test_agent_hello_split_across_reads_is_classified_agent():
    mux = _bare_mux()
    client = object()

    role, data = mux._consume_role_hello(client, b'{"cp_ro')
    assert role is None
    assert data == b""

    role, data = mux._consume_role_hello(client, b'le": "agent"}\n')
    assert role == "agent"
    assert data == b""
    assert mux._client_roles[client] == "agent"


def test_agent_hello_and_first_command_in_one_read_keeps_command():
    mux = _bare_mux()
    client = object()

    role, data = mux._consume_role_hello(client, b'{"cp_role": "agent"}\necho hi\n')
    assert role == "agent"
    assert data == b"echo hi\n"


def test_human_bytes_sharing_hello_prefix_are_forwarded_never_swallowed():
    mux = _bare_mux()
    client = object()

    role, data = mux._consume_role_hello(client, b'{"cp_role"')
    assert role is None

    # Diverges from the agent hello: this is human input.
    role, data = mux._consume_role_hello(client, b"x")
    assert role == "human"
    assert data == b'{"cp_role"x'
    assert mux._client_roles[client] == "human"


def test_malformed_hello_is_forwarded_as_human_input():
    mux = _bare_mux()
    client = object()

    role, data = mux._consume_role_hello(client, b'{"cp_role": "nope"}\n')
    assert role == "human"
    assert data == b'{"cp_role": "nope"}\n'


def test_stale_partial_hello_is_flushed_as_human_input(monkeypatch):
    mux = _bare_mux()
    client = object()
    delivered = []

    monkeypatch.setattr(
        mux, "_deliver_client_input",
        lambda sock, role, data: delivered.append((sock, role, data)),
    )

    role, _ = mux._consume_role_hello(client, b"{")
    assert role is None

    # Age the buffer past the timeout.
    buf, _seen = mux._client_buffers[client]
    mux._client_buffers[client] = (buf, 0.0)
    mux._flush_stale_partial_hellos()

    assert delivered == [(client, "human", b"{")]
    assert mux._client_roles[client] == "human"
    assert client not in mux._client_buffers
