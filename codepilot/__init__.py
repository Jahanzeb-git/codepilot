"""
CodePilot — A code-native agentic framework for building robust AI agents.

Quick start
-----------
    from codepilot import Runtime

    runtime = Runtime("agent.yaml")
    runtime.run("Create a FastAPI hello-world server.")

Streaming (pre-block reasoning streamed to user in real time)
-------------------------------------------------------------
    from codepilot import Runtime

    runtime = Runtime("agent.yaml", stream=True)
    runtime.run("Create a FastAPI hello-world server.")

Observable hooks
----------------
    from codepilot import Runtime, on_stream, on_finish

    runtime = Runtime("agent.yaml", stream=True)

    @on_stream(runtime)
    def handle_stream(text: str, **_):
        print(text, end="", flush=True)

    @on_finish(runtime)
    def handle_finish(summary: str, **_):
        print(f"\\nDone! {summary}")

    runtime.run("Refactor the database module.")
"""

__version__ = "0.8.0"
__author__  = "CodePilot"

# Core entry point
from .engine.runtime import Runtime

# AgentFile loader (useful for inspecting config without running)
from .core.agent_file import AgentConfig, AgentFile

# Session backends
from .core.session import InMemorySession, FileSession, DatabaseSession, BaseSession, create_session
from .core.memory import MemoryManager, MemoryConfig, ContextArchive

# Hook decorators — developers import these to wire up observability
from .engine.hooks import (
    EventType,
    HookSystem,
    on_stream,
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
    "DatabaseSession",
    "BaseSession",
    "create_session",
    "MemoryManager",
    "MemoryConfig",
    "ContextArchive",
    "EventType",
    "HookSystem",
    "on_stream",
    "on_tool_call",
    "on_tool_result",
    "on_ask_user",
    "on_permission_request",
    "on_finish",
    "on_user_message_queued",
    "on_user_message_injected",
]
