"""
File: provider.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description:
Unified LLM provider layer for the CodePilot agentic runtime.

Architectural Notes:
Every model request goes through LiteLLM (``litellm.acompletion``). A single
``LiteLLMProvider`` owns route resolution, API-key lookup, thinking/reasoning
parameters, and provider-continuity metadata. Provider-specific inference
implementations are intentionally gone: adding a provider means teaching
LiteLLM its prefix, not writing a new adapter here.

Route resolution keeps the historical AgentFile provider names working:

    anthropic          -> anthropic/<model>
    openai             -> openai/<model>
    gemini / google    -> gemini/<model>
    deepseek           -> deepseek/<model>
    alibaba / dashscope-> dashscope/<model>
    experientiallabs   -> openai/<model> + Experiential Labs base URL
    openai_compatible  -> openai/<model> + user-supplied api_base
    ollama             -> ollama/<model>
    <anything-else>    -> <provider>/<model>  (or a raw LiteLLM route)

Prompt caching:
    Anthropic is the only route where CodePilot plants explicit
    ``cache_control`` breakpoints (system prompt + up to three assistant
    anchors). Every other provider uses its own automatic caching. Sending
    ``cache_control`` to Gemini would opt it into paid explicit caching, so it
    is never injected outside direct Anthropic routes.

Continuity metadata:
    Anthropic extended thinking requires the previous turn's signed
    ``thinking_blocks`` to be replayed; DeepSeek requires
    ``reasoning_content``. Gemini and other providers may return
    ``provider_specific_fields`` (e.g. thought signatures) that must also be
    echoed back. ``ToolCallResponse`` carries all of it to the runtime, which
    stores it on the assistant history message.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import json
import os
import re
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Union

from ..core.prompt import SystemPromptParts
from ..core.tool_protocol import (
    StreamingToolCallAccumulator,
    ToolCall,
    format_messages_for_provider,
    parse_tool_arguments,
)


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------

@dataclass
class ToolCallResponse:
    """Unified response from a single chat_with_tools() turn."""

    #: The assistant's visible text (may be empty if it only called tools).
    text: str = ""
    #: Tool calls the model wants to execute this turn.
    tool_calls: List[ToolCall] = field(default_factory=list)
    #: Stop reason as normalized by LiteLLM ("tool_calls", "stop", ...).
    stop_reason: str = ""
    #: Anthropic signed thinking blocks — must be replayed on the next turn.
    thinking_blocks: List[Dict[str, Any]] = field(default_factory=list)
    #: DeepSeek/Qwen reasoning text — must be replayed on the next turn.
    reasoning_content: Optional[str] = None
    #: Provider-specific continuation data (e.g. Gemini thought signatures).
    provider_specific_fields: Dict[str, Any] = field(default_factory=dict)
    #: Normalized usage counters when the provider reports them.
    usage: Dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Cache breakpoints (Anthropic only)
# ---------------------------------------------------------------------------

def _cache_control(ttl: Optional[Union[str, int]] = None) -> Dict[str, Any]:
    ctrl: Dict[str, Any] = {"type": "ephemeral"}
    if ttl is not None:
        ctrl["ttl"] = ttl
    return ctrl


def _insert_cache_breakpoints(
    messages: List[Dict],
    ttl: Optional[Union[str, int]] = None,
) -> List[Dict]:
    """
    Return a copy of *messages* with Anthropic cache breakpoints on up to
    three assistant messages (the system prompt is handled separately, for the
    four-breakpoint Anthropic limit).

      1. The assistant message ending the PREVIOUS task (anchors history).
      2. The assistant message anchoring chunks of 5 steps in the current task.
      3. The most recent assistant message (for background TTL refresh).

    This never mutates the input. Assistant messages keep every content block
    and every tool call; ``cache_control`` is attached to the last cacheable
    block instead of replacing the message with a single text block (the old
    implementation dropped ``tool_use`` blocks and could corrupt history).
    """
    last_asst_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            last_asst_idx = i
            break

    if last_asst_idx is None:
        return messages

    current_task_start_idx = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            content = str(messages[i].get("content", ""))
            if re.match(r"^(?:<task_\d+>|\[Task \d+\])", content.strip()):
                current_task_start_idx = i
                break

    prev_task_asst_idx = None
    for i in range(current_task_start_idx - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            prev_task_asst_idx = i
            break

    active_asst_indices = [
        i for i in range(current_task_start_idx, len(messages))
        if messages[i].get("role") == "assistant"
    ]

    chunk_asst_idx = None
    if len(active_asst_indices) >= 5:
        chunk_idx = (len(active_asst_indices) // 5) * 5 - 1
        if chunk_idx >= 0:
            chunk_asst_idx = active_asst_indices[chunk_idx]

    indices_to_cache = {last_asst_idx}
    if prev_task_asst_idx is not None:
        indices_to_cache.add(prev_task_asst_idx)
    if chunk_asst_idx is not None:
        indices_to_cache.add(chunk_asst_idx)

    result = list(messages)
    for idx in indices_to_cache:
        msg = result[idx]
        cloned = dict(msg)
        ctrl = _cache_control(ttl)

        content = cloned.get("content")
        if isinstance(content, str):
            if content:
                cloned["content"] = [{
                    "type": "text",
                    "text": content,
                    "cache_control": ctrl,
                }]
            else:
                # Empty text blocks cannot be cached. Fall through to tool calls.
                content = None

        if isinstance(content, list):
            blocks = [dict(b) if isinstance(b, dict) else b for b in content]
            for block in reversed(blocks):
                if isinstance(block, dict) and block.get("type") in ("text", "tool_use"):
                    block["cache_control"] = ctrl
                    break
            cloned["content"] = blocks
        else:
            # No cacheable content block: attach to the final tool call. LiteLLM
            # forwards this to the matching Anthropic tool_use block.
            raw_calls = cloned.get("tool_calls") or []
            if raw_calls:
                calls = []
                for tc in raw_calls:
                    calls.append(dict(tc) if isinstance(tc, dict) else tc)
                last = calls[-1]
                if isinstance(last, dict):
                    last["cache_control"] = ctrl
                calls[-1] = last
                cloned["tool_calls"] = calls

        result[idx] = cloned

    return result


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

class LLMProvider(ABC):
    """Minimal async interface the runtime depends on."""

    #: True when the provider benefits from an explicit cache TTL refresh.
    supports_cache_refresh: bool = False

    @abstractmethod
    async def chat(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        **kwargs,
    ) -> str:
        ...

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        **kwargs,
    ) -> AsyncIterator[str]:
        """Default fallback: one complete chunk."""
        yield await self.chat(
            messages=messages,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    async def chat_with_tools(
        self,
        messages: List[Dict],
        tools: List[Dict],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        force_thinking: bool = False,
        **kwargs,
    ) -> ToolCallResponse:
        raise NotImplementedError(
            f"{type(self).__name__} does not implement native tool calling."
        )

    @staticmethod
    def _system_str(system: Union[str, SystemPromptParts, None]) -> Optional[str]:
        if system is None:
            return None
        if isinstance(system, SystemPromptParts):
            return system.full
        return system


# ---------------------------------------------------------------------------
# LiteLLM adapter
# ---------------------------------------------------------------------------

#: Historical provider names -> LiteLLM route prefixes.
_PROVIDER_ROUTES: Dict[str, str] = {
    "anthropic": "anthropic",
    "claude": "anthropic",
    "openai": "openai",
    "azure": "azure",
    "azure_openai": "azure",
    "gemini": "gemini",
    "google": "gemini",
    "deepseek": "deepseek",
    "alibaba": "dashscope",
    "dashscope": "dashscope",
    "qwen": "dashscope",
    "experientiallabs": "openai",
    "experiential": "openai",
    "openai_compatible": "openai",
    "openai-compatible": "openai",
    "custom": "openai",
    "local": "openai",
    "ollama": "ollama",
    "openrouter": "openrouter",
    "mistral": "mistral",
    "groq": "groq",
    "cohere": "cohere",
    "xai": "xai",
    "together": "together_ai",
    "together_ai": "together_ai",
    "fireworks": "fireworks_ai",
    "fireworks_ai": "fireworks_ai",
    "perplexity": "perplexity",
    "deepinfra": "deepinfra",
    "cerebras": "cerebras",
    "sambanova": "sambanova",
    "nvidia_nim": "nvidia_nim",
    "huggingface": "huggingface",
    "replicate": "replicate",
    "bedrock": "bedrock",
    "vertex": "vertex_ai",
    "vertex_ai": "vertex_ai",
    # Pass the model string through untouched.
    "litellm": "",
    "auto": "",
}

#: Providers whose credentials are normally required.
_KEY_REQUIRED_ROUTES = {
    "anthropic", "openai", "gemini", "deepseek", "dashscope", "openrouter",
    "mistral", "groq", "cohere", "xai", "together_ai", "fireworks_ai",
    "perplexity", "deepinfra", "cerebras", "sambanova", "nvidia_nim",
}

#: Routes that must stay on the non-streaming path.
#:
#: Anthropic's extended thinking replays *signed* thinking blocks, and those
#: signatures are only guaranteed to be complete on the full response. Streaming
#: there would silently drop them and 400 the following turn.
_NON_STREAM_ROUTES = {"anthropic"}

#: Conventional environment variables checked when api_key_env is not set
#: (or is set to the wrong provider's variable).
_PROVIDER_KEY_ENVS: Dict[str, List[str]] = {
    "anthropic": ["ANTHROPIC_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY"],
    "dashscope": ["DASHSCOPE_API_KEY", "ALIBABA_API_KEY"],
    "openrouter": ["OPENROUTER_API_KEY"],
    "mistral": ["MISTRAL_API_KEY"],
    "groq": ["GROQ_API_KEY"],
    "cohere": ["COHERE_API_KEY"],
    "xai": ["XAI_API_KEY"],
    "together_ai": ["TOGETHER_API_KEY", "TOGETHER_AI_API_KEY"],
    "fireworks_ai": ["FIREWORKS_API_KEY", "FIREWORKS_AI_API_KEY"],
    "perplexity": ["PERPLEXITY_API_KEY"],
    "deepinfra": ["DEEPINFRA_API_KEY"],
    "cerebras": ["CEREBRAS_API_KEY"],
    "sambanova": ["SAMBANOVA_API_KEY"],
    "nvidia_nim": ["NVIDIA_NIM_API_KEY", "NVIDIA_API_KEY"],
    "azure": ["AZURE_API_KEY"],
}

#: Provider-name aliases for gateways whose LiteLLM route is generic.
_PROVIDER_NAME_KEY_ENVS: Dict[str, List[str]] = {
    "experientiallabs": ["EXPERIENTIAL_API_KEY"],
    "experiential": ["EXPERIENTIAL_API_KEY"],
}

_DEFAULT_BASE_URLS: Dict[str, str] = {
    "experientiallabs": "https://api.experientiallabs.ai/v1",
    "experiential": "https://api.experientiallabs.ai/v1",
    # Preserve the international DashScope endpoint the old adapter used.
    "alibaba": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "dashscope": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
}

_ADAPTIVE_ANTHROPIC_HINTS = ("-4-7", "-4-6", "4.7", "4.6", "mythos", "fable")


def _litellm():
    """Import LiteLLM lazily so the error message is actionable."""
    try:
        import litellm  # noqa: WPS433 (runtime dependency)
    except ImportError as exc:  # pragma: no cover - depends on install
        raise ImportError(
            "LiteLLM is required for model inference. Install it with "
            "`pip install litellm` (or reinstall codepilot-ai)."
        ) from exc
    litellm.suppress_debug_info = True
    return litellm


def _first_env(names: List[str]) -> Optional[str]:
    for name in names:
        if not name:
            continue
        value = os.getenv(name)
        if value:
            return value
    return None


def _json_safe(value: Any) -> Any:
    """Return a JSON-serializable copy of provider metadata."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return _json_safe(value.model_dump())
        except Exception:
            pass
    return str(value)


def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a dict or an SDK object."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class LiteLLMProvider(LLMProvider):
    """
    Single adapter for every provider LiteLLM supports.

    ``api_base`` is optional and is forwarded to LiteLLM for OpenAI-compatible
    endpoints (vLLM, LM Studio, Ollama's OpenAI endpoint, gateways, ...).
    ``api_version`` covers Azure deployments.
    """

    _SYSTEM_CACHE_TTL = "1h"
    _CONV_CACHE_TTL: Optional[str] = None

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: Optional[str] = None,
        api_key_env: str = "",
        api_base: Optional[str] = None,
        api_version: Optional[str] = None,
        thinking_enabled: bool = False,
        thinking_budget: int = 8000,
        reasoning_effort: str = "high",
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        self.provider_name = (provider or "openai").strip().lower()
        self.model_name = (model or "").strip()
        self.api_key_env = (api_key_env or "").strip()
        self.api_base = (api_base or "").strip() or None
        self.api_version = (api_version or "").strip() or None
        self.thinking_enabled = bool(thinking_enabled)
        self.thinking_budget = int(thinking_budget or 8000)
        self.reasoning_effort = (reasoning_effort or "high").strip().lower()
        self.extra_headers = dict(extra_headers or {})

        self._route = self._resolve_route_prefix(self.provider_name)
        self.model = self._resolve_model_route()
        self.api_key = self._resolve_api_key(api_key)

        if not self.api_base:
            self.api_base = self._resolve_default_base_url()

        self.supports_cache_refresh = self._route == "anthropic"

    # ------------------------------------------------------------------ #
    #  Route / key resolution                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_route_prefix(provider: str) -> str:
        if provider in _PROVIDER_ROUTES:
            return _PROVIDER_ROUTES[provider]
        # Unknown provider: treat its name as a LiteLLM prefix. This covers the
        # long tail of LiteLLM providers without maintaining a second registry.
        if re.fullmatch(r"[a-z0-9_\-]+", provider or ""):
            return provider
        return "openai"

    def _resolve_model_route(self) -> str:
        if not self.model_name:
            raise ValueError("Model name cannot be empty.")

        if self._route == "":
            # Raw LiteLLM route (provider: litellm/auto).
            return self.model_name

        prefix = f"{self._route}/"
        if self.model_name.startswith(prefix):
            return self.model_name
        return prefix + self.model_name

    def _resolve_api_key(self, api_key: Optional[str]) -> Optional[str]:
        if api_key:
            return api_key

        env_names: List[str] = []
        if self.api_key_env:
            env_names.append(self.api_key_env)
        # Provider-name aliases cover gateways whose LiteLLM route is generic
        # (e.g. Experiential Labs rides on the `openai` route).
        env_names.extend(_PROVIDER_NAME_KEY_ENVS.get(self.provider_name, []))
        env_names.extend(_PROVIDER_KEY_ENVS.get(self._route, []))

        found = _first_env(env_names)
        if found:
            return found

        # A user-supplied base URL usually means a local or self-hosted
        # OpenAI-compatible server that needs no authentication. The OpenAI
        # SDK still wants a non-empty string, so use a harmless placeholder.
        if self.api_base:
            return "not-needed"

        # Ollama serves local models without credentials.
        if self._route == "ollama":
            return "not-needed"

        # Raw LiteLLM route: the model string carries its own provider prefix,
        # so let LiteLLM resolve that provider's credentials itself.
        if self._route == "":
            return None

        if self._route in _KEY_REQUIRED_ROUTES:
            expected_names = list(dict.fromkeys(
                ([self.api_key_env] if self.api_key_env else [])
                + _PROVIDER_NAME_KEY_ENVS.get(self.provider_name, [])
                + _PROVIDER_KEY_ENVS.get(self._route, [f"{self.provider_name.upper()}_API_KEY"])
            ))
            raise EnvironmentError(
                f"API key for provider '{self.provider_name}' is not set. "
                f"Set {', '.join(expected_names)} in the environment (or enter a key in Settings)."
            )

        # Let LiteLLM resolve provider-specific credentials (AWS, Vertex, etc.).
        return None

    def _resolve_default_base_url(self) -> Optional[str]:
        return _DEFAULT_BASE_URLS.get(self.provider_name)

    # ------------------------------------------------------------------ #
    #  Request assembly                                                    #
    # ------------------------------------------------------------------ #

    @property
    def _explicit_anthropic_cache(self) -> bool:
        return self._route == "anthropic"

    def _system_message(
        self,
        system: Union[str, SystemPromptParts, None],
    ) -> Optional[Dict[str, Any]]:
        if system is None:
            return None

        if not self._explicit_anthropic_cache:
            text = self._system_str(system)
            if not text:
                return None
            return {"role": "system", "content": text}

        if isinstance(system, SystemPromptParts) and system.static:
            blocks: List[Dict[str, Any]] = [{
                "type": "text",
                "text": system.static,
                "cache_control": _cache_control(self._SYSTEM_CACHE_TTL),
            }]
            if system.dynamic:
                blocks.append({"type": "text", "text": system.dynamic})
            return {"role": "system", "content": blocks}

        text = self._system_str(system)
        if not text:
            return None
        # A plain-string system prompt still gets one cache breakpoint so
        # direct ``chat()`` callers benefit from prompt caching too.
        return {
            "role": "system",
            "content": [{
                "type": "text",
                "text": text,
                "cache_control": _cache_control(self._SYSTEM_CACHE_TTL),
            }],
        }

    def _continuity_keys(self) -> set[str]:
        """
        Which provider-continuity fields are meaningful for this route.

        Sending another provider's private message fields to a strict API
        (notably OpenAI) can 400 the request, so filter them per route rather
        than relying on LiteLLM to scrub arbitrary message keys.
        """
        if self._route == "anthropic":
            return {"thinking_blocks"}
        if self._route == "deepseek":
            return {"reasoning_content"}
        if self._route in ("gemini", "vertex_ai", "dashscope"):
            return {"reasoning_content", "provider_specific_fields"}
        if self.provider_name in {
            "experientiallabs", "experiential", "openai_compatible",
            "openai-compatible", "custom", "local", "openrouter", "ollama",
            "litellm", "auto",
        }:
            return {"reasoning_content", "provider_specific_fields"}
        return set()

    def _filter_continuity(self, wire: List[Dict]) -> List[Dict]:
        """Remove provider-continuity fields that this route must not receive."""
        allowed = self._continuity_keys()
        for msg in wire:
            if msg.get("role") != "assistant":
                continue
            for key in ("thinking_blocks", "reasoning_content", "provider_specific_fields"):
                if key not in allowed:
                    msg.pop(key, None)
        return wire

    def _build_messages(
        self,
        messages: List[Dict],
        system: Union[str, SystemPromptParts, None],
        *,
        messages_are_formatted: bool = False,
    ) -> List[Dict]:
        wire = (
            [dict(m) for m in messages]
            if messages_are_formatted
            else format_messages_for_provider(messages, "openai")
        )

        wire = self._filter_continuity(wire)

        if self._explicit_anthropic_cache:
            wire = _insert_cache_breakpoints(wire, ttl=self._CONV_CACHE_TTL)

        system_message = self._system_message(system)
        if system_message is not None:
            wire = [system_message] + wire
        return wire

    def _thinking_params(self, *, force_thinking: bool = False) -> Dict[str, Any]:
        enabled = self.thinking_enabled or force_thinking
        if not enabled:
            return {}

        if self._route == "anthropic":
            if any(hint in self.model_name for hint in _ADAPTIVE_ANTHROPIC_HINTS):
                return {"thinking": {"type": "adaptive", "display": "summarized"}}
            return {"thinking": {"type": "enabled", "budget_tokens": self.thinking_budget}}

        if self._route == "deepseek":
            return {"thinking": {"type": "enabled"}}

        # OpenAI, Gemini, DashScope, and most OpenAI-compatible gateways.
        effort = self.reasoning_effort if self.reasoning_effort != "off" else "none"
        return {"reasoning_effort": effort}

    def _temperature_for_call(self, temperature: float, *, force_thinking: bool = False) -> Optional[float]:
        enabled = self.thinking_enabled or force_thinking
        if enabled and self._route in ("anthropic", "deepseek"):
            # Anthropic requires temperature=1 with extended thinking; DeepSeek
            # ignores temperature in thinking mode, so omit it entirely.
            return 1.0 if self._route == "anthropic" else None
        return temperature

    def _base_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "drop_params": True,
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_version:
            kwargs["api_version"] = self.api_version
        if self.extra_headers:
            kwargs["extra_headers"] = dict(self.extra_headers)
        return kwargs

    def _skip_mcp_handler_if_tools(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Bypass LiteLLM's optional MCP handler for plain function tools.

        LiteLLM 1.106.x imports its optional ``mcp`` package whenever ``tools``
        is present, even when every tool is a regular function schema. The
        runtime images do not install that package, so the import fails before
        the request reaches the provider. CodePilot proxies MCP servers as
        ordinary function tools, so the handler is never needed here.
        """
        if request.get("tools") and "_skip_mcp_handler" not in request:
            request["_skip_mcp_handler"] = True
        return request

    # ------------------------------------------------------------------ #
    #  Response parsing                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_tool_calls(message: Any) -> List[ToolCall]:
        raw_calls = _get_field(message, "tool_calls", None) or []
        calls: List[ToolCall] = []
        for tc in raw_calls:
            fn = _get_field(tc, "function", None)
            call_id = str(_get_field(tc, "id", "") or "")
            name = str(_get_field(fn, "name", "") or _get_field(tc, "name", "") or "")
            raw_args = _get_field(fn, "arguments", None)
            if raw_args is None:
                raw_args = _get_field(tc, "arguments", "") or ""
            if isinstance(raw_args, dict):
                args, repaired = raw_args, False
                raw_str = json.dumps(raw_args)
            else:
                raw_str = str(raw_args or "")
                try:
                    args, repaired = parse_tool_arguments(raw_str)
                except ValueError:
                    args, repaired = {}, False
            calls.append(ToolCall(
                call_id=call_id,
                name=name,
                arguments=args,
                raw_arguments=raw_str,
                was_repaired=repaired,
            ))
        return calls

    @staticmethod
    def _usage_dict(response: Any) -> Dict[str, int]:
        usage = _get_field(response, "usage", None)
        if usage is None:
            return {}
        out: Dict[str, int] = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = _get_field(usage, key, None)
            if value is not None:
                out[key] = int(value)
        details = _get_field(usage, "prompt_tokens_details", None)
        cached = _get_field(details, "cached_tokens", None) if details is not None else None
        if cached is None:
            cached = _get_field(usage, "cache_read_input_tokens", None)
        if cached is not None:
            out["cached_tokens"] = int(cached)
        return out

    def _parse_response(self, response: Any) -> ToolCallResponse:
        choices = _get_field(response, "choices", None) or []
        if not choices:
            raise RuntimeError("LLM returned no choices.")

        choice = choices[0]
        message = _get_field(choice, "message", None)
        if message is None:
            raise RuntimeError("LLM returned a choice without a message.")

        content = _get_field(message, "content", "") or ""
        if isinstance(content, list):
            # Some gateways return content blocks instead of a string.
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif isinstance(block, str):
                    parts.append(block)
            content = "\n".join(p for p in parts if p)

        thinking_blocks = _json_safe(_get_field(message, "thinking_blocks", None) or [])
        if not isinstance(thinking_blocks, list):
            thinking_blocks = []

        reasoning_content = _get_field(message, "reasoning_content", None)
        if reasoning_content is not None:
            reasoning_content = str(reasoning_content)

        provider_fields = _json_safe(_get_field(message, "provider_specific_fields", None) or {})
        if not isinstance(provider_fields, dict):
            provider_fields = {}

        return ToolCallResponse(
            text=str(content),
            tool_calls=self._parse_tool_calls(message),
            stop_reason=str(_get_field(choice, "finish_reason", "") or ""),
            thinking_blocks=thinking_blocks,
            reasoning_content=reasoning_content,
            provider_specific_fields=provider_fields,
            usage=self._usage_dict(response),
        )

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def chat(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        **kwargs,
    ) -> str:
        litellm = _litellm()
        request = self._base_kwargs()
        request.update(
            messages=self._build_messages(messages, system),
            max_tokens=max_tokens,
            **self._thinking_params(),
        )
        temp = self._temperature_for_call(temperature)
        if temp is not None:
            request["temperature"] = temp
        request.update(kwargs)

        response = await litellm.acompletion(**self._skip_mcp_handler_if_tools(request))
        parsed = self._parse_response(response)

        parts: List[str] = []
        if parsed.reasoning_content:
            parts.append(f"<thinking>\n{parsed.reasoning_content}\n</thinking>")
        for block in parsed.thinking_blocks:
            if isinstance(block, dict) and block.get("type") == "thinking":
                thinking = block.get("thinking") or block.get("text")
                if thinking:
                    parts.append(f"<thinking>\n{thinking}\n</thinking>")
        if parsed.text:
            parts.append(parsed.text)
        return "\n".join(parts)

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        **kwargs,
    ) -> AsyncIterator[str]:
        litellm = _litellm()
        request = self._base_kwargs()
        request.update(
            messages=self._build_messages(messages, system),
            max_tokens=max_tokens,
            stream=True,
            **self._thinking_params(),
        )
        temp = self._temperature_for_call(temperature)
        if temp is not None:
            request["temperature"] = temp
        request.update(kwargs)

        stream = await litellm.acompletion(**self._skip_mcp_handler_if_tools(request))
        in_thinking = False
        async for chunk in stream:
            choices = _get_field(chunk, "choices", None) or []
            if not choices:
                continue
            delta = _get_field(choices[0], "delta", None)
            if delta is None:
                continue

            reasoning = _get_field(delta, "reasoning_content", None)
            if reasoning:
                if not in_thinking:
                    in_thinking = True
                    yield "<thinking>\n"
                yield str(reasoning)
                continue

            text = _get_field(delta, "content", None)
            if text:
                if in_thinking:
                    in_thinking = False
                    yield "\n</thinking>\n"
                yield str(text)

        if in_thinking:
            yield "\n</thinking>\n"

    async def chat_with_tools(
        self,
        messages: List[Dict],
        tools: List[Dict],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        force_thinking: bool = False,
        on_text: Optional[Callable[[str], None]] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        **kwargs,
    ) -> ToolCallResponse:
        litellm = _litellm()
        request = self._base_kwargs()
        request.update(
            messages=self._build_messages(messages, system),
            max_tokens=max_tokens,
            **self._thinking_params(force_thinking=force_thinking),
        )
        temp = self._temperature_for_call(temperature, force_thinking=force_thinking)
        if temp is not None:
            request["temperature"] = temp

        if tools:
            request["tools"] = [dict(t) for t in tools]
            request["tool_choice"] = "auto"

        request.update(kwargs)

        # Token-level streaming: hand content/reasoning deltas to the caller as
        # they arrive while still assembling complete tool calls. Without this
        # the runtime can only emit the finished text as one blob, which is
        # what the UI showed as "Working…" followed by a sudden dump.
        if (on_text is not None or on_thinking is not None) and self._route not in _NON_STREAM_ROUTES:
            request["stream"] = True
            return await self._chat_with_tools_streamed(request, on_text, on_thinking)

        response = await litellm.acompletion(**self._skip_mcp_handler_if_tools(request))
        return self._parse_response(response)

    async def _chat_with_tools_streamed(
        self,
        request: Dict[str, Any],
        on_text: Optional[Callable[[str], None]] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
    ) -> ToolCallResponse:
        """Consume a streaming completion and assemble the full turn.

        Text and reasoning are forwarded to the callbacks as deltas arrive.
        Tool-call fragments are accumulated instead, because one call's JSON
        arguments can be split across many chunks and only parse once the
        stream ends.
        """
        litellm = _litellm()
        stream = await litellm.acompletion(**self._skip_mcp_handler_if_tools(request))

        accumulator = StreamingToolCallAccumulator()
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        thinking_blocks: List[Any] = []
        provider_fields: Dict[str, Any] = {}
        stop_reason = ""
        usage: Dict[str, int] = {}

        async for chunk in stream:
            chunk_usage = self._usage_dict(chunk)
            if chunk_usage:
                usage = chunk_usage

            choices = _get_field(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]

            finish = _get_field(choice, "finish_reason", None)
            if finish:
                stop_reason = str(finish)

            delta = _get_field(choice, "delta", None)
            if delta is None:
                continue

            reasoning = _get_field(delta, "reasoning_content", None)
            if reasoning:
                reasoning_parts.append(str(reasoning))
                if on_thinking is not None:
                    on_thinking(str(reasoning))

            blocks = _get_field(delta, "thinking_blocks", None)
            if blocks:
                thinking_blocks.extend(_json_safe(blocks) or [])

            fields = _get_field(delta, "provider_specific_fields", None)
            if isinstance(fields, dict):
                provider_fields.update(_json_safe(fields) or {})

            tool_deltas = _get_field(delta, "tool_calls", None)
            if tool_deltas:
                accumulator.feed(tool_deltas)

            text = _get_field(delta, "content", None)
            if text:
                text_parts.append(str(text))
                if on_text is not None:
                    on_text(str(text))

        return ToolCallResponse(
            text="".join(text_parts),
            tool_calls=accumulator.finish(),
            stop_reason=stop_reason,
            thinking_blocks=thinking_blocks,
            reasoning_content="".join(reasoning_parts) or None,
            provider_specific_fields=provider_fields,
            usage=usage,
        )

    # ------------------------------------------------------------------ #
    #  Anthropic cache TTL refresh                                         #
    # ------------------------------------------------------------------ #

    async def refresh_cache_ttl(
        self,
        messages: List[Dict],
        system: Union[str, SystemPromptParts, None] = None,
    ) -> None:
        """
        Fire a minimal inference call that rewrites the conversation cache
        breakpoints with a 1-hour TTL. Best-effort: failures are ignored.
        """
        if not self.supports_cache_refresh:
            return

        refreshed = format_messages_for_provider(messages, "openai")
        refreshed = self._filter_continuity(refreshed)
        refreshed = _insert_cache_breakpoints(refreshed, ttl="1h")

        has_cache = False
        for msg in refreshed:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if isinstance(content, list) and content:
                has_cache = True
                break
            if msg.get("tool_calls"):
                has_cache = True
                break
        if not has_cache:
            return

        # Anthropic requires a final user turn. If the last message is an
        # assistant message with pending tool calls, a bare "continue" would be
        # invalid; add matching tool_result blocks first.
        last = refreshed[-1]
        if last.get("role") == "assistant" and last.get("tool_calls"):
            for tc in last.get("tool_calls") or []:
                call_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "call_id", "")
                refreshed.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": "ok",
                })
        if refreshed[-1].get("role") != "user":
            refreshed.append({"role": "user", "content": "continue"})

        system_message = self._system_message(system)
        if system_message is not None:
            refreshed = [system_message] + refreshed

        litellm = _litellm()
        request = self._base_kwargs()
        request.update(
            messages=refreshed,
            max_tokens=1,
            temperature=0.0,
        )
        try:
            await litellm.acompletion(**self._skip_mcp_handler_if_tools(request))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Backward-compatible aliases
# ---------------------------------------------------------------------------
#
# These keep existing imports working for embedders that instantiated a
# provider directly. New code should use ``get_provider`` / LiteLLMProvider.

class AnthropicProvider(LiteLLMProvider):
    def __init__(self, api_key: str, model: str, thinking_enabled: bool = False,
                 thinking_budget: int = 8000, reasoning_effort: str = "high", **kwargs):
        super().__init__(
            provider="anthropic", model=model, api_key=api_key,
            thinking_enabled=thinking_enabled, thinking_budget=thinking_budget,
            reasoning_effort=reasoning_effort, **kwargs,
        )


class OpenAIProvider(LiteLLMProvider):
    def __init__(self, api_key: str, model: str, thinking_enabled: bool = False,
                 reasoning_effort: str = "high", **kwargs):
        super().__init__(
            provider="openai", model=model, api_key=api_key,
            thinking_enabled=thinking_enabled, reasoning_effort=reasoning_effort,
            **kwargs,
        )


class DeepSeekProvider(LiteLLMProvider):
    def __init__(self, api_key: str, model: str, thinking_enabled: bool = False,
                 reasoning_effort: str = "high", **kwargs):
        super().__init__(
            provider="deepseek", model=model, api_key=api_key,
            thinking_enabled=thinking_enabled, reasoning_effort=reasoning_effort,
            **kwargs,
        )


class AlibabaProvider(LiteLLMProvider):
    def __init__(self, api_key: str, model: str, thinking_enabled: bool = False, **kwargs):
        super().__init__(
            provider="alibaba", model=model, api_key=api_key,
            thinking_enabled=thinking_enabled, **kwargs,
        )


class ExperientialLabsProvider(LiteLLMProvider):
    def __init__(self, api_key: str, model: str, thinking_enabled: bool = False,
                 reasoning_effort: str = "high", **kwargs):
        super().__init__(
            provider="experientiallabs", model=model, api_key=api_key,
            thinking_enabled=thinking_enabled, reasoning_effort=reasoning_effort,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_provider(config) -> LLMProvider:
    """Build a provider from an AgentFile ``ModelConfig``."""
    return LiteLLMProvider(
        provider=config.provider,
        model=config.name,
        api_key_env=getattr(config, "api_key_env", "") or "",
        api_base=getattr(config, "api_base", None),
        api_version=getattr(config, "api_version", None),
        thinking_enabled=config.thinking.enabled,
        thinking_budget=config.thinking.budget_tokens,
        reasoning_effort=config.thinking.reasoning_effort,
    )
