import os
import platform
from pathlib import Path
from datetime import datetime
from typing import NamedTuple
from jinja2 import Environment, FileSystemLoader, select_autoescape


# -----------------------------------------------------------------------
#  System prompt split for provider-level caching
# -----------------------------------------------------------------------

# Sentinel that MUST appear in the rendered template exactly once.
# Everything before it is static (cacheable); everything after is dynamic.
_CACHE_SPLIT_MARKER = (
    "---\n## ENVIRONMENT"
)


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
    Renders the internal system prompt template (system_prompt.j2) and merges
    in the developer-supplied custom instructions.

    The developer's system_prompt from the AgentFile is APPENDED as a clearly
    delineated section — it can never override the core runtime behavioural
    instructions that make code-as-interface work.
    """

    def __init__(self, template_path: str = None):
        if template_path is None:
            template_path = Path(__file__).parent / ".." / "prompts" / "system_prompt.j2"

        template_path = Path(template_path).resolve()
        template_dir  = template_path.parent
        template_file = template_path.name

        engine = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(),
            keep_trailing_newline=True
        )
        self._template = engine.get_template(template_file)

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
        full = self._template.render(
            agent_name=agent_name,
            agent_role=agent_role or "a skilled software engineering assistant",
            developer_prompt=developer_prompt,
            tool_definitions=tool_definitions,
            work_dir=work_dir, 
            os_info=f"{platform.system()} {platform.release()}",
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            codebase_snapshot=codebase_snapshot,
            shell_info=shell_info,
            step_info=step_info,
            context_stress=context_stress,
        )

        # Split on the ENVIRONMENT section header.
        # The marker lives in the dynamic half so that the static part ends
        # with a clean trailing newline after the RULES section.
        idx = full.find(_CACHE_SPLIT_MARKER) 
        if idx == -1:
            # Marker missing (custom template?) — treat everything as static.
            return SystemPromptParts(static=full, dynamic="")

        return SystemPromptParts(
            static=full[:idx],
            dynamic=full[idx:],
        )
