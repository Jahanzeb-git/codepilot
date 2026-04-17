"""
File: prompt.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description:
System prompt renderer and cache-split manager for the CodePilot runtime.

Architectural Notes:
Renders two separate Jinja2 templates — static_instructions.j2 and
dynamic_instructions.j2 — and returns them as a SystemPromptParts object.

The static half (rules, tools, example) is identical across all agentic
steps and is passed to providers with a cache_control breakpoint for maximum
token reuse. The dynamic half (environment, codebase snapshot, step info)
changes every step and is never cached. This design reduces inference cost
significantly in long sessions.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

import platform
from pathlib import Path
from datetime import datetime
from typing import NamedTuple
from jinja2 import Environment, FileSystemLoader, select_autoescape


class SystemPromptParts(NamedTuple):
    """
    Holds the two halves of a rendered system prompt.

    Providers that support explicit cache control (Anthropic, Alibaba/Qwen)
    use ``static`` and ``dynamic`` separately.  Others call ``.full`` to get
    the concatenated string — zero behaviour change for them.
    """
    static: str    # rules, tools, example — identical across steps
    dynamic: str   # environment, step info, context stress — changes every step

    @property
    def full(self) -> str:
        """Concatenated prompt — backwards-compatible with str-based callers."""
        return self.static + self.dynamic


class PromptManager:
    """
    Loads and renders separate static and dynamic system prompt templates.

    static_instructions.j2  — cacheable, never changes between steps.
    dynamic_instructions.j2 — rendered fresh every step with current env state.

    The developer's system_prompt from the AgentFile is injected into the
    dynamic half as a clearly delineated section — it can never override the
    core runtime behavioural instructions in the static half.
    """

    def __init__(self, prompts_dir: str = None):
        if prompts_dir is None:
            prompts_dir = Path(__file__).parent / ".." / "prompts"

        prompts_dir = Path(prompts_dir).resolve()

        engine = Environment(
            loader=FileSystemLoader(prompts_dir),
            autoescape=select_autoescape(),
            keep_trailing_newline=True,
        )

        # Load both templates once at construction time
        self._static_tmpl  = engine.get_template("static_instructions.j2")
        self._dynamic_tmpl = engine.get_template("dynamic_instructions.j2")

    def render(
        self,
        agent_name: str,
        agent_role: str,
        developer_prompt: str,
        tool_definitions: str,
        work_dir: str,
        codebase_snapshot: str,
        shell_info: str = "",
        step_info: str = "",
        context_stress: str = "",
    ) -> SystemPromptParts:
        # Static half — only uses identity + tool definitions (never changes)
        static = self._static_tmpl.render(
            agent_name=agent_name,
            agent_role=agent_role or "a skilled software engineering assistant",
            tool_definitions=tool_definitions,
        )

        # Dynamic half — rendered fresh every step with current environment state
        dynamic = self._dynamic_tmpl.render(
            work_dir=work_dir,
            os_info=f"{platform.system()} {platform.release()}",
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            codebase_snapshot=codebase_snapshot,
            shell_info=shell_info,
            step_info=step_info,
            context_stress=context_stress,
            developer_prompt=developer_prompt,
        )

        return SystemPromptParts(static=static, dynamic=dynamic)
