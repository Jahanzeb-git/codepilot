"""
File: context.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description:
Agent-driven context management tools for the CodePilot runtime.

Architectural Notes:
Exposes the temporary archive_context maintenance tool. It is registered only
during a runtime-triggered context-maintenance turn.

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
from ..engine.hooks import EventType

if TYPE_CHECKING:
    from ..engine.runtime import Runtime


class ContextTools:
    """Provides context-maintenance tools when the runtime enables them."""

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
        """Replace one or more completed tasks with their factual summaries.

        Use only during an internal context-maintenance turn. Summaries must
        preserve exact files, commands, decisions, outcomes, and unresolved
        issues needed by the active task. The active task cannot be archived.

        Positions are validated and processed independently — a bad position
        in a batch does NOT block the valid ones in the same call. The result
        always reports exactly what happened, per position, so you can
        correct only what failed on your next call.

        Args:
            position: Task position (int) or tuple of positions.
            summary:  Summary string or list of strings (one per position).
            task:     Alias for 'position'. Use either position= or task=.

        Cannot archive the currently active task.
        """
        messages = self.runtime.messages
        memory   = self.runtime._memory
        provider = memory.config.provider_name
        system_tokens = getattr(self.runtime, "_prompt_cache_system_tokens", 0)

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

        before_pressure = memory.measure_context(messages, system_tokens)

        tmap = find_task_map(messages)
        if not tmap:
            return "ERROR: No tasks found in context. Nothing was archived."

        active_pos = max(tmap.keys())

        # Validate each position independently — invalid ones are recorded
        # as failures and skipped, never block the valid ones in the batch.
        succeeded: List[Tuple[int, int]] = []   # (position, net_tokens_saved)
        failed:    List[Tuple[int, str]] = []   # (position, reason)
        plan: List[Tuple[int, str]] = []

        seen = set()
        for pos, summ in zip(positions, summaries):
            if pos in seen:
                failed.append((pos, "duplicate position in this call — ignored"))
                continue
            seen.add(pos)
            if pos not in tmap:
                failed.append((pos, "not found in context"))
                continue
            if pos == active_pos:
                failed.append((pos, "is the active task — cannot be archived"))
                continue
            _, _, is_archived = tmap[pos]
            if is_archived:
                failed.append((pos, "already archived"))
                continue
            plan.append((pos, summ))

        # Process from highest position first so message indices don't shift
        # for the remaining ones still to be processed in this call.
        for pos, summ in sorted(plan, key=lambda x: x[0], reverse=True):
            tmap = find_task_map(messages)  # re-map after each mutation
            if pos not in tmap:
                failed.append((pos, "position shifted out of range mid-call — retry it separately"))
                continue

            start, end, _ = tmap[pos]
            original_msgs = messages[start:end]
            memory.archive.archive(pos, original_msgs)

            gross_saved = count_messages_tokens(original_msgs, provider)
            archived_msg = {
                "role": "user",
                "content": f"{TAG_ARCHIVED_TASK} {pos}]\n{summ}",
            }
            messages[start:end] = [archived_msg]
            net_saved = gross_saved - count_messages_tokens([archived_msg], provider)
            succeeded.append((pos, net_saved))

        total_saved = sum(saved for _, saved in succeeded)
        new_total = count_messages_tokens(messages, provider)
        after_pressure = memory.measure_context(messages, system_tokens)

        lines: List[str] = []
        if succeeded:
            done = ", ".join(f"Task {p} (-{s:,} tok)" for p, s in sorted(succeeded))
            lines.append(f"Archived: {done}.")
        if failed:
            skipped = "; ".join(f"Task {p}: {reason}" for p, reason in sorted(failed))
            lines.append(f"Skipped (not archived): {skipped}.")
        lines.append(
            f"History: ~{new_total:,} tokens (saved ~{total_saved:,}). "
            f"Context Stress: {before_pressure.context_stress * 100:.1f}% -> "
            f"{after_pressure.context_stress * 100:.1f}%."
        )

        if succeeded:
            self.runtime.hooks.emit(
                EventType.CONTEXT_DROP,
                before_pct=round(before_pressure.context_stress * 100),
                after_pct=round(after_pressure.context_stress * 100),
                tokens_saved=total_saved,
                tasks_archived=[p for p, _ in succeeded],
            )
            # Real progress was made — return to normal work. The runtime
            # will remeasure stress before the next non-maintenance step.
            lines.append("The runtime will remeasure Context Stress before normal work resumes.")
            self.runtime._context_maintenance_completed = True
        else:
            # Nothing succeeded — stay in maintenance mode so the next step
            # can correct the failures above using this exact feedback,
            # instead of silently returning to work at unresolved pressure.
            lines.append("Nothing was archived. Still in context maintenance — correct the above and call archive_context() again.")

        return "\n".join(lines)
