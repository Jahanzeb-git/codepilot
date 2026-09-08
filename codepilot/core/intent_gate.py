"""
File: intent_gate.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-09-04

Description:
    Action-intent detector for CodePilot's conflict-marker protocol.

Why this exists
---------------
The runtime treats "``parse_blocks`` found zero operations AND zero parse
errors" as a *conversational reply* and ends the step (see runtime.py, the
``if not operations and not parse_errors:`` branch).

That inference is wrong in one important case: when the model *tried to act*
but expressed the action in a shape the conflict-marker parser cannot see —
typically because a smaller / heavily-SFT'd model (e.g. kimi-k2.7) fell back
to its native tool-calling distribution instead of emitting our markers:

    <tool_call>{"name": "view_file", ...}</tool_call>
    ```python
    execute("main", "pytest -q")
    ```

To the parser this is "no blocks" → the step silently ends and the intended
action is **dropped with zero signal**.  For an embeddable runtime that is the
worst failure mode: the host application believes the agent chose not to act.

This module converts that *silent drop* into a *loud, recoverable event*: the
runtime feeds a corrective [EXECUTION RESULT] back to the model and lets it
re-emit the action in the correct protocol, instead of ending the turn.

Design contract
---------------
``detect_action_intent`` is called ONLY on the zero-ops / zero-errors path, so
a positive result always means a silently-dropped action.  It is deliberately
**high-precision, not high-recall**: a false positive costs at most a couple of
bounded corrective retries, but it must NEVER fire on a genuine conversational
answer (that would loop a chat reply).  We therefore key only on strong,
unambiguous "the model is trying to act" signals.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Signal 1 — foreign tool-call envelopes
# ---------------------------------------------------------------------------
# On-the-wire syntaxes the major open models emit when they revert to their
# SFT tool-calling distribution.  Each vendor invented its own delimiters
# (this is exactly why inference servers ship a per-family tool-call parser),
# so we match the union of the common ones.  These strings are effectively
# never present in ordinary prose, which keeps precision high.
_FOREIGN_ENVELOPE_RE = re.compile(
    r"""(?xi)
    (
        <\s*tool_call\b            # Hermes / Qwen / many others: <tool_call>
      | </\s*tool_call\s*>
      | <\s*tool▁call\b            # DeepSeek U+2581 variant
      | <\s*function_call\b        # generic
      | </\s*function_call\s*>
      | <\s*invoke\b               # XML "invoke name=..."
      | \[TOOL_CALLS\]             # Mistral / xLAM
      | <\|\s*tool[_▁]calls[_▁]begin\s*\|>   # DeepSeek-V3 style sentinels
      | <\|\s*tool[_▁]call[_▁]begin\s*\|>
      | <start_function_call\b     # FunctionGemma
      | <\s*tool_use\b             # Anthropic-flavoured leakage into text
      | \bfunctions\.[A-Za-z_]\w*\s*\(   # OpenAI "functions.foo(" namespace leak
    )
    """,
)

# ---------------------------------------------------------------------------
# Signal 2 — orphan conflict markers (broken / half-formed protocol)
# ---------------------------------------------------------------------------
# We only reach this module when parse_blocks() assembled NOTHING.  So a stray
# opening/closing marker means the model started our protocol and mangled it.
# We intentionally do NOT trigger on a bare "=======" line: that sequence shows
# up in markdown rules and prose, whereas 4+ '<' or '>' at line start does not.
_ORPHAN_MARKER_RE = re.compile(r"(?m)^[ \t]*(?:<{4,9}|>{4,9})[ \t]*[A-Za-z]*[ \t]*$")

# ---------------------------------------------------------------------------
# Signal 3 — an action fence carrying a real tool call
# ---------------------------------------------------------------------------
# The other common fallback is wrapping the action in a fenced code block:
#     ```python            ```tool            ```codepilot
#     execute("main", ...)  view_file("x")    write_file(...)
#     ```                   ```               ```
# We fire only when BOTH the fence language is action-shaped AND the body calls
# a *registered tool by name* — this avoids firing on illustrative snippets that
# merely quote a tool name in prose.
_ACTION_FENCE_RE = re.compile(
    r"```[ \t]*(python|py|json|tool|tool_code|codepilot)\b(.*?)```",
    re.IGNORECASE | re.DOTALL,
)


def _fence_calls_tool(body: str, tool_names: frozenset) -> Optional[str]:
    """Return the first registered tool name that is *called* inside *body*."""
    for name in tool_names:
        # word-boundary + optional whitespace + "(" → a call, not a mention
        if re.search(rf"\b{re.escape(name)}\s*\(", body):
            return name
    return None


def detect_action_intent(
    text: str,
    tool_names: Iterable[str] = (),
) -> Optional[str]:
    """
    Decide whether *text* is a *silently dropped action* rather than a
    conversational reply.

    Parameters
    ----------
    text : the model's raw generation for this step (already known to contain
           zero parseable conflict-marker blocks and zero parse errors).
    tool_names : names of the tools currently registered for the agent.  Used
           only by Signal 3 to distinguish a real tool call in a code fence
           from prose that happens to mention a tool.

    Returns
    -------
    A short human-readable reason string naming the detected fallback (suitable
    for feeding back to the model), or ``None`` when *text* is a genuine
    conversational reply and the step should end normally.

    Precision note
    --------------
    This is intentionally conservative.  Only the three strong signals above
    fire.  A plain natural-language answer — even one that names a tool in
    prose — returns ``None``.
    """
    if not text or not text.strip():
        return None

    m = _FOREIGN_ENVELOPE_RE.search(text)
    if m:
        snippet = m.group(0).strip()
        return f"foreign tool-call syntax {snippet!r} (native SFT format, not a conflict-marker block)"

    if _ORPHAN_MARKER_RE.search(text):
        return "an orphan/half-formed conflict marker (a block was started but could not be parsed)"

    names = frozenset(n for n in tool_names if n)
    if names:
        for fence in _ACTION_FENCE_RE.finditer(text):
            lang, body = fence.group(1), fence.group(2)
            called = _fence_calls_tool(body, names)
            if called:
                return f"a tool call {called!r} wrapped in a ```{lang.lower()} code fence instead of a conflict-marker block"

    return None


def format_intent_gate_feedback(reason: str) -> str:
    """
    Model-facing correction appended as an [EXECUTION RESULT] when the gate
    fires.  Tells the model its action was NOT executed and how to re-emit it.
    """
    return (
        f"[PROTOCOL] Your response looks like an attempted action ({reason}), "
        "but it was NOT in the required conflict-marker format, so NOTHING was "
        "executed and no files changed.\n"
        "To take an action you MUST emit a block: a path line on its own, then "
        "'<<<<<<< SEARCH', the exact current content, '=======', the new content, "
        "then '>>>>>>> REPLACE'. For codepilot.py, leave the SEARCH section empty "
        "(full override) and put your tool calls / Python in the REPLACE section.\n"
        "Re-emit your intended action now as proper block(s). Do not describe it — emit it."
    )
