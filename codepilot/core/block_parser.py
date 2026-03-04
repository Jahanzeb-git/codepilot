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
    def split(cls, text: str) -> Tuple[Optional[CodeBlock], List[CodeBlock], Optional[CodeBlock]]:
        """
        Returns (control_block, payload_blocks, completion_block).

        The control block is the first ```codepilot fenced block.
        The completion block is the ```completion fenced block (if present) —
        its content streams to the user and signals the agentic loop to stop.
        Payload blocks are all blocks between codepilot and completion (consumed
        by write_file). If no ```codepilot block exists, returns (None, [], None)
        — the response is a conversational reply with display-only code blocks.
        """
        blocks = cls.parse(text)
        if not blocks:
            return None, [], None

        for i, block in enumerate(blocks):
            if block.language == "codepilot":
                remaining        = blocks[i + 1:]
                payload_blocks   = [b for b in remaining if b.language != "completion"]
                completion_list  = [b for b in remaining if b.language == "completion"]
                completion_block = completion_list[0] if completion_list else None
                return block, payload_blocks, completion_block

        # No codepilot block → entire response is display/chat
        return None, [], None

