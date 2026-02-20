from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Iterator
import os


class LLMProvider(ABC):
    @abstractmethod
    def chat(
        self,
        messages: List[Dict[str, str]],
        system: str = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        ...

    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        system: str = None,
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


class OpenAIProvider(LLMProvider):
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
        system: str = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
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
        system: str = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> Iterator[str]:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
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
    def __init__(self, api_key: str, model: str):
        try:
            from anthropic import Anthropic
        except ImportError:
            raise ImportError("Install the anthropic package: pip install anthropic")
        self.client = Anthropic(api_key=api_key)
        self.model  = model

    def chat(
        self,
        messages: List[Dict[str, str]],
        system: str = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if system:
            kwargs["system"] = system

        response = self.client.messages.create(**kwargs)
        return response.content[0].text

    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        system: str = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> Iterator[str]:
        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if system:
            kwargs["system"] = system

        with self.client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                yield text


class TogetherProvider(LLMProvider):
    """Together AI — uses the OpenAI-compatible endpoint."""

    def __init__(self, api_key: str, model: str):
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("Install the openai package: pip install openai")
        self.client = OpenAI(
            api_key=api_key,
            base_url="https://api.together.xyz/v1",
        )
        self.model = model

    def chat(
        self,
        messages: List[Dict[str, str]],
        system: str = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
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
        system: str = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> Iterator[str]:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
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
    elif provider_name == "together":
        return TogetherProvider(api_key, config.name)
    else:
        raise ValueError(
            f"Unsupported provider: '{provider_name}'. "
            "Choose from: 'anthropic', 'openai', 'together'."
        )
