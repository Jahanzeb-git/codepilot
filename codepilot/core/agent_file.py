from typing import List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator
import yaml
import os
from typing import Dict, Any

 
class ModelConfig(BaseModel):
    provider: str = Field(..., description="LLM provider: 'anthropic', 'openai', or 'together'")
    name: str = Field(..., description="Model identifier, e.g. 'claude-3-5-sonnet-20241022'")
    api_key_env: str = Field(default="OPENAI_API_KEY", description="Name of the env var holding the API key")
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, gt=0)


class ToolConfig(BaseModel):
    name: str
    enabled: bool = True
    config: Dict[str, Any] = Field(default_factory=dict)


class RuntimeConfig(BaseModel):
    work_dir: str = Field(default=".", description="Absolute or relative path to the workspace root")
    unsafe_mode: bool = Field(default=False, description="Allow file writes outside work_dir")
    max_steps: int = Field(default=20, gt=0)
    allowed_imports: List[str] = Field(
        default_factory=lambda: [
            "re", "json", "math", "datetime", "random",
            "itertools", "collections", "typing", "pathlib",
        ],
        description="Python stdlib modules the control block is permitted to import",
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
    tools: List[ToolConfig] = Field(default_factory=list)

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

        # --- system_prompt: load from file if it looks like a path ---
        prompt_val = self.system_prompt
        if prompt_val.endswith((".md", ".txt", ".j2")):
            candidate = os.path.join(yaml_dir, prompt_val)
            if os.path.isfile(candidate):
                with open(candidate, "r", encoding="utf-8") as f:
                    object.__setattr__(self, "system_prompt", f.read())
            # If file doesn't exist we leave the string as-is
            # (could be a raw prompt that happens to end with .md)

        # --- work_dir: resolve relative to yaml_dir ---
        if not os.path.isabs(self.runtime.work_dir):
            resolved = os.path.abspath(
                os.path.join(yaml_dir, self.runtime.work_dir)
            )
            # Pydantic models are normally immutable; use object.__setattr__ trick
            # on the nested model:
            object.__setattr__(self.runtime, "work_dir", resolved)

    # ------------------------------------------------------------------
    # Class-level factory
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: str = "agent.yaml") -> "AgentConfig":
        abs_path = os.path.abspath(path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"AgentFile not found: {abs_path}")

        yaml_dir = os.path.dirname(abs_path)

        with open(abs_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        # Support both flat YAML and the nested 'agent:' convention
        data = raw.get("agent", raw)

        instance = cls(**data)
        instance.resolve_paths(yaml_dir)
        return instance


# Backward-compatible alias used in existing code
AgentFile = AgentConfig

