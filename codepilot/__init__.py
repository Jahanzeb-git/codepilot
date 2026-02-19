"""
CodePilot — A code-native agentic framework for building robust AI agents.

Quick start
-----------
    from codepilot import Runtime

    runtime = Runtime("agent.yaml")
    runtime.run("Create a FastAPI hello-world server.")

Observable hooks
----------------
    from codepilot import Runtime, on_think, on_finish

    runtime = Runtime("agent.yaml")

    @on_think(runtime)
    def handle_think(message: str, **_):
        print(f"Agent: {message}")

    @on_finish(runtime)
    def handle_finish(summary: str, **_):
        print(f"Done! {summary}")

    runtime.run("Refactor the database module.")
"""

__version__ = "0.2.1"
__author__  = "CodePilot"

# Core entry point
from .engine.runtime import Runtime

# AgentFile loader (useful for inspecting config without running)
from .core.agent_file import AgentConfig, AgentFile

# Session backends
from .core.session import InMemorySession, FileSession, BaseSession, create_session

# Hook decorators — developers import these to wire up observability
from .engine.hooks import (
    EventType,
    HookSystem,
    on_think,
    on_tool_call,
    on_tool_result,
    on_ask_user,
    on_permission_request,
    on_finish,
    on_user_message_queued,
    on_user_message_injected,
)

__all__ = [
    "Runtime",
    "AgentConfig",
    "AgentFile",
    "InMemorySession",
    "FileSession",
    "BaseSession",
    "create_session",
    "EventType",
    "HookSystem",
    "on_think",
    "on_tool_call",
    "on_tool_result",
    "on_ask_user",
    "on_permission_request",
    "on_finish",
    "on_user_message_queued",
    "on_user_message_injected",
]
