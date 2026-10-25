"""
File: tool_protocol.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-10-24

Native function-calling protocol for the CodePilot agentic runtime.

Converts the tool registry into provider-native JSON schemas and normalises
tool-call responses from every supported provider back into a unified
ToolCall / ToolResult model. This module is the translation layer between
the clean internal tool API and the wire format each LLM provider expects.

Supported providers: OpenAI, Anthropic, Alibaba/Qwen, DeepSeek,
ExperientialLabs (OpenAI-compatible).

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    """A single tool invocation parsed from an LLM response."""

    #: Provider-assigned call ID (used to match results back to calls).
    call_id: str
    #: Registered tool name.
    name: str
    #: Parsed arguments dict. May be empty for no-arg tools.
    arguments: Dict[str, Any] = field(default_factory=dict)
    #: Raw argument string before parsing — preserved for error reporting.
    raw_arguments: str = ""
    #: True when argument JSON was malformed but successfully auto-repaired.
    was_repaired: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """
        Return the OpenAI wire shape stored in conversation history.

        Arguments are re-serialized from the parsed dict instead of reusing
        ``raw_arguments``: a repaired (previously malformed) argument string
        must never be sent back to the provider verbatim.
        """
        return {
            "id": self.call_id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments),
            },
        }


@dataclass
class ToolResult:
    """The outcome of executing a single tool call."""

    #: Must match the ToolCall.call_id this result answers.
    call_id: str
    #: Registered tool name.
    name: str
    #: String content returned to the model.
    content: str
    #: True when the call raised an exception or the tool reported an error.
    is_error: bool = False


# ---------------------------------------------------------------------------
# Argument parsing — with auto-repair
# ---------------------------------------------------------------------------

def _repair_json(raw: str) -> str:
    """
    Best-effort repair of common JSON argument encoding mistakes made by LLMs.

    Handles:
      - Unescaped literal newlines inside string values.
      - Unescaped double quotes inside string values.
      - Trailing commas before closing braces / brackets.
    """
    # Replace literal (unescaped) newlines inside JSON strings with \\n.
    # This is the most common cause of json.loads() failures in tool calls.
    repaired = re.sub(
        r'(?<!\\)\n',
        r'\\n',
        raw,
    )
    # Remove trailing commas before } or ] (invalid JSON, valid in JS).
    repaired = re.sub(r',\s*([}\]])', r'\1', repaired)
    return repaired


def parse_tool_arguments(raw: str) -> tuple[Dict[str, Any], bool]:
    """
    Parse a raw JSON argument string into a dict.

    Returns a tuple of (arguments, was_repaired).  Tries strict parsing first;
    falls back to the repair pass if that fails.  Raises ValueError if the
    repaired string is also unparseable, so callers get a clear signal.
    """
    if not raw or raw.strip() in ("", "{}"):
        return {}, False

    try:
        return json.loads(raw), False
    except json.JSONDecodeError:
        pass

    repaired = _repair_json(raw)
    try:
        return json.loads(repaired), True
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Tool argument string is not valid JSON and could not be repaired: {exc}\n"
            f"Raw arguments: {raw!r}"
        ) from exc


# ---------------------------------------------------------------------------
# Schema generation — OpenAI / Anthropic / Gemini format
# ---------------------------------------------------------------------------

# Python type → JSON Schema type mapping.
_PY_TO_JSON_TYPE: Dict[str, str] = {
    "str":   "string",
    "int":   "integer",
    "float": "number",
    "bool":  "boolean",
    "list":  "array",
    "dict":  "object",
    "None":  "null",
}


def _param_json_type(annotation) -> str:
    """Convert a Python type annotation to a JSON Schema type string."""
    if annotation is inspect.Parameter.empty:
        return "string"
    name = getattr(annotation, "__name__", None) or str(annotation)
    # Handle Optional[X] → unwrap to X.
    origin = getattr(annotation, "__origin__", None)
    if origin is not None:
        args = getattr(annotation, "__args__", ())
        # Union[X, None] is Optional[X].
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            return _param_json_type(non_none[0])
    return _PY_TO_JSON_TYPE.get(name, "string")


def build_openai_tools(tools: Dict[str, Any]) -> List[Dict]:
    """
    Convert a {name: callable} tool dict into the OpenAI function-calling
    schema format used by OpenAI, Alibaba, DeepSeek, and ExperientialLabs.

    Each tool's description comes from its docstring (first paragraph only,
    to keep token cost low). Parameters are derived from the function signature.
    """
    result = []
    for name, func in tools.items():
        try:
            sig = inspect.signature(func)
        except (ValueError, TypeError):
            sig = None

        # Strip the full docstring to just the first paragraph.
        raw_doc = inspect.getdoc(func) or ""
        description = raw_doc.split("\n\n")[0].strip() or f"Call {name}."

        properties: Dict[str, Any] = {}
        required: List[str] = []

        if sig:
            for param_name, param in sig.parameters.items():
                # Skip 'self', private/internal parameters, and *args/**kwargs.
                if param_name == "self" or param_name.startswith("_"):
                    continue
                if param.kind in (
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                ):
                    continue

                json_type = _param_json_type(param.annotation)
                properties[param_name] = {"type": json_type}

                # A parameter is required when it has no default value.
                if param.default is inspect.Parameter.empty:
                    required.append(param_name)

        schema: Dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required

        result.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": schema,
            },
        })
    return result


def build_anthropic_tools(tools: Dict[str, Any]) -> List[Dict]:
    """
    Convert a {name: callable} tool dict into Anthropic's tool schema format.

    Anthropic uses `input_schema` instead of `parameters` and the outer
    wrapper is a flat dict, not nested under `"function"`.
    """
    openai_tools = build_openai_tools(tools)
    result = []
    for tool in openai_tools:
        fn = tool["function"]
        result.append({
            "name": fn["name"],
            "description": fn["description"],
            "input_schema": fn["parameters"],
        })
    return result


# ---------------------------------------------------------------------------
# Response normalisation — convert provider responses to ToolCall list
# ---------------------------------------------------------------------------

def parse_openai_tool_calls(response_message) -> List[ToolCall]:
    """
    Extract ToolCall objects from an OpenAI chat completion message object.

    Compatible with OpenAI, Alibaba, DeepSeek, and ExperientialLabs — all
    use the same response shape.
    """
    calls = []
    tool_calls = getattr(response_message, "tool_calls", None) or []
    for tc in tool_calls:
        raw = tc.function.arguments or ""
        try:
            args, repaired = parse_tool_arguments(raw)
        except ValueError:
            args, repaired = {}, False

        calls.append(ToolCall(
            call_id=tc.id,
            name=tc.function.name,
            arguments=args,
            raw_arguments=raw,
            was_repaired=repaired,
        ))
    return calls


def parse_anthropic_tool_calls(response_content: list) -> List[ToolCall]:
    """
    Extract ToolCall objects from an Anthropic message content block list.

    Anthropic sends tool calls as `tool_use` content blocks with `input`
    already parsed as a dict (no JSON decoding needed from our side).
    """
    calls = []
    for block in response_content or []:
        if getattr(block, "type", None) != "tool_use":
            continue
        # Anthropic gives us `input` as a dict already — wrap it back to
        # raw JSON so we can pass it through our standard path cleanly.
        raw = json.dumps(block.input or {})
        calls.append(ToolCall(
            call_id=block.id,
            name=block.name,
            arguments=block.input or {},
            raw_arguments=raw,
            was_repaired=False,
        ))
    return calls


# ---------------------------------------------------------------------------
# Tool result message builders
# ---------------------------------------------------------------------------

def build_openai_tool_result_messages(results: List[ToolResult]) -> List[Dict]:
    """
    Build the list of `tool` role messages that OpenAI expects after a
    function call turn.
    """
    return [
        {
            "role": "tool",
            "tool_call_id": r.call_id,
            "content": r.content,
        }
        for r in results
    ]


def build_anthropic_tool_result_content(results: List[ToolResult]) -> List[Dict]:
    """
    Build the `tool_result` content blocks that Anthropic expects in the
    following user message after a tool_use turn.
    """
    blocks = []
    for r in results:
        block: Dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": r.call_id,
            "content": r.content,
        }
        if r.is_error:
            block["is_error"] = True
        blocks.append(block)
    return blocks


# ---------------------------------------------------------------------------
# Streaming tool call accumulator
# ---------------------------------------------------------------------------

class StreamingToolCallAccumulator:
    """
    Accumulates streaming tool-call deltas from the OpenAI streaming API
    into complete ToolCall objects.

    OpenAI streams tool calls as a sequence of delta chunks where each
    chunk carries a partial argument JSON string.  This class merges those
    deltas into complete calls once the stream ends.
    """

    def __init__(self):
        # index → {id, name, arguments_chunks}
        self._calls: Dict[int, Dict] = {}

    def feed(self, delta_tool_calls: list) -> None:
        """Feed a list of tool_call delta objects from one streaming chunk."""
        for tc_delta in delta_tool_calls or []:
            idx = tc_delta.index
            if idx not in self._calls:
                self._calls[idx] = {
                    "id":   tc_delta.id or "",
                    "name": (tc_delta.function.name or "") if tc_delta.function else "",
                    "args": [],
                }
            if tc_delta.id:
                self._calls[idx]["id"] = tc_delta.id
            if tc_delta.function:
                if tc_delta.function.name:
                    self._calls[idx]["name"] = tc_delta.function.name
                if tc_delta.function.arguments:
                    self._calls[idx]["args"].append(tc_delta.function.arguments)

    def finish(self) -> List[ToolCall]:
        """Return the complete ToolCall list after the stream has ended."""
        result = []
        for entry in self._calls.values():
            raw = "".join(entry["args"])
            try:
                args, repaired = parse_tool_arguments(raw)
            except ValueError:
                args, repaired = {}, False
            result.append(ToolCall(
                call_id=entry["id"],
                name=entry["name"],
                arguments=args,
                raw_arguments=raw,
                was_repaired=repaired,
            ))
        return result


# ---------------------------------------------------------------------------
# Message formatting — provider-wire normalization
# ---------------------------------------------------------------------------

def format_messages_for_provider(messages: List[Dict], provider_name: str) -> List[Dict]:
    """
    Format internal conversation messages into the exact wire format required
    by the target LLM provider (Anthropic vs OpenAI-compatible).

    Handles:
      - Assistant turns with ToolCall objects or tool_calls lists.
      - Converting tool / tool_result records into OpenAI tool messages
        or grouping adjacent tool results into Anthropic tool_result user blocks.
    """
    provider = provider_name.lower()
    is_anthropic = provider == "anthropic"

    formatted: List[Dict] = []
    pending_anthropic_tool_results: List[Dict] = []

    def _flush_anthropic_results():
        if pending_anthropic_tool_results:
            formatted.append({
                "role": "user",
                "content": list(pending_anthropic_tool_results),
            })
            pending_anthropic_tool_results.clear()

    for msg in messages:
        role = msg.get("role")

        if role in ("tool", "tool_result"):
            call_id = msg.get("tool_call_id") or msg.get("call_id") or ""
            content = str(msg.get("content", ""))
            is_error = bool(msg.get("is_error", False))

            if is_anthropic:
                block: Dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": content,
                }
                if is_error:
                    block["is_error"] = True
                pending_anthropic_tool_results.append(block)
            else:
                formatted.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": content,
                })
            continue

        # Flush any pending Anthropic tool results before the next conversational turn.
        if is_anthropic and pending_anthropic_tool_results:
            _flush_anthropic_results()

        if role == "assistant":
            raw_content = msg.get("content")
            raw_calls = msg.get("tool_calls") or []

            tool_calls: List[ToolCall] = []
            for tc in raw_calls:
                if isinstance(tc, ToolCall):
                    tool_calls.append(tc)
                elif isinstance(tc, dict):
                    fn = tc.get("function", {})
                    call_id = tc.get("id") or tc.get("call_id") or ""
                    name = fn.get("name") or tc.get("name") or ""
                    args = fn.get("arguments") or tc.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args, _ = parse_tool_arguments(args)
                        except Exception:
                            args = {}
                    tool_calls.append(ToolCall(call_id=call_id, name=name, arguments=args))

            if is_anthropic:
                blocks = []
                # Replay signed thinking blocks before text/tool_use. Anthropic
                # rejects a tool-use turn when the preceding thinking block is
                # missing from the replayed history.
                for tb in msg.get("thinking_blocks") or []:
                    if isinstance(tb, dict):
                        blocks.append(dict(tb))
                if raw_content:
                    blocks.append({"type": "text", "text": str(raw_content)})
                for tc in tool_calls:
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.call_id,
                        "name": tc.name,
                        "input": tc.arguments,
                    })
                formatted.append({
                    "role": "assistant",
                    "content": blocks if blocks else (raw_content or ""),
                })
            else:
                # OpenAI accepts null content on tool-call turns. Using None
                # (rather than "") also avoids LiteLLM's Anthropic layer
                # injecting a placeholder text block into every tool_use turn.
                content_value = raw_content
                if (content_value is None or content_value == "") and tool_calls:
                    content_value = None
                out_msg: Dict[str, Any] = {
                    "role": "assistant",
                    "content": content_value,
                }
                if tool_calls:
                    out_msg["tool_calls"] = [
                        {
                            "id": tc.call_id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                # Always re-serialize the parsed arguments so a
                                # repaired (invalid) raw string is never replayed.
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in tool_calls
                    ]
                # Provider-continuity fields must survive the round trip:
                # Anthropic thinking_blocks, DeepSeek reasoning_content, and
                # Gemini/provider thought signatures.
                for key in ("thinking_blocks", "reasoning_content", "provider_specific_fields"):
                    value = msg.get(key)
                    if value:
                        out_msg[key] = value
                formatted.append(out_msg)

        else:
            formatted.append(dict(msg))

    if is_anthropic and pending_anthropic_tool_results:
        _flush_anthropic_results()

    return formatted
