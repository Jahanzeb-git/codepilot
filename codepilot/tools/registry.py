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
                First paragraph of the docstring.
        """
        lines = []
        for name, func in sorted(self._tools.items()):
            try:
                sig = inspect.signature(func)
            except (ValueError, TypeError):
                sig = ""

            doc = inspect.getdoc(func) or "No description."
            # Use only the first paragraph of the docstring to keep prompt tight.
            short_doc = doc.split("\n\n")[0].strip()

            lines.append(f"• {name}{sig}")
            lines.append(f"    {short_doc}")
            lines.append("")

        return "\n".join(lines).strip()
