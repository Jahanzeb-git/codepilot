"""
Regression tests for shell-option containment in the persistent terminal.

A persistent interactive bash session must survive any command an agent runs.
Before the fix, a command containing ``set -e`` (errexit) exited the shell on
the first failed command. The next ``execute`` then hit a dead session, and the
model was left without a truthful result.

The fix runs commands that can change shell options (``set``, ``source``,
``eval``, ``trap``, ``exec``) inside a contained subshell. On success their
working directory, variables, exported environment, aliases, and functions are
copied back; on failure nothing is copied back. All other commands run in the
persistent shell with option hygiene before and after.

These tests use a real bash PTY, so they are skipped on Windows.

Run with:

    python3 -m pytest tests/terminal_shell_option_tests.py -q
"""
from __future__ import annotations

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
    _CONTAINED_BASH_RE,
    TerminalManager,
    TerminalSession,
)

pytestmark = pytest.mark.skipif(IS_WINDOWS, reason="POSIX PTY integration test")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _session(cwd: str = "/tmp") -> TerminalSession:
    return TerminalSession(f"opt_{uuid.uuid4().hex[:8]}", cwd)


def _run(session: TerminalSession, command: str, timeout: int = 8):
    out, completed, rc = session.run(command, timeout=timeout)
    assert completed, f"command did not complete: {command!r} -> {out!r}"
    return out, rc


class _DummyRuntime:
    """Minimal runtime surface required by TerminalManager."""

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
        if tool_name == "execute":
            return {"max_output_chars": 10000}
        return {}

    def _append_execution(self, text: str) -> None:
        self.execution_log.append(text)


def _start_main(manager: TerminalManager) -> None:
    manager.start_default_terminal()
    assert "main" in manager._sessions, manager.runtime.execution_log


# ---------------------------------------------------------------------------
# containment detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "set -e; make",
    "set -euo pipefail\nmake test",
    "  set -x",
    "source ./env.sh",
    ". /etc/profile",
    'eval "$(direnv hook bash)"',
    "trap 'exit 1' DEBUG",
    "exec ./server",
    "ls && set -e",
    "if true; then set -e; fi",
    "for x in a; do set -e; done",
    "PROMPT_COMMAND='exit 1'",
    "export PS1='$ '",
    "PS0+='x'",
])
def test_commands_that_can_change_shell_state_are_contained(command):
    assert _CONTAINED_BASH_RE.search(command), command


@pytest.mark.parametrize("command", [
    "echo 'set -e'",
    "git commit -m 'set +x'",
    "settle -x",
    "./script.sh",
    "echo source x",
    "grep -r 'trap ' .",
])
def test_ordinary_commands_are_not_contained(command):
    assert not _CONTAINED_BASH_RE.search(command), command


# ---------------------------------------------------------------------------
# errexit must not kill the persistent shell
# ---------------------------------------------------------------------------

def test_errexit_failure_keeps_shell_alive():
    session = _session()
    try:
        out, rc = _run(session, "set -e\necho before\nfalse\necho after")
        assert out == "before"
        assert rc == 1
        assert session.is_alive(), "set -e killed the persistent shell"

        out, rc = _run(session, "echo still-alive")
        assert (out, rc) == ("still-alive", 0)
    finally:
        session.close()


def test_errexit_success_persists_state():
    session = _session()
    work = f"/tmp/cp_opt_{uuid.uuid4().hex[:8]}"
    try:
        out, rc = _run(
            session,
            "set -euo pipefail\n"
            f"mkdir -p {work}\n"
            f"cd {work}\n"
            "export CP_EXPORTED=exported\n"
            "CP_PLAIN=plain\n"
            "alias cp_hi='echo hi'\n"
            "cp_fn() { echo fn; }\n"
            "echo ok",
        )
        assert (out, rc) == ("ok", 0)

        out, rc = _run(
            session,
            "echo cwd=$PWD\n"
            "echo exp=$CP_EXPORTED\n"
            "echo plain=$CP_PLAIN\n"
            "alias cp_hi\n"
            "cp_fn",
        )
        assert f"cwd={work}" in out
        assert "exp=exported" in out
        assert "plain=plain" in out
        assert "echo hi" in out
        assert "fn" in out
        assert rc == 0
    finally:
        session.close()


def test_errexit_failure_does_not_apply_partial_state():
    session = _session()
    try:
        out, rc = _run(session, "set -e\nCP_LEAK=1\ncd /usr\nfalse")
        assert rc == 1
        assert session.is_alive()

        out, _ = _run(session, "echo cwd=$PWD; echo leak=$CP_LEAK")
        assert "cwd=/tmp" in out
        assert "leak=" in out and "leak=1" not in out
    finally:
        session.close()


def test_nested_function_failure_aborts_the_call():
    session = _session()
    try:
        out, rc = _run(
            session,
            "set -e\n"
            "g() { echo g-before; false; echo g-after; }\n"
            "echo before\n"
            "g\n"
            "echo after",
        )
        assert out == "before\ng-before"
        assert rc == 1
        assert session.is_alive()
    finally:
        session.close()


def test_exit_inside_contained_command_keeps_shell_alive():
    session = _session()
    try:
        out, rc = _run(session, "set -e\nexit 3")
        assert out == ""
        assert rc == 3
        assert session.is_alive()

        out, rc = _run(session, "echo alive")
        assert (out, rc) == ("alive", 0)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# other shell options must not leak
# ---------------------------------------------------------------------------

def test_nounset_does_not_persist():
    session = _session()
    try:
        out, rc = _run(session, "set -u\necho before\necho $UNSET_OPT_VAR\necho after")
        assert out.startswith("before")
        assert "after" not in out
        assert rc == 1
        assert session.is_alive()

        out, rc = _run(session, "echo value=$UNSET_OPT_VAR")
        assert (out, rc) == ("value=", 0)
    finally:
        session.close()


def test_xtrace_does_not_persist_or_pollute_output():
    session = _session()
    try:
        out, rc = _run(session, "set -x\necho traced")
        assert rc == 0
        assert "traced" in out
        assert "__cp_" not in out

        out, rc = _run(session, "echo clean")
        assert (out, rc) == ("clean", 0)
    finally:
        session.close()


def test_pipefail_does_not_persist():
    session = _session()
    try:
        _, rc = _run(session, "set -o pipefail\nfalse | true")
        assert rc == 1
        assert session.is_alive()

        out, rc = _run(session, "false | true\necho rc=$?")
        assert (out, rc) == ("rc=0", 0)
    finally:
        session.close()


def test_prompt_command_assignment_does_not_break_completion():
    """PROMPT_COMMAND drives the OSC 633 completion marker, so it must not leak."""
    session = _session()
    try:
        _, rc = _run(session, "PROMPT_COMMAND='exit 1'")
        assert rc == 0
        assert session.is_alive()

        # If the assignment leaked, the next prompt would exit the shell and
        # this command would never complete.
        out, rc = _run(session, "echo still-here")
        assert (out, rc) == ("still-here", 0)
    finally:
        session.close()


def test_syntax_error_does_not_leave_shell_waiting_for_input():
    """A typo must be a failed result, never a shell stuck on PS2."""
    session = _session()
    try:
        out, rc = _run(session, "echo )")
        assert rc == 2
        assert out.startswith("bash: syntax error")
        assert "echo )" not in out

        out, rc = _run(session, "echo after-typo")
        assert (out, rc) == ("after-typo", 0)

        # Incomplete compound commands are just as dangerous: bash would wait
        # for the missing ``fi`` and swallow everything sent afterwards.
        out, rc = _run(session, "if true; then\n  echo x")
        assert rc == 2
        assert "unexpected EOF" in out or "syntax error" in out

        out, rc = _run(session, "echo still-fine")
        assert (out, rc) == ("still-fine", 0)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# sourced scripts and eval are contained too
# ---------------------------------------------------------------------------

def test_sourced_script_with_errexit_cannot_kill_shell(tmp_path):
    script = tmp_path / "failing.sh"
    script.write_text("set -euo pipefail\necho sourced-before\nfalse\necho sourced-after\n")

    session = _session()
    try:
        out, rc = _run(session, f"source {script}")
        assert out == "sourced-before"
        assert rc == 1
        assert session.is_alive()

        out, rc = _run(session, "echo alive")
        assert (out, rc) == ("alive", 0)
    finally:
        session.close()


def test_eval_with_errexit_cannot_kill_shell():
    session = _session()
    try:
        out, rc = _run(session, "eval 'set -e; echo ev-before; false; echo ev-after'")
        assert out == "ev-before"
        assert rc == 1
        assert session.is_alive()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# dead-shell recovery through TerminalManager.execute
# ---------------------------------------------------------------------------

def test_execute_recovers_main_after_shell_exits(tmp_path):
    runtime = _DummyRuntime(work_dir=str(tmp_path))
    manager = TerminalManager(runtime)
    try:
        _start_main(manager)

        result = manager.execute(session_id="main", command="exit 3", timeout=8)
        assert "The shell exited while running this command" in result
        assert "status unknown" in result
        assert "fresh 'main' shell was started" in result
        assert "Environment Detection Notification" not in result

        follow_up = manager.execute(session_id="main", command="echo recovered", timeout=8)
        assert "recovered" in follow_up
        assert "[status: completed | return_code: 0" in follow_up
    finally:
        manager.cleanup_all()
        time.sleep(0.1)


def test_execute_recovers_main_when_shell_was_already_dead(tmp_path):
    runtime = _DummyRuntime(work_dir=str(tmp_path))
    manager = TerminalManager(runtime)
    try:
        _start_main(manager)
        manager._sessions["main"].close()
        assert not manager._sessions["main"].is_alive()

        result = manager.execute(session_id="main", command="echo should-not-run", timeout=8)
        assert "had already exited before this command could run" in result
        assert "should-not-run" not in result

        follow_up = manager.execute(session_id="main", command="echo back", timeout=8)
        assert "back" in follow_up
    finally:
        manager.cleanup_all()
        time.sleep(0.1)


def test_execute_does_not_restart_a_dead_non_main_session(tmp_path):
    runtime = _DummyRuntime(work_dir=str(tmp_path))
    manager = TerminalManager(runtime)
    try:
        _start_main(manager)
        sid = f"worker_{uuid.uuid4().hex[:6]}"
        manager.execute(
            session_id=sid, command="echo ready",
            timeout=8, new_terminal=True,
        )

        result = manager.execute(session_id=sid, command="exit 3", timeout=8)
        assert "The shell exited while running this command" in result
        assert "no longer available" in result
        assert "fresh 'main'" not in result

        follow_up = manager.execute(session_id=sid, command="echo nope", timeout=8)
        assert "session" in follow_up and "not found" in follow_up
    finally:
        manager.cleanup_all()
        time.sleep(0.1)


def test_execute_contained_command_result_is_normal(tmp_path):
    """A contained command must still report its real output and exit code."""
    runtime = _DummyRuntime(work_dir=str(tmp_path))
    manager = TerminalManager(runtime)
    try:
        _start_main(manager)

        result = manager.execute(
            session_id="main",
            command="set -e\nprintf 'one\\ntwo\\n'",
            timeout=8,
        )
        assert "one\ntwo" in result
        assert "[status: completed | return_code: 0" in result
        assert "shell exited" not in result
        assert "__cp_" not in result
    finally:
        manager.cleanup_all()
        time.sleep(0.1)
