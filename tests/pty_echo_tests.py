"""
Tests for PTY echo cleanup and prompt suppression.

The virtual-screen tests are deterministic. The integration tests use a real
bash PTY through the MuxServer, so they are skipped on Windows.

Run with:

    python3 -m pytest tests/pty_echo_tests.py -q
"""
from __future__ import annotations

import sys
import time
import uuid

import pytest

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from codepilot.core.vt import _C_GRACE_SECONDS, VirtualScreen  # noqa: E402
from codepilot.tools.terminal import IS_WINDOWS, TerminalSession  # noqa: E402


C = "\x1b]633;C\x07"
D0 = "\x1b]633;D;0\x07"
A = "\x1b]633;A\x07"
B = "\x1b]633;B\x07"


def test_agent_command_echo_is_cleared_at_c():
    screen = VirtualScreen()
    screen.reset()
    screen.begin_agent_command()

    screen.feed("user@host:/tmp$ echo hello\r\n")
    screen.feed(C)
    screen.feed("hello\r\n")
    screen.feed(D0)

    assert screen.snapshot() == "hello"


def test_user_command_echo_is_not_cleared_at_c():
    screen = VirtualScreen()
    screen.reset()

    screen.feed("user@host:/tmp$ echo hello\r\n")
    screen.feed(C)
    screen.feed("hello\r\n")
    screen.feed(D0)

    assert "echo hello" in screen.snapshot()
    assert "hello" in screen.snapshot()


def test_prompt_after_d_is_suppressed_until_b():
    screen = VirtualScreen()
    screen.reset()
    screen.begin_agent_command()

    screen.feed("echo hi\r\n")
    screen.feed(C)
    screen.feed("hi\r\n")
    screen.feed(D0)
    screen.feed(A)
    screen.feed("user@host:/tmp$ ")
    screen.feed(B)

    assert screen.snapshot() == "hi"

    # Once the prompt ends, normal user typing must render again.
    screen.feed("typed-by-user")
    assert screen.snapshot() == "hi\ntyped-by-user"


def test_fake_d_in_command_output_does_not_suppress_real_output():
    screen = VirtualScreen()
    screen.reset()
    screen.begin_agent_command()

    screen.feed("printf ...\r\n")
    screen.feed(C)
    screen.feed("before\r\n")
    screen.feed(D0)
    screen.feed("still-real-output\r\n")

    assert "still-real-output" in screen.snapshot()


def test_clear_rendered_preserves_shell_integration_state():
    screen = VirtualScreen()
    screen.reset()
    screen.feed(A)
    screen.feed("\x1b]633;P;Cwd=/workspace\x07")
    screen.feed("noise\r\n")

    screen.clear_rendered()

    assert screen.snapshot() == ""
    assert screen.cwd == "/workspace"
    assert screen.osc_633_a_count == 1


def test_no_shell_integration_leaves_output_untouched():
    screen = VirtualScreen()
    screen.reset()
    screen.begin_agent_command()

    screen.feed("echo hello\r\nhello\r\n")

    # No C marker, so nothing is cleared; the fallback lives in the session.
    assert "echo hello" in screen.snapshot()


def test_pre_c_d_is_held_until_c_and_discarded():
    screen = VirtualScreen()
    screen.reset()
    screen.begin_agent_command()

    screen.feed("echo hi\r\n")
    screen.feed(D0)              # stale startup D before C
    assert screen.command_finished is False
    assert screen._pending_d_rc == 0

    screen.feed(A)
    screen.feed("user@host:/tmp$ ")
    screen.feed(B)
    assert "user@host" not in screen.snapshot()

    screen.feed(C)               # the command really starts now
    screen.feed("hi\r\n")
    screen.feed(D0)              # real completion

    assert screen.command_finished is True
    assert screen.snapshot() == "hi"


def test_pre_c_d_is_promoted_when_c_never_arrives():
    screen = VirtualScreen()
    screen.reset()
    screen.begin_agent_command()

    screen.feed("echo )\r\n")
    screen.feed(D0)

    assert screen.promote_pending_d() is False
    assert screen.command_finished is False

    # Simulate the grace window elapsing.
    screen._command_started_at = time.monotonic() - (_C_GRACE_SECONDS + 0.1)
    assert screen.promote_pending_d() is True
    assert screen.command_finished is True
    assert screen.osc_633_d_rc == 0


def test_c_discards_a_held_d_without_completing():
    screen = VirtualScreen()
    screen.reset()
    screen.begin_agent_command()

    screen.feed(D0)
    assert screen._pending_d_rc == 0

    screen.feed(C)
    assert screen._pending_d_rc is None
    assert screen.command_finished is False


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX PTY integration test")
def test_real_pty_strips_echo_and_prompt():
    session = TerminalSession(f"echo_{uuid.uuid4().hex[:8]}", "/tmp")
    try:
        output, completed, rc = session.run("echo hello-world", timeout=5)
        assert completed and rc == 0
        assert output == "hello-world"

        output, completed, rc = session.run("echo -n ''", timeout=5)
        assert completed and rc == 0
        assert output == ""

        output, completed, rc = session.run(
            "printf 'a\\nb\\nc\\n'", timeout=5,
        )
        assert completed and rc == 0
        assert output == "a\nb\nc"
    finally:
        session.close()


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX PTY integration test")
def test_real_pty_syntax_error_has_no_prompt_or_echo():
    session = TerminalSession(f"echo_{uuid.uuid4().hex[:8]}", "/tmp")
    try:
        output, completed, rc = session.run("echo )", timeout=5)
        assert completed
        assert rc != 0
        assert output.startswith("bash: syntax error")
        assert "echo )" not in output
        assert "$ " not in output
    finally:
        session.close()


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX PTY integration test")
def test_real_pty_heredoc_and_timeout():
    session = TerminalSession(f"echo_{uuid.uuid4().hex[:8]}", "/tmp")
    try:
        output, completed, rc = session.run(
            "cat <<'EOF'\nline1\nline2\nEOF", timeout=5,
        )
        assert completed and rc == 0
        assert output == "line1\nline2"

        output, completed, rc = session.run(
            "sleep 1; echo later", timeout=0.5,
        )
        assert not completed
        assert output == ""

        output, completed, rc = session.read_new(timeout=5)
        assert completed and rc == 0
        assert "later" in output
    finally:
        session.close()
