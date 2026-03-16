import os
import platform
from datetime import datetime
from jinja2 import Environment, FileSystemLoader, select_autoescape


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
            template_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..", "prompts", "system_prompt.j2",
            )

        template_path = os.path.abspath(template_path)
        template_dir  = os.path.dirname(template_path)
        template_file = os.path.basename(template_path)

        env = Environment(
            loader=select_autoescape(),
            keep_trailing_newline=True,
        )
        # Use FileSystemLoader so Jinja2 can find any {% include %} partials later.
        env.loader = FileSystemLoader(template_dir)
        self._template = env.get_template(template_file)

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
        global_state_memory: str = "",
    ) -> str:
        return self._template.render(
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
            global_state_memory=global_state_memory,
        )
