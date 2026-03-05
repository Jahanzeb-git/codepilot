"""
codepilot.core.memory
~~~~~~~~~~~~~~~~~~~~~

Context memory manager for long-running agentic sessions.

Implements a three-zone progressive context compression system:

    Zone 0  — Active task: 100% raw context (non-negotiable).
    Zone 1  — Task summaries: each completed task collapsed to a single
              [TASK SUMMARY] message (~100-200 tokens) when pushed out
              by the next task submission.
    Zone 2  — Global summary: when accumulated task summaries exceed a
              token threshold, the older half is collapsed into a single
              [GLOBAL SUMMARY] message.

Summarization is task-aware: the summarizer receives the NEW task prompt
as context so it can bias retention toward information relevant to the
upcoming work.

Global State Memory is a structured JSON snapshot of the session's
cumulative actions (files created, modified, commands run, etc.) and
is injected into the system prompt every step.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..engine.provider import LLMProvider

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------
# Configuration dataclass
# -----------------------------------------------------------------------

class MemoryConfig:
    """
    Holds all tuning knobs for the context memory system.
    Populated from the `memory:` section in the AgentFile YAML.
    """

    def __init__(
        self,
        chars_per_token: float = 3.8,
        max_context_tokens: int = 120_000,
        min_task_tokens: int = 800,
        task_summary_max_tokens: int = 200,
        global_summary_threshold: float = 0.7,
        global_summary_max_tokens: int = 500,
    ):
        self.chars_per_token = chars_per_token
        self.max_context_tokens = max_context_tokens
        self.min_task_tokens = min_task_tokens
        self.task_summary_max_tokens = task_summary_max_tokens
        # Absolute token count at which global summarization triggers
        self.global_summary_threshold_tokens = int(
            max_context_tokens * global_summary_threshold
        )
        self.global_summary_max_tokens = global_summary_max_tokens


# -----------------------------------------------------------------------
# Tags
# -----------------------------------------------------------------------

TAG_USER_INPUT     = "[USER INPUT]"
TAG_TASK_SUMMARY   = "[TASK SUMMARY]"
TAG_GLOBAL_SUMMARY = "[GLOBAL SUMMARY]"


# -----------------------------------------------------------------------
# Token estimator
# -----------------------------------------------------------------------

def estimate_tokens(obj: Any, chars_per_token: float = 3.8) -> int:
    """
    Fast token estimate from character count.
    ±15% error — fine for threshold checks, not for billing.
    """
    if isinstance(obj, str):
        return int(len(obj) / chars_per_token)
    return int(len(json.dumps(obj, ensure_ascii=False)) / chars_per_token)


# -----------------------------------------------------------------------
# Task boundary detection
# -----------------------------------------------------------------------

def find_task_boundaries(messages: List[Dict]) -> List[int]:
    """
    Return the indices in `messages` where each task starts.
    A task starts at a message whose content begins with [USER INPUT].

    Returns:
        [i0, i1, i2, ...] — start indices of task 0, 1, 2, ...
        Ordered from oldest to newest.
    """
    boundaries = []
    for i, msg in enumerate(messages):
        content = msg.get("content", "")
        if content.startswith(TAG_USER_INPUT):
            boundaries.append(i)
    return boundaries


def extract_task_messages(
    messages: List[Dict],
    boundaries: List[int],
    task_index: int,
) -> List[Dict]:
    """
    Return the slice of messages belonging to a specific task
    (by its index in the boundaries list).
    """
    start = boundaries[task_index]
    if task_index + 1 < len(boundaries):
        end = boundaries[task_index + 1]
    else:
        end = len(messages)
    return messages[start:end]


# -----------------------------------------------------------------------
# Summarization prompts
# -----------------------------------------------------------------------

_TASK_SUMMARY_PROMPT = """\
You are a context compression assistant. Summarize the following completed \
agent task into a structured summary. The user is about to submit a new task \
(shown below) — bias your summary toward retaining information that may be \
relevant to the upcoming task.

## New task the user is about to submit:
{new_task}

## Completed task to summarize:
{task_content}

## Output format (respond ONLY with this JSON, no markdown fences):
{{
  "task": "<what the user asked for>",
  "files_read": ["<file:lines if relevant>"],
  "files_created": ["<file>"],
  "files_modified": ["<file:lines — what changed>"],
  "commands_run": ["<command — outcome>"],
  "result": "<concise outcome description>",
  "key_details": "<any critical details the next task might need>"
}}
"""

_GLOBAL_SUMMARY_PROMPT = """\
You are a context compression assistant. Summarize the following sequence of \
completed task summaries into a single cohesive global summary. Preserve key \
facts: files created/modified, critical decisions, unresolved issues.

## Task summaries to compress:
{summaries}

## Output format (respond ONLY with this JSON, no markdown fences):
{{
  "session_overview": "<what has been accomplished so far>",
  "files_created": ["<file>"],
  "files_modified": ["<file — what changed>"],
  "key_decisions": ["<decision>"],
  "unresolved": ["<open issue>"]
}}
"""


# -----------------------------------------------------------------------
# Global State Memory
# -----------------------------------------------------------------------

class GlobalStateMemory:
    """
    Structured snapshot of cumulative session state.
    Updated incrementally after each task summarization.
    Rendered into the system prompt every step.
    """

    def __init__(self):
        self._state: Dict[str, Any] = {
            "objective": "",
            "files_created": [],
            "files_modified": [],
            "commands_run": [],
            "open_issues": [],
            "key_decisions": [],
        }

    def update_from_task_summary(self, summary_json: Dict) -> None:
        """Merge a task summary's structured data into the global state."""
        if not summary_json:
            return

        # Update objective with latest task context
        task = summary_json.get("task", "")
        if task:
            self._state["objective"] = task

        # Merge lists — deduplicate by converting to set where appropriate
        for key in ("files_created", "files_modified", "commands_run"):
            new_items = summary_json.get(key, [])
            if isinstance(new_items, list):
                existing = self._state.get(key, [])
                for item in new_items:
                    if item and item not in existing:
                        existing.append(item)
                self._state[key] = existing

        # Carry forward unresolved issues
        unresolved = summary_json.get("unresolved", [])
        if isinstance(unresolved, list):
            for item in unresolved:
                if item and item not in self._state["open_issues"]:
                    self._state["open_issues"].append(item)

        # Key decisions from global summaries
        decisions = summary_json.get("key_decisions", [])
        if isinstance(decisions, list):
            for item in decisions:
                if item and item not in self._state["key_decisions"]:
                    self._state["key_decisions"].append(item)

    def update_from_global_summary(self, summary_json: Dict) -> None:
        """Merge a global summary's structured data into the global state."""
        self.update_from_task_summary(summary_json)

        overview = summary_json.get("session_overview", "")
        if overview:
            self._state["objective"] = overview

    def render(self) -> str:
        """Render the global state as formatted JSON for the system prompt."""
        # Only include non-empty fields
        rendered = {k: v for k, v in self._state.items() if v}
        if not rendered:
            return ""
        return json.dumps(rendered, indent=2, ensure_ascii=False)

    def to_dict(self) -> Dict:
        return dict(self._state)

    @classmethod
    def from_dict(cls, data: Dict) -> "GlobalStateMemory":
        instance = cls()
        if data:
            instance._state.update(data)
        return instance


# -----------------------------------------------------------------------
# Memory Manager
# -----------------------------------------------------------------------

class MemoryManager:
    """
    Orchestrates context compression for long-running sessions.

    Called at the start of each run() with the new task prompt.
    Modifies self.messages in-place to compress previous tasks
    according to the three-zone architecture.
    """

    def __init__(self, config: MemoryConfig, provider: "LLMProvider"):
        self.config = config
        self.provider = provider
        self.global_state = GlobalStateMemory()

    def process(
        self,
        messages: List[Dict],
        new_task: str,
    ) -> List[Dict]:
        """
        Called at the START of run(), BEFORE the new task is appended.

        1. Find task boundaries in messages
        2. Identify the most recent completed task (the one that just finished)
        3. If it exceeds min_task_tokens → summarize it (task-aware)
        4. Check total context → if over global threshold → global summarize
        5. Return the modified messages list

        Args:
            messages:  The current self.messages (will be modified in-place)
            new_task:  The user's new task prompt (for context-aware summarization)

        Returns:
            The (possibly modified) messages list.
        """
        boundaries = find_task_boundaries(messages)

        # Need at least one completed task to summarize
        if len(boundaries) < 1:
            return messages

        # The most recent task is the one that just completed.
        # We summarize it now, before appending the new task.
        last_task_idx = len(boundaries) - 1
        last_task_msgs = extract_task_messages(messages, boundaries, last_task_idx)

        # Check if it's already summarized
        if last_task_msgs and last_task_msgs[0].get("content", "").startswith(TAG_TASK_SUMMARY):
            # Already summarized — skip task-level, go to global check
            pass
        else:
            # Estimate tokens for the last completed task
            task_tokens = sum(
                estimate_tokens(m.get("content", ""), self.config.chars_per_token)
                for m in last_task_msgs
            )

            if task_tokens > self.config.min_task_tokens:
                # Summarize the last completed task
                messages = self._summarize_task(
                    messages, boundaries, last_task_idx, new_task
                )
            # else: task is small enough, keep as-is

        # --- Global summarization check ---
        total_tokens = sum(
            estimate_tokens(m.get("content", ""), self.config.chars_per_token)
            for m in messages
        )

        if total_tokens > self.config.global_summary_threshold_tokens:
            messages = self._global_summarize(messages)

        return messages

    # ------------------------------------------------------------------
    # Task-level summarization
    # ------------------------------------------------------------------

    def _summarize_task(
        self,
        messages: List[Dict],
        boundaries: List[int],
        task_index: int,
        new_task: str,
    ) -> List[Dict]:
        """
        Replace all messages of a single task with one [TASK SUMMARY] message.
        """
        start = boundaries[task_index]
        if task_index + 1 < len(boundaries):
            end = boundaries[task_index + 1]
        else:
            end = len(messages)

        task_msgs = messages[start:end]

        # Build the content string from all task messages
        task_content = "\n\n".join(
            f"[{m['role']}]\n{m['content']}" for m in task_msgs
        )

        # LLM summarization call
        prompt = _TASK_SUMMARY_PROMPT.format(
            new_task=new_task,
            task_content=task_content,
        )

        try:
            summary_text = self.provider.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=int(self.config.task_summary_max_tokens * self.config.chars_per_token),
            )

            # Parse the JSON response
            summary_json = self._parse_json_response(summary_text)

            # Update global state memory
            if summary_json:
                self.global_state.update_from_task_summary(summary_json)

            # Format the summary message
            summary_content = f"{TAG_TASK_SUMMARY}\n{summary_text}"

        except Exception as e:
            logger.warning(f"Task summarization failed: {e}. Keeping raw context.")
            return messages

        # Replace task messages with single summary
        summary_msg = {"role": "user", "content": summary_content}
        messages = messages[:start] + [summary_msg] + messages[end:]

        return messages

    # ------------------------------------------------------------------
    # Global summarization
    # ------------------------------------------------------------------

    def _global_summarize(self, messages: List[Dict]) -> List[Dict]:
        """
        When total tokens exceed the global threshold, find the midpoint
        and collapse everything in the older half into a global summary.
        """
        # Calculate cumulative token counts to find the midpoint
        total_tokens = sum(
            estimate_tokens(m.get("content", ""), self.config.chars_per_token)
            for m in messages
        )
        midpoint_target = total_tokens // 2

        # Walk from start, accumulating tokens until we cross midpoint
        cumulative = 0
        split_index = 0
        for i, msg in enumerate(messages):
            cumulative += estimate_tokens(
                msg.get("content", ""), self.config.chars_per_token
            )
            if cumulative >= midpoint_target:
                split_index = i + 1
                break

        if split_index <= 0:
            return messages

        # The older half to collapse
        older_messages = messages[:split_index]

        # Collect summaries from the older half
        summaries_text = "\n\n---\n\n".join(
            m.get("content", "") for m in older_messages
        )

        prompt = _GLOBAL_SUMMARY_PROMPT.format(summaries=summaries_text)

        try:
            summary_text = self.provider.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=int(self.config.global_summary_max_tokens * self.config.chars_per_token),
            )

            summary_json = self._parse_json_response(summary_text)
            if summary_json:
                self.global_state.update_from_global_summary(summary_json)

            global_msg = {
                "role": "user",
                "content": f"{TAG_GLOBAL_SUMMARY}\n{summary_text}",
            }

            # Replace older half with single global summary
            messages = [global_msg] + messages[split_index:]

        except Exception as e:
            logger.warning(f"Global summarization failed: {e}. Keeping context as-is.")

        return messages

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json_response(text: str) -> Optional[Dict]:
        """
        Parse a JSON response from the summarizer LLM.
        Handles cases where the LLM wraps the JSON in markdown fences.
        """
        cleaned = text.strip()

        # Strip markdown code fences if present
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            # Remove first and last lines (fences)
            if len(lines) >= 3:
                cleaned = "\n".join(lines[1:-1]).strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning(f"Could not parse summarizer JSON: {cleaned[:200]}")
            return None

    def get_state_json(self) -> str:
        """Return the global state memory rendered for the system prompt."""
        return self.global_state.render()

    def serialize_state(self) -> Dict:
        """Serialize the global state for persistence (e.g. in session data)."""
        return self.global_state.to_dict()

    def restore_state(self, data: Dict) -> None:
        """Restore global state from persisted data."""
        self.global_state = GlobalStateMemory.from_dict(data)
