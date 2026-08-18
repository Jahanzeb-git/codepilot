"""Verified model context profiles used when an AgentFile omits memory settings.

Profiles are intentionally keyed by exact provider/model identifiers.  A
provider is not a capacity tier: model limits vary within every provider.
Unknown models must declare their own context window in the AgentFile.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ModelProfile:
    context_tokens: int
    max_output_tokens: int
    recommended_max_tokens: int = 8192


_PROFILES: dict[tuple[str, str], ModelProfile] = {
    # OpenAI, verified August 2026.
    ("openai", "gpt-4o"): ModelProfile(128_000, 16_384),
    # Anthropic, verified August 2026.
    ("anthropic", "claude-fable-5"): ModelProfile(1_000_000, 128_000),
    ("anthropic", "claude-opus-5"): ModelProfile(1_000_000, 128_000),
    ("anthropic", "claude-sonnet-5"): ModelProfile(1_000_000, 128_000),
    ("anthropic", "claude-haiku-4-5"): ModelProfile(200_000, 64_000),
    # Alibaba Model Studio, verified August 2026.
    ("alibaba", "qwen-max"): ModelProfile(32_768, 8_192),
    ("alibaba", "deepseek-v4-flash"): ModelProfile(1_000_000, 393_216),
    ("alibaba", "deepseek-v4-pro"): ModelProfile(1_000_000, 393_216),
    # DeepSeek's direct API, verified August 2026.
    ("deepseek", "deepseek-chat"): ModelProfile(65_536, 8_192),
    ("deepseek", "deepseek-reasoner"): ModelProfile(65_536, 8_192),
    ("deepseek", "deepseek-v4-flash"): ModelProfile(1_000_000, 393_216),
    ("deepseek", "deepseek-v4-pro"): ModelProfile(1_000_000, 393_216),
}


def get_model_profile(provider: str, model: str) -> Optional[ModelProfile]:
    """Return the exact known profile, or ``None`` for an unverified model."""
    return _PROFILES.get((provider.lower(), model.lower()))
