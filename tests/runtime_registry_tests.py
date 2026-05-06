from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codepilot.core.block_parser import BlockParser, CodeBlock
from codepilot.engine.hooks import EventType
from codepilot.engine.runtime import AsyncRuntime
from codepilot.tools.filesystem import FilesystemTools


class _DummyProvider:
    async def chat(self, *args, **kwargs) -> str:
        return ""


class _ScriptedProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def chat(self, *args, **kwargs) -> str:
        if self._responses:
            return self._responses.pop(0)
        return "Standing by."


class _FilesystemRuntime:
    def __init__(self, work_dir: Path) -> None:
        self.config = SimpleNamespace(
            runtime=SimpleNamespace(work_dir=str(work_dir), unsafe_mode=False),
            tools=[],
        )
        self.hooks = SimpleNamespace(emit=lambda *args, **kwargs: None)
        self._payload_queue: list[CodeBlock] = []
        self._execution_buffer: list[str] = []
        self._step_edited_files: set[str] = set()
        self._step_write_count = 0

    def pop_next_payload_block(self):
        if self._payload_queue:
            return self._payload_queue.pop(0)
        return None

    def _append_execution(self, text: str) -> None:
        self._execution_buffer.append(text)

    def _tool_config(self, tool_name: str) -> dict:
        return {}

    def enqueue_payload(self, path: str, content: str = "replacement\n") -> None:
        self._payload_queue.append(
            CodeBlock(language="python", content=content, index=0, filename=path)
        )


class RuntimeRegistryTests(unittest.TestCase):
    def test_context_tools_are_registered_without_semantic_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent_file = root / "agent.yaml"
            workspace = root / "workspace"
            workspace.mkdir()
            agent_file.write_text(
                f"""
agent:
  name: TestAgent
  role: Test runtime registry.
  model:
    provider: openai
    name: test-model
    api_key_env: TEST_API_KEY
  runtime:
    work_dir: {workspace}
""",
                encoding="utf-8",
            )

            with (
                patch("codepilot.engine.runtime.get_provider", return_value=_DummyProvider()),
                patch("codepilot.tools.terminal.TerminalManager.start_default_terminal"),
            ):
                runtime = AsyncRuntime(str(agent_file))

            registered = runtime.registry.as_sandbox_dict()
            self.assertIn("archive_context", registered)
            self.assertIn("reveal_context", registered)
            self.assertIn("list_archived_context", registered)
            self.assertNotIn("semantic_search", registered)

    def test_context_tools_remain_registered_when_semantic_search_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent_file = root / "agent.yaml"
            workspace = root / "workspace"
            workspace.mkdir()
            agent_file.write_text(
                f"""
agent:
  name: TestAgent
  role: Test runtime registry.
  model:
    provider: openai
    name: test-model
    api_key_env: TEST_API_KEY
  runtime:
    work_dir: {workspace}
  tools:
    - name: semantic_search
      enabled: true
      config:
        provider: synthetic
""",
                encoding="utf-8",
            )

            with (
                patch("codepilot.engine.runtime.get_provider", return_value=_DummyProvider()),
                patch("codepilot.tools.terminal.TerminalManager.start_default_terminal"),
                patch("codepilot.tools.semantic.SemanticTools.validate_config"),
            ):
                runtime = AsyncRuntime(str(agent_file))

            registered = runtime.registry.as_sandbox_dict()
            self.assertIn("semantic_search", registered)
            self.assertIn("archive_context", registered)
            self.assertIn("reveal_context", registered)
            self.assertIn("list_archived_context", registered)

    def test_parser_errors_are_returned_to_the_agent_for_recovery(self) -> None:
        malformed_response = """I'll write the file now.

```codepilot
write_file("hello.py", mode="w")
```
"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent_file = root / "agent.yaml"
            workspace = root / "workspace"
            workspace.mkdir()
            agent_file.write_text(
                f"""
agent:
  name: TestAgent
  role: Test runtime parser recovery.
  model:
    provider: openai
    name: test-model
    api_key_env: TEST_API_KEY
  runtime:
    work_dir: {workspace}
    max_steps: 2
""",
                encoding="utf-8",
            )
            provider = _ScriptedProvider([malformed_response, "I will correct that next."])

            with (
                patch("codepilot.engine.runtime.get_provider", return_value=provider),
                patch("codepilot.tools.terminal.TerminalManager.start_default_terminal"),
                patch("codepilot.tools.terminal.TerminalManager.ensure_default_terminal"),
            ):
                runtime = AsyncRuntime(str(agent_file))
                for event_type in EventType:
                    runtime.hooks.clear(event_type)
                summary = asyncio.run(runtime.run("Create hello.py"))

            self.assertIsNone(summary)
            self.assertFalse((workspace / "hello.py").exists())

            parser_results = [
                msg["content"]
                for msg in runtime.messages
                if msg["role"] == "user" and msg["content"].startswith("[EXECUTION RESULT]\nPARSER ERROR:")
            ]
            self.assertEqual(1, len(parser_results))
            parser_result = parser_results[0]
            self.assertIn("No tool code from the previous response ran", parser_result)
            self.assertIn("Every Payload Block must include a filename= annotation", parser_result)
            self.assertIn("Payload count mismatch", parser_result)

    def test_parser_identifies_unannotated_payload_blocks(self) -> None:
        response = """Trying a write.

```codepilot
write_file("hello.py", mode="w")
```

```python
print("hello")
```
"""
        with self.assertRaisesRegex(ValueError, "missing filename="):
            BlockParser.split(response)

    def test_parser_rejects_inline_content_arguments(self) -> None:
        response = """Trying a write.

```codepilot
write_file("hello.py", mode="w", content="print('hello')")
```
"""
        with self.assertRaisesRegex(ValueError, "never accepts file content"):
            BlockParser.split(response)

    def test_parser_reports_invalid_literal_write_mode_as_root_cause(self) -> None:
        response = """Trying a multi edit.

```codepilot
write_file("hello.py", mode="multi-edit", edits=[(1, 1), (2, 2)])
```

```python filename=hello.py
one
```

```python filename=hello.py
two
```
"""
        with self.assertRaisesRegex(ValueError, "invalid mode 'multi-edit'"):
            BlockParser.split(response)

    def test_write_file_rejects_invalid_mode_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            runtime = _FilesystemRuntime(work_dir)
            runtime.enqueue_payload("hello.py", "print('hello')\n")
            tool = FilesystemTools(runtime)

            tool.write_file("hello.py", mode="append")

            self.assertFalse((work_dir / "hello.py").exists())
            self.assertIn("Unknown mode 'append'", runtime._execution_buffer[-1])
            self.assertIn("mode='a' for append", runtime._execution_buffer[-1])
            self.assertIn("No file was changed", runtime._execution_buffer[-1])

    def test_write_file_rejects_out_of_range_edit_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            target = work_dir / "hello.py"
            original = "line 1\nline 2\n"
            target.write_text(original, encoding="utf-8")
            runtime = _FilesystemRuntime(work_dir)
            runtime.enqueue_payload("hello.py", "replacement\n")
            tool = FilesystemTools(runtime)

            tool.write_file("hello.py", mode="edit", start_line=3, end_line=3)

            self.assertEqual(original, target.read_text(encoding="utf-8"))
            self.assertIn("outside 'hello.py'", runtime._execution_buffer[-1])
            self.assertIn("Call read_file('hello.py')", runtime._execution_buffer[-1])

    def test_read_file_reports_missing_file_as_tool_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            runtime = _FilesystemRuntime(work_dir)
            tool = FilesystemTools(runtime)

            returned = tool.read_file("missing.py")

            self.assertIn("[read_file] ERROR:", runtime._execution_buffer[-1])
            self.assertIn("'missing.py' not found", runtime._execution_buffer[-1])
            self.assertEqual(runtime._execution_buffer[-1], returned)

    def test_read_file_rejects_non_positive_start_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            target = work_dir / "hello.py"
            target.write_text("line 1\n", encoding="utf-8")
            runtime = _FilesystemRuntime(work_dir)
            tool = FilesystemTools(runtime)

            returned = tool.read_file("hello.py", start_line=0)

            self.assertIn("uses 1-indexed lines", returned)
            self.assertIn("[read_file] ERROR:", returned)

    def test_read_file_rejects_invalid_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            target = work_dir / "hello.py"
            target.write_text("line 1\nline 2\n", encoding="utf-8")
            runtime = _FilesystemRuntime(work_dir)
            tool = FilesystemTools(runtime)

            returned = tool.read_file("hello.py", start_line=2, end_line=1)

            self.assertIn("Invalid range", returned)
            self.assertIn("[read_file] ERROR:", returned)

    def test_read_file_rejects_start_line_beyond_eof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            target = work_dir / "hello.py"
            target.write_text("line 1\nline 2\n", encoding="utf-8")
            runtime = _FilesystemRuntime(work_dir)
            tool = FilesystemTools(runtime)

            returned = tool.read_file("hello.py", start_line=5)

            self.assertIn("file has only 2 lines", returned)
            self.assertIn("[read_file] ERROR:", returned)

    def test_read_file_rejects_non_integer_end_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            target = work_dir / "hello.py"
            target.write_text("line 1\nline 2\n", encoding="utf-8")
            runtime = _FilesystemRuntime(work_dir)
            tool = FilesystemTools(runtime)

            returned = tool.read_file("hello.py", start_line=1, end_line="two")

            self.assertIn("end_line must be an integer", returned)
            self.assertIn("[read_file] ERROR:", returned)

    def test_control_block_syntax_error_identifies_codepilot_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent_file = root / "agent.yaml"
            workspace = root / "workspace"
            workspace.mkdir()
            agent_file.write_text(
                f"""
agent:
  name: TestAgent
  role: Test runtime execution errors.
  model:
    provider: openai
    name: test-model
    api_key_env: TEST_API_KEY
  runtime:
    work_dir: {workspace}
""",
                encoding="utf-8",
            )

            with (
                patch("codepilot.engine.runtime.get_provider", return_value=_DummyProvider()),
                patch("codepilot.tools.terminal.TerminalManager.start_default_terminal"),
            ):
                runtime = AsyncRuntime(str(agent_file))
                for event_type in EventType:
                    runtime.hooks.clear(event_type)
                runtime._execute_sync("if True\n    print('never runs')")

            result = runtime._execution_buffer[-1]
            self.assertIn("generated ```codepilot Control Block, line 1", result)
            self.assertIn("SyntaxError", result)
            self.assertIn("No statements in the Control Block ran", result)
            self.assertIn('File "<codepilot-control-block>", line 1', result)

    def test_control_block_runtime_error_identifies_partial_execution_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent_file = root / "agent.yaml"
            workspace = root / "workspace"
            workspace.mkdir()
            agent_file.write_text(
                f"""
agent:
  name: TestAgent
  role: Test runtime execution errors.
  model:
    provider: openai
    name: test-model
    api_key_env: TEST_API_KEY
  runtime:
    work_dir: {workspace}
""",
                encoding="utf-8",
            )

            with (
                patch("codepilot.engine.runtime.get_provider", return_value=_DummyProvider()),
                patch("codepilot.tools.terminal.TerminalManager.start_default_terminal"),
            ):
                runtime = AsyncRuntime(str(agent_file))
                for event_type in EventType:
                    runtime.hooks.clear(event_type)
                runtime._execute_sync("print('before')\nmissing_tool()\nprint('after')")

            combined = "\n\n".join(runtime._execution_buffer)
            self.assertIn("before", combined)
            self.assertIn("generated ```codepilot Control Block, line 2", combined)
            self.assertIn("NameError", combined)
            self.assertIn("Statements before the failing line may already have run", combined)
            self.assertIn('File "<codepilot-control-block>", line 2', combined)


if __name__ == "__main__":
    unittest.main()
