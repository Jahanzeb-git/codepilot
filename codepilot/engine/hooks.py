from typing import Callable, List, Dict, Any, Optional
from enum import Enum


class EventType(Enum):
    START                 = "start"                  # Agent loop begins
    STEP                  = "step"                   # A new agentic step is starting
    STREAM                = "stream"                 # Pre-block reasoning text (streamed)
    TOOL_CALL             = "tool_call"              # A tool is about to be invoked
    TOOL_RESULT           = "tool_result"            # A tool returned a result
    ASK_USER              = "ask_user"               # Agent is asking the user a question
    PERMISSION_REQUEST    = "permission_request"     # Tool requires user permission
    SECURITY_ERROR        = "security_error"         # AST validation rejected the code
    RUNTIME_ERROR         = "runtime_error"          # Execution error occurred
    FINISH                = "finish"                 # Agent called done() — task complete
    MAX_STEPS             = "max_steps"              # Loop ended due to step limit
    USER_MESSAGE_QUEUED   = "user_message_queued"    # send_message() was called
    USER_MESSAGE_INJECTED = "user_message_injected"  # Message inserted into context
    SESSION_RESET         = "session_reset"          # reset() was called


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
    def _default_tool_call(tool: str, args: dict, **_):
        arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
        print(f"\n⚙️  [{tool}]({arg_str})")

    @staticmethod
    def _default_tool_result(tool: str, result: str, **_):
        preview = result[:300] + ("…" if len(result) > 300 else "")
        print(f"   ↳ {preview}")

    @staticmethod
    def _default_security_error(error: str, **_):
        print(f"\n🔒 Security violation: {error}")

    @staticmethod
    def _default_runtime_error(error: str, **_):
        print(f"\n❌ Runtime error:\n{error}")

    @staticmethod
    def _default_finish(summary: str, **_):
        print(f"\n✅ Done: {summary}")

    @staticmethod
    def _default_max_steps(**_):
        print("\n⚠️  Maximum step limit reached. Agent stopped.")

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
