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
      - Block[0]  → Control Block   (always Python; executed by the runtime)
      - Block[1+] → Payload Blocks  (side-loaded by write_file)
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

        control_block is None when the LLM produces a plain-text response
        with no code blocks (treated as a conversational reply).
        """
        blocks = cls.parse(text)
        if not blocks:
            return None, []
        return blocks[0], blocks[1:]
