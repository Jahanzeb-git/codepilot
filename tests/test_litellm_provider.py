"""
Tests for the unified LiteLLM provider layer.

These tests never make a network call: they cover route resolution, API-key
selection, Anthropic cache-breakpoint construction, provider-continuity
metadata, and response parsing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from codepilot.core.prompt import SystemPromptParts
from codepilot.core.tool_protocol import ToolCall, format_messages_for_provider
from codepilot.engine.provider import (
    LiteLLMProvider,
    _insert_cache_breakpoints,
)


# ---------------------------------------------------------------------------
# Route resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider,model,expected", [
    ("anthropic", "claude-sonnet-5", "anthropic/claude-sonnet-5"),
    ("openai", "gpt-4o", "openai/gpt-4o"),
    ("gemini", "gemini-2.5-pro", "gemini/gemini-2.5-pro"),
    ("google", "gemini-2.5-pro", "gemini/gemini-2.5-pro"),
    ("deepseek", "deepseek-chat", "deepseek/deepseek-chat"),
    ("alibaba", "qwen-max", "dashscope/qwen-max"),
    ("dashscope", "qwen-max", "dashscope/qwen-max"),
    ("experientiallabs", "qwen3.8-27b", "openai/qwen3.8-27b"),
    ("openai_compatible", "meta-llama/Llama-3", "openai/meta-llama/Llama-3"),
    ("ollama", "llama3.1", "ollama/llama3.1"),
    ("openrouter", "anthropic/claude-sonnet-4", "openrouter/anthropic/claude-sonnet-4"),
    ("litellm", "openrouter/anthropic/claude-sonnet-4", "openrouter/anthropic/claude-sonnet-4"),
    ("some_future_provider", "model-x", "some_future_provider/model-x"),
])
def test_model_route_resolution(provider, model, expected):
    p = LiteLLMProvider(provider=provider, model=model, api_key="test-key")
    assert p.model == expected


def test_already_prefixed_model_is_not_prefixed_twice():
    p = LiteLLMProvider(provider="anthropic", model="anthropic/claude-sonnet-5", api_key="k")
    assert p.model == "anthropic/claude-sonnet-5"


def test_experiential_labs_and_dashscope_default_base_urls():
    exp = LiteLLMProvider(provider="experientiallabs", model="m", api_key="k")
    assert exp.api_base == "https://api.experientiallabs.ai/v1"

    ali = LiteLLMProvider(provider="alibaba", model="m", api_key="k")
    assert ali.api_base == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

def test_explicit_key_wins(monkeypatch):
    monkeypatch.setenv("SOME_KEY", "from-env")
    p = LiteLLMProvider(provider="openai", model="gpt-4o", api_key="explicit", api_key_env="SOME_KEY")
    assert p.api_key == "explicit"


def test_api_key_env_and_provider_fallback(monkeypatch):
    monkeypatch.delenv("CUSTOM_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-env")
    p = LiteLLMProvider(provider="anthropic", model="claude-sonnet-5", api_key_env="CUSTOM_KEY")
    assert p.api_key == "anthropic-env"


def test_missing_key_raises_for_key_required_provider(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(EnvironmentError):
        LiteLLMProvider(provider="openai", model="gpt-4o", api_key_env="OPENAI_API_KEY")


def test_local_endpoint_needs_no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    p = LiteLLMProvider(
        provider="openai_compatible",
        model="llama3.1",
        api_base="http://127.0.0.1:8000/v1",
        api_key_env="",
    )
    assert p.api_key == "not-needed"


def test_experiential_labs_env_alias_is_used(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("EXPERIENTIAL_API_KEY", "exp-key")
    p = LiteLLMProvider(provider="experientiallabs", model="qwen3.8-27b", api_key_env="")
    assert p.api_key == "exp-key"


def test_gateway_default_base_still_requires_a_key(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("ALIBABA_API_KEY", raising=False)
    with pytest.raises(EnvironmentError):
        LiteLLMProvider(provider="alibaba", model="qwen-max", api_key_env="")


def test_provider_without_static_key_can_defer_to_litellm():
    p = LiteLLMProvider(provider="bedrock", model="anthropic.claude-sonnet-4", api_key_env="")
    assert p.api_key is None


# ---------------------------------------------------------------------------
# Anthropic cache breakpoints
# ---------------------------------------------------------------------------

def test_breakpoints_preserve_tool_calls_and_do_not_mutate_input():
    messages = [
        {"role": "user", "content": "[Task 1] inspect"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "view_file", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]
    result = _insert_cache_breakpoints(messages)

    # The tool call must not be dropped or replaced by a text block.
    cached = result[1]
    assert cached["tool_calls"][0]["id"] == "call_1"
    assert cached["tool_calls"][0]["cache_control"] == {"type": "ephemeral"}
    # Input is untouched.
    assert "cache_control" not in messages[1]["tool_calls"][0]


def test_breakpoints_keep_text_and_attach_to_text_block():
    messages = [
        {"role": "user", "content": "[Task 1] hi"},
        {"role": "assistant", "content": "hello", "tool_calls": []},
    ]
    result = _insert_cache_breakpoints(messages, ttl="1h")
    block = result[1]["content"][0]
    assert block["type"] == "text"
    assert block["text"] == "hello"
    assert block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_breakpoints_handle_content_list():
    messages = [
        {"role": "user", "content": "[Task 1] hi"},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "x"}],
            "tool_calls": [],
        },
    ]
    result = _insert_cache_breakpoints(messages)
    assert result[1]["content"][0]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# System prompt / cache policy
# ---------------------------------------------------------------------------

def test_anthropic_system_static_is_cached_dynamic_is_not():
    p = LiteLLMProvider(provider="anthropic", model="claude-sonnet-5", api_key="k")
    system = SystemPromptParts(static="RULES", dynamic="ENV")
    wire = p._build_messages([{"role": "user", "content": "hi"}], system)

    assert wire[0]["role"] == "system"
    blocks = wire[0]["content"]
    assert blocks[0]["text"] == "RULES"
    assert blocks[0]["cache_control"]["ttl"] == "1h"
    assert "cache_control" not in blocks[1]


def test_anthropic_plain_string_system_gets_cache_control():
    p = LiteLLMProvider(provider="anthropic", model="claude-sonnet-5", api_key="k")
    wire = p._build_messages([{"role": "user", "content": "hi"}], "PLAIN RULES")
    assert wire[0]["role"] == "system"
    assert wire[0]["content"][0]["text"] == "PLAIN RULES"
    assert wire[0]["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_non_anthropic_routes_never_receive_cache_control():
    p = LiteLLMProvider(
        provider="gemini", model="gemini-2.5-pro", api_key="k",
        thinking_enabled=True,
    )
    messages = [
        {"role": "user", "content": "[Task 1] hi"},
        {"role": "assistant", "content": "hello", "tool_calls": []},
    ]
    wire = p._build_messages(messages, SystemPromptParts(static="RULES", dynamic="ENV"))

    assert wire[0]["content"] == "RULESENV"
    assert "cache_control" not in repr(wire)
    assert p.supports_cache_refresh is False


def test_anthropic_advertises_cache_refresh():
    p = LiteLLMProvider(provider="anthropic", model="claude-sonnet-5", api_key="k")
    assert p.supports_cache_refresh is True


# ---------------------------------------------------------------------------
# Continuity metadata
# ---------------------------------------------------------------------------

def test_anthropic_keeps_thinking_blocks_and_drops_other_provider_fields():
    p = LiteLLMProvider(provider="anthropic", model="claude-sonnet-5", api_key="k")
    messages = [{
        "role": "assistant",
        "content": "hi",
        "tool_calls": [],
        "thinking_blocks": [{"type": "thinking", "thinking": "t", "signature": "s"}],
        "reasoning_content": "r",
        "provider_specific_fields": {"x": 1},
    }]
    wire = p._build_messages(messages, None)
    assert wire[0]["thinking_blocks"][0]["signature"] == "s"
    assert "reasoning_content" not in wire[0]
    assert "provider_specific_fields" not in wire[0]


def test_deepseek_keeps_reasoning_content():
    p = LiteLLMProvider(provider="deepseek", model="deepseek-chat", api_key="k")
    messages = [{
        "role": "assistant",
        "content": "hi",
        "tool_calls": [],
        "reasoning_content": "thinking",
        "thinking_blocks": [{"type": "thinking"}],
    }]
    wire = p._build_messages(messages, None)
    assert wire[0]["reasoning_content"] == "thinking"
    assert "thinking_blocks" not in wire[0]


def test_gemini_keeps_provider_specific_fields():
    p = LiteLLMProvider(provider="gemini", model="gemini-2.5-pro", api_key="k")
    messages = [{
        "role": "assistant",
        "content": "hi",
        "tool_calls": [],
        "provider_specific_fields": {"thought_signature": "abc"},
    }]
    wire = p._build_messages(messages, None)
    assert wire[0]["provider_specific_fields"]["thought_signature"] == "abc"


def test_openai_drops_foreign_continuity_fields():
    p = LiteLLMProvider(provider="openai", model="gpt-4o", api_key="k")
    messages = [{
        "role": "assistant",
        "content": "hi",
        "tool_calls": [],
        "thinking_blocks": [{"type": "thinking"}],
        "reasoning_content": "r",
        "provider_specific_fields": {"x": 1},
    }]
    wire = p._build_messages(messages, None)
    assert "thinking_blocks" not in wire[0]
    assert "reasoning_content" not in wire[0]
    assert "provider_specific_fields" not in wire[0]


# ---------------------------------------------------------------------------
# Thinking parameters
# ---------------------------------------------------------------------------

def test_thinking_parameters_per_route():
    anthropic = LiteLLMProvider(
        provider="anthropic", model="claude-sonnet-5", api_key="k",
        thinking_enabled=True, thinking_budget=4096,
    )
    assert anthropic._thinking_params() == {
        "thinking": {"type": "enabled", "budget_tokens": 4096}
    }

    deepseek = LiteLLMProvider(
        provider="deepseek", model="deepseek-chat", api_key="k",
        thinking_enabled=True,
    )
    assert deepseek._thinking_params() == {"thinking": {"type": "enabled"}}

    openai = LiteLLMProvider(
        provider="openai", model="gpt-5", api_key="k",
        thinking_enabled=True, reasoning_effort="medium",
    )
    assert openai._thinking_params() == {"reasoning_effort": "medium"}


def test_deepseek_temperature_is_omitted_when_thinking():
    p = LiteLLMProvider(
        provider="deepseek", model="deepseek-chat", api_key="k",
        thinking_enabled=True,
    )
    assert p._temperature_for_call(0.0) is None

    assert p._temperature_for_call(0.0, force_thinking=True) is None
    p2 = LiteLLMProvider(provider="openai", model="gpt-4o", api_key="k")
    assert p2._temperature_for_call(0.2) == 0.2


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _fake_response():
    message = SimpleNamespace(
        content="I will inspect it.",
        tool_calls=[
            SimpleNamespace(
                id="call_42",
                function=SimpleNamespace(
                    name="view_file",
                    arguments='{"path": "a.py"}',
                ),
            )
        ],
        thinking_blocks=[{
            "type": "thinking",
            "thinking": "need to inspect",
            "signature": "sig",
        }],
        reasoning_content="reasoning text",
        provider_specific_fields={"thought_signature": "abc"},
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            prompt_tokens_details=SimpleNamespace(cached_tokens=7),
        ),
    )


def test_parse_response_collects_text_tools_and_metadata():
    p = LiteLLMProvider(provider="anthropic", model="claude-sonnet-5", api_key="k")
    parsed = p._parse_response(_fake_response())

    assert parsed.text == "I will inspect it."
    assert parsed.stop_reason == "tool_calls"
    assert parsed.tool_calls[0].call_id == "call_42"
    assert parsed.tool_calls[0].name == "view_file"
    assert parsed.tool_calls[0].arguments == {"path": "a.py"}
    assert parsed.thinking_blocks[0]["signature"] == "sig"
    assert parsed.reasoning_content == "reasoning text"
    assert parsed.provider_specific_fields["thought_signature"] == "abc"
    assert parsed.usage["total_tokens"] == 15
    assert parsed.usage["cached_tokens"] == 7


def test_tool_call_to_dict_re_serializes_arguments():
    call = ToolCall(
        call_id="c1",
        name="write_file",
        arguments={"path": "a.py", "content": "x"},
        raw_arguments="{broken json",
        was_repaired=True,
    )
    wire = call.to_dict()
    assert wire["type"] == "function"
    assert wire["function"]["arguments"] == '{"path": "a.py", "content": "x"}'


# ---------------------------------------------------------------------------
# Message formatter
# ---------------------------------------------------------------------------

def test_formatter_preserves_continuity_fields():
    messages = [{
        "role": "assistant",
        "content": "hi",
        "tool_calls": [ToolCall(call_id="c1", name="view_file", arguments={"path": "a"})],
        "thinking_blocks": [{"type": "thinking", "thinking": "t", "signature": "s"}],
        "reasoning_content": "r",
        "provider_specific_fields": {"x": 1},
    }]
    wire = format_messages_for_provider(messages, "openai")
    assert wire[0]["thinking_blocks"][0]["signature"] == "s"
    assert wire[0]["reasoning_content"] == "r"
    assert wire[0]["provider_specific_fields"] == {"x": 1}
    # Arguments are re-serialized from the parsed dict.
    assert wire[0]["tool_calls"][0]["function"]["arguments"] == '{"path": "a"}'


# ---------------------------------------------------------------------------
# LiteLLM request plumbing
# ---------------------------------------------------------------------------

def test_chat_with_tools_skips_litellm_mcp_handler(monkeypatch):
    """
    LiteLLM 1.106.x imports its optional `mcp` package whenever `tools` is
    present. The runner image does not install it, so CodePilot must opt out of
    that handler for regular function tools.
    """
    import asyncio
    import codepilot.engine.provider as provider_module

    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return _fake_response()

    fake_litellm = SimpleNamespace(
        acompletion=fake_acompletion,
        suppress_debug_info=True,
    )
    monkeypatch.setattr(provider_module, "_litellm", lambda: fake_litellm)

    p = LiteLLMProvider(provider="openai", model="gpt-4o", api_key="k")
    asyncio.run(p.chat_with_tools(
        messages=[{"role": "user", "content": "hi"}],
        tools=[{
            "type": "function",
            "function": {
                "name": "view_file",
                "description": "d",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        max_tokens=32,
    ))

    assert captured["_skip_mcp_handler"] is True
    assert captured["tools"][0]["function"]["name"] == "view_file"


def test_chat_without_tools_does_not_set_mcp_flag(monkeypatch):
    import asyncio
    import codepilot.engine.provider as provider_module

    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return _fake_response()

    fake_litellm = SimpleNamespace(
        acompletion=fake_acompletion,
        suppress_debug_info=True,
    )
    monkeypatch.setattr(provider_module, "_litellm", lambda: fake_litellm)

    p = LiteLLMProvider(provider="openai", model="gpt-4o", api_key="k")
    asyncio.run(p.chat(messages=[{"role": "user", "content": "hi"}], max_tokens=32))

    assert "_skip_mcp_handler" not in captured


def test_refresh_cache_ttl_filters_foreign_continuity_fields(monkeypatch):
    import asyncio
    import codepilot.engine.provider as provider_module

    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    fake_litellm = SimpleNamespace(
        acompletion=fake_acompletion,
        suppress_debug_info=True,
    )
    monkeypatch.setattr(provider_module, "_litellm", lambda: fake_litellm)

    p = LiteLLMProvider(provider="anthropic", model="claude-sonnet-5", api_key="k")
    asyncio.run(p.refresh_cache_ttl(
        messages=[
            {"role": "user", "content": "[Task 1] hi"},
            {
                "role": "assistant",
                "content": "done",
                "reasoning_content": "foreign",
                "provider_specific_fields": {"x": 1},
            },
        ],
        system=SystemPromptParts(static="RULES", dynamic="ENV"),
    ))

    wire = captured["messages"]
    assistant = [m for m in wire if m["role"] == "assistant"][0]
    assert "reasoning_content" not in assistant
    assert "provider_specific_fields" not in assistant
    # The 1-hour conversation breakpoint was written.
    assert assistant["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # Anthropic needs a trailing user turn.
    assert wire[-1]["role"] == "user"
