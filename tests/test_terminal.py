from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
import time

# Import from the local repo checkout explicitly, never from an installed wheel.
REPO_ROOT = Path.home() / "codepilot"
sys.path.insert(0, str(REPO_ROOT))
logging.disable(logging.CRITICAL)

from codepilot.engine.hooks import EventType, HookSystem  # noqa: E402
from codepilot.tools.terminal import TerminalManager  # noqa: E402


class _DummyRuntime:
    """Minimal runtime surface required by TerminalManager for standalone testing."""

    def __init__(self, work_dir: str) -> None:
        self.config = SimpleNamespace(
            runtime=SimpleNamespace(work_dir=work_dir)
        )
        self.hooks = HookSystem()
        self.hooks.clear(EventType.STREAM)
        self.hooks.clear(EventType.TOOL_CALL)
        self.hooks.clear(EventType.TOOL_RESULT)
        self.hooks.clear(EventType.SECURITY_ERROR)
        self.hooks.clear(EventType.RUNTIME_ERROR)
        self.hooks.clear(EventType.FINISH)
        self.hooks.clear(EventType.MAX_STEPS)
        self.messages: list[dict[str, str]] = []
        self.execution_log: list[str] = []

    def _tool_config(self, tool_name: str) -> dict:
        if tool_name == "execute":
            return {"max_output_chars": 10000}
        return {}

    def _append_execution(self, text: str) -> None:
        self.execution_log.append(text)


def main() -> None:
    runtime = _DummyRuntime(work_dir=str(REPO_ROOT))
    manager = TerminalManager(runtime)
    manager.start_default_terminal()

    print("\n✅ MuxServer is live at /tmp/codepilot_main.sock")
    print("   → NOW refresh http://localhost:7681 in your browser!")
    print("   → Commands start in 5 seconds...\n")
    time.sleep(5)

    result = manager.execute(
        session_id="main",
        command="""python3 -c 'name=input("What is your name? "); print(f"Hello, {name}")'""",
        timeout=5,
    )
    print(result)
    time.sleep(1)
    
    result1 = manager.send_input("main", "Jahanzeb Ahmed\n", 1)
    print("\n")
    print(result1)
    print("\n")
    result2 = manager.read_output("main", 1)
    print(result2)
    print("\n")
    result3 = manager.execute("main", "cd ~ && cat example.rs", 2)
    print(result3)
    print("\n")
    result4 = manager.execute("main", "cd ~/python/drills && python3 day9_drills.py", 5)
    print(result4)
    print("\n")

    result5 = manager.execute("main", "sudo apt update", 5)
    print(result5)
    print("\n")
    result6 = manager.send_input("main", "ubuntu@2002\n", 60)
    print(result6)
    print("\n")
    result7 = manager.execute("main", "cd ~/omniroot-agent && docker compose up", 20)
    print(result7)
    print("\n")
    result8 = manager.send_input("main", "\x03", 20)
    print(result8)
    print("\n")
    result9 = manager.execute("main", "ls /nonexistent/path", 1)
    print(result9)
    print("\n")
    result10 = manager.execute("main", "nonexist_command", 1)
    print(result10)
    print("\n")
    result11 = manager.execute("main", "python3 -c 'import sys; sys.exit(42)'", 2)
    print(result11)
    print("\n")
    result12 = manager.execute("main", "python3", 3)
    print(result12)
    print("\n")
    result13 = manager.send_input("main", "x = 42\n", 2)
    print(result13)
    print("\n")
    result14 = manager.send_input("main", "print(x * 2)\n", 2)
    print(result14)
    print("\n")
    result15 = manager.send_input("main", "\x04", 2)
    print(result15)
    print("\n")
    result16 = manager.execute("session_b", "export MYVAR='hello world' && echo $MYVAR", 2, True)
    print(result16)
    print("\n")
    result17 = manager.execute("main", "echo $MYVAR", 1)
    print(result17)
    print("\n")

    manager.cleanup_all()


if __name__ == "__main__":
    main()
