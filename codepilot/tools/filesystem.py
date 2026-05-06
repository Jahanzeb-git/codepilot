"""
File: filesystem.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description:
Filesystem read/write tools for the CodePilot agentic runtime.

Architectural Notes:
Provides write_file and read_file tools to the agent's sandbox. write_file
uses a Payload Block side-loading mechanism — file content is never passed
as a string argument but instead consumed from the next queued payload block
in the runtime, solving the LLM string-escaping problem entirely.
Supports create, append, edit, and multi_edit modes with optional permission
gating via the HookSystem.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

import os
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from ..core.block_parser import CodeBlock
from ..engine.hooks import EventType

if TYPE_CHECKING:
    from ..engine.runtime import Runtime


class FilesystemTools:
    _VALID_WRITE_MODES = frozenset({"w", "a", "edit", "insert", "multi_edit"})

    def __init__(self, runtime: "Runtime"):
        self.runtime = runtime

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

    def _write_error(self, message: str) -> None:
        result = f"[write_file] ERROR: {message}"
        self.runtime._append_execution(result)
        self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="write_file", result=result)

    def _read_error(self, message: str) -> str:
        result = f"[read_file] ERROR: {message}"
        self.runtime._append_execution(result)
        self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="read_file", result=result)
        return result

    @classmethod
    def _valid_modes_text(cls) -> str:
        return "'w', 'a', 'edit', 'insert', or 'multi_edit'"

    @staticmethod
    def _is_int(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def _validate_edits_shape(self, edits) -> Optional[str]:
        if not isinstance(edits, list) or not edits:
            return "mode='multi_edit' requires a non-empty edits list like edits=[(10, 12), (25, 25)]."
        for idx, item in enumerate(edits, start=1):
            if (
                not isinstance(item, (tuple, list))
                or len(item) != 2
                or not self._is_int(item[0])
                or not self._is_int(item[1])
            ):
                return (
                    f"edits item {idx} is invalid: {item!r}. Each multi_edit item "
                    "must be a two-integer tuple/list: (start_line, end_line)."
                )
        return None

    def _validate_replace_range(self, path: str, start_line: int, end_line: int, total_lines: int, mode: str) -> Optional[str]:
        if not self._is_int(start_line) or not self._is_int(end_line):
            return (
                f"mode='{mode}' line ranges must be integers. Got "
                f"start_line={start_line!r}, end_line={end_line!r}."
            )
        if start_line < 1 or end_line < 1:
            return (
                f"mode='{mode}' uses 1-indexed inclusive line ranges. Got "
                f"L{start_line}-{end_line}; line numbers must be >= 1."
            )
        if start_line > end_line:
            return (
                f"mode='{mode}' got an invalid range L{start_line}-{end_line}: "
                "start_line must be <= end_line."
            )
        if end_line > total_lines:
            return (
                f"mode='{mode}' range L{start_line}-{end_line} is outside '{path}', "
                f"which has {total_lines} lines. Call read_file('{path}') to get "
                "current line numbers, then retry with an in-range edit."
            )
        return None

    def write_file(
        self,
        path: str,
        start_line: int = None,
        end_line: int = None,
        after_line: int = None,
        mode: str = "w",
        edits: list = None,
    ):
        """
        Write or edit a file. Content comes from the next Payload Block —
        never pass content as a string argument. Each write_file() call
        consumes one Payload Block in order.

        mode='w'          — overwrite the entire file (default). Example: write_file("file.txt", mode="w")
        mode='a'          — append content to the end of the file. Example: write_file("file.txt", mode="a")
        mode='edit'       — replace lines start_line..end_line (1-indexed, inclusive). Example: write_file("file.txt", mode="edit", start_line=1, end_line=5)
        mode='insert'     — insert after after_line without removing anything. Example: write_file("file.txt", mode="insert", after_line=5)
        mode='multi_edit' — pass edits=[(start, end), (start, end)]. Safe for many edits. Each tuple have (start_line, end_line) in a list. Provide one payload block for each. Example: write_file("file.txt", mode="multi_edit", edits=[(1, 5), (10, 15)])

        Up to 5 file writes (mode='w'/'a') are allowed per step (you can call write_file multiple times in a single agentic step if each call is for a different file).
        Edits: one per file per agentic step to prevent line-number drift.
        """
        ui_status = ""
        if mode == "w":
            ui_status = f"Creating a file {path}..."
        elif mode == "a":
            ui_status = f"Appending to a file {path}..."
        elif mode == "edit":
            ui_status = f"Editing a file {path}: L{start_line}-{end_line}"
        elif mode == "insert":
            ui_status = f"Inserting into a file {path}: L{after_line}"
        elif mode == "multi_edit":
            if isinstance(edits, list) and edits and self._validate_edits_shape(edits) is None:
                min_line = min(s for s, e in edits)
                max_line = max(e for s, e in edits)
                ui_status = f"Refactoring a file {path}: L{min_line}-{max_line}"
            else:
                ui_status = f"Refactoring a file {path}..."

        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="write_file",
            args={
                "path": path, "mode": mode,
                "start_line": start_line, "end_line": end_line,
                "after_line": after_line, "edits": edits,
                "ui_status": ui_status,
            },
        )

        # ------------------------------------------------------------------ #
        #  Argument validation before consuming Payload Blocks                 #
        # ------------------------------------------------------------------ #
        if mode not in self._VALID_WRITE_MODES:
            self._write_error(
                f"Unknown mode {mode!r} for '{path}'. Valid modes are "
                f"{self._valid_modes_text()}. Use mode='a' for append and "
                "mode='multi_edit' for multiple non-contiguous edits. No file was changed."
            )
            return

        if mode == "multi_edit":
            edits_error = self._validate_edits_shape(edits)
            if edits_error:
                self._write_error(f"{edits_error} No file was changed.")
                return

        # ------------------------------------------------------------------ #
        #  ALWAYS consume the payload block(s) first to maintain ordering     #
        # ------------------------------------------------------------------ #
        payloads: list[CodeBlock] = []
        if mode == "multi_edit":
            for _ in edits:
                p = self.runtime.pop_next_payload_block()
                if p is None:
                    self._write_error(
                        f"Not enough Payload Blocks for {len(edits)} multi_edit edits "
                        f"on '{path}'. Provide one Payload Block per edits tuple, in the "
                        "same order, each annotated filename=<same path>. No file was changed."
                    )
                    return
                payloads.append(p)
        else:
            p = self.runtime.pop_next_payload_block()
            if p is None:
                self._write_error(
                    f"No Payload Block found for '{path}'. Provide exactly one fenced "
                    "Payload Block immediately after the ```codepilot block, annotated "
                    f"as filename={path}. Do not pass file content as a write_file() "
                    "argument. No file was changed."
                )
                return
            payloads.append(p)

        # ------------------------------------------------------------------ #
        #  Step-level guards                                                   #
        # ------------------------------------------------------------------ #
        if mode in ("edit", "insert", "multi_edit"):
            edited_files = self.runtime._step_edited_files
            if path in edited_files:
                self._write_error(
                    f"Only one edit/insert/multi_edit per file per step is allowed. "
                    f"'{path}' was already edited this step. Use a separate agentic "
                    "step or combine all non-contiguous edits for this file into one "
                    "mode='multi_edit' call. This write was skipped."
                )
                return
            edited_files.add(path)

        else:
            if self.runtime._step_write_count >= 5:
                self._write_error(
                    f"Maximum 5 mode='w'/'a' file writes per step exceeded. "
                    f"Skipped '{path}'. Continue in the next agentic step for "
                    "additional file writes."
                )
                return
            self.runtime._step_write_count += 1

        # ------------------------------------------------------------------ #
        #  Permission gate                                                     #
        # ------------------------------------------------------------------ #
        tool_cfg = self.runtime._tool_config("write_file")
        if tool_cfg.get("require_permission", False):
            if mode == "edit":
                op = f"replace lines {start_line}–{end_line}"
            elif mode == "insert":
                op = f"insert after line {after_line}"
            else:
                op = "full file"
            if not self._request_permission("write_file", f"Write '{path}' ({op})"):
                self._write_error(
                    f"Permission denied by user/runtime policy for '{path}'. No file "
                    "was changed. Ask the user for permission or choose a different "
                    "approach; do not assume the write succeeded."
                )
                return

        # ------------------------------------------------------------------ #
        #  Execute the write — wrapped in try/except for error isolation.     #
        #  If this write fails, subsequent write_file() calls in the same     #
        #  step still execute. The LLM sees exactly which writes succeeded    #
        #  and which failed.                                                   #
        # ------------------------------------------------------------------ #
        try:
            new_content = payloads[0].content if payloads else ""
            abs_path    = self._safe_path(path)

            # -------------------------------------------------------------- #
            #  mode='multi_edit'                                               #
            # -------------------------------------------------------------- #
            if mode == "multi_edit":
                if not Path(abs_path).is_file():
                    raise FileNotFoundError(f"Cannot edit '{path}': file does not exist.")

                with open(abs_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()

                total_lines = len(lines)
                for idx, (s_line, e_line) in enumerate(edits, start=1):
                    range_error = self._validate_replace_range(path, s_line, e_line, total_lines, "multi_edit")
                    if range_error:
                        raise ValueError(f"edits item {idx}: {range_error}")

                # Pair edits with payloads, sort by start_line DESCENDING 
                # (bottom-to-top) so earlier line numbers don't shift!
                operations = list(zip(edits, payloads))
                operations.sort(key=lambda op: op[0][0], reverse=True)
                
                applied = 0
                for (s_line, e_line), block in operations:
                    s_idx = max(0, s_line - 1)
                    e_idx = min(len(lines), e_line)
                    content = block.content
                    if content and not content.endswith("\n"):
                        content += "\n"
                    lines = lines[:s_idx] + [content] + lines[e_idx:]
                    applied += 1

                with open(abs_path, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                
                # Report each block in original top-to-bottom order so the
                # LLM can cross-reference against its own codepilot block.
                block_details = []
                for (s_line, e_line), block in sorted(
                    zip(edits, payloads), key=lambda op: op[0][0]
                ):
                    new_lines = block.content.count("\n") + (
                        1 if block.content and not block.content.endswith("\n") else 0
                    )
                    block_details.append(f"L{s_line}–{e_line} → {new_lines} lines")

                with open(abs_path, "r", encoding="utf-8") as f:
                    total_lines = sum(1 for _ in f)

                detail_str = ", ".join(f"[{d}]" for d in block_details)
                result = (
                    f"[write_file] '{path}' multi-edited: "
                    f"applied {applied} block replacements "
                    f"({detail_str}). "
                    f"File now has {total_lines} lines total."
                )

            # -------------------------------------------------------------- #
            #  mode='w'                                                        #
            # -------------------------------------------------------------- #
            elif mode == "w":
                Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                line_count = new_content.count("\n") + (
                    1 if new_content and not new_content.endswith("\n") else 0
                )
                result = (
                    f"[write_file] '{path}' written "
                    f"({len(new_content)} bytes, {line_count} lines)."
                )

            # -------------------------------------------------------------- #
            #  mode='a'                                                        #
            # -------------------------------------------------------------- #
            elif mode == "a":
                Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
                if new_content:
                    if Path(abs_path).is_file() and Path(abs_path).stat().st_size > 0:
                        with open(abs_path, "rb") as f:
                            f.seek(-1, os.SEEK_END)
                            if f.read(1) != b"\n":
                                new_content = "\n" + new_content

                with open(abs_path, "a", encoding="utf-8") as f:
                    f.write(new_content)
                appended_lines = new_content.count("\n") + (
                    1 if new_content and not new_content.endswith("\n") else 0
                )
                with open(abs_path, "r", encoding="utf-8") as f:
                    total_lines = sum(1 for _ in f)
                result = (
                    f"[write_file] '{path}' appended "
                    f"({len(new_content)} bytes, {appended_lines} lines). "
                    f"File now has {total_lines} lines total."
                )

            # -------------------------------------------------------------- #
            #  mode='edit'                                                     #
            # -------------------------------------------------------------- #
            elif mode == "edit":
                if start_line is None or end_line is None:
                    raise ValueError("mode='edit' requires both start_line and end_line.")
                if not Path(abs_path).is_file():
                    raise FileNotFoundError(f"Cannot edit '{path}': file does not exist.")

                with open(abs_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()

                range_error = self._validate_replace_range(path, start_line, end_line, len(lines), "edit")
                if range_error:
                    raise ValueError(range_error)

                start_idx = start_line - 1
                end_idx   = end_line

                if new_content and not new_content.endswith("\n"):
                    new_content += "\n"

                with open(abs_path, "w", encoding="utf-8") as f:
                    f.writelines(lines[:start_idx])
                    f.write(new_content)
                    f.writelines(lines[end_idx:])

                new_line_count = new_content.count("\n")
                old_line_count = end_idx - start_idx
                result = (
                    f"[write_file] '{path}' edited: "
                    f"replaced lines {start_line}–{end_line} "
                    f"({old_line_count} → {new_line_count} lines). "
                    f"File now has {len(lines) - old_line_count + new_line_count} lines total."
                )

            # -------------------------------------------------------------- #
            #  mode='insert'                                                   #
            # -------------------------------------------------------------- #
            elif mode == "insert":
                if after_line is None:
                    raise ValueError("mode='insert' requires the after_line parameter.")
                if not self._is_int(after_line):
                    raise ValueError(f"mode='insert' requires after_line to be an integer. Got {after_line!r}.")
                if not Path(abs_path).is_file():
                    raise FileNotFoundError(f"Cannot insert into '{path}': file does not exist.")

                with open(abs_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()

                if after_line < 0 or after_line > len(lines):
                    raise ValueError(
                        f"mode='insert' after_line={after_line} is outside '{path}', "
                        f"which has {len(lines)} lines. Use after_line=0 for top-of-file "
                        "insertion, or call read_file() to choose an in-range line."
                    )

                insert_idx = after_line

                if new_content and not new_content.endswith("\n"):
                    new_content += "\n"

                with open(abs_path, "w", encoding="utf-8") as f:
                    f.writelines(lines[:insert_idx])
                    f.write(new_content)
                    f.writelines(lines[insert_idx:])

                inserted_lines = new_content.count("\n")
                new_total      = len(lines) + inserted_lines
                result = (
                    f"[write_file] '{path}' inserted {inserted_lines} line(s) after line {after_line}. "
                    f"File now has {new_total} lines total. "
                    f"Former line {after_line + 1} is now line {after_line + inserted_lines + 1}."
                )

            else:
                raise ValueError(
                    f"Unknown mode '{mode}'. Use {self._valid_modes_text()}."
                )

        except Exception as exc:
            result = (
                f"[write_file] ERROR writing '{path}': {exc} No file was changed "
                "unless the error occurred during a low-level OS write. Re-read the "
                "target file before retrying if there is any chance of partial change."
            )

        self.runtime._append_execution(result)
        self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="write_file", result=result)

        # Register with watcher so external changes are detected next step
        if hasattr(self.runtime, '_watcher') and 'ERROR' not in result:
            try:
                self.runtime._watcher.register(self._safe_path(path))
            except Exception:
                pass

    def read_file(self, path: str, start_line: int = 1, end_line: int = None) -> str:
        """
        Read a file and return its content with 1-indexed line numbers.
        Output format per line:  '    6 | self.timeout = 5'
        Always call this before write_file with mode='edit' or mode='insert'
        to confirm exact line numbers before touching anything.
        Multiple read_file() calls per agentic step are allowed.
        """
        ui_status = f"Reading a file {path}: L{start_line}-{end_line}" if end_line else f"Reading a file {path}..."
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="read_file",
            args={"path": path, "start_line": start_line, "end_line": end_line, "ui_status": ui_status},
        )
        try:
            if not self._is_int(start_line):
                return self._read_error(
                    f"start_line must be an integer >= 1. Got {start_line!r}. "
                    "Use read_file(path) for the full file, or pass explicit integer line numbers."
                )
            if start_line < 1:
                return self._read_error(
                    f"read_file() uses 1-indexed lines. Got start_line={start_line}. "
                    "Line numbers must be >= 1."
                )
            if end_line is not None:
                if not self._is_int(end_line):
                    return self._read_error(
                        f"end_line must be an integer >= start_line, or None. Got {end_line!r}."
                    )
                if end_line < start_line:
                    return self._read_error(
                        f"Invalid range for '{path}': start_line={start_line}, end_line={end_line}. "
                        "end_line must be >= start_line."
                    )

            abs_path = self._safe_path(path)
            if not Path(abs_path).is_file():
                return self._read_error(
                    f"'{path}' not found in the workspace. Confirm the path from the codebase snapshot "
                    "or use a search/read step on the correct file path."
                )

            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()

            total = len(all_lines)
            if total == 0:
                result = f"[read_file] '{path}' (lines 1-0 of 0)\n[END OF FILE]"
                self.runtime._append_execution(result)
                self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="read_file", result=result)
                if hasattr(self.runtime, '_watcher'):
                    self.runtime._watcher.register(abs_path)
                return "[END OF FILE]"

            if start_line > total:
                return self._read_error(
                    f"Requested start_line={start_line} for '{path}', but the file has only {total} lines. "
                    f"Use a start_line between 1 and {total}."
                )

            e = end_line if end_line is not None else total
            if e > total:
                e = total

            s = start_line - 1
            numbered = [f"{i + s + 1:5} | {line.rstrip()}" for i, line in enumerate(all_lines[s:e])]
            content  = "\n".join(numbered)

            footer = ""
            if e >= total:
                footer = "\n[END OF FILE]"
            else:
                remaining = total - e
                footer = f"\n[TRUNCATED: {remaining} lines remaining]"

            result = (
                f"[read_file] '{path}' (lines {s + 1}–{e} of {total})\n"
                + content
                + footer
            )

            self.runtime._append_execution(result)
            self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="read_file", result=result)

            if hasattr(self.runtime, '_watcher'):
                self.runtime._watcher.register(abs_path)

            return content + footer
        except Exception as exc:
            return self._read_error(
                f"Unexpected failure while reading '{path}': {exc}. "
                "Treat this as a tool failure from read_file(); do not assume any file content was returned."
            )
