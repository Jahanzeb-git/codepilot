"""
File: registry.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Central tool registry for the CodePilot agentic runtime.

Acts as the single source of truth for all tools available to the agent.
Supports two modes:

  - Native function-calling mode: tools are sent to the provider as JSON
    schemas via the API. The registry generates those schemas with
    build_tool_schemas() and dispatches incoming ToolCall objects with
    dispatch().

  - Legacy text-protocol mode (kept for backward compatibility): the
    registry generates a human-readable definition block that gets
    injected into the system prompt via get_definitions().

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

import inspect
from typing import Callable, Dict, List, Optional

from ..core.tool_protocol import ToolCall, build_openai_tools


class ToolRegistry:
    """
    Central registry for all CodePilot tools.

    Tools are registered as plain callables (usually bound methods).
    In native function-calling mode, call build_tool_schemas() to get
    provider-ready JSON schemas and dispatch() to execute incoming calls.
    In legacy mode, get_definitions() returns a formatted string for
    the system prompt.
    """

    def __init__(self):
        self._tools: Dict[str, Callable] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, name: str, func: Callable):
        """Register a single callable under the given tool name."""
        self._tools[name] = func

    def unregister(self, name: str):
        """Remove a tool by name. No-op if the tool is not registered.

        Used by the runtime to hide a tool from the LLM (e.g., when
        semantic_search is enabled but misconfigured).
        """
        self._tools.pop(name, None)

    def register_from_instance(self, instance, only: list = None):
        """
        Register all public, non-dunder methods on *instance* as tools.
        Pass `only=[...]` to restrict to specific method names.
        """
        for method_name, method in inspect.getmembers(instance, predicate=inspect.ismethod):
            if method_name.startswith("_"):
                continue
            if only and method_name not in only:
                continue
            self._tools[method_name] = method

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> Optional[Callable]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        """Return the names of all currently registered tools."""
        return list(self._tools.keys())

    def as_sandbox_dict(self) -> Dict[str, Callable]:
        """Returns a copy of {name: callable} suitable for exec() globals."""
        return dict(self._tools)

    # ------------------------------------------------------------------
    # Native function-calling support
    # ------------------------------------------------------------------

    def build_tool_schemas(self) -> List[Dict]:
        """
        Build the OpenAI-format JSON schema list for all registered tools.

        Pass the result directly to provider.chat_with_tools(tools=...).
        Compatible with OpenAI, Alibaba, DeepSeek, and ExperientialLabs.
        For Anthropic, the provider converts this format automatically.
        """
        return build_openai_tools(self._tools)

    def dispatch(self, call: ToolCall) -> str:
        """
        Execute a ToolCall and return the string result.

        Looks up the registered callable by name, calls it with the
        parsed arguments, and returns a string suitable for the tool
        result message. Returns an error string if the tool is not
        registered or raises an exception.
        """
        func = self._tools.get(call.name)
        if func is None:
            return (
                f"[ERROR] Unknown tool '{call.name}'. "
                f"Available tools: {', '.join(sorted(self._tools))}."
            )

        try:
            kwargs = dict(call.arguments)
            #---forward argument repair flag to tools that accept it---
            if call.was_repaired:
                sig = inspect.signature(func)
                if "_was_repaired" in sig.parameters:
                    kwargs["_was_repaired"] = True

            result = func(**kwargs)
            # Tools that return None (fire-and-forget via _append_execution)
            # don't need a separate return value — return empty string so the
            # caller can still build a well-formed tool result message.
            return str(result) if result is not None else ""
        except TypeError as exc:
            return (
                f"[ERROR] Tool '{call.name}' called with wrong arguments: {exc}. "
                f"Expected signature: {inspect.signature(func)}"
            )
        except Exception as exc:
            return f"[ERROR] Tool '{call.name}' raised {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    # Legacy text-protocol prompt generation
    # ------------------------------------------------------------------

    def get_definitions(self) -> str:
        """
        Returns a human-readable tool reference block for the system prompt.

        Groups tools by category and prioritizes critical tools first to
        exploit the primacy effect, mitigating LLM "Lost in the Middle" syndrome.
        """
        categories = {
            "### 📁 Filesystem & Workspace Tools": ["view_file", "write_file", "edit_file", "find_and_replace_many", "find", "semantic_search"],
            "### 💻 Terminal & Execution Tools": ["execute", "read_output", "send_input", "terminate_terminal"],
            "### 🔌 MCP Tools": ["mcp"],
            "### 🧠 Context Maintenance": ["archive_context"],
            "### 🤖 Sub-Agent Delegation": ["spawn_subagent", "await_subagent"],
            "### ⚙️ Runtime Control & Interaction": ["ask_user"],
        }

        lines = []
        registered_names = set(self._tools.keys())

        for category_header, tool_names in categories.items():
            active_tools_in_cat = [n for n in tool_names if n in registered_names]
            if active_tools_in_cat:
                lines.append(category_header)
                for name in active_tools_in_cat:
                    registered_names.remove(name)
                    func = self._tools[name]
                    try:
                        sig = inspect.signature(func)
                    except (ValueError, TypeError):
                        sig = ""

                    doc = inspect.getdoc(func) or "No description."
                    indented = "\n".join(
                        f"    {line}" if line.strip() else ""
                        for line in doc.splitlines()
                    )

                    lines.append(f"• {name}{sig}")
                    lines.append(indented)
                    lines.append("")

        if registered_names:
            lines.append("### 🔧 Other Tools")
            for name in sorted(registered_names):
                func = self._tools[name]
                try:
                    sig = inspect.signature(func)
                except (ValueError, TypeError):
                    sig = ""

                doc = inspect.getdoc(func) or "No description."
                indented = "\n".join(
                    f"    {line}" if line.strip() else ""
                    for line in doc.splitlines()
                )

                lines.append(f"• {name}{sig}")
                lines.append(indented)
                lines.append("")

        return "\n".join(lines).strip()
