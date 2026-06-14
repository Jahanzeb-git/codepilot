"""
File: context.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description:
Agent-driven context management tools for the CodePilot runtime.

Architectural Notes:
Exposes three tools to the agent sandbox: archive_context, reveal_context,
and list_archived_context. These allow the agent to actively manage its own
context window during long-running tasks by replacing completed task messages
with summaries, while preserving the originals in a ContextArchive for
later retrieval without disrupting the LLM's cache state.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
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

        Context management guidelines:
        - Check context stress each step as a pressure signal.
        - Archive at most once per task, never repeatedly. Never as an
          opening move.
        - Prioritize old completed tasks with no bearing on current work.
        - Immediate preceding tasks likely share context — don't archive
          speculatively.
        - Low stress: skip archiving entirely. High stress: act, but you
          judge what's load-bearing.
        - Summaries are your ONLY memory of that task (unless you call
          reveal_context). Pack densely: exact files, commands, decisions,
          errors, outcomes. Reading it later must tell you exactly what
          happened, where, and why — so you can judge whether revealing
          is needed. Never be vague.

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

        from ..core.memory import count_tokens
        sys_tokens = 0
        sys_prompt = self.runtime._last_system_prompt
        if sys_prompt:
            sys_tokens = count_tokens(sys_prompt.full, provider)

        old_total = count_messages_tokens(messages, provider) + sys_tokens

        # Process from highest position first so message indices don't shift
        total_saved = 0
        for pos, summ in sorted(
            zip(positions, summaries), key=lambda x: x[0], reverse=True
        ):
            start, end, _ = tmap[pos]
            original_msgs = messages[start:end]

            # Store originals in archive
            memory.archive.archive(pos, original_msgs)

            # Count gross tokens saved
            gross_saved = count_messages_tokens(original_msgs, provider)

            # Replace with single archived message
            archived_msg = {
                "role": "user",
                "content": f"{TAG_ARCHIVED_TASK} {pos}]\n{summ}",
            }
            messages[start:end] = [archived_msg]

            # Calculate net savings
            net_saved = gross_saved - count_messages_tokens([archived_msg], provider)
            total_saved += net_saved

            # Rebuild task map after modification for next iteration
            tmap = find_task_map(messages)

        # Report result
        new_total = count_messages_tokens(messages, provider) + sys_tokens
        max_tok = memory.config.max_context_tokens
        new_pct = round(new_total / max_tok * 100) if max_tok else 0

        before_pct = round(old_total / max_tok * 100) if max_tok else 0

        archived_str = ", ".join(str(p) for p in sorted(positions))
        result_msg = (
            f"Archived Task(s) {archived_str}. "
            f"Context reduced by ~{total_saved:,} tokens. "
            f"({new_total:,} / {max_tok:,} — {new_pct}%)"
        )

        # Emit CONTEXT_DROP event so CLI/UI can render a progress message
        from ..engine.hooks import EventType
        self.runtime.hooks.emit(
            EventType.CONTEXT_DROP,
            before_pct=before_pct,
            after_pct=new_pct,
            tokens_saved=total_saved,
            tasks_archived=sorted(positions),
        )

        return result_msg

    def reveal_context(self, position: int) -> str:
        """Read a previously archived task's full context as text.

        Returns the original detailed messages as a string so you can
        read them exactly as they occurred, without forcefully injecting
        them back into the historical timeline (which would break context caching).

        Args:
            position: Task position (int) to reveal.
        """
        memory = self.runtime._memory

        if not memory.archive.is_archived(position):
            return f"ERROR: Task {position} is not archived. Nothing to reveal."

        # Retrieve a copy of the originals
        original_msgs = memory.archive.reveal(position)
        
        # Build a readable text output
        lines = [f"=== DETAILED HISTORY FOR TASK {position} ==="]
        for msg in original_msgs:
            role = str(msg.get("role", "")).upper()
            content = str(msg.get("content", ""))
            
            lines.append(f"\n[{role}]")
            lines.append(content)
            
        lines.append(f"\n=== END OF TASK {position} HISTORY ===")
        return "\n".join(lines)

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
