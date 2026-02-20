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
        The answer is returned to your control block and also appears in [EXECUTION RESULT].
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
