"""
File: hooks.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description: 
Observable event bus for the CodePilot runtime.

Architectural Notes:
Implements an event-driven architecture allowing external systems (like web servers 
or UI clients) to stream agent responses and monitor tool executions in real-time 
without tightly coupling to the core LLM execution loop.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

from typing import Callable, List, Dict, Any, Optional
from enum import Enum


class EventType(Enum):
    START                 = "start"                  # Agent loop begins
    STEP                  = "step"                   # A new agentic step is starting
    STREAM                = "stream"                 # Pre-block reasoning text (streamed)
    THINKING_STREAM       = "thinking_stream"        # Internal chain-of-thought tokens (never in STREAM)
    TOOL_CALL             = "tool_call"              # A tool is about to be invoked
    TOOL_RESULT           = "tool_result"            # A tool returned a result
    ASK_USER              = "ask_user"               # Agent is asking the user a question
    PERMISSION_REQUEST    = "permission_request"     # Tool requires user permission
    SECURITY_ERROR        = "security_error"         # AST validation rejected the code
    RUNTIME_ERROR         = "runtime_error"          # Execution error occurred
    FINISH                = "finish"                 # Task complete (completion block detected)
    MAX_STEPS             = "max_steps"              # Loop ended due to step limit
    USER_MESSAGE_QUEUED   = "user_message_queued"    # send_message() was called
    USER_MESSAGE_INJECTED = "user_message_injected"  # Message inserted into context
    SESSION_RESET         = "session_reset"          # reset() was called
    CONTEXT_MAINTENANCE_START = "context_maintenance_start"  # runtime forced a context cleanup turn
    CONTEXT_DROP          = "context_drop"           # archive_context (or the emergency backstop) reduced context size
    SUBAGENT_SPAWN        = "subagent_spawn"         # A sub-agent was spawned
    SUBAGENT_MESSAGE      = "subagent_message"       # Sub-agent sent a message to main
    SUBAGENT_FINISH       = "subagent_finish"        # A sub-agent completed its task
    LLM_RESPONSE          = "llm_response"           # Raw LLM generation before history compression (observability)


class HookSystem:
    """
    Observable event bus for the CodePilot runtime.

    Handlers can be registered for any EventType. For PERMISSION_REQUEST, the
    handler may return a bool (True = approved). If no handler is registered
    for PERMISSION_REQUEST, the runtime falls back to a CLI prompt.
    """

    def __init__(self):
        self._hooks: Dict[EventType, List[Callable]] = {e: [] for e in EventType}
        self._install_defaults()

    # ------------------------------------------------------------------
    # Default handlers (thin CLI output so the library is useful out of
    # the box even with zero configuration)
    # ------------------------------------------------------------------

    def _install_defaults(self):
        self.register(EventType.STREAM, self._default_stream)
        self.register(EventType.TOOL_CALL, self._default_tool_call)
        self.register(EventType.TOOL_RESULT, self._default_tool_result)
        self.register(EventType.SECURITY_ERROR, self._default_security_error)
        self.register(EventType.RUNTIME_ERROR, self._default_runtime_error)
        self.register(EventType.FINISH, self._default_finish)
        self.register(EventType.MAX_STEPS, self._default_max_steps)

    @staticmethod
    def _default_stream(text: str, **_):
        print(text, end="", flush=True)

    @staticmethod
    def _default_tool_call(tool: str, args: dict, label: str = "", **_):
        display = label if label else ", ".join(f"{k}={v!r}" for k, v in args.items())
        print(f"\n[{tool}] {display}")

    @staticmethod
    def _default_tool_result(tool: str, result: str, **_):
        preview = result[:300] + ("…" if len(result) > 300 else "")
        print(f"{preview}")

    @staticmethod
    def _default_security_error(error: str, **_):
        print(f"\nSecurity violation: {error}")

    @staticmethod
    def _default_runtime_error(error: str, **_):
        print(f"\nRuntime error:\n{error}")

    @staticmethod
    def _default_finish(summary: str, **_):
        print(f"\nDone: {summary}")

    @staticmethod
    def _default_max_steps(**_):
        print("\nMaximum step limit reached. Agent stopped.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, event_type: EventType, callback: Callable):
        """Register a callback for an event. Multiple callbacks are supported."""
        self._hooks[event_type].append(callback)

    def clear(self, event_type: EventType):
        """Remove all handlers for an event (including defaults)."""
        self._hooks[event_type] = []

    def emit(self, event_type: EventType, **data) -> Optional[Any]:
        """
        Fire all handlers for an event.

        For PERMISSION_REQUEST, returns the value from the *last* registered
        handler that returns a non-None value, or None if none did.
        """
        last_return = None
        for callback in self._hooks[event_type]:
            try:
                ret = callback(**data)
                if ret is not None:
                    last_return = ret
            except Exception as exc:
                print(f"[HookSystem] Error in handler for {event_type}: {exc}")
        return last_return


# ---------------------------------------------------------------------------
# Decorator helpers for developer ergonomics
# ---------------------------------------------------------------------------

def on_stream(runtime_instance):
    """Decorator: register a handler for STREAM events (pre-block reasoning text)."""
    def decorator(func: Callable):
        runtime_instance.hooks.clear(EventType.STREAM)
        runtime_instance.hooks.register(EventType.STREAM, func)
        return func
    return decorator


def on_tool_call(runtime_instance):
    """Decorator: register a handler for TOOL_CALL events."""
    def decorator(func: Callable):
        runtime_instance.hooks.clear(EventType.TOOL_CALL)
        runtime_instance.hooks.register(EventType.TOOL_CALL, func)
        return func
    return decorator


def on_tool_result(runtime_instance):
    """Decorator: register a handler for TOOL_RESULT events."""
    def decorator(func: Callable):
        runtime_instance.hooks.clear(EventType.TOOL_RESULT)
        runtime_instance.hooks.register(EventType.TOOL_RESULT, func)
        return func
    return decorator


def on_ask_user(runtime_instance):
    """Decorator: register a handler for ASK_USER events."""
    def decorator(func: Callable):
        runtime_instance.hooks.register(EventType.ASK_USER, func)
        return func
    return decorator


def on_permission_request(runtime_instance):
    """
    Decorator: register a permission handler.
    Handler receives (tool, description) and must return True or False.
    """
    def decorator(func: Callable):
        runtime_instance.hooks.clear(EventType.PERMISSION_REQUEST)
        runtime_instance.hooks.register(EventType.PERMISSION_REQUEST, func)
        return func
    return decorator


def on_finish(runtime_instance):
    """Decorator: register a handler for FINISH events."""
    def decorator(func: Callable):
        runtime_instance.hooks.clear(EventType.FINISH)
        runtime_instance.hooks.register(EventType.FINISH, func)
        return func
    return decorator


def on_user_message_queued(runtime_instance):
    """Decorator: fires when send_message() is called (message not yet in context)."""
    def decorator(func: Callable):
        runtime_instance.hooks.register(EventType.USER_MESSAGE_QUEUED, func)
        return func
    return decorator


def on_user_message_injected(runtime_instance):
    """Decorator: fires when a queued message is inserted into LLM context."""
    def decorator(func: Callable):
        runtime_instance.hooks.register(EventType.USER_MESSAGE_INJECTED, func)
        return func
    return decorator


def on_runtime_error(runtime_instance):
    """Decorator: register a handler for RUNTIME_ERROR events (parser errors, etc.)."""
    def decorator(func: Callable):
        runtime_instance.hooks.clear(EventType.RUNTIME_ERROR)
        runtime_instance.hooks.register(EventType.RUNTIME_ERROR, func)
        return func
    return decorator


def on_thinking_stream(runtime_instance):
    """Decorator: register a handler for THINKING_STREAM events.

    Fires with thinking=<str> for each chunk of model chain-of-thought.
    These are NEVER emitted on the STREAM event — kept completely separate
    so the CLI can render them without polluting the main text stream.
    Handler receives: thinking (str).
    """
    def decorator(func: Callable):
        runtime_instance.hooks.clear(EventType.THINKING_STREAM)
        runtime_instance.hooks.register(EventType.THINKING_STREAM, func)
        return func
    return decorator


def on_context_maintenance_start(runtime_instance):
    """Decorator: fires when the runtime forces a context-cleanup turn.

    This fires BEFORE the agent acts — at the moment measured context
    pressure crosses the trigger threshold — so a UI can show something
    like "Cleaning up context..." while the agent decides what to archive.

    Handler receives: stress_pct (int), history_tokens (int),
    safe_budget (int), candidates (str — human-readable per-task breakdown).
    """
    def decorator(func: Callable):
        runtime_instance.hooks.register(EventType.CONTEXT_MAINTENANCE_START, func)
        return func
    return decorator


def on_context_drop(runtime_instance):
    """Decorator: fires when context was actually reduced — either by the
    agent calling archive_context(), or by the emergency backstop when no
    completed tasks were available to offer the agent.

    Handler receives: before_pct (int), after_pct (int), tokens_saved (int),
    tasks_archived (list[int] — empty when the emergency backstop fired).
    """
    def decorator(func: Callable):
        runtime_instance.hooks.register(EventType.CONTEXT_DROP, func)
        return func
    return decorator


def on_subagent_spawn(runtime_instance):
    """Decorator: fires when main agent spawns a sub-agent.

    Handler receives: agent_id (int), task_summary (str).
    """
    def decorator(func: Callable):
        runtime_instance.hooks.register(EventType.SUBAGENT_SPAWN, func)
        return func
    return decorator


def on_subagent_message(runtime_instance):
    """Decorator: fires when a sub-agent sends a message to the main agent.

    Handler receives: agent_id (int), message (str).
    Returns: reply string to unblock the sub-agent, or None to leave blocked.
    """
    def decorator(func: Callable):
        runtime_instance.hooks.register(EventType.SUBAGENT_MESSAGE, func)
        return func
    return decorator


def on_subagent_finish(runtime_instance):
    """Decorator: fires when a sub-agent completes its task.

    Handler receives: agent_id (int), summary (str), files_written (list[str]),
    elapsed_seconds (float), error (str|None).
    """
    def decorator(func: Callable):
        runtime_instance.hooks.register(EventType.SUBAGENT_FINISH, func)
        return func
    return decorator


def on_llm_response(runtime_instance):
    """Decorator: fires with the raw, uncompressed LLM generation text.

    Fires immediately after inference and before history compression or
    execution. Use this for observability / trace logging — the payload
    is the exact string the model produced, including all fenced blocks.

    Handler receives: step (int), response (str).
    """
    def decorator(func: Callable):
        runtime_instance.hooks.register(EventType.LLM_RESPONSE, func)
        return func
    return decorator