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
    def split(cls, text: str) -> Tuple[Optional[CodeBlock], List[CodeBlock], Optional[str]]:
        """
        Returns (control_block, payload_blocks, protocol_warning).

        The control block is the FIRST ```codepilot block. If the model
        accidentally generates a second ```codepilot block, it is ignored —
        payload collection stops at that boundary.

        Payload blocks are fenced blocks after codepilot that carry a
        filename= annotation — side-loaded by write_file() in order.

        Raises ValueError if payload filename= annotations don't match
        the write_file() calls parsed from the control block (wrong filename,
        inline content= argument, or missing annotation on a required payload).

        Surplus payload blocks beside tools that don't consume them (e.g.
        view_file, execute) are silently trimmed from the returned list and
        a descriptive protocol_warning string is returned instead of raising.
        The warning is injected into the execution result so the model sees an
        accurate correction signal on the next agentic step.

        If no ```codepilot block exists, returns (None, [], None) — the
        response is a conversational reply.
        """
        # Defensive pre-processing: strip any <thinking>...</thinking> blocks
        # before parsing. The runtime's streaming state machine already strips
        # these, but this is belt-and-suspenders against any edge case where
        # thinking content (which may contain filename= code fence examples
        # from the docstrings) could otherwise trip the payload validator.
        import re as _re
        if "<thinking>" in text:
            text = _re.sub(r"<thinking>.*?</thinking>\n?", "", text, flags=_re.DOTALL)

        # Strip out any docstring examples that the model might regurgitate.
        # The tool docstrings wrap examples in tags like <a>, <b>, <c>, <example>.
        # If a tag immediately contains a codepilot block, we strip the entire tag
        # so the parser doesn't accidentally execute the example.
        text = _re.sub(
            r"<(a|b|c|d|e|example|tool_docs)>\s*```codepilot.*?.*?</\1>\n?",
            "", text, flags=_re.DOTALL
        )

        blocks = cls.parse(text)
        if not blocks:
            return None, [], None

        for i, block in enumerate(blocks):
            if block.language == "codepilot":
                remaining = blocks[i + 1:]
                # Collect payloads until second codepilot block (if any).
                # Only blocks with a filename= annotation are payload blocks.
                # Display-only blocks (e.g. ```python for explanation) never
                # carry filename= so they pass through safely.
                payload_blocks: List[CodeBlock] = []
                unannotated_blocks: List[CodeBlock] = []
                for b in remaining:
                    if b.language == "codepilot":
                        break  # Second codepilot block — stop collecting
                    if b.filename is not None:
                        payload_blocks.append(b)
                    else:
                        unannotated_blocks.append(b)

                # Validate payload annotations. May raise ValueError for genuine
                # protocol violations, or return a non-None warning string for
                # recoverable surplus-payload deviations.
                valid_payloads, warning = cls._validate_payload_filenames(
                    block, payload_blocks, unannotated_blocks
                )

                return block, valid_payloads, warning

        # No codepilot block → entire response is display/chat
        return None, [], None


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
                        # Check for from_cache=True keyword — if present, no payload block needed
                        from_cache = False
                        for kw in node.keywords:
                            if kw.arg == "from_cache" and isinstance(kw.value, ast.Constant) and kw.value.value:
                                from_cache = True
                                break
                        if node.func.id in ("write_file", "edit_file"):
                            payload_count = 0 if from_cache else 1
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
    def salvage_payloads_for_cache(
        cls, text: str
    ) -> List[Tuple[str, str]]:
        """
        Best-effort extraction of (target_path, content) pairs from a raw LLM
        response that failed validation. Used to populate the payload cache even
        when BlockParser.split() raises a ValueError, so the LLM can retry using
        write_file(path, from_cache=True) without re-emitting content.

        Strategy:
          1. Find the first ```codepilot block and extract tool call target paths
             in order using the AST (same as _extract_write_file_calls).
          2. Find all annotated payload blocks (filename= present) in document order.
          3. Match targets to payloads positionally — annotation errors are ignored
             because we trust the AST-extracted call order, not the wrong annotation.
          4. Return only (target_path, content) pairs we can confidently match.
        """
        blocks = cls.parse(text)
        if not blocks:
            return []

        # Find first codepilot block
        control = next((b for b in blocks if b.language == "codepilot"), None)
        if control is None:
            return []

        # Extract ordered target paths from AST (ignoring from_cache calls — no payload)
        write_calls, _, has_syntax_error = cls._extract_write_file_calls(control.content)
        if has_syntax_error:
            return []

        # Only calls that expect a payload (count == 1)
        targets = [fp for fp, cnt in write_calls if cnt == 1]
        if not targets:
            return []

        # Collect all blocks after the codepilot block that have a filename annotation
        # OR no annotation (we'll try to match by position regardless)
        ctrl_idx = next(i for i, b in enumerate(blocks) if b is control)
        candidate_blocks: List[CodeBlock] = []
        for b in blocks[ctrl_idx + 1:]:
            if b.language == "codepilot":
                break  # Stop at second codepilot block
            # Include blocks with OR without filename annotation —
            # position-based matching doesn't require a correct annotation
            candidate_blocks.append(b)

        # Match positionally: target[i] ↔ candidate_blocks[i]
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
            raise ValueError(
                "write_file() / edit_file() was called with an inline content-like "
                "argument (content=, payload=, text=, or data=). These tools never "
                "accept file content as a Python argument. Put the file content in a "
                "Payload Block immediately after the ```codepilot block, annotated "
                "with filename=<same path>."
            )

        write_calls, has_dynamic, has_syntax_error = cls._extract_write_file_calls(control_block.content)

        # If there's a SyntaxError, bypass payload validation so the execution engine
        # can crash and feed the real SyntaxError back to the LLM.
        if has_syntax_error:
            return payload_blocks, None

        # Guard: fire only when a tool call exists with a non-literal (dynamic) path.
        if not write_calls and has_dynamic:
            raise ValueError(
                "view_file() / write_file() / edit_file() call found, but its first argument was "
                "not a literal quoted path. These tools must use a literal path like "
                'edit_file("src/app.py", ...) so the runtime can '
                "validate Payload Block filename= annotations. For computed content "
                "or dynamic paths, use Python native file I/O with WORK_DIR instead."
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

        # Flatten (filepath, count) into an ordered list of expected filenames
        expected: List[str] = []
        for filepath, count in write_calls:
            for _ in range(count):
                expected.append(filepath)

        # --- Surplus payloads: more blocks than needed ---
        # Trim the excess and emit a soft warning. The valid prefix is consumed
        # normally by write_file() calls in order.
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
            # Still validate the required prefix before returning
            cls._check_filename_annotations(valid_blocks, expected)
            return valid_blocks, warning

        # --- Fewer payloads than required: hard error ---
        if len(payload_blocks) < len(expected):
            if unannotated_blocks:
                block_numbers = ", ".join(str(b.index + 1) for b in unannotated_blocks)
                raise ValueError(
                    f"Payload Block(s) after the ```codepilot block are missing filename= "
                    f"annotations (block number(s): {block_numbers}). Found "
                    f"{len(payload_blocks)} annotated payload block(s), expected "
                    f"{len(expected)} (tool calls requiring payloads in order: "
                    f"{', '.join(f'{fp} ×{n}' if n > 1 else fp for fp, n in write_calls)}). "
                    "Every payload must be fenced like "
                    "```python filename=path/to/file.py."
                )
            summary = ", ".join(
                f"{fp} ×{n}" if n > 1 else fp for fp, n in write_calls
            )
            raise ValueError(
                f"Payload count mismatch: {len(payload_blocks)} payload block(s) found, "
                f"expected {len(expected)} "
                f"(tool calls requiring payloads in order: {summary})."
            )

        # --- Exact match: validate filename= annotations ---
        cls._check_filename_annotations(payload_blocks, expected)
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
                # from_cache=True means no payload block needed for this call
                from_cache = False
                if tool in _WRITE_TOOLS:
                    for kw in node.keywords:
                        if kw.arg == "from_cache" and isinstance(kw.value, ast.Constant) and kw.value.value:
                            from_cache = True
                            break
                needs_payload = (1 if tool in _WRITE_TOOLS else 0) if not from_cache else 0
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
    def _check_filename_annotations(
        cls,
        payload_blocks: List[CodeBlock],
        expected: List[str],
    ) -> None:
        """Hard-error check for per-block filename= annotation correctness."""
        for i, (block, exp_path) in enumerate(zip(payload_blocks, expected)):
            if block.filename is None:
                raise ValueError(
                    f"Payload block {i + 1} is missing a filename= annotation. "
                    f"Expected: ```<lang> filename={exp_path}. "
                    f"Every payload block must declare its target file."
                )
            actual_norm   = block.filename.replace("\\", "/")
            expected_norm = exp_path.replace("\\", "/")
            if actual_norm != expected_norm:
                raise ValueError(
                    f"Payload block {i + 1} filename mismatch: "
                    f"annotated as 'filename={block.filename}' "
                    f"but tool call {i + 1} targets '{exp_path}'. "
                    f"Payload blocks must appear in the same order as tool calls."
                )
