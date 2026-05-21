"""
File: context.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description:
Workspace file tree snapshot generator for the CodePilot runtime.

Architectural Notes:
Generates a JSON snapshot of the workspace directory tree including line
counts for every file. This snapshot is injected into the system prompt
every step so the agent always has an accurate, up-to-date map of what
files exist and their approximate size without needing to call ls or find.
Common noise directories (node_modules, __pycache__, .git, etc.) are
excluded automatically to keep the snapshot concise and relevant.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

import json
from pathlib import Path
from typing import Any, Dict, List


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
        self.root_dir = Path(root_dir).resolve()          # Path, not str
        self.max_depth = max_depth
        self.ignored_dirs = _IGNORED_DIRS | set(ignored_dirs or [])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _line_count(self, file_path: Path) -> int:
        try:
            with file_path.open("r", encoding="utf-8", errors="ignore") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def _build_tree(self, current_path: Path, depth: int) -> Dict[str, Any]:
        if depth > self.max_depth:
            return {"...": "(max depth reached)"}

        tree: Dict[str, Any] = {}
        try:
            entries = sorted(current_path.iterdir(), key=lambda p: p.name)
        except PermissionError:
            return {"error": "permission denied"}

        for entry in entries:
            if entry.is_dir():
                if entry.name in self.ignored_dirs:
                    continue
                tree[entry.name] = self._build_tree(entry, depth + 1)
            else:
                tree[entry.name] = {"lines": self._line_count(entry)}

        return tree

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_snapshot(self) -> Dict[str, Any]:
        """Returns the raw nested dict keyed by the workspace absolute path."""
        return {str(self.root_dir): self._build_tree(self.root_dir, depth=0)}

    def get_formatted_snapshot(self) -> str:
        """Returns the snapshot serialised as pretty-printed JSON."""
        return json.dumps(self.get_snapshot(), indent=2)