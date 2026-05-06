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
from typing import List, Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class CodeBlock:
    language: str            # e.g. "python", "codepilot", "completion", "text"
    content: str             # raw block content, trailing newline stripped
    index: int               # 0-based position in the response
    filename: Optional[str] = field(default=None)  # parsed from filename= annotation


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
    _FENCE_RE = re.compile(r"```([^\n]*)\n(.*?)```", re.DOTALL)

    # Extracts filename= from a fence tag. Supports quoted and bare values:
    #   filename=routes/profile.py   filename="routes/profile.py"
    _FILENAME_RE = re.compile(r'filename=(?:"([^"]+)"|\'([^\']+)\'|(\S+))')
    _WRITE_FILE_RE = re.compile(r'write_file\s*\(')
    _INLINE_CONTENT_ARG_RE = re.compile(
        r'write_file\s*\([^)]*\b(content|payload|text|data)\s*=',
        re.DOTALL,
    )
    _VALID_WRITE_MODES = frozenset({"w", "a", "edit", "insert", "multi_edit"})


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
            blocks.append(CodeBlock(language=language, content=content, index=idx, filename=filename))
        return blocks


    @classmethod
    def split(cls, text: str) -> Tuple[Optional[CodeBlock], List[CodeBlock], Optional[CodeBlock]]:
        """
        Returns (control_block, payload_blocks, completion_block).

        The control block is the first ```codepilot block.
        The completion block is the ```completion block — its presence signals
        the agentic loop to terminate after this step.
        Payload blocks are every block after codepilot that isn't completion —
        side-loaded by write_file() in order.

        Raises ValueError if payload filename= annotations don't match
        the write_file() calls parsed from the control block.

        If no ```codepilot block exists, returns (None, [], None) — the
        response is a conversational reply with display-only code blocks.
        """
        blocks = cls.parse(text)
        if not blocks:
            return None, [], None

        for i, block in enumerate(blocks):
            if block.language == "codepilot":
                remaining        = blocks[i + 1:]
                # Only blocks with a filename= annotation are payload blocks.
                # Display-only blocks (```python for explanation, etc.) never carry
                # filename= so they pass through safely even if placed after the
                # codepilot block, without triggering validation.
                payload_blocks   = [b for b in remaining if b.language != "completion" and b.filename is not None]
                unannotated_blocks = [b for b in remaining if b.language != "completion" and b.filename is None]
                completion_list  = [b for b in remaining if b.language == "completion"]
                completion_block = completion_list[0] if completion_list else None

                # Validate filename= annotations before handing off to runtime
                cls._validate_payload_filenames(block, payload_blocks, unannotated_blocks)

                return block, payload_blocks, completion_block

        # No codepilot block → entire response is display/chat
        return None, [], None


    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_write_file_calls(control_content: str) -> List[Tuple[str, int]]:
        """
        Parse write_file() calls from control block source using a paren-balanced
        scanner — regex alone can't handle nested structures like edits=[(a,b),(c,d)].

        Returns list of (filepath, payload_count) in call order:
          payload_count = 1  for all regular modes (create / edit / append)
          payload_count = N  for multi_edit, where N = number of edit tuples
        """
        results: List[Tuple[str, int]] = []

        for start_match in re.finditer(r'write_file\s*\(', control_content):
            # Walk forward from the opening paren, tracking depth
            start = start_match.end() - 1  # position of '('
            depth = 0
            i = start
            while i < len(control_content):
                ch = control_content[i]
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        break
                i += 1

            call_args = control_content[start + 1 : i]  # content inside outer parens

            # First positional arg is the filepath string
            path_match = re.match(r'\s*["\']([^"\']+)["\']', call_args)
            if not path_match:
                continue
            filepath = path_match.group(1)

            mode_match = re.search(r'mode\s*=\s*["\']([^"\']+)["\']', call_args)
            if mode_match:
                mode = mode_match.group(1)
                if mode not in BlockParser._VALID_WRITE_MODES:
                    valid = "', '".join(sorted(BlockParser._VALID_WRITE_MODES))
                    raise ValueError(
                        f"write_file() for '{filepath}' uses invalid mode '{mode}'. "
                        f"Valid modes are '{valid}'. Use mode='a' for append and "
                        "mode='multi_edit' for multiple non-contiguous edits."
                    )

            # Detect multi_edit and count its tuples
            is_multi_edit = bool(re.search(r'mode\s*=\s*["\']multi_edit["\']', call_args))
            if is_multi_edit:
                edits_match = re.search(r'edits\s*=\s*\[', call_args)
                if edits_match:
                    # Walk to the matching ] to isolate the edits list
                    edits_start = edits_match.end()
                    depth2 = 1
                    j = edits_start
                    while j < len(call_args) and depth2 > 0:
                        if call_args[j] == '[':
                            depth2 += 1
                        elif call_args[j] == ']':
                            depth2 -= 1
                        j += 1
                    edits_content = call_args[edits_start : j - 1]
                    # Each tuple starts with '(' — count them
                    payload_count = edits_content.count('(')
                else:
                    payload_count = 1
            else:
                payload_count = 1

            results.append((filepath, payload_count))

        return results


    @classmethod
    def _validate_payload_filenames(
        cls,
        control_block: CodeBlock,
        payload_blocks: List[CodeBlock],
        unannotated_blocks: List[CodeBlock],
    ) -> None:
        """
        Cross-checks payload block filename= annotations against the write_file()
        calls parsed from the control block.

        Raises ValueError with a precise, human-readable (and LLM-readable) message
        describing exactly what went wrong and what was expected.

        Three failure modes:
          1. Payload count doesn't match total expected across all write_file() calls
          2. A payload block is missing its filename= annotation
          3. A payload block's filename= doesn't match the corresponding write_file() target
        """
        if cls._INLINE_CONTENT_ARG_RE.search(control_block.content):
            raise ValueError(
                "write_file() was called with an inline content-like argument "
                "(content=, payload=, text=, or data=). write_file() never accepts "
                "file content as a Python argument. Put the file content in a "
                "Payload Block immediately after the ```codepilot block, annotated "
                "with filename=<same path>."
            )

        write_calls = cls._extract_write_file_calls(control_block.content)

        if not write_calls and cls._WRITE_FILE_RE.search(control_block.content):
            raise ValueError(
                "write_file() call found, but its first argument was not a literal "
                "quoted path. Payload-backed write_file() calls must use a literal "
                "path like write_file(\"src/app.py\", mode=\"w\") so the runtime can "
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
                    f"{len(expected)} (write_file calls in order: "
                    f"{', '.join(f'{fp} ×{n}' if n > 1 else fp for fp, n in write_calls)}). "
                    "Every write_file() payload must be fenced like "
                    "```python filename=path/from/write_file.py."
                )
            summary = ", ".join(
                f"{fp} ×{n}" if n > 1 else fp for fp, n in write_calls
            )
            raise ValueError(
                f"Payload count mismatch: {len(payload_blocks)} payload block(s) found, "
                f"expected {len(expected)} "
                f"(write_file calls in order: {summary})."
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
                    f"but write_file() call {i + 1} targets '{exp_path}'. "
                    f"Payload blocks must appear in the same order as write_file() calls."
                )
