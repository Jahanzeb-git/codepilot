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
        self.runtime.hooks.emit(EventType.ASK_USER, question=question)

        answer_from_hook = self.runtime.hooks.emit(
            EventType.PERMISSION_REQUEST, tool="ask_user", description=question,
        )
        if isinstance(answer_from_hook, str):
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
