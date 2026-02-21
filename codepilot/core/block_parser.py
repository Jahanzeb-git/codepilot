import re
from typing import List, Optional, Tuple
from dataclasses import dataclass


@dataclass
class CodeBlock:
    language: str   # e.g. "python", "cpp", "text", ""
    content: str    # raw block content, trailing newline stripped
    index: int      # 0-based position in the response


class BlockParser:
    """
    Parses an LLM response and extracts fenced Markdown code blocks.

    The runtime convention:
      - The first ```codepilot block → Control Block (Python; executed)
      - Blocks after it            → Payload Blocks (side-loaded by write_file)
      - All other blocks (```python, ```json, …) are display-only markdown,
        safe to include in chat/explanations without risk of execution.
    """

    # Matches ```lang\\n...content...\\n``` (non-greedy, DOTALL)
    _FENCE_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)

    @classmethod
    def parse(cls, text: str) -> List[CodeBlock]:
        blocks: List[CodeBlock] = []
        for idx, match in enumerate(cls._FENCE_RE.finditer(text)):
            lang = match.group(1).strip().lower() or "text"
            content = match.group(2)
            # Strip a single trailing newline that the fence introduces, but
            # preserve any intentional blank lines inside the content.
            content = content.rstrip("\n")
            blocks.append(CodeBlock(language=lang, content=content, index=idx))
        return blocks

    @classmethod
    def split(cls, text: str) -> Tuple[Optional[CodeBlock], List[CodeBlock]]:
        """
        Returns (control_block, payload_blocks).

        The control block is the first ```codepilot fenced block.
        All blocks after it are payload blocks (consumed by write_file).
        If no ```codepilot block exists, returns (None, []) — the response
        is a conversational reply with display-only code blocks.
        """
        blocks = cls.parse(text)
        if not blocks:
            return None, []

        for i, block in enumerate(blocks):
            if block.language == "codepilot":
                return block, blocks[i + 1:]

        # No codepilot block → entire response is display/chat
        return None, []

