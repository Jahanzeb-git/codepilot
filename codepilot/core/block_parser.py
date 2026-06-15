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
    # and the block content. Group 1 = tag, Group 2 = content.
    _FENCE_RE = re.compile(r"^```([^\n]*)\n(.*?)^```\s*$", re.DOTALL | re.MULTILINE)

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
            tag = match.group(1)
            content = match.group(2)
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
    def split(cls, text: str) -> Tuple[Optional[CodeBlock], List[CodeBlock]]:
        """
        Returns (control_block, payload_blocks).

        The control block is the FIRST ```codepilot block. If the model
        accidentally generates a second ```codepilot block, it is ignored —
        payload collection stops at that boundary.

        Payload blocks are fenced blocks after codepilot that carry a
        filename= annotation — side-loaded by write_file() in order.

        Raises ValueError if payload filename= annotations don't match
        the write_file() calls parsed from the control block.

        If no ```codepilot block exists, returns (None, []) — the
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
            return None, []

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

                # Validate filename= annotations before handing off to runtime
                cls._validate_payload_filenames(block, payload_blocks, unannotated_blocks)

                return block, payload_blocks

        # No codepilot block → entire response is display/chat
        return None, []


    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @classmethod
    def _extract_write_file_calls(cls, control_content: str) -> Tuple[List[Tuple[str, int]], bool, bool]:
        """
        Parse view_file, write_file, edit_file calls from the control block using AST.
        Returns (write_calls, has_dynamic_path, has_syntax_error).
        write_calls is a list of (filepath, payload_count) in call order.
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
                        payload_count = 1 if node.func.id in ("write_file", "edit_file") else 0
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
    def _validate_payload_filenames(
        cls,
        control_block: CodeBlock,
        payload_blocks: List[CodeBlock],
        unannotated_blocks: List[CodeBlock],
    ) -> None:
        """
        Cross-checks payload block filename= annotations against file_editor() and
        legacy write_file() calls parsed from the control block.

        Raises ValueError with a precise, human-readable (and LLM-readable) message
        describing exactly what went wrong and what was expected.

        Three failure modes:
          1. Payload count doesn't match total expected across all tool calls
          2. A payload block is missing its filename= annotation
          3. A payload block's filename= doesn't match the corresponding tool call target
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
            return

        # Guard: fire only when a tool call exists with a non-literal (dynamic) path.
        if not write_calls and has_dynamic:
            raise ValueError(
                "view_file() / write_file() / edit_file() call found, but its first argument was "
                "not a literal quoted path. These tools must use a literal path like "
                'edit_file("src/app.py", ...) so the runtime can '
                "validate Payload Block filename= annotations. For computed content "
                "or dynamic paths, use Python native file I/O with WORK_DIR instead."
            )

        # No write_file calls → no payload blocks expected
        if not write_calls:
            if payload_blocks:
                raise ValueError(
                    f"Payload mismatch: {len(payload_blocks)} payload block(s) present "
                    f"but no write_file() calls found in control block. "
                    f"Payload blocks are only valid alongside write_file() calls."
                )
            return

        # Flatten (filepath, count) into an ordered list of expected filenames
        expected: List[str] = []
        for filepath, count in write_calls:
            for _ in range(count):
                expected.append(filepath)

        # --- Failure mode 1: count mismatch ---
        if len(payload_blocks) != len(expected):
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

        # --- Failure modes 2 & 3: per-block annotation check ---
        for i, (block, exp_path) in enumerate(zip(payload_blocks, expected)):
            if block.filename is None:
                raise ValueError(
                    f"Payload block {i + 1} is missing a filename= annotation. "
                    f"Expected: ```<lang> filename={exp_path}. "
                    f"Every payload block must declare its target file."
                )
            # Normalise separators before comparing (Windows vs POSIX paths)
            actual_norm   = block.filename.replace("\\", "/")
            expected_norm = exp_path.replace("\\", "/")
            if actual_norm != expected_norm:
                raise ValueError(
                    f"Payload block {i + 1} filename mismatch: "
                    f"annotated as 'filename={block.filename}' "
                    f"but tool call {i + 1} targets '{exp_path}'. "
                    f"Payload blocks must appear in the same order as tool calls."
                )
