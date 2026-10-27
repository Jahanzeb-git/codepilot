"""
File: context.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description:
Workspace file tree snapshot generator for the CodePilot runtime.

Architectural Notes:
Generates a compact, indented tree of the workspace with a line count for
every file. The snapshot is injected into the dynamic half of the system
prompt every step so the agent always has an accurate map of what files
exist and their approximate size without calling ls or find.

The format is deliberately terse: one line per entry, two spaces of
indentation per directory level, and a trailing line count for files. The
snapshot is bounded by both a file count and a character budget so a large
workspace cannot crowd out the conversation. Noise directories are skipped,
and line counts are cached by (mtime, size) to avoid re-reading unchanged
files on every step.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_IGNORED_DIRS = frozenset([
    ".git", "__pycache__", "venv", ".venv", "env",
    "node_modules", ".idea", ".vscode", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    # Tool and runtime state — never source the agent needs to navigate.
    ".grepai", ".codepilot", ".cache",
    # Framework / bundler caches.
    ".next", ".nuxt", ".turbo", ".parcel-cache",
    # Test and environment artifacts.
    "coverage", ".tox", ".nox", ".eggs",
    ".gradle", ".terraform", ".serverless",
])

#: Directory suffixes that are always generated artifacts.
_IGNORED_DIR_SUFFIXES = (".egg-info",)

#: Default bounds. The snapshot is re-rendered every step, so both caps are
#: intentionally conservative; the model can always use find/list_dir for
#: anything the snapshot omits.
DEFAULT_MAX_FILES = 2000
DEFAULT_MAX_CHARS = 16000

#: Cap on the line-count cache. Cleared wholesale when exceeded; this is a
#: coarse bound, not an LRU.
_MAX_LINE_CACHE = 5000


class ContextManager:
    """
    Generates a compact tree snapshot of the workspace, including line counts
    for every file. The snapshot starts with the absolute workspace path so
    the agent always knows the exact paths to use.
    """

    def __init__(
        self,
        root_dir: str,
        max_depth: int = 6,
        ignored_dirs: List[str] = None,
        max_files: int = DEFAULT_MAX_FILES,
        max_chars: int = DEFAULT_MAX_CHARS,
    ):
        self.root_dir = Path(root_dir).resolve()          # Path, not str
        self.max_depth = max_depth
        self.ignored_dirs = _IGNORED_DIRS | set(ignored_dirs or [])
        self.max_files = max(1, int(max_files))
        self.max_chars = max(1, int(max_chars))
        #: path -> (mtime_ns, size, line_count or None for binary)
        self._line_cache: Dict[str, Tuple[int, int, Optional[int]]] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _line_count(self, file_path: Path) -> Optional[int]:
        """
        Return the line count, or None for a binary file.

        Cached by (mtime, size) while unchanged. Binary detection looks for a
        NUL byte in the first 8 KB, matching the watcher's rule. Counting is
        streaming so a large binary or log cannot be loaded into memory.
        """
        try:
            stat = file_path.stat()
        except OSError:
            return 0

        key = str(file_path)
        cached = self._line_cache.get(key)
        if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]

        count = 0
        binary = False
        last_byte = b""
        try:
            with file_path.open("rb") as f:
                first_chunk = True
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    if first_chunk:
                        first_chunk = False
                        if b"\x00" in chunk[:8192]:
                            binary = True
                            break
                    count += chunk.count(b"\n")
                    last_byte = chunk[-1:]
        except OSError:
            count = 0

        result: Optional[int] = None if binary else count
        if result is not None and last_byte and last_byte != b"\n":
            result += 1

        if len(self._line_cache) >= _MAX_LINE_CACHE:
            self._line_cache.clear()
        self._line_cache[key] = (stat.st_mtime_ns, stat.st_size, result)
        return result

    def _is_ignored_dir(self, name: str) -> bool:
        return name in self.ignored_dirs or name.endswith(_IGNORED_DIR_SUFFIXES)

    def _format_tree(self, current_path: Path, depth: int, state: Dict[str, Any]) -> str:
        """Render one directory level and recurse, honouring both caps."""
        if depth > self.max_depth:
            return f"{'  ' * depth}... (max depth)\n"

        try:
            entries = sorted(
                current_path.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except (PermissionError, OSError):
            return f"{'  ' * depth}[permission denied]\n"

        out: List[str] = []
        for entry in entries:
            if state["truncated"]:
                break

            try:
                is_dir = entry.is_dir()
            except OSError:
                continue

            if is_dir:
                if self._is_ignored_dir(entry.name):
                    continue
                line = f"{'  ' * depth}{entry.name}/\n"
                if not self._append(out, line, state):
                    break
                out.append(self._format_tree(entry, depth + 1, state))
                continue

            if state["files"] >= self.max_files:
                state["truncated"] = True
                break

            count = self._line_count(entry)
            if count is None:
                line = f"{'  ' * depth}{entry.name} [binary]\n"
            else:
                line = f"{'  ' * depth}{entry.name} {count}\n"
            if not self._append(out, line, state):
                break
            state["files"] += 1
            if count is not None:
                state["lines"] += count

        return "".join(out)

    @staticmethod
    def _append(out: List[str], line: str, state: Dict[str, Any]) -> bool:
        """Append a rendered line unless it would exceed the char budget."""
        if state["chars"] + len(line) > state["max_chars"]:
            state["truncated"] = True
            return False
        out.append(line)
        state["chars"] += len(line)
        return True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_snapshot(self) -> Dict[str, Any]:
        """Return the raw nested dict (kept for callers that inspect it)."""
        return {str(self.root_dir): self._raw_tree(self.root_dir, depth=0)}

    def _raw_tree(self, current_path: Path, depth: int) -> Dict[str, Any]:
        """Nested-dict form used by get_snapshot(). Not capped by chars."""
        if depth > self.max_depth:
            return {"...": "(max depth reached)"}
        try:
            entries = sorted(current_path.iterdir(), key=lambda p: p.name)
        except (PermissionError, OSError):
            return {"error": "permission denied"}

        tree: Dict[str, Any] = {}
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                if self._is_ignored_dir(entry.name):
                    continue
                tree[entry.name] = self._raw_tree(entry, depth + 1)
            else:
                count = self._line_count(entry)
                tree[entry.name] = (
                    {"binary": True} if count is None else {"lines": count}
                )
        return tree

    def get_formatted_snapshot(self) -> str:
        """Return the compact, bounded tree used in the system prompt."""
        state: Dict[str, Any] = {
            "files": 0,
            "lines": 0,
            "chars": 0,
            "truncated": False,
            "max_chars": self.max_chars,
        }
        body = self._format_tree(self.root_dir, depth=0, state=state)

        header = f"{self.root_dir} [{state['files']} files, {state['lines']} lines]"
        if state["truncated"]:
            header += (
                f" (truncated: showing the first {state['files']} files; "
                "use find or list_dir for the rest)"
            )
        return f"{header}\n{body}".rstrip("\n")
