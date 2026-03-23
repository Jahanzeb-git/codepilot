"""
Text / regex search via ripgrep (rg) for the CodePilot agentic runtime.

Provides 1 tool: find()
  - scope='file'     — search within a single file
  - scope='files'    — search within a list of files
  - scope='codebase' — search the entire workspace (honours .gitignore)

Falls back to a pure-Python implementation when rg is not installed,
with a built-in exclusion list for common noise directories.
"""

import fnmatch
import os
import re
import shutil
import subprocess
from typing import List, Optional, Union, TYPE_CHECKING

from ..engine.hooks import EventType

if TYPE_CHECKING:
    from ..engine.runtime import Runtime


class SearchTools:

    def __init__(self, runtime: "Runtime"):
        self.runtime = runtime
        self._has_rg: Optional[bool] = None

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _check_rg(self) -> bool:
        if self._has_rg is None:
            self._has_rg = shutil.which("rg") is not None
        return self._has_rg

    def _safe_path(self, path: str) -> str:
        work_dir = os.path.abspath(self.runtime.config.runtime.work_dir)
        abs_path = os.path.abspath(os.path.join(work_dir, path))
        if not self.runtime.config.runtime.unsafe_mode:
            if not abs_path.startswith(work_dir + os.sep) and abs_path != work_dir:
                raise PermissionError(
                    f"'{path}' is outside workspace '{work_dir}'. "
                    "Enable unsafe_mode in AgentFile to allow this."
                )
        return abs_path

    def _rel(self, abs_path: str, work_dir: str) -> str:
        """Return a path relative to work_dir, or the original if outside."""
        try:
            return os.path.relpath(abs_path, work_dir)
        except ValueError:
            return abs_path

    def _format(self, lines: list, max_results: int) -> str:
        if not lines:
            return "[find] No matches found."
        total  = len(lines)
        shown  = lines[:max_results]
        header = f"[find] {min(total, max_results)} match(es)"
        if total > max_results:
            header += f" (showing {max_results} of {total})"
        return header + ":\n" + "\n".join(shown)

    # ------------------------------------------------------------------ #
    #  Tool entry point                                                    #
    # ------------------------------------------------------------------ #

    def find(
        self,
        pattern: str,
        scope: str = "codebase",
        target: Union[str, List[str], None] = None,
        include: str = None,
        max_results: int = 50,
    ) -> str:
        """
        Search for a text pattern (regex) across a file, list of files, or the
        whole codebase. Returns one 'file:line:matched_line' per matching line.

        scope='file'     — single file. target='path/to/file.py'
        scope='files'    — multiple files. target=['a.py', 'b.py']
        scope='codebase' — entire workspace. Use include='*.py' to restrict by glob.

        Examples:
          find(pattern=r'class \w+Error', scope='codebase', include='*.py')
          find(pattern=r'def login\(', scope='file', target='routes/auth.py')

        Use raw strings for regex special chars: r'validate_email\(' not 'validate_email('.
        """
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="find",
            args={"pattern": pattern, "scope": scope, "target": target, "include": include},
            label=f"Searching: {pattern!r}",
        )

        try:
            if self._check_rg():
                result = self._find_rg(pattern, scope, target, include, max_results)
            else:
                result = self._find_python(pattern, scope, target, include, max_results)
        except Exception as exc:
            result = f"[find] ERROR: {exc}"

        self.runtime._append_execution(result)
        self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="find", result=result)
        return result

    # ------------------------------------------------------------------ #
    #  ripgrep backend (preferred)                                         #
    # ------------------------------------------------------------------ #

    def _find_rg(
        self,
        pattern: str,
        scope: str,
        target,
        include: Optional[str],
        max_results: int,
    ) -> str:
        work_dir = os.path.abspath(self.runtime.config.runtime.work_dir)
        # Run from work_dir so all output paths are relative automatically.
        cmd = ["rg", "--with-filename", "--line-number", "--no-heading", pattern]

        if scope == "codebase":
            if include:
                cmd += ["--glob", include]
            cmd.append(".")

        elif scope == "file":
            if not target:
                return "[find] ERROR: scope='file' requires a target path."
            rel = self._rel(self._safe_path(target), work_dir)
            cmd.append(rel)

        elif scope == "files":
            if not target:
                return "[find] ERROR: scope='files' requires a target list."
            targets = [target] if isinstance(target, str) else target
            cmd += [self._rel(self._safe_path(t), work_dir) for t in targets]

        else:
            return f"[find] ERROR: Unknown scope '{scope}'. Use 'file', 'files', or 'codebase'."

        r = subprocess.run(cmd, capture_output=True, text=True, cwd=work_dir)
        # rg exit 0 = matches, 1 = no matches (not an error), 2 = real error
        if r.returncode == 2:
            return f"[find] rg error: {r.stderr.strip()}"

        lines = r.stdout.strip().splitlines() if r.stdout.strip() else []
        return self._format(lines, max_results)

    # ------------------------------------------------------------------ #
    #  Pure-Python fallback                                                #
    # ------------------------------------------------------------------ #

    _NOISE_DIRS = frozenset({
        "node_modules", ".git", "__pycache__", ".venv", "venv",
        "env", "dist", "build", "target", ".mypy_cache", ".ruff_cache",
        ".pytest_cache", "coverage", ".tox",
    })

    def _find_python(
        self,
        pattern: str,
        scope: str,
        target,
        include: Optional[str],
        max_results: int,
    ) -> str:
        work_dir = os.path.abspath(self.runtime.config.runtime.work_dir)
        compiled = re.compile(pattern)
        matches: List[str] = []

        def search_file(abs_path: str) -> bool:
            """Returns True when max_results is reached and we should stop."""
            rel = self._rel(abs_path, work_dir)
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                    for lineno, line in enumerate(fh, 1):
                        if compiled.search(line):
                            matches.append(f"{rel}:{lineno}:{line.rstrip()}")
                            if len(matches) >= max_results:
                                return True
            except (OSError, PermissionError):
                pass
            return False

        if scope == "codebase":
            for root, dirs, files in os.walk(work_dir):
                dirs[:] = [d for d in dirs if d not in self._NOISE_DIRS]
                for fname in files:
                    if include and not fnmatch.fnmatch(fname, os.path.basename(include)):
                        continue
                    if search_file(os.path.join(root, fname)):
                        break

        elif scope == "file":
            if not target:
                return "[find] ERROR: scope='file' requires a target path."
            search_file(self._safe_path(target))

        elif scope == "files":
            if not target:
                return "[find] ERROR: scope='files' requires a target list."
            targets = [target] if isinstance(target, str) else target
            for t in targets:
                if search_file(self._safe_path(t)):
                    break

        else:
            return f"[find] ERROR: Unknown scope '{scope}'. Use 'file', 'files', or 'codebase'."

        return self._format(matches, max_results)
