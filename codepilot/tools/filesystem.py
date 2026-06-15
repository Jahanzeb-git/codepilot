"""
File: filesystem.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16
Updated: 2026-06-12

Description:
Unified filesystem tools for the CodePilot agentic runtime.

Architectural Notes:
Provides three micro-tools:
  - view_file:  Read file content with 1-indexed line numbers.
  - write_file: Write or overwrite a complete file (payload block).
  - edit_file:  Search-and-replace a unique text region.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

import os
from pathlib import Path
from typing import Optional, Union, TYPE_CHECKING

from ..core.block_parser import CodeBlock
from ..engine.hooks import EventType

if TYPE_CHECKING:
    from ..engine.runtime import Runtime


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class _SearchResult:
    """Result of a single search-and-replace attempt."""
    __slots__ = ("ok", "new_content", "old_line_count", "new_line_count", "error")

    def __init__(
        self,
        ok: bool,
        new_content: str = "",
        old_line_count: int = 0,
        new_line_count: int = 0,
        error: str = "",
    ):
        self.ok = ok
        self.new_content = new_content
        self.old_line_count = old_line_count
        self.new_line_count = new_line_count
        self.error = error


def _apply_str_replace(file_content: str, search: str, replacement: str) -> _SearchResult:
    """
    Find a unique block of lines in *file_content* that matches *search* and
    replace the ENTIRE matched lines with *replacement*.
    """
    # Split file into lines, preserving line endings.
    file_lines = file_content.splitlines(keepends=True)

    # Build normalised (stripped) search lines, dropping empty boundary lines.
    raw_search_lines = search.splitlines()
    while raw_search_lines and not raw_search_lines[0].strip():
        raw_search_lines.pop(0)
    while raw_search_lines and not raw_search_lines[-1].strip():
        raw_search_lines.pop()

    if not raw_search_lines:
        return _SearchResult(
            ok=False,
            error=(
                "search= is blank or whitespace-only. Provide 2-4 unique, "
                "non-blank lines from the file that uniquely identify the region "
                "to replace."
            ),
        )

    # Strip each search line for comparison (ignore indentation).
    needle = [line.strip() for line in raw_search_lines]
    n = len(needle)

    # Sliding window: find all positions where stripped file lines match needle.
    matches = []
    for i in range(len(file_lines) - n + 1):
        window = [file_lines[i + j].rstrip("\n").rstrip("\r").strip() for j in range(n)]
        if window == needle:
            matches.append(i)

    if len(matches) == 0:
        first_line = needle[0]
        return _SearchResult(
            ok=False,
            error=(
                f"<<<<<<< SEARCH chunk not found in file. The leading line "
                f'"{first_line}" (and its surrounding lines) does not match any '
                f"lines in the file. Possible causes: (1) the file was edited "
                f"since this content was read — call view_file() to "
                f"refresh; (2) the text no longer exists in this form."
            ),
        )
    if len(matches) > 1:
        first_line = needle[0]
        return _SearchResult(
            ok=False,
            error=(
                f"<<<<<<< SEARCH chunk is ambiguous — found {len(matches)} matching "
                f'regions (leading line: "{first_line}"). Expand the SEARCH block '
                f"to include more surrounding unique lines so the match "
                f"is unambiguous."
            ),
        )

    # Exactly one match found. Replace those whole lines with the payload.
    match_start = matches[0]
    old_line_count = n

    # Normalise replacement: always ends with exactly one newline.
    repl = replacement.rstrip("\n") + "\n" if replacement.strip() else "\n"
    new_line_count = repl.count("\n")

    new_lines = file_lines[:match_start] + [repl] + file_lines[match_start + n:]
    new_content = "".join(new_lines)

    return _SearchResult(
        ok=True,
        new_content=new_content,
        old_line_count=old_line_count,
        new_line_count=new_line_count,
    )


# ---------------------------------------------------------------------------
# FilesystemTools
# ---------------------------------------------------------------------------

class FilesystemTools:
    """Filesystem tools for the CodePilot agentic runtime."""

    def __init__(self, runtime: "Runtime"):
        self.runtime = runtime

    # ------------------------------------------------------------------
    # Internal plumbing
    # ------------------------------------------------------------------

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

    def _emit_error(self, tool: str, message: str) -> None:
        result = f"[{tool}] ERROR: {message}"
        self.runtime._append_execution(result)
        self.runtime.hooks.emit(EventType.TOOL_RESULT, tool=tool, result=result)

    def _emit_result(self, tool: str, message: str) -> None:
        self.runtime._append_execution(message)
        self.runtime.hooks.emit(EventType.TOOL_RESULT, tool=tool, result=message)

    @staticmethod
    def _is_int(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    @staticmethod
    def _payload_filename_error(path: str, payloads: list) -> Optional[str]:
        """Validate that each payload block carries the correct filename= annotation."""
        expected = path.replace("\\", "/")
        for idx, payload in enumerate(payloads, start=1):
            if payload.filename is None:
                return (
                    f"Payload block {idx} for '{path}' is missing filename={path}. "
                    "No file was changed."
                )
            actual = payload.filename.replace("\\", "/")
            if actual != expected:
                return (
                    f"Payload block {idx} filename mismatch: got filename={payload.filename}, "
                    f"expected filename={path}. No file was changed."
                )
        return None

    # ------------------------------------------------------------------
    # Tool: view_file
    # ------------------------------------------------------------------

    def view_file(self, path: str, start_line: int = 1, end_line: int = None) -> None:
        """
        Read file content with 1-indexed line numbers. Use start_line/end_line to target
        a specific range; omit both to read the entire file.
        
        [CRITICAL CONSTRAINT]
        NEVER output a payload block (```lang filename=...) when using view_file().
        It does NOT consume payloads!

        Example 1:
        ```codepilot
        view_file("app.py")
        ```
        Example 2:
        ```codepilot
        view_file("requirements.txt")
        view_file("main.py", start_line=50, end_line=80)
        ```
        """
        ui_status = (
            f"Reading {path}: L{start_line}-{end_line}"
            if end_line else f"Reading {path}..."
        )
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="view_file",
            args={"path": path, "start_line": start_line,
                  "end_line": end_line, "ui_status": ui_status},
        )
        try:
            if not self._is_int(start_line):
                self._emit_error(
                    "view_file",
                    f"start_line must be an integer >= 1, got {start_line!r}. "
                    "Omit start_line/end_line to read the whole file.",
                )
                return
            if start_line < 1:
                self._emit_error(
                    "view_file",
                    f"start_line={start_line} is invalid; line numbers are 1-indexed.",
                )
                return
            if end_line is not None:
                if not self._is_int(end_line):
                    self._emit_error(
                        "view_file",
                        f"end_line must be an integer >= start_line, got {end_line!r}.",
                    )
                    return
                if end_line < start_line:
                    self._emit_error(
                        "view_file",
                        f"end_line={end_line} must be >= start_line={start_line}.",
                    )
                    return

            abs_path = self._safe_path(path)
            if not Path(abs_path).is_file():
                self._emit_error(
                    "view_file",
                    f"'{path}' not found in the workspace. Verify the path from the "
                    "codebase snapshot or use find() to locate the file.",
                )
                return

            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()

            total = len(all_lines)
            if total == 0:
                result = f"[view_file] '{path}' is empty (0 lines).\n[END OF FILE]"
                self._emit_result("view_file", result)
                if hasattr(self.runtime, "_watcher"):
                    self.runtime._watcher.register(abs_path)
                return

            if start_line > total:
                self._emit_error(
                    "view_file",
                    f"start_line={start_line} exceeds '{path}' length of {total} lines. "
                    f"Use a value between 1 and {total}.",
                )
                return

            e = end_line if end_line is not None else total
            if e > total:
                e = total

            s = start_line - 1
            numbered = [f"{i + s + 1:5} | {line.rstrip()}" for i, line in enumerate(all_lines[s:e])]
            content = "\n".join(numbered)

            footer = (
                "\n[END OF FILE]" if e >= total
                else f"\n[TRUNCATED: {total - e} lines remaining — use start_line={e + 1} to continue]"
            )

            result = (
                f"[view_file] '{path}' (lines {s + 1}–{e} of {total})\n"
                + content + footer
            )
            self._emit_result("view_file", result)

            if hasattr(self.runtime, "_watcher"):
                self.runtime._watcher.register(abs_path)

        except Exception as exc:
            self._emit_error(
                "view_file",
                f"Unexpected failure reading '{path}': {exc}. "
                "Treat this as a tool failure; do not assume any content was returned.",
            )

    # ------------------------------------------------------------------
    # Tool: write_file
    # ------------------------------------------------------------------

    def write_file(self, path: str) -> None:
        """
        Write or overwrite a file completely. Creates parent directories automatically.
        
        [CRITICAL CONSTRAINT]
        You CAN provide ONE payload block immediately after your ```codepilot block,
        annotated as filename=<path> OR MULTIPLE payload blocks ordered per write_file() call in ```codepilot block.
        
        Example 1:
        ```codepilot
        write_file("app.py")
        ```
        ```python filename=app.py
        # full file content here
        ```
        Example 2:
        ```codepilot
        write_file("file.txt")
        write_file("example.py")
        ```
        ```text filename=file.txt
        content for file.txt
        ```
        ```python filename=example.py
        # content for example.py
        ```
        """
        ui_status = f"Creating {path}..."
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="write_file",
            args={"path": path, "ui_status": ui_status},
        )

        # Enforce write budget
        if self.runtime._step_write_count >= 5:
            self._emit_error(
                "write_file",
                f"Maximum 5 file writes per step exceeded. "
                f"Skipped '{path}'. Continue in the next agentic step.",
            )
            return
        self.runtime._step_write_count += 1

        # Consume payload block BEFORE any error that might short-circuit
        payload = self.runtime.pop_next_payload_block()
        if payload is None:
            self._emit_error(
                "write_file",
                f"No Payload Block found for '{path}'. Provide exactly one fenced "
                "Payload Block immediately after the ```codepilot block, annotated "
                f"as filename={path}. Never pass file content as a function argument.",
            )
            return

        err = self._payload_filename_error(path, [payload])
        if err:
            self._emit_error("write_file", err)
            return

        # Permission gate
        tool_cfg = self.runtime._tool_config("write_file")
        if tool_cfg.get("require_permission", False):
            if not self._request_permission("write_file", f"Create/overwrite '{path}'"):
                self._emit_error(
                    "write_file",
                    f"Permission denied for '{path}'. No file was changed.",
                )
                return

        try:
            abs_path = self._safe_path(path)
            Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
            content = payload.content
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            result = (
                f"[write_file] '{path}' created "
                f"({len(content)} bytes, {line_count} lines)."
            )
            self._emit_result("write_file", result)
            if hasattr(self.runtime, "_watcher"):
                try:
                    self.runtime._watcher.register(self._safe_path(path))
                except Exception:
                    pass
        except Exception as exc:
            self._emit_error(
                "write_file",
                f"Failed to write '{path}': {exc} No file was changed unless the "
                "error occurred during a low-level OS write.",
            )

    # ------------------------------------------------------------------
    # Tool: edit_file
    # ------------------------------------------------------------------

    def edit_file(self, path: str) -> None:
        """
        Search-and-replace regions of text. You can include multiple SEARCH chunks in the Payload Block.
        
        [CRITICAL CONSTRAINT]
        You MUST provide exactly ONE payload block immediately after your ```codepilot block,
        annotated as filename=<path>.
        
        Inside the Payload Block, use Git conflict markers EXACTLY like this:
        <<<<<<< SEARCH
        def process(self):
            pass
        =======
        def process(self):
            result = self._run()
            return result
        >>>>>>> REPLACE

        Example 1:
        ```codepilot
        edit_file("app.py")
        ```
        ```python filename=app.py
        <<<<<<< SEARCH
        TIMEOUT=5
        =======
        TIMEOUT=15
        >>>>>>> REPLACE

        Example 2:
        ```codepilot
        edit_file("decorator.py")
        ```
        ```python filename=decorator.py
        <<<<<<< SEARCH
            def wrapper(*args, **kwargs):
        =======
            def wrapper(*args, **kwargs):
                print("Before calling the function...")
        >>>>>>> REPLACE
        <<<<<<< SEARCH
        MAX_RETRIES = 5
        =======
        MAX_RETRIES = 10
        >>>>>>> REPLACE
        ```
        """
        ui_status = f"Editing {path}..."
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="edit_file",
            args={"path": path, "ui_status": ui_status},
        )

        # Guard: only one edit per file per step
        edited_files = self.runtime._step_edited_files
        if path in edited_files:
            self._emit_error(
                "edit_file",
                f"Only one edit_file per file per step is allowed. '{path}' was "
                "already edited this step. You can include multiple <<<<<<< SEARCH chunks "
                "in a single Payload Block to make multiple edits at once.",
            )
            return
        edited_files.add(path)

        # Consume payload
        payload = self.runtime.pop_next_payload_block()
        if payload is None:
            self._emit_error(
                "edit_file",
                f"No Payload Block found for '{path}'. Provide exactly one fenced "
                "Payload Block immediately after the ```codepilot block, annotated "
                f"as filename={path}.",
            )
            return

        err = self._payload_filename_error(path, [payload])
        if err:
            self._emit_error("edit_file", err)
            return

        # Parse chunks
        chunks = []
        lines = payload.content.splitlines(keepends=True)
        state = "OUTSIDE"
        search_lines = []
        replace_lines = []
        
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("<<<<<<< SEARCH"):
                if state != "OUTSIDE":
                    self._emit_error("edit_file", f"Malformed payload for '{path}': Nested <<<<<<< SEARCH marker found.")
                    return
                state = "SEARCH"
                search_lines = []
                replace_lines = []
            elif stripped.startswith("======="):
                if state != "SEARCH":
                    self._emit_error("edit_file", f"Malformed payload for '{path}': ======= found without preceding <<<<<<< SEARCH.")
                    return
                state = "REPLACE"
            elif stripped.startswith(">>>>>>> REPLACE"):
                if state != "REPLACE":
                    self._emit_error("edit_file", f"Malformed payload for '{path}': >>>>>>> REPLACE found without preceding =======.")
                    return
                state = "OUTSIDE"
                chunks.append(("".join(search_lines), "".join(replace_lines)))
            else:
                if state == "SEARCH":
                    search_lines.append(line)
                elif state == "REPLACE":
                    replace_lines.append(line)
        
        if state != "OUTSIDE":
            self._emit_error("edit_file", f"Malformed payload for '{path}': Incomplete SEARCH/REPLACE chunk. Missing >>>>>>> REPLACE.")
            return

        if not chunks:
            self._emit_error(
                "edit_file",
                f"No SEARCH/REPLACE chunks found in payload for '{path}'. "
                "Ensure you use <<<<<<< SEARCH, =======, and >>>>>>> REPLACE markers."
            )
            return

        # Permission gate
        tool_cfg = self.runtime._tool_config("edit_file")
        if tool_cfg.get("require_permission", False):
            if not self._request_permission("edit_file", f"Edit '{path}' ({len(chunks)} chunks)"):
                self._emit_error(
                    "edit_file",
                    f"Permission denied for '{path}'. No file was changed.",
                )
                return

        try:
            abs_path = self._safe_path(path)
            if not Path(abs_path).is_file():
                raise FileNotFoundError(f"Cannot edit '{path}': file does not exist.")

            with open(abs_path, "r", encoding="utf-8") as f:
                current_content = f.read()

            edit_results: list[str] = []
            succeeded = 0
            failed = 0

            for idx, (search_str, replace_str) in enumerate(chunks, start=1):
                sr = _apply_str_replace(current_content, search_str, replace_str)
                if sr.ok:
                    current_content = sr.new_content
                    delta = sr.new_line_count - sr.old_line_count
                    delta_str = f"Δ{delta:+d}" if delta != 0 else "Δ0"
                    edit_results.append(
                        f"  ✓ edit {idx}: replaced {sr.old_line_count}-line region "
                        f"with {sr.new_line_count} lines ({delta_str})"
                    )
                    succeeded += 1
                else:
                    edit_results.append(f"  ✗ edit {idx}: {sr.error}")
                    failed += 1

            if succeeded > 0:
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(current_content)
                if hasattr(self.runtime, "_watcher"):
                    try:
                        self.runtime._watcher.register(abs_path)
                    except Exception:
                        pass

            total_lines = current_content.count("\n") + (
                1 if current_content and not current_content.endswith("\n") else 0
            )

            status = f"{succeeded}/{len(chunks)} edits applied"
            if failed:
                status += f" ({failed} failed)"
            detail = "\n".join(edit_results)
            result = (
                f"[edit_file] '{path}' edited: {status}.\n"
                f"{detail}\n"
                f"File now has {total_lines} lines."
            )
            self._emit_result("edit_file", result)

        except Exception as exc:
            self._emit_error(
                "edit_file",
                f"Failed while editing '{path}': {exc}",
            )
