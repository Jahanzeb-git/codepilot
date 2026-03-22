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
            if edits:
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
        #  ALWAYS consume the payload block(s) first to maintain ordering     #
        # ------------------------------------------------------------------ #
        payloads: list[CodeBlock] = []
        if mode == "multi_edit":
            if not edits:
                result = "[write_file] ERROR: mode='multi_edit' requires the 'edits' parameter."
                self.runtime._append_execution(result)
                self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="write_file", result=result)
                return
            for _ in edits:
                p = self.runtime.pop_next_payload_block()
                if p is None:
                    result = f"[write_file] ERROR: Not enough Payload Blocks for {len(edits)} edits."
                    self.runtime._append_execution(result)
                    self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="write_file", result=result)
                    return
                payloads.append(p)
        else:
            p = self.runtime.pop_next_payload_block()
            if p is None:
                result = (
                    f"[write_file] ERROR: No Payload Block found for '{path}'. "
                    "Provide a fenced code block for each write_file() call."
                )
                self.runtime._append_execution(result)
                self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="write_file", result=result)
                return
            payloads.append(p)

        # ------------------------------------------------------------------ #
        #  Step-level guards                                                   #
        # ------------------------------------------------------------------ #
        if mode in ("edit", "insert", "multi_edit"):
            edited_files = self.runtime._step_edited_files
            if path in edited_files:
                result = (
                    f"[write_file] ERROR: Only one edit/insert per FILE per step. "
                    f"'{path}' was already edited this step. Use a separate step."
                )
                self.runtime._append_execution(result)
                self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="write_file", result=result)
                return
            edited_files.add(path)

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
            new_content = payloads[0].content if payloads else ""
            abs_path    = self._safe_path(path)
            parent      = os.path.dirname(abs_path)
            if parent:
                os.makedirs(parent, exist_ok=True)

            # -------------------------------------------------------------- #
            #  mode='multi_edit'                                               #
            # -------------------------------------------------------------- #
            if mode == "multi_edit":
                if not os.path.isfile(abs_path):
                    raise FileNotFoundError(f"Cannot edit '{path}': file does not exist.")

                with open(abs_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()

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

        # Register with watcher so external changes are detected next step
        if hasattr(self.runtime, '_watcher'):
            self.runtime._watcher.register(abs_path)

        return "\n".join(numbered)
