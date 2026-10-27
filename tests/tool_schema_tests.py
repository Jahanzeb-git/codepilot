"""
Tests for the native tool-schema contract.

These are tracked tests (the ``test_*.py`` files in this directory are
gitignored scratch tests). Run with:

    python3 -m pytest tests/tool_schema_tests.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from codepilot.core.tool_protocol import build_anthropic_tools, build_openai_tools  # noqa: E402
from codepilot.engine.runtime import AsyncRuntime  # noqa: E402
from codepilot.tools.context import ContextTools  # noqa: E402
from codepilot.tools.filesystem import FilesystemTools  # noqa: E402
from codepilot.tools.interaction import InteractionTools  # noqa: E402
from codepilot.tools.mcp_tool import MCPTools  # noqa: E402
from codepilot.tools.search import SearchTools  # noqa: E402
from codepilot.tools.semantic import SemanticTools  # noqa: E402
from codepilot.tools.subagent import SubAgentTools  # noqa: E402
from codepilot.tools.terminal import TerminalManager  # noqa: E402


BUILTIN_TOOLS = {
    "view_file": FilesystemTools.view_file,
    "write_file": FilesystemTools.write_file,
    "edit_file": FilesystemTools.edit_file,
    "find_and_replace_many": FilesystemTools.find_and_replace_many,
    "execute": TerminalManager.execute,
    "read_output": TerminalManager.read_output,
    "send_input": TerminalManager.send_input,
    "terminate_terminal": TerminalManager.terminate_terminal,
    "ask_user": InteractionTools.ask_user,
    "find": SearchTools.find,
    "semantic_search": SemanticTools.semantic_search,
    "spawn_subagent": SubAgentTools.spawn_subagent,
    "await_subagent": SubAgentTools.await_subagent,
    "archive_context": ContextTools.archive_context,
    "mcp": MCPTools.mcp,
    "retry_block": AsyncRuntime.retry_block,
}


def _by_name() -> dict:
    return {
        tool["function"]["name"]: tool["function"]
        for tool in build_openai_tools(BUILTIN_TOOLS)
    }


def test_every_builtin_tool_has_a_complete_description():
    for name, fn in _by_name().items():
        assert len(fn["description"]) >= 100, f"{name} description is too short"
        assert fn["description"].strip() == fn["description"], name


def test_every_parameter_has_a_description():
    for name, fn in _by_name().items():
        for param_name, schema in fn["parameters"]["properties"].items():
            assert schema.get("description"), f"{name}.{param_name} lacks a description"


def test_optional_and_array_parameter_types_are_inferred():
    tools = _by_name()

    write_props = tools["write_file"]["parameters"]["properties"]
    assert write_props["cache_id"]["type"] == "integer"

    sub_props = tools["spawn_subagent"]["parameters"]["properties"]
    assert sub_props["tools"]["type"] == "array"
    assert sub_props["tools"]["items"] == {"type": "string"}

    find_props = tools["find"]["parameters"]["properties"]
    assert find_props["target"]["anyOf"] == [
        {"type": "string"},
        {"type": "array", "items": {"type": "string"}},
    ]

    archive_props = tools["archive_context"]["parameters"]["properties"]
    assert archive_props["position"]["anyOf"] == [
        {"type": "integer"},
        {"type": "array", "items": {"type": "integer"}},
    ]
    assert archive_props["summary"]["anyOf"] == [
        {"type": "string"},
        {"type": "array", "items": {"type": "string"}},
    ]

    mcp_props = tools["mcp"]["parameters"]["properties"]
    assert mcp_props["arguments"]["type"] == "object"


def test_enums_and_required_parameters_are_declared():
    tools = _by_name()

    assert tools["execute"]["parameters"]["required"] == ["command"]
    assert tools["execute"]["parameters"]["properties"]["shell"]["enum"] == [
        "bash", "powershell", "cmd",
    ]
    assert tools["find"]["parameters"]["properties"]["scope"]["enum"] == [
        "file", "files", "codebase",
    ]
    assert tools["semantic_search"]["parameters"]["properties"]["mode"]["enum"] == [
        "search", "trace_callers", "trace_callees", "trace_graph",
    ]
    assert "default" not in tools["execute"]["parameters"]["properties"]["command"]


def test_custom_tool_keeps_full_docstring_and_infers_types():
    def custom_tool(path: str, count: Optional[int] = None, tags: List[str] = None) -> str:
        """First paragraph.

        Second paragraph explains returns, side effects, and failure modes.

        Args:
            path: File path to process.
            count: Optional repeat count.
            tags: Optional labels.
        """
        return path

    fn = build_openai_tools({"custom_tool": custom_tool})[0]["function"]

    assert "First paragraph." in fn["description"]
    assert "Second paragraph explains returns" in fn["description"]
    assert "Args:" not in fn["description"]

    props = fn["parameters"]["properties"]
    assert props["path"]["type"] == "string"
    assert props["count"]["type"] == "integer"
    assert props["tags"]["type"] == "array"
    assert props["tags"]["items"] == {"type": "string"}
    assert props["path"]["description"] == "File path to process."
    assert props["count"]["description"] == "Optional repeat count."
    assert props["tags"]["description"] == "Optional labels."


def test_anthropic_conversion_preserves_parameter_contracts():
    openai_tools = build_openai_tools(BUILTIN_TOOLS)
    anthropic_tools = build_anthropic_tools(BUILTIN_TOOLS)

    assert len(anthropic_tools) == len(openai_tools)

    by_name = {tool["name"]: tool for tool in anthropic_tools}
    edit = by_name["edit_file"]
    assert edit["description"] == next(
        tool["function"]["description"]
        for tool in openai_tools
        if tool["function"]["name"] == "edit_file"
    )
    assert edit["input_schema"]["properties"]["old_str"]["description"]
