"""
File: filesystem.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16
Updated: 2026-10-25

Filesystem tools for the CodePilot agentic runtime — native tool-calling edition.

Tools exposed:
  - view_file:   Read a file with 1-indexed line numbers.
  - write_file:  Write or overwrite a file. Content arrives as a direct
                 string argument — no payload blocks or fenced code needed.
  - edit_file:   Search-and-replace a unique region. Uses a multi-pass
                 forgiving algorithm so minor model mistakes in old_str
                 still result in a correct edit instead of a hard failure.
  - find_and_replace_many: Replace every occurrence of a literal string
                 across an entire file.

Write / edit caching:
  When write_file or edit_file fails due to an OS error (PermissionError,
  FileNotFoundError, etc.) the content is saved to an in-memory cache keyed
  by a numeric ID. The model is told the ID and asked to fix the underlying
  issue then retry with cache_id=N — no content regeneration needed.

JSON argument repair:
  If a write_file call arrives with a repaired JSON argument string (detected
  by the protocol layer via ToolCall.was_repaired), the write still proceeds
  and a soft warning is appended to the tool result so the model can improve
  future calls.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, List, Tuple, TYPE_CHECKING

from ..engine.hooks import EventType

if TYPE_CHECKING:
    from ..engine.runtime import AsyncRuntime


# ---------------------------------------------------------------------------
# Edit result — carries match outcome for one SEARCH/REPLACE chunk
# ---------------------------------------------------------------------------

class _EditResult:
    """Outcome of a single search-and-replace attempt."""

    __slots__ = ("ok", "new_content", "removed_lines", "added_lines", "error", "warning")

    def __init__(
        self,
        ok: bool,
        new_content: str = "",
        removed_lines: int = 0,
        added_lines: int = 0,
        error: str = "",
        warning: str = "",
    ):
        self.ok           = ok
        self.new_content  = new_content
        self.removed_lines = removed_lines
        self.added_lines   = added_lines
        self.error        = error
        self.warning      = warning

    @property
    def old_line_count(self) -> int:
        return self.removed_lines

    @property
    def new_line_count(self) -> int:
        return self.added_lines


# ---------------------------------------------------------------------------
# Forgiving search-and-replace algorithm
# ---------------------------------------------------------------------------

def _stripped(line: str) -> str:
    """Strip leading and trailing whitespace from a line."""
    return line.strip()


def _non_blank(lines: List[str]) -> List[str]:
    """Return only non-blank lines (preserving original strings)."""
    return [l for l in lines if l.strip()]


def _find_windows_stripped(file_lines: List[str], needle: List[str]) -> List[int]:
    """
    Sliding-window search — each file line and needle line is compared
    after stripping all leading/trailing whitespace.

    Returns the list of start indices (into file_lines) where the needle
    matches. An index i means file_lines[i:i+len(needle)] matches needle.
    """
    n = len(needle)
    stripped_needle = [_stripped(l) for l in needle]
    matches = []
    for i in range(len(file_lines) - n + 1):
        window = [_stripped(file_lines[i + j]) for j in range(n)]
        if window == stripped_needle:
            matches.append(i)
    return matches


def _find_windows_exact(file_lines: List[str], needle: List[str]) -> List[int]:
    """
    Sliding-window search — exact match including indentation.
    Each line is rstripped (to ignore trailing whitespace / CRLF) but
    leading whitespace is preserved.

    Returns the list of start indices where the needle matches exactly.
    """
    n = len(needle)
    exact_needle = [l.rstrip() for l in needle]
    matches = []
    for i in range(len(file_lines) - n + 1):
        window = [file_lines[i + j].rstrip() for j in range(n)]
        if window == exact_needle:
            matches.append(i)
    return matches


def _find_anchor_regions(
    file_lines: List[str],
    first_anchor: str,
    last_anchor: str,
    expected_n: int,
    strip: bool = True,
) -> List[Tuple[int, int]]:
    """
    Find all (start, end) index pairs where:
      - file_lines[start] matches first_anchor
      - file_lines[end]   matches last_anchor
      - end - start + 1   == expected_n

    When strip=True, comparison ignores indentation.
    When strip=False, comparison is exact (rstripped).

    Returns a list of (start_idx, end_idx) tuples.
    """
    compare = _stripped if strip else (lambda l: l.rstrip())
    n = expected_n
    regions = []
    for i in range(len(file_lines) - n + 1):
        end = i + n - 1
        if compare(file_lines[i]) == compare(first_anchor) and \
           compare(file_lines[end]) == compare(last_anchor):
            regions.append((i, end))
    return regions


def _apply_str_replace(file_content: str, old_str: str, new_str: str) -> _EditResult:
    """
    Forgiving multi-pass search-and-replace.

    Replaces the unique region in *file_content* matched by *old_str* with
    *new_str*. Passes run in order until exactly one match is found:

    Pass 1 — Full old_str, indentation stripped.
        All lines of old_str are stripped and compared against stripped
        file lines. If unique → apply.

    Pass 2 — Full old_str, exact indentation.
        Run only when Pass 1 found multiple matches. If unique → apply.

    Pass 3 — First-and-last anchor pair, indentation stripped.
        Use only the first and last non-blank lines of old_str as anchors.
        Searches for (anchor_first, anchor_last) pairs separated by exactly
        len(old_str_lines) - 1 lines. If unique → apply (middle content
        of old_str is ignored). This tolerates mistakes in middle lines.

    Pass 4 — First-and-last anchor pair, exact indentation.
        Run only when Pass 3 found multiple matches. If unique → apply.

    Pass 5 — Progressive bottom-up anchor expansion.
        Run only when Pass 4 still found multiple matches. Adds lines from
        the bottom of old_str upward one at a time (second-to-last, then
        third-to-last, …). For each extension, checks whether the bottom
        anchor window (last_anchor + extension lines above it) uniquely
        identifies a contiguous region within any of the Pass 4 candidates.
        Stops as soon as a unique region is confirmed.

    Indentation preservation:
        When the match was found via anchor-only passes (3-5), the base
        indentation of the matched region in the file is used to prefix
        each line of new_str that is not blank.

    Returns an _EditResult with ok=True on success or ok=False with a
    human-readable error string the model can act on.
    """
    file_lines = file_content.splitlines(keepends=True)
    raw_old_lines = old_str.splitlines()

    # Drop blank boundary lines (models sometimes add trailing newlines).
    while raw_old_lines and not raw_old_lines[0].strip():
        raw_old_lines.pop(0)
    while raw_old_lines and not raw_old_lines[-1].strip():
        raw_old_lines.pop()

    if not raw_old_lines:
        return _EditResult(
            ok=False,
            error=(
                "old_str is blank or contains only whitespace. "
                "Provide the exact lines from the file that identify the region to replace."
            ),
        )

    n = len(raw_old_lines)  # expected region height in the file

    # ------------------------------------------------------------------ #
    # Pass 1 — full match, indentation stripped                           #
    # ------------------------------------------------------------------ #
    p1_matches = _find_windows_stripped(file_lines, raw_old_lines)

    if len(p1_matches) == 1:
        return _build_result(file_lines, p1_matches[0], n, new_str)

    # ------------------------------------------------------------------ #
    # Pass 2 — full match, exact indentation (disambiguation)             #
    # ------------------------------------------------------------------ #
    if len(p1_matches) > 1:
        p2_matches = _find_windows_exact(file_lines, raw_old_lines)
        if len(p2_matches) == 1:
            return _build_result(file_lines, p2_matches[0], n, new_str)
        # If still multiple, fall through to anchor-based passes.
        # If p2 has zero matches (indentation truly wrong), also fall through.

    # No full-string match at all — only anchor-based passes remain.
    if n == 1:
        # Single-line old_str: no anchor pair possible — report clearly.
        first_line_stripped = _stripped(raw_old_lines[0])
        if len(p1_matches) > 1:
            return _EditResult(
                ok=False,
                error=(
                    f"old_str is ambiguous — {len(p1_matches)} lines match "
                    f"{first_line_stripped!r}. Add more surrounding lines to old_str "
                    "to make the region unique, or use find_and_replace_many() "
                    "to replace all occurrences."
                ),
            )
        return _EditResult(
            ok=False,
            error=(
                f"old_str line not found: {first_line_stripped!r}. "
                "Verify the line exists in the file with view_file() and retry."
            ),
        )

    first_anchor = raw_old_lines[0]
    last_anchor  = raw_old_lines[-1]

    # ------------------------------------------------------------------ #
    # Pass 3 — first+last anchor, indentation stripped                    #
    # ------------------------------------------------------------------ #
    p3_regions = _find_anchor_regions(
        file_lines, first_anchor, last_anchor, n, strip=True
    )

    if len(p3_regions) == 1:
        start, end = p3_regions[0]
        return _build_result(file_lines, start, end - start + 1, new_str)

    # ------------------------------------------------------------------ #
    # Pass 4 — first+last anchor, exact indentation                       #
    # ------------------------------------------------------------------ #
    if len(p3_regions) > 1:
        p4_regions = _find_anchor_regions(
            file_lines, first_anchor, last_anchor, n, strip=False
        )
        if len(p4_regions) == 1:
            start, end = p4_regions[0]
            return _build_result(file_lines, start, end - start + 1, new_str)

        if not p4_regions:
            # Indentation was wrong even for anchors — fall back to p3 candidates.
            p4_regions = p3_regions

        # ------------------------------------------------------------------ #
        # Pass 5 — progressive bottom-up expansion inside p4 candidates       #
        # ------------------------------------------------------------------ #
        # Build an expanding "bottom window" from old_str lines. Start with
        # the last anchor (already in p4_regions), then add lines from the
        # bottom of old_str upward: old_str[-2], old_str[-3], …
        #
        # For each candidate region (start, end) in p4_regions we check
        # whether the bottom window (size k, stripped) matches the bottom k
        # lines of that region in the file. Candidates that fail are dropped.
        # Once exactly one candidate survives → apply.
        #
        # This handles the classic case of two functions both ending with
        # `return "hi"`: as we expand upward we eventually hit a distinguishing
        # line unique to one function and the other candidate is eliminated.

        surviving = list(p4_regions)

        for k in range(2, n):
            # Bottom window: last k lines of old_str
            bottom_window = [_stripped(l) for l in raw_old_lines[n - k:]]

            surviving = [
                (start, end)
                for (start, end) in surviving
                if [_stripped(file_lines[end - k + 1 + j]) for j in range(k)]
                   == bottom_window
            ]

            if len(surviving) == 1:
                start, end = surviving[0]
                return _build_result(file_lines, start, end - start + 1, new_str)
            if not surviving:
                break

    # ------------------------------------------------------------------ #
    # Failure — build a useful error message                              #
    # ------------------------------------------------------------------ #
    first_line_str = _stripped(raw_old_lines[0])
    if len(p1_matches) == 0 and (not p3_regions if n > 1 else True):
        return _EditResult(
            ok=False,
            error=(
                f"old_str not found in file. Leading line {first_line_str!r} "
                "does not appear in the file. The file may have changed since you "
                "last read it — call view_file() to refresh and retry."
            ),
        )

    # Multiple candidates survived all passes.
    candidate_count = len(p3_regions) if p3_regions else len(p1_matches)
    return _EditResult(
        ok=False,
        error=(
            f"old_str is ambiguous — {candidate_count} matching regions found "
            f"(leading line: {first_line_str!r}). "
            "Add more surrounding lines to old_str to make it unique, or "
            "use find_and_replace_many() if the change applies to all occurrences."
        ),
    )


def _build_result(
    file_lines: List[str],
    start: int,
    region_n: int,
    new_str: str,
) -> _EditResult:
    """
    Apply a confirmed replacement: splice new_str in place of
    file_lines[start : start + region_n], preserving the base indentation
    of the matched region.
    """
    old_snippet = file_lines[start : start + region_n]

    # Detect base indentation from the first non-blank line of the matched region.
    base_indent = ""
    for line in old_snippet:
        stripped = line.lstrip()
        if stripped:
            base_indent = line[: len(line) - len(stripped)]
            break

    # new_str is inserted literally as the model specified — preserves
    # the model's exact intended indentation, including intentional
    # unindenting (e.g. extracting methods to module scope) or indenting.
    new_lines_raw = new_str.splitlines(keepends=True)
    if new_lines_raw and not new_lines_raw[-1].endswith("\n"):
        new_lines_raw[-1] += "\n"

    indented_new = new_lines_raw

    removed = region_n
    added   = len(indented_new)

    new_file_lines = file_lines[:start] + indented_new + file_lines[start + region_n:]
    new_content    = "".join(new_file_lines)

    return _EditResult(
        ok=True,
        new_content=new_content,
        removed_lines=removed,
        added_lines=added,
    )


# ---------------------------------------------------------------------------
# FilesystemTools
# ---------------------------------------------------------------------------

class FilesystemTools:
    """Filesystem tools for the CodePilot agentic runtime."""

    def __init__(self, runtime: "AsyncRuntime"):
        self.runtime = runtime
        #---content cache for OS-error retries---
        self._content_cache: dict[int, str] = {}
        self._cache_counter: int = 0

    # ------------------------------------------------------------------
    # Internal plumbing
    # ------------------------------------------------------------------

    def _cache_content(self, content: str) -> int:
        """Store content for retry after an OS error. Returns the cache ID."""
        self._cache_counter += 1
        self._content_cache[self._cache_counter] = content
        return self._cache_counter

    def _pop_cache(self, cache_id: int) -> Optional[str]:
        """Retrieve and remove cached content by ID. Returns None if not found."""
        return self._content_cache.pop(cache_id, None)

    def _request_permission(self, tool: str, description: str) -> bool:
        result = self.runtime.hooks.emit(
            EventType.PERMISSION_REQUEST, tool=tool, description=description,
        )
        if result is not None:
            return bool(result)
        return input(f"\n[Permission] {description}\nApprove? [y/N]: ").strip().lower() in ("y", "yes")

    def _safe_path(self, path: str) -> str:
        work_dir = Path(self.runtime.config.runtime.work_dir).resolve()
        abs_path = (work_dir / path).resolve()
        if not self.runtime.config.runtime.unsafe_mode:
            if not abs_path.is_relative_to(work_dir):
                raise PermissionError(
                    f"'{path}' is outside workspace '{work_dir}'. "
                    "Enable unsafe_mode in AgentFile to allow this."
                )
        return str(abs_path)

    def _emit_ok(self, tool: str, message: str) -> str:
        self.runtime._append_execution(message)
        self.runtime.hooks.emit(EventType.TOOL_RESULT, tool=tool, result=message)
        return message

    def _emit_err(self, tool: str, message: str) -> str:
        full = f"[{tool} ERROR] {message}"
        self.runtime._append_execution(full)
        self.runtime.hooks.emit(EventType.TOOL_RESULT, tool=tool, result=full)
        return full

    def _check_permission(self, tool: str, description: str) -> bool:
        tool_cfg = self.runtime._tool_config(tool)
        if tool_cfg.get("require_permission", False):
            return self._request_permission(tool, description)
        return True

    def _os_error_message(self, tool: str, path: str, exc: Exception, cache_id: int) -> str:
        """Build an actionable OS error message with the cache ID."""
        err_type = type(exc).__name__
        lines = [
            f"Failed to access or modify '{path}' ({err_type}: {exc}).",
            f"Payload cached as cache_id={cache_id}. Do NOT regenerate content.",
            "Diagnose and fix the underlying issue (e.g. check path, permissions, disk space, or process locks), then retry with:",
            f"  {tool}(path=\"{path}\", cache_id={cache_id})",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tool: view_file
    # ------------------------------------------------------------------

    def view_file(self, path: str, start_line: int = 1, end_line: int = None) -> str:
        """
        Read file content with 1-indexed line numbers.

        Use start_line / end_line to read only the lines you need. Omit
        end_line to read through the end of the file. Always read the result
        before emitting a dependent edit.
        """
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="view_file",
            args={"path": path, "start_line": start_line, "end_line": end_line},
        )
        try:
            if not isinstance(start_line, int) or isinstance(start_line, bool):
                return self._emit_err(
                    "view_file",
                    f"start_line must be an integer >= 1, got {start_line!r}.",
                )
            if start_line < 1:
                return self._emit_err(
                    "view_file",
                    f"start_line={start_line} is invalid — line numbers are 1-indexed.",
                )
            if end_line is not None:
                if not isinstance(end_line, int) or isinstance(end_line, bool):
                    return self._emit_err(
                        "view_file",
                        f"end_line must be an integer, got {end_line!r}.",
                    )
                if end_line < start_line:
                    return self._emit_err(
                        "view_file",
                        f"end_line={end_line} must be >= start_line={start_line}.",
                    )

            abs_path = self._safe_path(path)
            if not Path(abs_path).is_file():
                return self._emit_err(
                    "view_file",
                    f"'{path}' not found. Check the path in the codebase snapshot.",
                )

            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()

            total = len(all_lines)
            if total == 0:
                result = f"[view_file] '{path}' is empty.\n[END OF FILE]"
                return self._emit_ok("view_file", result)

            if start_line > total:
                return self._emit_err(
                    "view_file",
                    f"start_line={start_line} exceeds file length of {total} lines.",
                )

            e = min(end_line, total) if end_line is not None else total
            s = start_line - 1
            numbered = [f"{i + s + 1:5} | {line.rstrip()}" for i, line in enumerate(all_lines[s:e])]
            content = "\n".join(numbered)
            footer = (
                "\n[END OF FILE]" if e >= total
                else f"\n[TRUNCATED: {total - e} lines remaining — use start_line={e + 1} to continue]"
            )
            result = f"[view_file] '{path}' (lines {s + 1}–{e} of {total})\n{content}{footer}"
            self._emit_ok("view_file", result)

            if hasattr(self.runtime, "_watcher"):
                self.runtime._watcher.register(abs_path)

            return result

        except Exception as exc:
            return self._emit_err(
                "view_file",
                f"Unexpected error reading '{path}': {exc}.",
            )

    # ------------------------------------------------------------------
    # Tool: write_file
    # ------------------------------------------------------------------

    def write_file(
        self,
        path: str,
        content: str = "",
        cache_id: Optional[int] = None,
        _was_repaired: bool = False,
    ) -> str:
        """
        Create a new file or overwrite an existing file completely.

        Pass the full file content as the 'content' argument. Parent
        directories are created automatically.

        If a previous write failed with an OS error (PermissionError, wrong
        path, etc.) the runtime cached the content and returned a cache_id.
        Fix the underlying issue first, then retry by passing that cache_id —
        the content argument can be omitted in that case:
          write_file(path="correct/path.py", cache_id=<N>)
        """
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="write_file",
            args={"path": path, "cache_id": cache_id},
        )

        # ---------------------------------------------------------------- #
        # Resolve content — either from the argument or from the retry cache
        # ---------------------------------------------------------------- #
        if cache_id is not None:
            cached = self._pop_cache(cache_id)
            if cached is None:
                available = sorted(self._content_cache)
                hint = (
                    f" Available cache IDs: {available}." if available
                    else " The cache is empty — no failed write was recorded."
                )
                return self._emit_err(
                    "write_file",
                    f"cache_id={cache_id} not found.{hint}",
                )
            content = cached

        if not content and cache_id is None:
            return self._emit_err(
                "write_file",
                "content argument is empty. Provide the full file content.",
            )

        if not self._check_permission("write_file", f"Create/overwrite '{path}'"):
            return self._emit_err("write_file", f"Permission denied for '{path}'.")

        try:
            abs_path = self._safe_path(path)
            Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
            existed = Path(abs_path).exists()
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)

            line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            verb = "updated" if existed else "created"
            cache_note = " (from cache)" if cache_id is not None else ""
            result = f"[write_file] '{path}' {verb}{cache_note} ({len(content)} bytes, {line_count} lines)."

            if _was_repaired:
                result += (
                    "\n[Notice] The JSON arguments for this call contained encoding "
                    "mistakes that were automatically repaired. Watch for escaping issues "
                    "in future write_file calls (e.g. use \\n for newlines inside strings)."
                )

            if hasattr(self.runtime, "_watcher"):
                try:
                    self.runtime._watcher.register(abs_path)
                except Exception:
                    pass

            return self._emit_ok("write_file", result)

        except Exception as exc:
            new_cache_id = self._cache_content(content)
            return self._emit_err(
                "write_file",
                self._os_error_message("write_file", path, exc, new_cache_id),
            )

    # ------------------------------------------------------------------
    # Tool: edit_file
    # ------------------------------------------------------------------

    def edit_file(
        self,
        path: str,
        old_str: str = "",
        new_str: str = "",
        cache_id: Optional[int] = None,
    ) -> str:
        """
        Replace a unique region in a file identified by old_str with new_str.

        old_str must uniquely identify the region to replace. It does not need
        to be letter-perfect — the tool uses a forgiving multi-pass algorithm:

        1. Full old_str match, indentation ignored.
        2. Full old_str match, exact indentation (disambiguates pass 1).
        3. First+last line anchor pair, indentation ignored (tolerates mistakes
           in middle lines of old_str).
        4. First+last anchor pair, exact indentation.
        5. Progressive bottom-up expansion — adds lines from the bottom of
           old_str upward until the region is uniquely identified.

        For large edits across multiple independent regions in the same file,
        call edit_file() multiple times — one call per region.

        If a previous edit failed with an OS error, the runtime cached the
        arguments and returned a cache_id. Fix the issue and retry:
          edit_file(path="correct/path.py", old_str="...", new_str="...", cache_id=<N>)
        """
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="edit_file",
            args={"path": path, "cache_id": cache_id},
        )

        # ---------------------------------------------------------------- #
        # Resolve old_str / new_str — from arguments or from retry cache   #
        # ---------------------------------------------------------------- #
        if cache_id is not None:
            cached = self._pop_cache(cache_id)
            if cached is None:
                available = sorted(self._content_cache)
                hint = (
                    f" Available cache IDs: {available}." if available
                    else " The cache is empty."
                )
                return self._emit_err(
                    "edit_file",
                    f"cache_id={cache_id} not found.{hint}",
                )
            # Cache stores "old_str\x00new_str" packed together.
            packed = cached
            sep    = packed.find("\x00")
            old_str = packed[:sep]
            new_str = packed[sep + 1:]

        if not old_str:
            return self._emit_err(
                "edit_file",
                "old_str is empty. Provide the lines from the file that identify the region to replace.",
            )

        if not self._check_permission("edit_file", f"Edit '{path}'"):
            return self._emit_err("edit_file", f"Permission denied for '{path}'.")

        try:
            abs_path = self._safe_path(path)
            if not Path(abs_path).is_file():
                return self._emit_err(
                    "edit_file",
                    f"'{path}' does not exist. Use write_file() to create a new file.",
                )

            with open(abs_path, "r", encoding="utf-8") as f:
                current = f.read()

            result = _apply_str_replace(current, old_str, new_str)

            if not result.ok:
                #---cache old_str + new_str so the model can retry after fixing the path---
                if cache_id is None:
                    new_cache_id = self._cache_content(old_str + "\x00" + new_str)
                    return self._emit_err(
                        "edit_file",
                        f"{result.error}\n"
                        f"Edit arguments cached as cache_id={new_cache_id} for retry.",
                    )
                return self._emit_err("edit_file", result.error)

            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(result.new_content)

            if hasattr(self.runtime, "_watcher"):
                try:
                    self.runtime._watcher.register(abs_path)
                except Exception:
                    pass

            total_lines = result.new_content.count("\n") + (
                1 if result.new_content and not result.new_content.endswith("\n") else 0
            )
            delta = result.added_lines - result.removed_lines
            delta_str = f"Δ{delta:+d}" if delta != 0 else "Δ0"
            cache_note = " (from cache)" if cache_id is not None else ""

            msg = (
                f"[edit_file] '{path}' updated{cache_note}: "
                f"replaced {result.removed_lines}-line region with {result.added_lines} lines "
                f"({delta_str}). File now has {total_lines} lines."
            )
            if result.warning:
                msg += f"\n[Notice] {result.warning}"

            return self._emit_ok("edit_file", msg)

        except Exception as exc:
            if cache_id is None:
                new_cache_id = self._cache_content(old_str + "\x00" + new_str)
                return self._emit_err(
                    "edit_file",
                    self._os_error_message("edit_file", path, exc, new_cache_id),
                )
            return self._emit_err("edit_file", f"Failed to edit '{path}': {exc}.")

    # ------------------------------------------------------------------
    # Tool: find_and_replace_many
    # ------------------------------------------------------------------

    def find_and_replace_many(self, path: str, find: str, replace: str) -> str:
        """
        Replace every occurrence of an exact literal string in a file.

        Use this when the same text appears multiple times and all instances
        must change (e.g. renaming a variable, constant, or import path
        across an entire file). For targeted single-region edits, use
        edit_file() instead.
        """
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="find_and_replace_many",
            args={"path": path, "find": find, "replace": replace},
        )

        if not find:
            return self._emit_err(
                "find_and_replace_many",
                "find= must be a non-empty string.",
            )

        try:
            abs_path = self._safe_path(path)
            if not Path(abs_path).is_file():
                return self._emit_err(
                    "find_and_replace_many",
                    f"'{path}' not found in the workspace.",
                )

            with open(abs_path, "r", encoding="utf-8") as f:
                original = f.read()

            count = original.count(find)
            if count == 0:
                return self._emit_err(
                    "find_and_replace_many",
                    f"String {find!r} not found in '{path}'. Nothing was changed.",
                )

            if not self._check_permission(
                "find_and_replace_many",
                f"Replace {count} occurrence(s) of {find!r} in '{path}'",
            ):
                return self._emit_err(
                    "find_and_replace_many",
                    f"Permission denied for '{path}'.",
                )

            new_content = original.replace(find, replace)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            if hasattr(self.runtime, "_watcher"):
                try:
                    self.runtime._watcher.register(abs_path)
                except Exception:
                    pass

            result = (
                f"[find_and_replace_many] '{path}': replaced {count} occurrence(s) of "
                f"{find!r} with {replace!r}."
            )
            return self._emit_ok("find_and_replace_many", result)

        except Exception as exc:
            return self._emit_err(
                "find_and_replace_many",
                f"Unexpected error in '{path}': {exc}.",
            )
