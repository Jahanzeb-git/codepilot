"""Provider failures must end the turn after one reported error.

Regression: the runtime used to `continue` after an LLM exception, retrying the
same failing request up to ``max_steps`` and emitting the identical
RUNTIME_ERROR (and therefore the same UI error card) once per step.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

from codepilot.engine.hooks import EventType
from codepilot.engine.runtime import AsyncRuntime


class _FailingProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_tools(self, **kwargs):
        self.calls += 1
        raise RuntimeError("RateLimitError: free allowance is used up")


def test_provider_error_is_reported_once_and_stops_the_loop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        workspace.mkdir()
        agent_file = root / "agent.yaml"
        agent_file.write_text(
            f"""
agent:
  name: TestAgent
  role: Provider error regression.
  model:
    provider: openai
    name: test-model
    api_key_env: TEST_API_KEY
  runtime:
    work_dir: {workspace}
    max_steps: 8
  memory:
    max_context_tokens: 128000
""",
            encoding="utf-8",
        )
        provider = _FailingProvider()

        with (
            patch("codepilot.engine.runtime.get_provider", return_value=provider),
            patch("codepilot.tools.terminal.TerminalManager.start_default_terminal"),
            patch("codepilot.tools.terminal.TerminalManager.ensure_default_terminal"),
        ):
            runtime = AsyncRuntime(str(agent_file))
            for event_type in EventType:
                runtime.hooks.clear(event_type)

            errors: list[str] = []
            finishes: list[str] = []
            runtime.hooks.register(EventType.RUNTIME_ERROR, lambda error, **_: errors.append(error))
            runtime.hooks.register(EventType.FINISH, lambda summary="", **_: finishes.append(summary))

            result = asyncio.run(runtime.run("Do something"))

        assert result is None
        assert provider.calls == 1, "the failing provider was retried per step"
        assert len(errors) == 1, f"expected one error event, got {len(errors)}"
        assert "RateLimitError" in errors[0]
        assert len(finishes) == 1, "the turn did not finish cleanly after the error"
