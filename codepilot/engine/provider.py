"""
File: provider.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description:
LLM provider abstraction layer for the CodePilot agentic runtime.

Architectural Notes:
Implements a unified async interface (LLMProvider) for OpenAI, Anthropic,
AlibabaCloud/Qwen, and DeepSeek providers. Handles explicit prompt caching
(cache_control breakpoints) for Anthropic and Alibaba, and extended thinking
for Claude and DeepSeek.
Rolling cache breakpoints are injected on the last assistant message to
maximise token reuse across agentic steps without redundant re-processing.
DeepSeek uses fully automatic server-side caching — no breakpoints needed.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Iterator, AsyncIterator, Union
import os

from ..core.prompt import SystemPromptParts
import re


def _insert_cache_breakpoints(messages: List[Dict], ttl: Optional[Union[str, int]] = None) -> List[Dict]:
    """
    Inserts cache_control breakpoints on up to three key assistant messages
    (plus the System prompt which is handled separately, totalling 4 allowed breakpoints).
    
    1. The assistant message ending the PREVIOUS task (anchors completed history).
    2. The assistant message anchoring chunks of 5 steps in the CURRENT task.
    3. The most recent assistant message (for background TTL refresh).
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

    active_asst_indices = []
    for i in range(current_task_start_idx, len(messages)):
        if messages[i].get("role") == "assistant":
            active_asst_indices.append(i)

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
        content = msg.get("content", "")
        cache_ctrl = {"type": "ephemeral"}
        if ttl is not None:
            cache_ctrl["ttl"] = ttl

        result[idx] = {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": content if isinstance(content, str) else content,
                    "cache_control": cache_ctrl,
                }
            ],
        }

    return result


class LLMProvider(ABC):
    @abstractmethod
    async def chat(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        ...

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        """
        Stream LLM response token by token.

        Default fallback: calls chat() and yields the complete response as a
        single chunk.  Override in subclasses for true token-level streaming.
        """
        yield await self.chat(
            messages=messages, system=system,
            temperature=temperature, max_tokens=max_tokens,
        )

    # ------------------------------------------------------------------ #
    #  Helper: resolve SystemPromptParts to a flat string                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _system_str(system: Union[str, SystemPromptParts, None]) -> Optional[str]:
        """Collapse *system* to a plain string (for providers with no split support)."""
        if system is None:
            return None
        if isinstance(system, SystemPromptParts):
            return system.full
        return system


class OpenAIProvider(LLMProvider):
    """OpenAI — caching is fully automatic, no API changes needed."""

    def __init__(
        self,
        api_key: str,
        model: str,
        thinking_enabled: bool = False,
        reasoning_effort: str = "high",
    ):
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise ImportError("Install the openai package: pip install openai")
        self.client           = AsyncOpenAI(api_key=api_key)
        self.model            = model
        self.thinking_enabled = thinking_enabled
        self.reasoning_effort = reasoning_effort

    async def chat(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        msgs = []
        sys_text = self._system_str(system)
        if sys_text:
            msgs.append({"role": "system", "content": sys_text})
        msgs.extend(messages)

        kwargs = dict(
            model=self.model,
            messages=msgs,
            max_tokens=max_tokens,
        )

        if self.thinking_enabled:
            # temperature is unsupported for reasoning models.
            # Map 'max' (DeepSeek-specific) to 'high' for OpenAI.
            kwargs["reasoning_effort"] = "high" if self.reasoning_effort == "max" else self.reasoning_effort
        else:
            kwargs["temperature"] = temperature

        response = await self.client.chat.completions.create(**kwargs)
        return response.choices[0].message.content

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        msgs = []
        sys_text = self._system_str(system)
        if sys_text:
            msgs.append({"role": "system", "content": sys_text})
        msgs.extend(messages)

        kwargs = dict(
            model=self.model,
            messages=msgs,
            max_tokens=max_tokens,
            stream=True,
        )

        if self.thinking_enabled:
            # temperature is unsupported for reasoning models.
            # Map 'max' (DeepSeek-specific) to 'high' for OpenAI.
            kwargs["reasoning_effort"] = "high" if self.reasoning_effort == "max" else self.reasoning_effort
        else:
            kwargs["temperature"] = temperature

        stream = await self.client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content


class AnthropicProvider(LLMProvider):
    """
    Anthropic Claude — explicit cache control.

    System prompt:
        Split into two content blocks: static (cache_control 1h) and dynamic
        (no cache_control).  The static block contains tools, rules, and the
        example — it is identical across steps and cached.

    Conversational messages:
        A rolling breakpoint is placed on the last assistant message with a
        5-minute TTL.  This ensures the growing conversation prefix is cached
        and only the newest messages are re-processed.
    """

    # Minimum tokens for Anthropic to honour a cache breakpoint.
    # Sonnet 4.6: 2048.  Haiku / Opus: 1024–4096.
    # We set the breakpoint regardless — below-threshold, Anthropic silently
    # ignores it (no error), so this is always safe.
    _SYSTEM_CACHE_TTL = "1h"       # system prompt → survives between tasks
    _CONV_CACHE_TTL   = None       # conversation → 5min default (omit ttl field)

    def __init__(
        self,
        api_key: str,
        model: str,
        thinking_enabled: bool = False,
        thinking_budget: int = 8000,
        reasoning_effort: str = "high",
    ):
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            raise ImportError("Install the anthropic package: pip install anthropic")
        self.client           = AsyncAnthropic(api_key=api_key)
        self.model            = model
        self.thinking_enabled = thinking_enabled
        self.thinking_budget  = thinking_budget
        self.reasoning_effort = reasoning_effort

    # ------------------------------------------------------------------ #
    #  System prompt → two content blocks with cache_control               #
    # ------------------------------------------------------------------ #

    @classmethod
    def _build_system_blocks(
        cls,
        system: Union[str, SystemPromptParts, None],
    ) -> list:
        """
        Return the Anthropic `system` parameter as a list of content blocks.

        If *system* is a SystemPromptParts, the static half gets cache_control
        and the dynamic half does not.  Otherwise we fall back to a single
        non-cached block.
        """
        if system is None:
            return []

        if isinstance(system, SystemPromptParts) and system.static:
            blocks = [
                {
                    "type": "text",
                    "text": system.static,
                    "cache_control": {"type": "ephemeral", "ttl": cls._SYSTEM_CACHE_TTL},
                },
            ]
            if system.dynamic:
                blocks.append({
                    "type": "text",
                    "text": system.dynamic,
                })
            return blocks

        # Plain string or empty dynamic — single un-cached block.
        text = system if isinstance(system, str) else system.full
        return [{"type": "text", "text": text}]

    # ------------------------------------------------------------------ #
    #  Messages → rolling breakpoint on last assistant message             #
    # ------------------------------------------------------------------ #

    @classmethod
    def _add_rolling_breakpoint(cls, messages: List[Dict]) -> List[Dict]:
        return _insert_cache_breakpoints(messages, ttl=cls._CONV_CACHE_TTL)

    # ------------------------------------------------------------------ #
    #  TTL refresh call — upgrade last breakpoint to 1h                    #
    # ------------------------------------------------------------------ #

    async def refresh_cache_ttl(self, messages: List[Dict], system=None) -> None:
        """
        Fire a minimal inference call to upgrade the conversational cache
        breakpoint TTL to 1 hour.

        Called by the runtime's background timer when the agent goes idle.
        The response is discarded — context is NOT modified.
        """
        refreshed = _insert_cache_breakpoints(messages, ttl="1h")
        
        has_cache = False
        for msg in refreshed:
            if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
                has_cache = True
                break
                
        if not has_cache:
            return  # Nothing to refresh.

        # We need a final user message after the breakpoint for the API call.
        # If the last message is already user, we're fine.
        # If the last message is assistant (chat mode), append a minimal user msg.
        if refreshed[-1].get("role") != "user":
            refreshed.append({"role": "user", "content": "continue"})

        kwargs = dict(
            model=self.model,
            messages=refreshed,
            max_tokens=1,
            temperature=0.0,
        )

        system_blocks = self._build_system_blocks(system)
        if system_blocks:
            kwargs["system"] = system_blocks

        try:
            await self.client.messages.create(**kwargs)
        except Exception:
            pass  # Refresh is best-effort — failure just means cache expires.

    # ------------------------------------------------------------------ #
    #  Main chat / stream                                                  #
    # ------------------------------------------------------------------ #

    async def chat(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        kwargs = dict(
            model=self.model,
            messages=self._add_rolling_breakpoint(messages),
            # Extended thinking requires temperature=1.0 — enforce it regardless
            # of what the agent config says.
            temperature=1.0 if self.thinking_enabled else temperature,
            max_tokens=max_tokens,
        )

        if self.thinking_enabled:
            is_adaptive = any(x in self.model for x in ("-4-7", "-4-6", "4.7", "4.6", "mythos"))
            if is_adaptive:
                kwargs["thinking"] = {
                    "type": "adaptive",
                    "effort": self.reasoning_effort.lower(),
                }
            else:
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self.thinking_budget,
                }

        system_blocks = self._build_system_blocks(system)
        if system_blocks:
            kwargs["system"] = system_blocks

        response = await self.client.messages.create(**kwargs)

        # Reconstruct the full response string, including thinking tags so they:
        #   (a) stream to the user via _emit_prefence_text in non-stream mode, and
        #   (b) are stored in the assistant turn for conversational continuity.
        # Iterating blocks is also the crash-safe way to extract text —
        # content[0] is a ThinkingBlock when extended thinking is on.
        parts = []
        for block in response.content:
            if block.type == "thinking":
                parts.append(f"<thinking>\n{block.thinking}\n</thinking>")
            elif block.type == "text":
                parts.append(block.text)
        return "\n".join(parts)

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        kwargs = dict(
            model=self.model,
            messages=self._add_rolling_breakpoint(messages),
            temperature=1.0 if self.thinking_enabled else temperature,
            max_tokens=max_tokens,
        )

        if self.thinking_enabled:
            is_adaptive = any(x in self.model for x in ("-4-7", "-4-6", "4.7", "4.6", "mythos"))
            if is_adaptive:
                kwargs["thinking"] = {
                    "type": "adaptive",
                    "effort": self.reasoning_effort.lower(),
                }
            else:
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self.thinking_budget,
                }

        system_blocks = self._build_system_blocks(system)
        if system_blocks:
            kwargs["system"] = system_blocks

        if self.thinking_enabled:
            # Use the raw event stream so we can intercept thinking blocks and
            # wrap them in tags before they hit the runtime's state machine.
            # The state machine emits everything before the first ```codepilot
            # fence to the user in real time — thinking tags ride that path
            # naturally with zero changes to the streaming logic.
            from anthropic.types import (
                ContentBlockStartEvent,
                ContentBlockDeltaEvent,
                ContentBlockStopEvent,
            )
            in_thinking = False
            async with self.client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if isinstance(event, ContentBlockStartEvent):
                        if event.content_block.type == "thinking":
                            in_thinking = True
                            yield "<thinking>\n"
                        else:
                            in_thinking = False
                    elif isinstance(event, ContentBlockDeltaEvent):
                        if event.delta.type == "thinking_delta":
                            yield event.delta.thinking
                        elif event.delta.type == "text_delta":
                            yield event.delta.text
                    elif isinstance(event, ContentBlockStopEvent) and in_thinking:
                        yield "\n</thinking>\n"
                        in_thinking = False
        else:
            async with self.client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    yield text


class AlibabaProvider(LLMProvider):
    """
    Alibaba Cloud DashScope (Qwen) — uses OpenAI client.
    System prompt is cached automatically (no explicit blocks needed).
    Conversational messages use explicit cache_control on the last assistant message.
    """

    def __init__(self, api_key: str, model: str, thinking_enabled: bool = False):
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise ImportError("Install the openai package: pip install openai")
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        )
        self.model            = model
        self.thinking_enabled = thinking_enabled

    @classmethod
    def _add_rolling_breakpoint(cls, messages: List[Dict]) -> List[Dict]:
        return _insert_cache_breakpoints(messages, ttl=300)

    def _build_kwargs(self, messages, system, temperature, max_tokens) -> dict:
        msgs = []
        sys_text = self._system_str(system)
        if sys_text:
            msgs.append({"role": "system", "content": sys_text})
        msgs.extend(self._add_rolling_breakpoint(messages))

        kwargs = dict(
            model=self.model,
            messages=msgs,
            max_tokens=max_tokens,
        )

        if self.thinking_enabled:
            kwargs["extra_body"] = {"enable_thinking": True}
        else:
            kwargs["temperature"] = temperature

        return kwargs

    async def chat(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        kwargs = self._build_kwargs(messages, system, temperature, max_tokens)
        response = await self.client.chat.completions.create(**kwargs)

        msg = response.choices[0].message
        content          = msg.content or ""
        reasoning_content = getattr(msg, "reasoning_content", None) or ""

        if reasoning_content:
            return f"<thinking>\n{reasoning_content}\n</thinking>\n{content}"
        return content

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        kwargs = self._build_kwargs(messages, system, temperature, max_tokens)
        kwargs["stream"] = True

        stream = await self.client.chat.completions.create(**kwargs)

        in_thinking = False

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # --- reasoning / thinking content ---
            reasoning_chunk = getattr(delta, "reasoning_content", None)
            if reasoning_chunk:
                if not in_thinking:
                    # First reasoning token — open the tag.
                    yield "<thinking>\n"
                    in_thinking = True
                yield reasoning_chunk
                continue

            # --- regular content ---
            if in_thinking:
                # Reasoning stream ended — close the tag before answer.
                yield "\n</thinking>\n"
                in_thinking = False

            if delta.content:
                yield delta.content

        # Guard: close tag if stream ended while still in thinking
        if in_thinking:
            yield "\n</thinking>\n"


class DeepSeekProvider(LLMProvider):
    """
    DeepSeek (V4-Flash / V4-Pro) — uses the OpenAI SDK pointed at
    ``https://api.deepseek.com``.

    Context caching:
        Fully automatic on DeepSeek's server side (disk-based KV cache).
        No ``cache_control`` annotations or rolling breakpoints are needed.
        Cache TTL is hours-to-days — no background refresh timer required.

    Thinking mode:
        Enabled via ``extra_body={"thinking": {"type": "enabled"}}`` and
        the ``reasoning_effort`` parameter ("high" or "max").
        Per DeepSeek docs, ``temperature`` is ignored in thinking mode so
        we omit it entirely to avoid confusion.
        Chain-of-thought arrives in ``delta.reasoning_content`` (streaming)
        or ``message.reasoning_content`` (non-streaming).
        It is wrapped in ``<thinking>...</thinking>`` tags and prepended to
        the returned string — exactly like ``AnthropicProvider`` — so the
        runtime's streaming state machine and conversation history work
        identically without any changes.
        Per docs, ``reasoning_content`` does NOT need to be re-sent in
        subsequent turns for non-tool-call conversations; only ``content``
        is stored in history.
    """

    _BASE_URL = "https://api.deepseek.com"

    def __init__(
        self,
        api_key: str,
        model: str,
        thinking_enabled: bool = False,
        reasoning_effort: str = "high",
    ):
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise ImportError("Install the openai package: pip install openai")
        self.client           = AsyncOpenAI(api_key=api_key, base_url=self._BASE_URL)
        self.model            = model
        self.thinking_enabled = thinking_enabled
        self.reasoning_effort = reasoning_effort

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _build_kwargs(self, messages, system, temperature, max_tokens) -> dict:
        """Build the common kwargs dict for a chat completions call."""
        msgs = []
        sys_text = self._system_str(system)
        if sys_text:
            msgs.append({"role": "system", "content": sys_text})
        msgs.extend(messages)

        kwargs = dict(
            model=self.model,
            messages=msgs,
            max_tokens=max_tokens,
        )

        if self.thinking_enabled:
            # temperature is silently ignored by DeepSeek in thinking mode
            # (per docs) — omit it to keep the request clean.
            kwargs["reasoning_effort"] = self.reasoning_effort
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        else:
            kwargs["temperature"] = temperature
            # Must explicitly disable thinking — omitting the parameter defaults
            # to thinking ON for deepseek-v4-flash and deepseek-v4-pro.
            # Per DeepSeek API docs: pass {"thinking": {"type": "disabled"}} to
            # ensure reasoning_content is None and no chain-of-thought is generated.
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        return kwargs

    # ------------------------------------------------------------------ #
    #  chat (non-streaming)                                                #
    # ------------------------------------------------------------------ #

    async def chat(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        kwargs = self._build_kwargs(messages, system, temperature, max_tokens)
        response = await self.client.chat.completions.create(**kwargs)

        msg = response.choices[0].message
        content          = msg.content or ""
        reasoning_content = getattr(msg, "reasoning_content", None) or ""

        if self.thinking_enabled and reasoning_content:
            return f"<thinking>\n{reasoning_content}\n</thinking>\n{content}"
        return content

    # ------------------------------------------------------------------ #
    #  chat_stream (streaming)                                             #
    # ------------------------------------------------------------------ #

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        kwargs = self._build_kwargs(messages, system, temperature, max_tokens)
        kwargs["stream"] = True

        stream = await self.client.chat.completions.create(**kwargs)

        in_thinking = False

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # --- reasoning / thinking content ---
            reasoning_chunk = getattr(delta, "reasoning_content", None)
            if reasoning_chunk:
                if not in_thinking:
                    # First reasoning token — open the tag.
                    yield "<thinking>\n"
                    in_thinking = True
                yield reasoning_chunk
                continue

            # --- regular content ---
            if in_thinking:
                # Reasoning stream ended — close the tag before answer.
                yield "\n</thinking>\n"
                in_thinking = False

            if delta.content:
                yield delta.content

        # Guard: close tag if stream ended while still in thinking
        # (should not happen in practice, but defensive).
        if in_thinking:
            yield "\n</thinking>\n"


def get_provider(config) -> LLMProvider:
    provider_name = config.provider.lower()
    api_key = os.getenv(config.api_key_env)

    if not api_key:
        raise EnvironmentError(
            f"API key environment variable '{config.api_key_env}' is not set. "
            "Export it before running the agent."
        )

    if provider_name == "openai":
        return OpenAIProvider(
            api_key, config.name,
            thinking_enabled=config.thinking.enabled,
            reasoning_effort=config.thinking.reasoning_effort,
        )
    elif provider_name == "anthropic":
        return AnthropicProvider(
            api_key, config.name,
            thinking_enabled=config.thinking.enabled,
            thinking_budget=config.thinking.budget_tokens,
            reasoning_effort=config.thinking.reasoning_effort,
        )
    elif provider_name == "alibaba":
        return AlibabaProvider(
            api_key, config.name,
            thinking_enabled=config.thinking.enabled,
        )
    elif provider_name == "deepseek":
        return DeepSeekProvider(
            api_key, config.name,
            thinking_enabled=config.thinking.enabled,
            reasoning_effort=config.thinking.reasoning_effort,
        )
    else:
        raise ValueError(
            f"Unsupported provider: '{provider_name}'. "
            "Choose from: 'anthropic', 'openai', 'alibaba', 'deepseek'."
        )
