"""
File: agent_file.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description:
AgentFile YAML schema definition and loader for the CodePilot runtime.

Architectural Notes:
Uses Pydantic v2 models to define and validate the full agent configuration
schema (model, tools, runtime, memory). AgentConfig.load() resolves all
relative paths (work_dir, system_prompt) against the YAML file's directory,
not the caller's CWD, making agent files portable. Supports both a flat YAML
structure and the nested 'agent:' convention for backward compatibility.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

 
class ThinkingConfig(BaseModel):
    """Extended thinking / reasoning config — Anthropic, OpenAI, Gemini, DeepSeek, and Alibaba models."""
    enabled: bool = False
    budget_tokens: int = Field(
        default=8000, gt=0,
        description="Max tokens the model may spend on internal reasoning (Anthropic only)."
    )
    reasoning_effort: str = Field(
        default="high",
        description="Reasoning effort level: 'high' or 'max' for DeepSeek, 'low', 'medium', or 'high' for OpenAI/Gemini."
    )


class ModelConfig(BaseModel):
    provider: str = Field(..., description="LLM provider: 'anthropic', 'openai', 'gemini', 'alibaba', or 'deepseek'")
    name: str = Field(..., description="Model identifier, e.g. 'claude-3-5-sonnet-20241022'")
    api_key_env: str = Field(default="OPENAI_API_KEY", description="Name of the env var holding the API key")
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, gt=0)
    thinking: ThinkingConfig = Field(
        default_factory=ThinkingConfig,
        description="Extended thinking settings (Anthropic, DeepSeek, OpenAI, Gemini, Alibaba). "
                    "When enabled, temperature is forced to 1.0 automatically for Anthropic, and thinking mode is enabled for OpenAI, Gemini, DeepSeek, and Alibaba."
    )


class ToolConfig(BaseModel):
    name: str
    enabled: bool = True
    config: Dict[str, Any] = Field(default_factory=dict)


class RuntimeConfig(BaseModel):
    work_dir: str = Field(default=".", description="Absolute or relative path to the workspace root")
    unsafe_mode: bool = Field(default=False, description="Allow file writes outside work_dir")
    max_steps: int = Field(default=20, gt=0)


class MemoryConfigModel(BaseModel):
    max_context_tokens: int = Field(default=120_000, gt=0)
    global_summary_threshold: float = Field(default=0.9, gt=0.0, le=1.0)
    global_summary_max_tokens: int = Field(default=500, gt=0)


class SubAgentsConfig(BaseModel):
    """Configuration for the sub-agent spawning feature."""
    enabled: bool = Field(
        default=False,
        description="Enable spawn_subagent / await_subagent tools on this runtime."
    )
    max_steps: int = Field(
        default=20, gt=0,
        description="Max agentic steps allowed per sub-agent."
    )


class MCPServerConfig(BaseModel):
    """
    A single MCP server entry inside the 'mcp' tool config block.

    Example agent.yaml fragment::

        tools:
          - name: mcp
            enabled: true
            config:
              servers:
                - name: tavily
                  url: https://mcp.tavily.com/mcp/?tavilyApiKey=...
                  api_key_env: TAVILY_API_KEY   # optional env-var name
                  api_key_param: tavilyApiKey   # optional query-param or header
    """
    name:          str = Field(..., description="Logical server name, e.g. 'tavily'.")
    url:           str = Field(..., description="Full MCP server endpoint URL.")
    api_key_env:   Optional[str] = Field(
        default=None,
        description="Name of the env var holding the API key (never put the key literal here)."
    )
    api_key_param: Optional[str] = Field(
        default=None,
        description="Query-parameter or header name the server expects the key in."
    )


class AgentConfig(BaseModel):
    """
    Mirrors the top-level 'agent:' block in an AgentFile YAML.
    Keeps a reference to the YAML file's directory so relative paths (work_dir,
    system_prompt) are resolved correctly regardless of the caller's CWD.
    """
    name: str
    role: Optional[str] = None
    # After validation this will always be the rendered string, never a file path.
    system_prompt: str = Field(default="")
    model: ModelConfig
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    memory: MemoryConfigModel = Field(default_factory=MemoryConfigModel)
    tools: List[ToolConfig] = Field(default_factory=list)
    sub_agents: SubAgentsConfig = Field(default_factory=SubAgentsConfig)

    # Internal: set by AgentConfig.load(), not from YAML directly.
    _yaml_dir: str = ""

    @field_validator("system_prompt", mode="before")
    @classmethod
    def _resolve_prompt(cls, v: str) -> str:
        # File resolution happens in load() after we know _yaml_dir.
        return v

    def resolve_paths(self, yaml_dir: str):
        """
        Called by AgentConfig.load() after construction.
        Resolves work_dir and system_prompt relative to the YAML file location.
        """
        object.__setattr__(self, "_yaml_dir", yaml_dir)

        base = Path(yaml_dir)

        # --- system_prompt: load from file if it looks like a path ---
        prompt_val = self.system_prompt
        if prompt_val.endswith((".md", ".txt", ".j2")):
            candidate = base / prompt_val
            if candidate.is_file():
                object.__setattr__(self, "system_prompt", candidate.read_text(encoding="utf-8"))
            # If file doesn't exist we leave the string as-is
            # (could be a raw prompt that happens to end with .md)

        # --- work_dir: resolve relative to yaml_dir ---
        work_dir_path = Path(self.runtime.work_dir)
        if not work_dir_path.is_absolute():
            resolved = (base / work_dir_path).resolve()
            # Pydantic models are normally immutable; use object.__setattr__ trick
            # on the nested model:
            object.__setattr__(self.runtime, "work_dir", str(resolved))

        # --- auto-adjust max_context_tokens if it is the default ---
        if self.memory.max_context_tokens == 120_000:
            model_name = self.model.name.lower()
            new_max = 120_000
            if "deepseek" in model_name:
                new_max = 64_000
            elif "claude-3" in model_name:
                new_max = 200_000
            elif "gpt-4" in model_name or "qwen" in model_name:
                new_max = 128_000
            
            if new_max != 120_000:
                object.__setattr__(self.memory, "max_context_tokens", new_max)

    # ------------------------------------------------------------------
    # Class-level factory
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: str = "agent.yaml") -> "AgentConfig":
        abs_path = Path(path).resolve()
        if not abs_path.is_file():
            raise FileNotFoundError(f"AgentFile not found: {abs_path}")

        yaml_dir = str(abs_path.parent)

        raw: dict = yaml.safe_load(abs_path.read_text(encoding="utf-8"))

        # Support both flat YAML and the nested 'agent:' convention
        data = raw.get("agent", raw)

        instance = cls(**data)
        instance.resolve_paths(yaml_dir)
        return instance


# Backward-compatible alias used in existing code
AgentFile = AgentConfig
