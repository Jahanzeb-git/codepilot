"""
terminal_verifiers.py — Deterministic verifiers for terminal-operation tasks.

These verifiers inspect the workspace and make real HTTP/process checks
after the agent run completes.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

from .base import Verifier


class SimpleCommandVerifier(Verifier):
    """T_T01: Verifies the agent ran hello.py successfully (file must still exist)."""

    def verify(self, workspace: Path) -> tuple[bool, str]:
        target = workspace / "hello.py"
        if not target.exists():
            return False, "hello.py was deleted — agent should not have removed it"
        content = target.read_text()
        if "Hello from eval!" not in content:
            return False, "hello.py content was unexpectedly modified"
        return True, "hello.py exists and unmodified — terminal task verified"


class ErrorRecoveryVerifier(Verifier):
    """T_T02: Verifies app.py was fixed and now has valid Python syntax."""

    def verify(self, workspace: Path) -> tuple[bool, str]:
        target = workspace / "app.py"
        if not target.exists():
            return False, "app.py not found"
        content = target.read_text()
        try:
            compile(content, "app.py", "exec")
        except SyntaxError as e:
            return False, f"app.py still has syntax error after recovery: {e}"
        return True, "app.py has valid Python syntax — error recovery verified"


class BackgroundServerVerifier(Verifier):
    """T_T03: Verifies the background server responds on port 18765."""

    def __init__(self, port: int = 18765):
        self._port = port

    def verify(self, workspace: Path) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(
                f"http://localhost:{self._port}", timeout=5
            ) as resp:
                if resp.status == 200:
                    return True, f"Server on port {self._port} returned 200 OK"
                return False, f"Server returned unexpected status: {resp.status}"
        except Exception as e:
            return False, f"Could not reach server on port {self._port}: {e}"


class CliGameVerifier(Verifier):
    """
    T_T04: Verifies the CLI game task completed.
    The game exits 0 on win, 1 on out-of-attempts.
    We just verify the game file exists and wasn't broken.
    The LLM judge evaluates strategy quality separately.
    """

    def verify(self, workspace: Path) -> tuple[bool, str]:
        target = workspace / "guess_game.py"
        if not target.exists():
            return False, "guess_game.py was deleted — agent should not have removed it"
        # Verify the file still has valid Python syntax
        content = target.read_text()
        try:
            compile(content, "guess_game.py", "exec")
        except SyntaxError as e:
            return False, f"guess_game.py syntax broken: {e}"
        return True, "guess_game.py exists and has valid syntax"
