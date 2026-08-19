"""
File: block_parser.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description:
LLM response parser for the CodePilot Code-as-Interface paradigm.

Architectural Notes:
Parses fenced Markdown blocks from LLM responses and classifies them into
three categories: the ```codepilot control block (executed Python), ```python
filename=... payload blocks (side-loaded by write_file), and the ```completion
block (signals task completion to the runtime loop). Cross-validates payload
block filename= annotations against write_file() calls in the control block
using a paren-balanced scanner that handles nested structure correctly.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

import re
import ast
from typing import List, Optional, Tuple
from dataclasses import dataclass, field

class ProtocolViolationError(ValueError):
    def __init__(self, message: str, control_block: Optional['CodeBlock'] = None):
        super().__init__(message)
        self.control_block = control_block


@dataclass
class CodeBlock:
    language: str            # e.g. "python", "codepilot", "completion", "text"
    content: str             # raw block content, trailing newline stripped
    index: int               # 0-based position in the response
    filename: Optional[str] = field(default=None)  # parsed from filename= annotation
    start_pos: int = field(default=0)   # character position of opening fence in source
    end_pos: int = field(default=0)     # character position after closing fence in source


class BlockParser:
    """
    Parses an LLM response and extracts fenced Markdown code blocks.

    Runtime convention:
      - ```codepilot                  → Control Block (executed Python)
      - ```python filename=a.py       → Payload Block (side-loaded by write_file)
      - ```completion                 → Completion Block (streamed to user, loop terminates)
      - All other blocks              → Display-only markdown, never executed

    Payload blocks must carry a filename= annotation that matches the corresponding
    write_file() call in the control block. split() validates this and raises a
    descriptive ValueError on any mismatch — count, missing annotation, or wrong file.
    """

    # Captures the full fence tag line (e.g. "python filename=routes/profile.py")
    # and the block content. Group 1 = opening backticks, Group 2 = tag, Group 3 = content, Group 4 = closing backticks.
    _FENCE_RE = re.compile(r"^(```+)([^\n]*)\n(.*?)^(\1)\s*$", re.DOTALL | re.MULTILINE)

    # Extracts filename= from a fence tag. Supports quoted and bare values:
    #   filename=routes/profile.py   filename="routes/profile.py"
    _FILENAME_RE = re.compile(r'filename=(?:"([^"]+)"|\'([^\']+)\'|(\S+))')
    _INLINE_CONTENT_ARG_RE = re.compile(
        r'(?:write_file|edit_file|view_file)\s*\([^)]*\b(content|payload|text|data)\s*=',
        re.DOTALL,
    )


    @classmethod
    def _parse_fence_tag(cls, tag: str) -> Tuple[str, Optional[str]]:
        """
        Parse a fence tag line into (language, filename).

        Examples:
          'python filename=routes/profile.py' → ('python', 'routes/profile.py')
          'python filename="utils/validators.py"' → ('python', 'utils/validators.py')
          'codepilot'                         → ('codepilot', None)
          ''                                  → ('text', None)
        """
        tag = tag.strip()

        # Language is always the leading word token
        lang_match = re.match(r'(\w+)', tag)
        language = lang_match.group(1).lower() if lang_match else "text"

        # Extract filename= if present — one of three capture groups will match
        filename: Optional[str] = None
        fn_match = cls._FILENAME_RE.search(tag)
        if fn_match:
            filename = fn_match.group(1) or fn_match.group(2) or fn_match.group(3)

        return language, filename

    @classmethod
    def parse(cls, text: str) -> List[CodeBlock]:
        blocks: List[CodeBlock] = []
        for idx, match in enumerate(cls._FENCE_RE.finditer(text)):
            tag = match.group(2)
            content = match.group(3)
            # Strip the single trailing newline the fence introduces, but
            # preserve any intentional blank lines inside the content.
            content = content.rstrip("\n")
            language, filename = cls._parse_fence_tag(tag)
            blocks.append(CodeBlock(
                language=language, content=content, index=idx, filename=filename,
                start_pos=match.start(), end_pos=match.end(),
            ))
        return blocks


    @classmethod
    @classmethod
    def split(cls, text: str) -> Tuple[Optional[CodeBlock], List[CodeBlock], Optional[str]]:
        """
        Returns (control_block, payload_blocks, protocol_warning).
        """
        import re as _re
        if "<thinking>" in text:
            text = _re.sub(r"<thinking>.*?</thinking>\n?", "", text, flags=_re.DOTALL)

        text = _re.sub(
            r"<(a|b|c|d|e|example|tool_docs)>\s*```codepilot.*?.*?</\1>\n?",
            "", text, flags=_re.DOTALL
        )

        # Force a newline before any fenced block that is glued to previous text
        # (e.g. "Fixing bug.```codepilot" -> "Fixing bug.\n```codepilot")
        text = _re.sub(r"([^\n])(```[a-zA-Z0-9_-]*)", r"\1\n\2", text)

        blocks = cls.parse(text)
        if not blocks:
            return None, [], None

        control_block = next((b for b in blocks if b.language == "codepilot"), None)
        if not control_block:
            return None, [], None

        # Determine the text bounds for payload scanning
        # Stop at the second codepilot block, if any
        control_idx = blocks.index(control_block)
        second_control = next((b for b in blocks[control_idx+1:] if b.language == "codepilot"), None)
        
        start_pos = control_block.end_pos
        end_pos = second_control.start_pos if second_control else len(text)
        
        remaining_text = text[start_pos:end_pos]
        
        # Regex to find filename headers, bypassing standard _FENCE_RE which breaks on nested backticks
        header_re = _re.compile(r"^[ \t]*```([a-zA-Z0-9_-]*)[ \t]+(?:filename=([^\n]+))", _re.MULTILINE)
        headers = list(header_re.finditer(remaining_text))
        
        payload_blocks: List[CodeBlock] = []
        content_re = _re.compile(r"<{5,9}\s*CONTENT\s*\n(.*?)\n>{5,9}\s*CONTENT", _re.DOTALL)
        search_re = _re.compile(r"<{5,9}\s*SEARCH\s*\n")
        replace_re = _re.compile(r"\n>{5,9}\s*REPLACE")
        
        for i, h_match in enumerate(headers):
            language = h_match.group(1) or "text"
            filename_raw = h_match.group(2).strip()
            if filename_raw.startswith('"') and filename_raw.endswith('"'):
                filename = filename_raw[1:-1]
            elif filename_raw.startswith("'") and filename_raw.endswith("'"):
                filename = filename_raw[1:-1]
            else:
                filename = filename_raw
                
            block_start = start_pos + h_match.start()
            limit = end_pos
            if i + 1 < len(headers):
                limit = start_pos + headers[i+1].start()
                
            search_chunk = text[block_start:limit]
            
            c_match = content_re.search(search_chunk)
            if c_match:
                content = c_match.group(1)
            else:
                s_match = search_re.search(search_chunk)
                r_matches = list(replace_re.finditer(search_chunk))
                if s_match and r_matches:
                    r_match = r_matches[-1]
                    content = search_chunk[s_match.start():r_match.end()].strip()
                else:
                    # Fallback to _FENCE_RE
                    fb_match = cls._FENCE_RE.search(search_chunk)
                    if fb_match:
                        content = fb_match.group(3).rstrip('\n')
                    else:
                        content = ""
                        
            payload_blocks.append(CodeBlock(
                language=language,
                content=content,
                index=i+1, # arbitrary index
                filename=filename,
                start_pos=block_start,
                end_pos=limit
            ))

        # We must also detect unannotated blocks to maintain original error messaging
        # Unannotated blocks are fences in remaining_text that lack filename=
        # But we only care if we have fewer payloads than expected.
        unannotated_blocks = []
        # Fallback _FENCE_RE on remaining_text to find blocks without filename
        for b in cls.parse(remaining_text):
            if b.filename is None and b.language != "codepilot":
                unannotated_blocks.append(b)

        valid_payloads, warning = cls._validate_payload_filenames(
            control_block, payload_blocks, unannotated_blocks
        )
        return control_block, valid_payloads, warning


    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @classmethod
    def _extract_write_file_calls(cls, control_content: str) -> Tuple[List[Tuple[str, int]], bool, bool]:
        """
        Parse view_file, write_file, edit_file calls from the control block using AST.
        Returns (write_calls, has_dynamic_path, has_syntax_error).
        write_calls is a list of (filepath, payload_count) in call order.

        If from_cache=True is passed to write_file/edit_file, payload_count is set
        to 0 for that call — the content will be loaded from the runtime cache
        instead of a payload block.
        """
        class ToolCallVisitor(ast.NodeVisitor):
            def __init__(self):
                self.calls = []
                self.has_dynamic = False

            def visit_Call(self, node):
                self.generic_visit(node)
                if isinstance(node.func, ast.Name) and node.func.id in ("view_file", "write_file", "edit_file"):
                    filepath = "<unknown>"
                    if node.args:
                        arg0 = node.args[0]
                        if isinstance(arg0, ast.Constant):
                            filepath = str(arg0.value)
                        elif getattr(ast, 'Str', type(None)) is not type(None) and isinstance(arg0, getattr(ast, 'Str', type(None))):
                            filepath = arg0.s
                        else:
                            self.has_dynamic = True
                    else:
                        self.has_dynamic = True

                    if filepath != "<unknown>":
                        # Check for from_cache_id=<int> keyword — if present, no payload block needed
                        has_cache_id = False
                        for kw in node.keywords:
                            if kw.arg == "from_cache_id" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int):
                                has_cache_id = True
                                break
                        if node.func.id in ("write_file", "edit_file"):
                            payload_count = 0 if has_cache_id else 1
                        else:
                            payload_count = 0
                        self.calls.append((node.lineno, node.col_offset, filepath, payload_count))

        try:
            tree = ast.parse(control_content)
        except SyntaxError:
            return [], False, True

        visitor = ToolCallVisitor()
        visitor.visit(tree)
        visitor.calls.sort()
        results = [(fp, cnt) for _, _, fp, cnt in visitor.calls]
        return results, visitor.has_dynamic, False


    @classmethod
    @classmethod
    def salvage_payloads_for_cache(
        cls, text: str
    ) -> List[Tuple[str, str]]:
        blocks = cls.parse(text)
        if not blocks:
            return []

        control = next((b for b in blocks if b.language == "codepilot"), None)
        if control is None:
            return []

        write_calls, _, has_syntax_error = cls._extract_write_file_calls(control.content)
        if has_syntax_error:
            return []

        targets = [fp for fp, cnt in write_calls if cnt == 1]
        if not targets:
            return []

        # Find second codepilot
        control_idx = blocks.index(control)
        second_control = next((b for b in blocks[control_idx+1:] if b.language == "codepilot"), None)
        
        start_pos = control.end_pos
        end_pos = second_control.start_pos if second_control else len(text)
        
        remaining_text = text[start_pos:end_pos]
        
        # In salvage, we accept ALL blocks in remaining_text, both annotated and unannotated,
        # but we use the explicit marker extraction for the annotated ones if possible.
        # Actually, since we're salvaging, let's just use cls.parse on remaining_text
        # and then apply the marker extraction to each chunk between blocks.
        import re as _re
        candidate_blocks = cls.parse(remaining_text)
        
        content_re = _re.compile(r"<{5,9}\s*CONTENT\s*\n(.*?)\n>{5,9}\s*CONTENT", _re.DOTALL)
        search_re = _re.compile(r"<{5,9}\s*SEARCH\s*\n")
        replace_re = _re.compile(r"\n>{5,9}\s*REPLACE")
        
        for j, c_block in enumerate(candidate_blocks):
            limit = len(remaining_text)
            if j + 1 < len(candidate_blocks):
                limit = candidate_blocks[j + 1].start_pos
                
            search_chunk = remaining_text[c_block.start_pos:limit]
            
            c_match = content_re.search(search_chunk)
            if c_match:
                c_block.content = c_match.group(1)
            else:
                s_match = search_re.search(search_chunk)
                r_matches = list(replace_re.finditer(search_chunk))
                if s_match and r_matches:
                    r_match = r_matches[-1]
                    c_block.content = search_chunk[s_match.start():r_match.end()].strip()

        pairs: List[Tuple[str, str]] = []
        for target, block in zip(targets, candidate_blocks):
            pairs.append((target, block.content))

        return pairs


    @classmethod
    def _validate_payload_filenames(
        cls,
        control_block: CodeBlock,
        payload_blocks: List[CodeBlock],
        unannotated_blocks: List[CodeBlock],
    ) -> Tuple[List[CodeBlock], Optional[str]]:
        """
        Cross-checks payload block filename= annotations against write_file() /
        edit_file() calls parsed from the control block.

        Returns (valid_payload_blocks, warning).

          valid_payload_blocks — the trimmed list of payloads the runtime should
                                 actually consume. Surplus blocks (generated beside
                                 tools that don't need a payload) are excluded.
          warning              — a human/LLM-readable protocol deviation message when
                                 surplus payload blocks were detected, or None when
                                 the response is fully conformant.

        Raises ValueError for genuine, unrecoverable protocol violations:
          1. Inline content= / payload= argument passed to write_file/edit_file.
          2. Dynamic (non-literal) tool call path that can't be validated.
          3. Fewer payload blocks than required write_file/edit_file calls.
          4. A required payload block is missing its filename= annotation.
          5. A payload block's filename= doesn't match the expected tool call target.

        Surplus payloads (more blocks than needed) are NOT a hard error — they are
        trimmed and reported as a protocol warning so the operation can succeed.
        """
        if cls._INLINE_CONTENT_ARG_RE.search(control_block.content):
            raise ProtocolViolationError(
                "write_file() / edit_file() was called with an inline content-like "
                "argument (content=, payload=, text=, or data=). These tools never "
                "accept file content as a Python argument. Put the file content in a "
                "Payload Block immediately after the ```codepilot block, annotated "
                "with filename=<same path>.",
                control_block
            )

        write_calls, has_dynamic, has_syntax_error = cls._extract_write_file_calls(control_block.content)

        # If there's a SyntaxError, bypass payload validation so the execution engine
        # can crash and feed the real SyntaxError back to the LLM.
        if has_syntax_error:
            return payload_blocks, None

        if not write_calls and has_dynamic:
            raise ProtocolViolationError(
                "view_file() / write_file() / edit_file() call found, but its first argument was "
                "not a literal quoted path. These tools must use a literal path like "
                'edit_file("src/app.py", ...) so the runtime can '
                "validate Payload Block filename= annotations. For computed content "
                "or dynamic paths, use Python native file I/O with WORK_DIR instead.",
                control_block
            )

        # Build the full list of tool calls for warning diagnostics (all tools, not just writers)
        all_calls, _, _ = cls._extract_all_tool_calls(control_block.content)

        # No write_file/edit_file calls → no payload blocks expected.
        # Any payload blocks present are surplus — soft warning, not a hard error.
        if not write_calls:
            if payload_blocks:
                warning = cls._build_surplus_warning(
                    write_calls=write_calls,
                    all_calls=all_calls,
                    found=len(payload_blocks),
                    expected=0,
                    surplus_blocks=payload_blocks,
                )
                return [], warning
            return [], None

        _SAFE_TOOLS = {"write_file", "edit_file", "view_file", "find", "semantic_search"}
        is_safe_block = all(tool in _SAFE_TOOLS for tool, _, _ in all_calls)

        # Flatten (filepath, count) into an ordered list of expected filenames
        expected: List[str] = []
        for filepath, count in write_calls:
            for _ in range(count):
                expected.append(filepath)

        missing_or_mismatch = False
        error_msg = ""

        if len(payload_blocks) < len(expected):
            missing_or_mismatch = True
            if unannotated_blocks:
                block_numbers = ", ".join(str(b.index + 1) for b in unannotated_blocks)
                error_msg = (f"Payload Block(s) missing filename= annotations (blocks: {block_numbers}). "
                             f"Found {len(payload_blocks)} annotated payload(s), expected {len(expected)}.")
            else:
                error_msg = f"Payload count mismatch: {len(payload_blocks)} payload block(s) found, expected {len(expected)}."
        else:
            # check annotations on the valid prefix
            for i, (block, exp_path) in enumerate(zip(payload_blocks[:len(expected)], expected)):
                if block.filename is None:
                    missing_or_mismatch = True
                    error_msg = f"Payload block {i + 1} is missing a filename= annotation. Expected: filename={exp_path}."
                    break
                if block.filename.replace("\\", "/") != exp_path.replace("\\", "/"):
                    missing_or_mismatch = True
                    error_msg = f"Payload block {i + 1} filename mismatch: got 'filename={block.filename}', expected 'filename={exp_path}'."
                    break

        if missing_or_mismatch:
            provided_filenames = [b.filename for b in payload_blocks if b.filename]
            synthetic_feedback = cls._build_synthetic_feedback(
                control_block=control_block, 
                all_calls=all_calls, 
                error_msg=error_msg, 
                is_partial=is_safe_block,
                provided_filenames=provided_filenames
            )
            if is_safe_block:
                return payload_blocks, synthetic_feedback
            else:
                raise ProtocolViolationError(synthetic_feedback, control_block)

        if len(payload_blocks) > len(expected):
            surplus_blocks = payload_blocks[len(expected):]
            valid_blocks   = payload_blocks[:len(expected)]
            warning = cls._build_surplus_warning(
                write_calls=write_calls,
                all_calls=all_calls,
                found=len(payload_blocks),
                expected=len(expected),
                surplus_blocks=surplus_blocks,
            )
            return valid_blocks, warning

        return payload_blocks, None

    # ------------------------------------------------------------------
    # Surplus-warning helpers
    # ------------------------------------------------------------------

    @classmethod
    def _extract_all_tool_calls(cls, control_content: str) -> Tuple[List[Tuple[str, str, int]], bool, bool]:
        """
        Like _extract_write_file_calls but tracks ALL known tools so the
        surplus warning can name every tool that was called.
        Returns (calls, has_dynamic, has_syntax_error) where each call is
        (tool_name, filepath, needs_payload:0|1).
        """
        _WRITE_TOOLS = {"write_file", "edit_file"}
        _READ_TOOLS  = {"view_file", "execute", "read_output", "send_input",
                        "find", "semantic_search", "ask_user", "task"}
        _ALL_TOOLS   = _WRITE_TOOLS | _READ_TOOLS

        class AllToolVisitor(ast.NodeVisitor):
            def __init__(self):
                self.calls: list = []
                self.has_dynamic = False

            def visit_Call(self, node):
                self.generic_visit(node)
                if not (isinstance(node.func, ast.Name) and node.func.id in _ALL_TOOLS):
                    return
                tool = node.func.id
                filepath = "<unknown>"
                if node.args:
                    arg0 = node.args[0]
                    if isinstance(arg0, ast.Constant):
                        filepath = str(arg0.value)
                    elif getattr(ast, "Str", type(None)) is not type(None) and isinstance(arg0, getattr(ast, "Str", type(None))):
                        filepath = arg0.s
                    else:
                        if tool in _WRITE_TOOLS:
                            self.has_dynamic = True
                # from_cache_id=<int> means no payload block needed for this call
                has_cache_id = False
                if tool in _WRITE_TOOLS:
                    for kw in node.keywords:
                        if kw.arg == "from_cache_id" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int):
                            has_cache_id = True
                            break
                needs_payload = (1 if tool in _WRITE_TOOLS else 0) if not has_cache_id else 0
                self.calls.append((node.lineno, node.col_offset, tool, filepath, needs_payload))

        try:
            tree = ast.parse(control_content)
        except SyntaxError:
            return [], False, True

        visitor = AllToolVisitor()
        visitor.visit(tree)
        visitor.calls.sort()
        results = [(tool, fp, cnt) for _, _, tool, fp, cnt in visitor.calls]
        return results, visitor.has_dynamic, False

    @classmethod
    def _build_surplus_warning(
        cls,
        write_calls: List[Tuple[str, int]],
        all_calls: List[Tuple[str, str, int]],
        found: int,
        expected: int,
        surplus_blocks: List[CodeBlock],
    ) -> str:
        """
        Build a precise, LLM-readable protocol deviation warning for surplus
        payload blocks. Names every tool that was called, how many payloads
        each needs, and exactly which blocks were surplus and ignored.
        """
        from collections import Counter
        surplus_n = len(surplus_blocks)

        # Tally tool calls by name
        tool_counts: Counter = Counter(tool for tool, _fp, _cnt in all_calls)

        # Build per-tool breakdown line
        breakdown_parts: List[str] = []
        for tool, count in sorted(tool_counts.items()):
            needs = 1 if tool in {"write_file", "edit_file"} else 0
            total_needed = needs * count
            plural_tool = "call" if count == 1 else "calls"
            plural_need = "payload" if total_needed == 1 else "payloads"
            breakdown_parts.append(
                f"{count} {tool}() {plural_tool} → needs {total_needed} {plural_need}"
            )
        breakdown = "; ".join(breakdown_parts)

        surplus_desc = (
            f"{surplus_n} surplus payload block"
            + ("s" if surplus_n > 1 else "")
            + " ("
            + ", ".join(f"block #{b.index + 1}" for b in surplus_blocks)
            + ")"
        )

        return (
            f"Protocol Deviation Detected: {found} payload block(s) generated "
            f"but only {expected} needed. "
            f"Tool call breakdown: [{breakdown}]. "
            f"{surplus_desc} was ignored — the operation completed successfully. "
            f"No corrective action required for this step. "
            f"In future steps, do not emit payload blocks alongside "
            f"view_file(), execute(), or other non-writing tools."
        )

    @classmethod
    def _build_synthetic_feedback(
        cls, 
        control_block: CodeBlock, 
        all_calls: List[Tuple[str, str, int]], 
        error_msg: str,
        is_partial: bool,
        provided_filenames: List[str]
    ) -> str:
        """Generates the highly engineered synthetic feedback prompt."""
        
        retry_calls = []
        if is_partial:
            # Only retry the payload-requiring calls that lacked a matching payload block
            for call in all_calls:
                tool, filepath, needs_payload = call
                if needs_payload and filepath not in provided_filenames:
                    retry_calls.append(call)
        else:
            retry_calls = all_calls

        synthetic = "Let me fix the issue real quick.\n```codepilot\n"
        if not is_partial:
            synthetic += control_block.content + "\n"
        else:
            for tool, filepath, _ in retry_calls:
                synthetic += f'{tool}("{filepath}")\n'
        synthetic += "```\n"

        for tool, filepath, needs_payload in retry_calls:
            if needs_payload:
                synthetic += f"```python filename={filepath}\n"
                if tool == "write_file":
                    synthetic += "<<<<<<< SEARCH\n=======\n# full file content here\n>>>>>>> REPLACE\n```\n"
                elif tool == "edit_file":
                    synthetic += "<<<<<<< SEARCH\n# content to search\n=======\n# content to replace\n>>>>>>> REPLACE\n```\n"
                else:
                    synthetic += "<<<<<<< SEARCH\n=======\n...\n>>>>>>> REPLACE\n```\n"

        return (
            f"PROTOCOL VIOLATION: {error_msg}\n\n"
            f"To fix your next response generate response formatted AS IS wrote below:\n"
            f"{synthetic}"
        )
