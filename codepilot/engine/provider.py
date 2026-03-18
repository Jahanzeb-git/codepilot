from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Iterator, Union
import os

from ..core.prompt import SystemPromptParts


class LLMProvider(ABC):
    @abstractmethod
    def chat(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        ...

    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> Iterator[str]:
        """
        Stream LLM response token by token.

        Default fallback: calls chat() and yields the complete response as a
        single chunk.  Override in subclasses for true token-level streaming.
        """
        yield self.chat(
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

    def __init__(self, api_key: str, model: str):
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("Install the openai package: pip install openai")
        self.client = OpenAI(api_key=api_key)
        self.model  = model

    def chat(
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

        response = self.client.chat.completions.create(
            model=self.model,
            messages=msgs,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content

    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> Iterator[str]:
        msgs = []
        sys_text = self._system_str(system)
        if sys_text:
            msgs.append({"role": "system", "content": sys_text})
        msgs.extend(messages)

        stream = self.client.chat.completions.create(
            model=self.model,
            messages=msgs,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        for chunk in stream:
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

    def __init__(self, api_key: str, model: str):
        try:
            from anthropic import Anthropic
        except ImportError:
            raise ImportError("Install the anthropic package: pip install anthropic")
        self.client = Anthropic(api_key=api_key)
        self.model  = model

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
        """
        Return a shallow copy of *messages* with cache_control on the last
        assistant message.

        The last assistant message's content is converted to a content-block
        list format so Anthropic can attach cache_control to it.  All other
        messages are returned untouched.
        """
        # Find the index of the last assistant message.
        last_asst_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "assistant":
                last_asst_idx = i
                break

        if last_asst_idx is None:
            return messages  # No assistant message yet — nothing to cache.

        result = list(messages)  # shallow copy of the list

        asst_msg = result[last_asst_idx]
        content = asst_msg.get("content", "")

        # Build the content-block with cache_control.
        cache_ctrl = {"type": "ephemeral"}
        if cls._CONV_CACHE_TTL is not None:
            cache_ctrl["ttl"] = cls._CONV_CACHE_TTL

        cached_msg = {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": content if isinstance(content, str) else content,
                    "cache_control": cache_ctrl,
                }
            ],
        }

        result[last_asst_idx] = cached_msg
        return result

    # ------------------------------------------------------------------ #
    #  TTL refresh call — upgrade last breakpoint to 1h                    #
    # ------------------------------------------------------------------ #

    def refresh_cache_ttl(self, messages: List[Dict], system=None) -> None:
        """
        Fire a minimal inference call to upgrade the conversational cache
        breakpoint TTL to 1 hour.

        Called by the runtime's background timer when the agent goes idle.
        The response is discarded — context is NOT modified.
        """
        # Find the last assistant message.
        last_asst_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "assistant":
                last_asst_idx = i
                break

        if last_asst_idx is None:
            return  # Nothing to refresh.

        # Build messages with 1h TTL breakpoint on last assistant.
        refreshed = list(messages)
        asst_msg = refreshed[last_asst_idx]
        content = asst_msg.get("content", "")

        refreshed[last_asst_idx] = {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": content if isinstance(content, str) else content,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
            ],
        }

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
            self.client.messages.create(**kwargs)
        except Exception:
            pass  # Refresh is best-effort — failure just means cache expires.

    # ------------------------------------------------------------------ #
    #  Main chat / stream                                                  #
    # ------------------------------------------------------------------ #

    def chat(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        kwargs = dict(
            model=self.model,
            messages=self._add_rolling_breakpoint(messages),
            temperature=temperature,
            max_tokens=max_tokens,
        )

        system_blocks = self._build_system_blocks(system)
        if system_blocks:
            kwargs["system"] = system_blocks

        response = self.client.messages.create(**kwargs)
        return response.content[0].text

    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> Iterator[str]:
        kwargs = dict(
            model=self.model,
            messages=self._add_rolling_breakpoint(messages),
            temperature=temperature,
            max_tokens=max_tokens,
        )

        system_blocks = self._build_system_blocks(system)
        if system_blocks:
            kwargs["system"] = system_blocks

        with self.client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                yield text


class AlibabaProvider(LLMProvider):
    """
    Alibaba Cloud DashScope (Qwen) — uses OpenAI client.
    System prompt is cached automatically (no explicit blocks needed).
    Conversational messages use explicit cache_control on the last assistant message.
    """

    def __init__(self, api_key: str, model: str):
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("Install the openai package: pip install openai")
        self.client = OpenAI(
            api_key=api_key,
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        )
        self.model = model

    @classmethod
    def _add_rolling_breakpoint(cls, messages: List[Dict]) -> List[Dict]:
        """
        Return a shallow copy of *messages* with cache_control on the last
        assistant message. DashScope expects exactly this Anthropic-style 
        cache block in the messages list, but with a fixed 300s TTL.
        """
        last_asst_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "assistant":
                last_asst_idx = i
                break

        if last_asst_idx is None:
            return messages

        result = list(messages)
        asst_msg = result[last_asst_idx]
        content = asst_msg.get("content", "")

        result[last_asst_idx] = {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": content if isinstance(content, str) else content,
                    "cache_control": {"type": "ephemeral", "ttl": 300},
                }
            ],
        }
        return result

    def chat(
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
        msgs.extend(self._add_rolling_breakpoint(messages))

        response = self.client.chat.completions.create(
            model=self.model,
            messages=msgs,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content

    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        system: Union[str, SystemPromptParts, None] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> Iterator[str]:
        msgs = []
        sys_text = self._system_str(system)
        if sys_text:
            msgs.append({"role": "system", "content": sys_text})
        msgs.extend(self._add_rolling_breakpoint(messages))

        stream = self.client.chat.completions.create(
            model=self.model,
            messages=msgs,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content


def get_provider(config) -> LLMProvider:
    provider_name = config.provider.lower()
    api_key = os.getenv(config.api_key_env)

    if not api_key:
        raise EnvironmentError(
            f"API key environment variable '{config.api_key_env}' is not set. "
            "Export it before running the agent."
        )

    if provider_name == "openai":
        return OpenAIProvider(api_key, config.name)
    elif provider_name == "anthropic":
        return AnthropicProvider(api_key, config.name)
    elif provider_name == "alibaba":
        return AlibabaProvider(api_key, config.name)
    else:
        raise ValueError(
            f"Unsupported provider: '{provider_name}'. "
            "Choose from: 'anthropic', 'openai', 'alibaba'."
        )
