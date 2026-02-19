import os
import json
from typing import Dict, Any, List


_IGNORED_DIRS = frozenset([
    ".git", "__pycache__", "venv", ".venv", "env",
    "node_modules", ".idea", ".vscode", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
])


class ContextManager:
    """
    Generates a JSON snapshot of the workspace's file tree, including line
    counts for every file. The snapshot is keyed by the absolute path of the
    workspace root so the agent always knows the exact paths to use.
    """

    def __init__(
        self,
        root_dir: str,
        max_depth: int = 6,
        ignored_dirs: List[str] = None,
    ):
        self.root_dir = os.path.abspath(root_dir)
        self.max_depth = max_depth
        self.ignored_dirs = _IGNORED_DIRS | set(ignored_dirs or [])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _line_count(self, file_path: str) -> int:
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def _build_tree(self, current_path: str, depth: int) -> Dict[str, Any]:
        if depth > self.max_depth:
            return {"...": "(max depth reached)"}

        tree: Dict[str, Any] = {}
        try:
            entries = sorted(os.listdir(current_path))
        except PermissionError:
            return {"error": "permission denied"}

        for entry in entries:
            full = os.path.join(current_path, entry)
            if os.path.isdir(full):
                if entry in self.ignored_dirs:
                    continue
                tree[entry] = self._build_tree(full, depth + 1)
            else:
                tree[entry] = {"lines": self._line_count(full)}

        return tree

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_snapshot(self) -> Dict[str, Any]:
        """Returns the raw nested dict keyed by the workspace absolute path."""
        return {self.root_dir: self._build_tree(self.root_dir, depth=0)}

    def get_formatted_snapshot(self) -> str:
        """Returns the snapshot serialised as pretty-printed JSON."""
        return json.dumps(self.get_snapshot(), indent=2)
