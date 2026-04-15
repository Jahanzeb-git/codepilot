"""
File: registry.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description:
Central tool registry for the CodePilot agentic runtime.

Architectural Notes:
Acts as the single source of truth for all tools available to the agent's
exec() sandbox. Registered tools are plain Python callables (bound methods).
The registry auto-generates human-readable tool definitions from each
callable's signature and docstring, which are injected into the system
prompt every step so the agent always has an accurate, up-to-date reference.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

import inspect
from typing import Callable, Dict, Optional


class ToolRegistry:
    """
    Central registry for all CodePilot tools.

    Tools are registered as plain callables (usually bound methods).
    The registry auto-generates a formatted definition string for the
    system prompt based on each callable's signature and docstring.
    """

    def __init__(self):
        self._tools: Dict[str, Callable] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, name: str, func: Callable):
        """Register a single callable under the given tool name."""
        self._tools[name] = func

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

    def as_sandbox_dict(self) -> Dict[str, Callable]:
        """Returns a copy of {name: callable} suitable for exec() globals."""
        return dict(self._tools)

    # ------------------------------------------------------------------
    # Prompt generation
    # ------------------------------------------------------------------

    def get_definitions(self) -> str:
        """
        Returns a human-readable tool reference block for the system prompt.

        Format per tool:
            tool_name(param: type = default, ...) -> ...
                Full docstring (indented).
        """
        lines = []
        for name, func in sorted(self._tools.items()):
            try:
                sig = inspect.signature(func)
            except (ValueError, TypeError):
                sig = ""

            doc = inspect.getdoc(func) or "No description."
            # Indent every line of the docstring so it visually nests under
            # the tool signature — keeps the prompt scannable.
            indented = "\n".join(
                f"    {line}" if line.strip() else ""
                for line in doc.splitlines()
            )

            lines.append(f"• {name}{sig}")
            lines.append(indented)
            lines.append("")

        return "\n".join(lines).strip()
