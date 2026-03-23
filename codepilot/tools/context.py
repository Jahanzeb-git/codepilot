"""
codepilot.tools.context
~~~~~~~~~~~~~~~~~~~~~~~

Agent-driven context management tools: archive, reveal, list.
"""

from __future__ import annotations

from typing import List, Tuple, TYPE_CHECKING, Union

from ..core.memory import (
    TAG_ARCHIVED_TASK,
    count_messages_tokens,
    find_task_map,
)

if TYPE_CHECKING:
    from ..engine.runtime import Runtime


class ContextTools:
    """Provides archive_context, reveal_context, list_archived_context."""

    def __init__(self, runtime: "Runtime"):
        self.runtime = runtime

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def archive_context(
        self,
        position: Union[int, Tuple[int, ...], None] = None,
        summary: Union[str, List[str], None] = None,
        task: Union[int, Tuple[int, ...], None] = None,
    ) -> str:
        """Archive completed task context, replacing it with your summary.

        Summary must capture what happened — files touched, outcome,
        key details — so reading it later tells you whether to reveal.

        Args:
            position: Task position (int) or tuple of positions.
            summary:  Summary string or list of strings (one per position).
            task:     Alias for 'position'. Use either position= or task=.

        Cannot archive the currently active task.
        """
        messages = self.runtime.messages
        memory   = self.runtime._memory
        provider = memory.config.provider_name

        if position is not None and task is not None:
            return "ERROR: Provide only one of 'position' or 'task', not both."
        if position is None:
            position = task
        if position is None:
            return "ERROR: Missing required argument: 'position' (or alias 'task')."
        if summary is None:
            return "ERROR: Missing required argument: 'summary'."

        # Normalise inputs
        positions = (position,) if isinstance(position, int) else tuple(position)
        summaries = [summary] if isinstance(summary, str) else list(summary)

        if len(positions) != len(summaries):
            return (
                "ERROR: Number of positions and summaries must match. "
                f"Got {len(positions)} positions and {len(summaries)} summaries."
            )

        # Build current task map
        tmap = find_task_map(messages)
        if not tmap:
            return "ERROR: No tasks found in context."

        active_pos = max(tmap.keys())

        # Validate all positions before modifying anything
        for pos in positions:
            if pos not in tmap:
                return f"ERROR: Task {pos} not found in context."
            if pos == active_pos:
                return f"ERROR: Cannot archive Task {pos} — it is the active task."
            _, _, is_archived = tmap[pos]
            if is_archived:
                return (
                    f"ERROR: Task {pos} is already archived. "
                    f"Use reveal_context({pos}) to restore it first."
                )

        # Process from highest position first so message indices don't shift
        total_saved = 0
        for pos, summ in sorted(
            zip(positions, summaries), key=lambda x: x[0], reverse=True
        ):
            start, end, _ = tmap[pos]
            original_msgs = messages[start:end]

            # Store originals in archive
            memory.archive.archive(pos, original_msgs)

            # Count tokens saved
            saved = count_messages_tokens(original_msgs, provider)
            total_saved += saved

            # Replace with single archived message
            archived_msg = {
                "role": "user",
                "content": f"{TAG_ARCHIVED_TASK} {pos}]\n{summ}",
            }
            messages[start:end] = [archived_msg]

            # Rebuild task map after modification for next iteration
            tmap = find_task_map(messages)

        # Report result
        new_total = count_messages_tokens(messages, provider)
        max_tok = memory.config.max_context_tokens
        new_pct = round(new_total / max_tok * 100) if max_tok else 0

        archived_str = ", ".join(str(p) for p in sorted(positions))
        return (
            f"Archived Task(s) {archived_str}. "
            f"Context reduced by ~{total_saved:,} tokens. "
            f"({new_total:,} / {max_tok:,} — {new_pct}%)"
        )

    def reveal_context(self, position: int) -> str:
        """Restore a previously archived task's full context.

        Replaces the archived summary with original messages exactly
        where they were. Use when you need exact details from an
        earlier task.

        Args:
            position: Task position (int) to reveal.
        """
        messages = self.runtime.messages
        memory   = self.runtime._memory
        provider = memory.config.provider_name

        if not memory.archive.is_archived(position):
            return f"ERROR: Task {position} is not archived. Nothing to reveal."

        # Find the [ARCHIVED TASK N] placeholder
        tmap = find_task_map(messages)
        if position not in tmap:
            return f"ERROR: Archived placeholder for Task {position} not found in context."

        start, end, is_archived = tmap[position]
        if not is_archived:
            return f"ERROR: Task {position} is not archived in context."

        # Retrieve originals
        original_msgs = memory.archive.reveal(position)
        expanded_tokens = count_messages_tokens(original_msgs, provider)

        # Replace placeholder with originals
        messages[start:end] = original_msgs

        # Report result
        new_total = count_messages_tokens(messages, provider)
        max_tok = memory.config.max_context_tokens
        new_pct = round(new_total / max_tok * 100) if max_tok else 0

        return (
            f"Revealed Task {position}. "
            f"Context expanded by ~{expanded_tokens:,} tokens. "
            f"({new_total:,} / {max_tok:,} — {new_pct}%)"
        )

    def list_archived_context(self) -> str:
        """List all archived tasks with summaries and token savings.

        Use in long sessions to recall what was archived earlier.
        """
        memory   = self.runtime._memory
        provider = memory.config.provider_name
        all_archived = memory.archive.list_all()

        if not all_archived:
            return "No archived tasks."

        lines = ["Archived tasks:"]
        for pos in sorted(all_archived.keys()):
            saved_tokens = memory.archive.token_count(pos, provider)

            # Find summary from context
            tmap = find_task_map(self.runtime.messages)
            summary = "?"
            if pos in tmap:
                start, _, is_archived = tmap[pos]
                if is_archived:
                    content = self.runtime.messages[start].get("content", "")
                    # Extract summary after "[ARCHIVED TASK N]\n"
                    parts = content.split("\n", 1)
                    summary = parts[1] if len(parts) > 1 else content

            # Truncate summary for display
            if len(summary) > 150:
                summary = summary[:147] + "..."

            lines.append(
                f"  Task {pos} (~{saved_tokens:,} tokens saved): "
                f"\"{summary}\""
            )

        return "\n".join(lines)
