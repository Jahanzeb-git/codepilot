from pathlib import Path
from codepilot.engine.hooks import EventType

class TranscriptLogger:
    def __init__(self, task_id: str, report_dir: Path):
        self.log_path = report_dir / f"{task_id}_transcript.md"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # Clear previous run
        self.log_path.write_text(f"# Transcript for {task_id}\n\n", encoding="utf-8")
        self.step_counter = 0

    def attach(self, hooks):
        hooks.register(EventType.STEP, self._on_step)
        hooks.register(EventType.LLM_RESPONSE, self._on_llm_response)
        hooks.register(EventType.TOOL_RESULT, self._on_tool_result)
        hooks.register(EventType.RUNTIME_ERROR, self._on_runtime_error)
        hooks.register(EventType.FINISH, self._on_finish)

    def _append(self, content: str):
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(content + "\n\n")

    def _on_step(self, step: int, **_):
        self.step_counter = step
        self._append(f"## Step {step}")

    def _on_llm_response(self, response: str, **_):
        self._append(f"### Agent Response\n```\n{response}\n```")

    def _on_tool_result(self, tool: str, result: str, **_):
        self._append(f"### Tool Result ({tool})\n```\n{result}\n```")

    def _on_runtime_error(self, message: str, **_):
        self._append(f"### Runtime Error\n```\n{message}\n```")

    def _on_finish(self, **_):
        self._append(f"## Task Finished Cleanly")
