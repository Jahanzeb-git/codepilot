"""
File: interaction.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description:
Human-in-the-loop interaction tools for the CodePilot agentic runtime.

Architectural Notes:
Provides the ask_user tool, which allows the agent to pause execution and
request clarification from the human operator. Answers are routed through
the HookSystem's ASK_USER event, enabling web apps to supply answers
programmatically (e.g., via WebSocket) without blocking the event loop.
Falls back to a blocking stdin prompt in CLI environments.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

from typing import TYPE_CHECKING

from ..engine.hooks import EventType

if TYPE_CHECKING:
    from ..engine.runtime import Runtime


class InteractionTools:

    def __init__(self, runtime: "Runtime"):
        self.runtime = runtime

    def ask_user(self, question: str) -> str:
        """
        Ask the human operator a question and return their answer as a string.
        Use this whenever a requirement is ambiguous enough that assuming would
        force an architectural decision on the user's behalf. Never guess — ask.
        Call it from codepilot.py. The answer is returned to that script and also
        appears in [EXECUTION RESULT] for the next step.
        """
        self.runtime.hooks.emit(EventType.TOOL_CALL, tool="ask_user", args={"question": question})

        # Fire ASK_USER for the answer path.
        # If any registered handler returns a non-empty string, use that as
        # the answer (e.g. a web app that feeds answers from a UI widget).
        # Otherwise fall back to a blocking stdin prompt.
        #
        # Previously this (incorrectly) fired PERMISSION_REQUEST to get an
        # answer, which conflated permission gating with Q&A: a handler that
        # returned True to approve shell commands would corrupt ask_user()
        # by giving it the boolean True as its answer.
        answer_from_hook = self.runtime.hooks.emit(EventType.ASK_USER, question=question)
        if isinstance(answer_from_hook, str) and answer_from_hook:
            answer = answer_from_hook
        else:
            try:
                answer = input(f"\n[Agent asks]: {question}\n[You]: ").strip()
            except EOFError:
                answer = ""

        result = f"[ask_user] Q: {question}\n[ask_user] A: {answer}"
        self.runtime._append_execution(result)
        self.runtime.hooks.emit(EventType.TOOL_RESULT, tool="ask_user", result=result)
        return answer
