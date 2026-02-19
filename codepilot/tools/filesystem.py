import os
from typing import Optional, TYPE_CHECKING

from ..core.block_parser import CodeBlock
from ..engine.hooks import EventType

if TYPE_CHECKING:
    from ..engine.runtime import Runtime


class FilesystemTools:

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
        work_dir = os.path.abspath(self.runtime.config.runtime.work_dir)
        abs_path = os.path.abspath(os.path.join(work_dir, path))
        if not self.runtime.config.runtime.unsafe_mode:
            if not abs_path.startswith(work_dir + os.sep) and abs_path != work_dir:
                raise PermissionError(
                    f"'{path}' is outside workspace '{work_dir}'. "
                    "Enable unsafe_mode in AgentFile to allow this."
                )
        return abs_path

    def write_file(
        self,
        path: str,
        start_line: int = None,
        end_line: int = None,
        after_line: int = None,
        mode: str = "w",
    ):
        """
        Write or edit a file. Content comes from the Payload Block immediately
        after your control block — never as a string argument. Omitting the
        Payload Block raises an error.

        mode='w'      — overwrite the entire file (default).
        mode='a'      — append content to the end of the file.
        mode='edit'   — replace lines start_line..end_line (1-indexed, inclusive).
                        Always call read_file first to confirm exact line numbers.
                        Requires both start_line and end_line.
        mode='insert' — insert content after after_line without removing anything.
                        Lines below shift down. Use to add a function, block, or
                        section between two existing lines.
                        Requires after_line. Use after_line=0 to insert at top.
                        Always call read_file first to confirm the target line.
        """
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="write_file",
            args={
                "path": path, "mode": mode,
                "start_line": start_line, "end_line": end_line,
                "after_line": after_line,
            },
        )

        tool_cfg = self.runtime._tool_config("write_file")
        if tool_cfg.get("require_permission", False):
            if mode == "edit":
                op = f"replace lines {start_line}–{end_line}"
            elif mode == "insert":
                op = f"insert after line {after_line}"
            else:
                op = "full file"
            if not self._request_permission("write_file", f"Write '{path}' ({op})"):
                result = f"[write_file] Permission denied for '{path}'."
                self.runtime._append_execution(result)
                self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="write_file", result=result)
                return

        payload: Optional[CodeBlock] = self.runtime.pop_next_payload_block()
        if payload is None:
            raise RuntimeError(
                f"write_file('{path}'): No Payload Block found. "
                "Provide a second fenced code block immediately after this one."
            )

        new_content = payload.content
        abs_path    = self._safe_path(path)
        parent      = os.path.dirname(abs_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        # ------------------------------------------------------------------ #
        #  mode='w'                                                            #
        # ------------------------------------------------------------------ #
        if mode == "w":
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            result = f"[write_file] '{path}' written ({len(new_content)} bytes)."

        # ------------------------------------------------------------------ #
        #  mode='a'                                                            #
        # ------------------------------------------------------------------ #
        elif mode == "a":
            with open(abs_path, "a", encoding="utf-8") as f:
                f.write(new_content)
            result = f"[write_file] '{path}' appended ({len(new_content)} bytes)."

        # ------------------------------------------------------------------ #
        #  mode='edit'  — replace start_line..end_line with new content       #
        # ------------------------------------------------------------------ #
        elif mode == "edit":
            if start_line is None or end_line is None:
                raise ValueError("mode='edit' requires both start_line and end_line.")
            if not os.path.isfile(abs_path):
                raise FileNotFoundError(f"Cannot edit '{path}': file does not exist.")

            with open(abs_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            start_idx = max(0, start_line - 1)           # inclusive, 0-based
            end_idx   = min(len(lines), end_line)         # exclusive slice end

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

        # ------------------------------------------------------------------ #
        #  mode='insert'  — insert content after after_line, nothing removed  #
        # ------------------------------------------------------------------ #
        elif mode == "insert":
            if after_line is None:
                raise ValueError("mode='insert' requires the after_line parameter.")
            if not os.path.isfile(abs_path):
                raise FileNotFoundError(f"Cannot insert into '{path}': file does not exist.")

            with open(abs_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            # after_line=0  → insert before the first line (prepend)
            # after_line=N  → insert after line N (1-indexed)
            insert_idx = min(len(lines), max(0, after_line))

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
                f"Unknown mode '{mode}'. Use 'w', 'a', 'edit', or 'insert'."
            )

        self.runtime._append_execution(result)
        self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="write_file", result=result)

    def read_file(self, path: str, start_line: int = 1, end_line: int = None) -> str:
        """
        Read a file and return its content with 1-indexed line numbers.
        Output format per line:  '    6 | self.timeout = 5'
        Always call this before write_file with mode='edit' or mode='insert'
        to confirm exact line numbers before touching anything.
        """
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="read_file",
            args={"path": path, "start_line": start_line, "end_line": end_line},
        )
        abs_path = self._safe_path(path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"read_file: '{path}' not found.")

        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        total = len(all_lines)
        s     = max(0, start_line - 1)
        e     = end_line if end_line is not None else total

        numbered = [f"{i + s + 1:5} | {line.rstrip()}" for i, line in enumerate(all_lines[s:e])]
        result   = (
            f"[read_file] '{path}' (lines {s + 1}–{min(e, total)} of {total})\n"
            + "\n".join(numbered)
        )

        self.runtime._append_execution(result)
        self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="read_file", result=result)
        return "\n".join(numbered)
