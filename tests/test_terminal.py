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
        command="""neofetch""",
        timeout=3,
    )
    print(result)
    time.sleep(1)
    
    ##print("\n")
    #print(result1)
    #print("\n")
    #result2 = manager.read_output("main", 1)
    #print(result2)
    #print("\n")
    #time.sleep(1)
    #result3 = manager.execute("main", "", 5)
    #print(result3)
    #print("\n")
    #time.sleep(1)
    #result4 = manager.execute("main", "cd ~/python/drills && python3 day9_drills.py", 5)
    #print(result4)
    print("\n")
    print("✅ All automated tests finished.")
    print("⏳ Keeping the backend alive for 10 minutes so you can play with Code-Server...")
    print("   → Try clicking the '+' button in Code-Server's terminal panel and select 'CodePilot Terminal'!")
    print("   → Any commands you type will be piped to Python!")
    
    try:
        time.sleep(1000)
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        manager.cleanup_all()

    
if __name__ == "__main__":
    main()
