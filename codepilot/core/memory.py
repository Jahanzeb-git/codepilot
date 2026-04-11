"""
codepilot.core.memory
~~~~~~~~~~~~~~~~~~~~~

Agent-driven context management for long-running agentic sessions.

The agent manages its own context window using three internal tools:

    archive_context(position, summary)
        — compress a completed task's messages into an agent-provided
          summary. Original messages are stored for later reveal.

    reveal_context(position)
        — restore an archived task's full messages in-place.

    list_archived_context()
        — review what has been archived (summaries + token savings).

A token stress signal is injected into the system prompt every step,
giving the agent the information it needs to make context decisions.

Global summarization remains as a safety net at 90% context utilisation
— it fires only when the agent hasn't kept things under control.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from ..engine.provider import LLMProvider

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------
# tiktoken setup — cl100k_base with provider fudge factors
# -----------------------------------------------------------------------

try:
    import tiktoken
    _ENCODING = tiktoken.get_encoding("cl100k_base")
except ImportError:
    _ENCODING = None
    logger.warning(
        "tiktoken not installed — falling back to character-based estimation. "
        "Install with: pip install tiktoken"
    )

PROVIDER_FUDGE = {
    "anthropic": 1.1,   # cl100k undercounts ~10% for Claude
    "openai":    1.0,   # exact
    "together":  1.0,   # Qwen uses cl100k natively
}


def count_tokens(text: str, provider: str = "openai") -> int:
    """
    Count tokens using cl100k_base with provider-specific fudge factor.

    tiktoken (Rust-backed) encodes ~1-5M tokens/sec.
    A 50k-token context encodes in ~20ms.
    """
    if not text:
        return 0
    fudge = PROVIDER_FUDGE.get(provider, 1.0)
    if _ENCODING is not None:
        return int(len(_ENCODING.encode(text)) * fudge)
    # Fallback: character-based estimate (~3.8 chars/token)
    return int(len(text) / 3.8 * fudge)


def count_messages_tokens(
    messages: List[Dict], provider: str = "openai"
) -> int:
    """Sum token counts across all message contents."""
    return sum(count_tokens(m.get("content", ""), provider) for m in messages)


# -----------------------------------------------------------------------
# Tags
# -----------------------------------------------------------------------

TAG_USER_INPUT     = "[USER INPUT]"
TAG_ARCHIVED_TASK  = "[ARCHIVED TASK"   # e.g. "[ARCHIVED TASK 3]"
TAG_GLOBAL_SUMMARY = "[GLOBAL SUMMARY]"


# -----------------------------------------------------------------------
# Task map — find which messages belong to which task
# -----------------------------------------------------------------------

_TASK_PATTERN    = re.compile(r"^\[Task (\d+)\]")
_ARCHIVED_PATTERN = re.compile(r"^\[ARCHIVED TASK (\d+)\]")


def find_task_map(
    messages: List[Dict],
) -> Dict[int, Tuple[int, int, bool]]:
    """
    Scan messages and return a mapping of task positions to their ranges.

    Returns:
        {position: (start_idx, end_idx, is_archived)}
        start_idx is inclusive, end_idx is exclusive.
    """
    task_map: Dict[int, Tuple[int, int, bool]] = {}
    open_tasks: List[Tuple[int, int]] = []   # (position, start_idx)

    for i, msg in enumerate(messages):
        content = msg.get("content", "")

        # Archived task — always a single message
        archived_match = _ARCHIVED_PATTERN.match(content)
        if archived_match:
            # Close any open task first
            if open_tasks:
                pos, start = open_tasks.pop()
                task_map[pos] = (start, i, False)
            a_pos = int(archived_match.group(1))
            task_map[a_pos] = (i, i + 1, True)
            continue

        # New task boundary
        task_match = _TASK_PATTERN.match(content)
        if task_match:
            # Close previous open task
            if open_tasks:
                pos, start = open_tasks.pop()
                task_map[pos] = (start, i, False)
            t_pos = int(task_match.group(1))
            open_tasks.append((t_pos, i))
            continue

        # Legacy format: [USER INPUT] without [Task N] prefix
        if content.startswith(TAG_USER_INPUT):
            if open_tasks:
                pos, start = open_tasks.pop()
                task_map[pos] = (start, i, False)
            # Assign position 0 for legacy — scan will build correctly
            # when multiple legacy tasks exist (they just use order)
            legacy_pos = max(task_map.keys(), default=0) + 1 if task_map else 1
            open_tasks.append((legacy_pos, i))

    # Close the last open task
    if open_tasks:
        pos, start = open_tasks.pop()
        task_map[pos] = (start, len(messages), False)

    return task_map


def get_highest_task_position(messages: List[Dict]) -> int:
    """Return the highest task position found in messages, or 0 if none."""
    highest = 0
    for msg in messages:
        content = msg.get("content", "")
        m = _TASK_PATTERN.match(content)
        if m:
            highest = max(highest, int(m.group(1)))
        m = _ARCHIVED_PATTERN.match(content)
        if m:
            highest = max(highest, int(m.group(1)))
    return highest


# -----------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------

class MemoryConfig:
    """
    Tuning knobs for the context memory system.
    Populated from the `memory:` section in the AgentFile YAML.
    """

    def __init__(
        self,
        max_context_tokens: int = 120_000,
        global_summary_threshold: float = 0.9,
        global_summary_max_tokens: int = 500,
        provider_name: str = "openai",
    ):
        self.max_context_tokens = max_context_tokens
        self.provider_name = provider_name
        # Safety-net threshold — fires only when agent hasn't archived enough
        self.global_summary_threshold_tokens = int(
            max_context_tokens * global_summary_threshold
        )
        self.global_summary_max_tokens = global_summary_max_tokens


# -----------------------------------------------------------------------
# Summarization prompt (global safety net only)
# -----------------------------------------------------------------------

_GLOBAL_SUMMARY_PROMPT = """\
You are a context compression assistant. Summarize the following sequence of \
completed task content into a single cohesive paragraph. Preserve key facts: \
files created or modified, critical decisions made, and any unresolved issues. \
Write in plain prose — no JSON, no headers, no lists.

## Content to compress:
{summaries}
"""


# -----------------------------------------------------------------------
# Context Archive — reversible storage for archived tasks
# -----------------------------------------------------------------------

class ContextArchive:
    """
    Stores original messages for archived tasks so they can be revealed.

    Each entry maps a task position to the list of message dicts that
    were replaced by the archive summary.
    """

    def __init__(self):
        self._store: Dict[int, List[Dict]] = {}

    def archive(self, position: int, messages: List[Dict]) -> None:
        """Store a task's original messages."""
        self._store[position] = list(messages)

    def reveal(self, position: int) -> List[Dict]:
        """Return a copy of the original messages for a task without removing them from archive."""
        return list(self._store[position])

    def is_archived(self, position: int) -> bool:
        return position in self._store

    def list_all(self) -> Dict[int, List[Dict]]:
        return dict(self._store)

    def token_count(self, position: int, provider: str = "openai") -> int:
        """Return the token count of an archived task's original messages."""
        msgs = self._store.get(position, [])
        return count_messages_tokens(msgs, provider)

    def clear_position(self, position: int) -> None:
        """Permanently delete an archived task (used by global summarizer)."""
        self._store.pop(position, None)

    def serialize(self) -> Dict:
        return {"store": {str(k): v for k, v in self._store.items()}}

    @classmethod
    def deserialize(cls, data: Dict) -> "ContextArchive":
        inst = cls()
        raw = data.get("store", {})
        inst._store = {int(k): v for k, v in raw.items()}
        return inst


# -----------------------------------------------------------------------
# Memory Manager
# -----------------------------------------------------------------------

class MemoryManager:
    """
    Orchestrates agent-driven context management.

    Responsibilities:
      - Token counting (tiktoken cl100k_base)
      - Context stress calculation (per-task breakdown)
      - ContextArchive management (archive/reveal storage)
      - Global summarization safety net (90% threshold)
    """

    def __init__(self, config: MemoryConfig, provider: "LLMProvider"):
        self.config = config
        self.provider = provider
        self.archive = ContextArchive()

    # ------------------------------------------------------------------
    # Safety-net global summarization
    # ------------------------------------------------------------------

    async def process(self, messages: List[Dict]) -> List[Dict]:
        """
        Safety-net only. Called at the start of run().

        If total tokens exceed the 90% threshold and the agent hasn't kept
        things under control, fire global summarization to prevent overflow.
        """
        total = count_messages_tokens(messages, self.config.provider_name)

        if total > self.config.global_summary_threshold_tokens:
            logger.info(
                "Global safety net triggered: %d tokens > %d threshold",
                total, self.config.global_summary_threshold_tokens,
            )
            messages = await self._global_summarize(messages)

        return messages

    async def _global_summarize(self, messages: List[Dict]) -> List[Dict]:
        """
        Collapse the older half of context into a [GLOBAL SUMMARY].

        When an [ARCHIVED TASK N] placeholder is consumed, its ContextArchive
        entry is permanently deleted — the original is no longer recoverable.
        """
        total = count_messages_tokens(messages, self.config.provider_name)
        midpoint_target = total // 2

        # Walk from start, accumulating tokens until we cross midpoint
        cumulative = 0
        split_index = 0
        for i, msg in enumerate(messages):
            cumulative += count_tokens(
                msg.get("content", ""), self.config.provider_name
            )
            if cumulative >= midpoint_target:
                split_index = i + 1
                break

        if split_index <= 0:
            return messages

        older_messages = messages[:split_index]

        # Permanently delete any ContextArchive entries being consumed
        for msg in older_messages:
            content = msg.get("content", "")
            m = _ARCHIVED_PATTERN.match(content)
            if m:
                pos = int(m.group(1))
                self.archive.clear_position(pos)
                logger.info(
                    "Global summarizer permanently deleted archive for Task %d",
                    pos,
                )

        # Build the text to summarise
        summaries_text = "\n\n---\n\n".join(
            m.get("content", "") for m in older_messages
        )

        prompt = _GLOBAL_SUMMARY_PROMPT.format(summaries=summaries_text)

        try:
            summary_text = await self.provider.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=int(self.config.global_summary_max_tokens * 4),
            )

            global_msg = {
                "role": "user",
                "content": f"{TAG_GLOBAL_SUMMARY}\n{summary_text}",
            }

            messages = [global_msg] + messages[split_index:]

        except Exception as e:
            logger.warning(
                "Global summarization failed: %s. Keeping context as-is.", e
            )

        return messages

    # ------------------------------------------------------------------
    # Context stress signal
    # ------------------------------------------------------------------

    def build_context_stress(self, messages: List[Dict]) -> str:
        """
        Build the context stress signal for the system prompt.

        Returns a human-readable string with total token usage and
        per-task breakdown, updated every agentic step.
        """
        provider = self.config.provider_name
        total = count_messages_tokens(messages, provider)
        max_tok = self.config.max_context_tokens
        pct = round(total / max_tok * 100) if max_tok else 0

        tmap = find_task_map(messages)

        lines = [f"Context: {total:,} / {max_tok:,} tokens ({pct}%)"]

        if tmap:
            # Find the active task (highest position)
            active_pos = max(tmap.keys())

            for pos in sorted(tmap.keys()):
                start, end, is_archived = tmap[pos]
                if is_archived:
                    summary_preview = messages[start]["content"].split(
                        "\n", 1
                    )[-1][:80]
                    lines.append(
                        f"  Task {pos}: ARCHIVED "
                        f"— \"{summary_preview}...\""
                    )
                else:
                    task_tokens = count_messages_tokens(
                        messages[start:end], provider
                    )
                    marker = " ← active" if pos == active_pos else ""
                    lines.append(
                        f"  Task {pos}: {task_tokens:,} tokens{marker}"
                    )

        if pct >= 70:
            lines.append("⚡ Context stress elevated.")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def serialize_state(self) -> Dict:
        """Serialize memory state for session persistence."""
        return {
            "archive": self.archive.serialize(),
        }

    def restore_state(self, data: Dict) -> None:
        """Restore memory state from persisted data."""
        if not data:
            return
        arch = data.get("archive")
        if arch:
            self.archive = ContextArchive.deserialize(arch)
