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
import types
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union, get_args, get_origin

from .tool_schemas import TOOL_SCHEMAS


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
    "NoneType": "null",
    "Any":   "string",
}


def _split_top_level(text: str) -> List[str]:
    """Split a comma-separated annotation on top-level commas only."""
    parts: List[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(text):
        if char in "[(":
            depth += 1
        elif char in "])":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _schema_from_string(annotation: str) -> Dict[str, Any]:
    """
    Infer a JSON schema from a stringized annotation.

    ``from __future__ import annotations`` turns every annotation into a
    string unless ``eval_str=True`` succeeds. This fallback keeps common
    typing forms correct (Optional, Union, List, Tuple, Dict) even when a
    forward reference cannot be resolved.
    """
    text = annotation.strip().strip("'\"")
    if text.startswith("typing."):
        text = text[len("typing."):]

    if text.startswith("Optional[") and text.endswith("]"):
        return _schema_from_string(text[len("Optional["):-1])

    if text.startswith("Union[") and text.endswith("]"):
        members = [
            m for m in _split_top_level(text[len("Union["):-1])
            if m not in ("None", "NoneType")
        ]
        if len(members) == 1:
            return _schema_from_string(members[0])
        return {"anyOf": [_schema_from_string(m) for m in members]}

    for prefix in ("List", "list", "Sequence", "Set", "set", "FrozenSet"):
        if text.startswith(prefix + "[") and text.endswith("]"):
            inner = _split_top_level(text[len(prefix) + 1:-1])
            return {
                "type": "array",
                "items": _schema_from_string(inner[0]) if inner else {},
            }

    for prefix in ("Tuple", "tuple"):
        if text.startswith(prefix + "[") and text.endswith("]"):
            members = [
                m for m in _split_top_level(text[len(prefix) + 1:-1])
                if m != "..."
            ]
            if len(members) == 1:
                return {"type": "array", "items": _schema_from_string(members[0])}
            if members:
                return {
                    "type": "array",
                    "items": {"anyOf": [_schema_from_string(m) for m in members]},
                }
            return {"type": "array", "items": {}}

    for prefix in ("Dict", "dict", "Mapping", "mapping"):
        if text.startswith(prefix + "[") and text.endswith("]"):
            return {"type": "object"}

    base = text.rsplit(".", 1)[-1]
    return {"type": _PY_TO_JSON_TYPE.get(base, "string")}


def _param_schema(annotation) -> Dict[str, Any]:
    """Convert a Python type annotation to a JSON Schema fragment."""
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}
    if isinstance(annotation, str):
        return _schema_from_string(annotation)
    if annotation is Any:
        return {}
    if annotation is type(None):
        return {"type": "null"}

    origin = get_origin(annotation)
    if origin is not None:
        args = get_args(annotation)

        if origin is types.UnionType or origin is Union:
            non_none = [a for a in args if a is not type(None)]
            if len(non_none) == 1:
                return _param_schema(non_none[0])
            return {"anyOf": [_param_schema(a) for a in non_none]}

        if origin is tuple:
            members = [a for a in args if a is not Ellipsis]
            if len(members) == 1:
                return {"type": "array", "items": _param_schema(members[0])}
            if members:
                return {
                    "type": "array",
                    "items": {"anyOf": [_param_schema(a) for a in members]},
                }
            return {"type": "array", "items": {}}

        if origin in (list, set, frozenset):
            return {
                "type": "array",
                "items": _param_schema(args[0]) if args else {},
            }

        if origin is dict:
            return {"type": "object"}

    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is str:
        return {"type": "string"}
    if annotation is list:
        return {"type": "array", "items": {}}
    if annotation is dict:
        return {"type": "object"}
    if annotation is tuple:
        return {"type": "array", "items": {}}

    return {"type": "string"}


def _safe_signature(func):
    """Return a signature with evaluated annotations when possible."""
    try:
        return inspect.signature(func, eval_str=True)
    except Exception:
        try:
            return inspect.signature(func)
        except (ValueError, TypeError):
            return None


_DOC_SECTION_STOP = re.compile(
    r"^\s*(Returns?|Raises?|Yields?|Examples?|Notes?|Attributes?|See Also)\s*:?\s*$",
    re.IGNORECASE,
)
_DOC_ARG_HEADER = re.compile(
    r"^\s*(Args|Arguments)\s*:?\s*$",
    re.IGNORECASE,
)
_DOC_ARG_ENTRY = re.compile(
    r"^\s{0,8}([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*:\s*(.*)$"
)


def _parse_docstring_params(doc: str) -> Dict[str, str]:
    """
    Extract Google-style ``Args:`` parameter descriptions from a docstring.

    Only the explicit ``Args:``/``Arguments:`` form is parsed. Numpy-style
    sections are intentionally ignored rather than guessed at, because a
    wrong description is worse than no description. Built-in tools use
    curated metadata instead of this fallback.
    """
    params: Dict[str, str] = {}
    in_args = False
    current: Optional[str] = None

    for line in doc.splitlines():
        stripped = line.strip()

        if not in_args:
            if _DOC_ARG_HEADER.match(line):
                in_args = True
            continue

        if not stripped:
            continue
        if _DOC_SECTION_STOP.match(line):
            break

        match = _DOC_ARG_ENTRY.match(line)
        if match:
            current = match.group(1)
            params[current] = match.group(2).strip()
        elif current:
            params[current] = f"{params[current]} {stripped}".strip()

    return params


def _strip_args_section(doc: str) -> str:
    """Remove a Google-style Args section from a docstring description."""
    lines = doc.splitlines()
    kept: List[str] = []
    skipping = False

    for line in lines:
        if not skipping and _DOC_ARG_HEADER.match(line):
            skipping = True
            continue
        if skipping:
            if _DOC_SECTION_STOP.match(line):
                skipping = False
                kept.append(line)
            continue
        kept.append(line)

    return "\n".join(kept).strip()


def build_openai_tools(tools: Dict[str, Any]) -> List[Dict]:
    """
    Convert a {name: callable} tool dict into the OpenAI function-calling
    schema format used by OpenAI, Alibaba, DeepSeek, and ExperientialLabs.

    Signatures supply names, types, and defaults. Curated metadata in
    ``tool_schemas.TOOL_SCHEMAS`` supplies the complete description and
    per-parameter contracts for built-in tools. Unregistered/custom tools
    fall back to their full docstring and inferred signature, so no tool
    ever reaches the model with a truncated contract.
    """
    result = []
    for name, func in tools.items():
        sig = _safe_signature(func)

        meta = TOOL_SCHEMAS.get(name, {})
        raw_doc = inspect.getdoc(func) or ""
        description = meta.get("description")
        if not description:
            description = _strip_args_section(raw_doc) or f"Call {name}."

        curated_params = meta.get("parameters") or {}
        doc_params = _parse_docstring_params(raw_doc)

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

                prop = _param_schema(param.annotation)
                param_meta = dict(curated_params.get(param_name, {}))
                if "description" not in param_meta and param_name in doc_params:
                    param_meta["description"] = doc_params[param_name]

                # Metadata may override the inferred shape (enums, unions
                # that should be a single type, array item schemas, ...).
                if "type" in param_meta:
                    prop.pop("anyOf", None)
                    prop["type"] = param_meta["type"]
                if "items" in param_meta:
                    prop["items"] = param_meta["items"]
                if "enum" in param_meta:
                    prop["enum"] = param_meta["enum"]
                if "description" in param_meta:
                    prop["description"] = param_meta["description"]
                if "minimum" in param_meta:
                    prop["minimum"] = param_meta["minimum"]
                if "maximum" in param_meta:
                    prop["maximum"] = param_meta["maximum"]

                if param.default is not inspect.Parameter.empty and param.default is not None:
                    # Empty-string defaults are placeholders for "optional
                    # unless retrying"; the description carries that rule.
                    if not (isinstance(param.default, str) and param.default == ""):
                        try:
                            json.dumps(param.default)
                            prop.setdefault("default", param.default)
                        except (TypeError, ValueError):
                            pass

                properties[param_name] = prop

                # A parameter is required when it has no default value.
                if param.default is inspect.Parameter.empty:
                    required.append(param_name)

        # Explicit required list wins (e.g. execute(command=...) has a
        # permissive default for positional calls but is semantically
        # required in native function-calling).
        if "required" in meta:
            required = list(meta["required"])
            for required_name in required:
                properties.get(required_name, {}).pop("default", None)

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

def _delta_field(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a pydantic delta object or a plain dict."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


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

    def _index_for(self, raw_index: Any, call_id: str) -> int:
        """Resolve the slot a fragment belongs to.

        LiteLLM normally sets ``index`` on every fragment. Some gateways omit
        it, so fall back to the last slot — allocating a new one only when a
        genuinely new call id shows up.
        """
        if raw_index is not None:
            return int(raw_index)
        if self._calls and call_id and call_id not in {v["id"] for v in self._calls.values()}:
            return max(self._calls) + 1
        return max(self._calls) if self._calls else 0

    def feed(self, delta_tool_calls: list) -> None:
        """Feed a list of tool_call delta objects from one streaming chunk."""
        for tc_delta in delta_tool_calls or []:
            call_id = _delta_field(tc_delta, "id") or ""
            function = _delta_field(tc_delta, "function")
            idx = self._index_for(_delta_field(tc_delta, "index"), call_id)

            if idx not in self._calls:
                self._calls[idx] = {
                    "id":   call_id,
                    "name": (_delta_field(function, "name") or "") if function else "",
                    "args": [],
                }
            if call_id:
                self._calls[idx]["id"] = call_id
            if function:
                name = _delta_field(function, "name")
                if name:
                    self._calls[idx]["name"] = name
                arguments = _delta_field(function, "arguments")
                if arguments:
                    self._calls[idx]["args"].append(arguments)

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
