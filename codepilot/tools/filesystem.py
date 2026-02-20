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
        Write or edit a file. Content comes from the next Payload Block —
        never pass content as a string argument. Each write_file() call
        consumes one Payload Block in order.

        mode='w'      — overwrite the entire file (default).
        mode='a'      — append content to the end of the file.
        mode='edit'   — replace lines start_line..end_line (1-indexed, inclusive).
                        Always read_file first. One edit per step only.
        mode='insert' — insert after after_line without removing anything.
                        Use after_line=0 for top of file. One insert per step only.

        Up to 5 file writes (mode='w'/'a') are allowed per step.
        Edits and inserts: one per step to prevent line-number drift.
        """
        self.runtime.hooks.emit(
            EventType.TOOL_CALL, tool="write_file",
            args={
                "path": path, "mode": mode,
                "start_line": start_line, "end_line": end_line,
                "after_line": after_line,
            },
        )

        # ------------------------------------------------------------------ #
        #  ALWAYS consume the payload block first to maintain ordering for    #
        #  multi-file writes. If we return early (guard, error), the payload  #
        #  for THIS call is still consumed so the next write_file() gets the  #
        #  correct payload.                                                    #
        # ------------------------------------------------------------------ #
        payload: Optional[CodeBlock] = self.runtime.pop_next_payload_block()
        if payload is None:
            result = (
                f"[write_file] ERROR: No Payload Block found for '{path}'. "
                "Provide a fenced code block for each write_file() call."
            )
            self.runtime._append_execution(result)
            self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="write_file", result=result)
            return

        # ------------------------------------------------------------------ #
        #  Step-level guards                                                   #
        # ------------------------------------------------------------------ #
        if mode in ("edit", "insert"):
            if self.runtime._step_edit_count > 0:
                result = (
                    f"[write_file] ERROR: Only one edit/insert per step is allowed. "
                    f"Skipped '{path}'. Use a separate step for additional edits."
                )
                self.runtime._append_execution(result)
                self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="write_file", result=result)
                return
            self.runtime._step_edit_count += 1
        else:
            if self.runtime._step_write_count >= 5:
                result = (
                    f"[write_file] ERROR: Maximum 5 file writes per step exceeded. "
                    f"Skipped '{path}'."
                )
                self.runtime._append_execution(result)
                self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="write_file", result=result)
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
                result = f"[write_file] Permission denied for '{path}'."
                self.runtime._append_execution(result)
                self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="write_file", result=result)
                return

        # ------------------------------------------------------------------ #
        #  Execute the write — wrapped in try/except for error isolation.     #
        #  If this write fails, subsequent write_file() calls in the same     #
        #  step still execute. The LLM sees exactly which writes succeeded    #
        #  and which failed.                                                   #
        # ------------------------------------------------------------------ #
        try:
            new_content = payload.content
            abs_path    = self._safe_path(path)
            parent      = os.path.dirname(abs_path)
            if parent:
                os.makedirs(parent, exist_ok=True)

            # -------------------------------------------------------------- #
            #  mode='w'                                                        #
            # -------------------------------------------------------------- #
            if mode == "w":
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                result = f"[write_file] '{path}' written ({len(new_content)} bytes)."

            # -------------------------------------------------------------- #
            #  mode='a'                                                        #
            # -------------------------------------------------------------- #
            elif mode == "a":
                with open(abs_path, "a", encoding="utf-8") as f:
                    f.write(new_content)
                result = f"[write_file] '{path}' appended ({len(new_content)} bytes)."

            # -------------------------------------------------------------- #
            #  mode='edit'                                                     #
            # -------------------------------------------------------------- #
            elif mode == "edit":
                if start_line is None or end_line is None:
                    raise ValueError("mode='edit' requires both start_line and end_line.")
                if not os.path.isfile(abs_path):
                    raise FileNotFoundError(f"Cannot edit '{path}': file does not exist.")

                with open(abs_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()

                start_idx = max(0, start_line - 1)
                end_idx   = min(len(lines), end_line)

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
                if not os.path.isfile(abs_path):
                    raise FileNotFoundError(f"Cannot insert into '{path}': file does not exist.")

                with open(abs_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()

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

        except Exception as exc:
            result = f"[write_file] ERROR writing '{path}': {exc}"

        self.runtime._append_execution(result)
        self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="write_file", result=result)

    def read_file(self, path: str, start_line: int = 1, end_line: int = None) -> str:
        """
        Read a file and return its content with 1-indexed line numbers.
        Output format per line:  '    6 | self.timeout = 5'
        Always call this before write_file with mode='edit' or mode='insert'
        to confirm exact line numbers before touching anything.
        Multiple read_file() calls per step are allowed.
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
