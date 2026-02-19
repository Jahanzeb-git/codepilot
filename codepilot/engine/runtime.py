import io
import os
import queue
import sys
import threading
import traceback
from typing import Dict, List, Optional, Any

from ..core.agent_file import AgentConfig
from ..core.ast_validator import ASTValidator, SecurityViolation
from ..core.block_parser import BlockParser, CodeBlock
from ..core.context import ContextManager
from ..core.prompt import PromptManager
from ..core.session import BaseSession, create_session
from ..engine.hooks import EventType, HookSystem
from ..engine.provider import get_provider
from ..tools.filesystem import FilesystemTools
from ..tools.interaction import InteractionTools
from ..tools.registry import ToolRegistry
from ..tools.shell import ShellTools


_ROLE_USER      = "user"
_ROLE_ASSISTANT = "assistant"

TAG_USER_INPUT       = "[USER INPUT]"
TAG_USER_INJECTION   = "[USER MESSAGE]"
TAG_EXECUTION_RESULT = "[EXECUTION RESULT]"


class Runtime:
    """
    The CodePilot agentic loop with multi-turn and session persistence support.

    Multi-turn
    ----------
    Calling ``runtime.run(task)`` multiple times on the same instance continues
    the conversation — the LLM sees the complete history of all previous tasks
    and can reason about prior work without repeating it or hallucinating.

    To start completely fresh, call ``runtime.reset()`` first.

    Session backends
    ----------------
    memory (default)
        History lives in RAM for the process lifetime.  Zero config.
        Ideal for a CLI while-loop where you want continuity within a session
        but don't need history to survive process restarts.

    file
        History is serialised to ~/.codepilot/sessions/<session_id>.json
        after every completed task.  Survives restarts.  Multiple sessions
        can be maintained simultaneously using different session_ids.

    Examples::

        # in-memory multi-turn (default)
        runtime = Runtime("agent.yaml")
        runtime.run("Create a FastAPI app")
        runtime.run("Now add JWT authentication to it")  # full history available

        # file-backed, auto session id from agent name
        runtime = Runtime("agent.yaml", session="file")

        # file-backed, explicit id — resumes if file exists
        runtime = Runtime("agent.yaml", session="file", session_id="my-project")

        # inspect current session
        print(runtime.session)

        # start fresh (wipes history in memory + on disk)
        runtime.reset()

    Mid-task injection
    ------------------
    From any thread, call ``runtime.send_message(text)`` while ``run()`` is
    executing.  The message is queued and injected at the step boundary with
    the ``[USER MESSAGE]`` tag so the LLM can distinguish it from the original
    task ``[USER INPUT]``.
    """

    def __init__(
        self,
        agent_file: str,
        session: str = "memory",
        session_id: Optional[str] = None,
        session_dir=None,
    ):
        """
        Args:
            agent_file:  Path to the AgentFile YAML.
            session:     'memory' (default) or 'file'.
            session_id:  Unique name for this session.  Defaults to the agent
                         name from the AgentFile (lowercased, spaces → hyphens).
            session_dir: Override default session directory for file backend.
        """
        self.config: AgentConfig = AgentConfig.load(agent_file)
        self.provider = get_provider(self.config.model)
        self.hooks    = HookSystem()

        self.context_manager = ContextManager(self.config.runtime.work_dir)
        self.prompt_manager  = PromptManager()

        self._fs_tools          = FilesystemTools(self)
        self._shell_tools       = ShellTools(self)
        self._interaction_tools = InteractionTools(self)

        self.registry = ToolRegistry()
        self._register_enabled_tools()

        # ------------------------------------------------------------------ #
        #  Session / persistence                                               #
        # ------------------------------------------------------------------ #
        _sid = session_id or self.config.name.lower().replace(" ", "-")
        self.session: BaseSession = create_session(
            backend=session,
            session_id=_sid,
            agent_name=self.config.name,
            session_dir=session_dir,
        )

        # Hydrate conversation history from persisted session (no-op for memory
        # on first use, or file backend when no file exists yet).
        self.messages: List[Dict[str, str]] = self.session.load()

        # ------------------------------------------------------------------ #
        #  Per-step ephemeral state                                            #
        # ------------------------------------------------------------------ #
        self._payload_queue:    List[CodeBlock] = []
        self._execution_buffer: List[str]       = []

        # ------------------------------------------------------------------ #
        #  Control flags                                                       #
        # ------------------------------------------------------------------ #
        self._done:         bool = False
        self._done_summary: str  = ""
        self._abort:        bool = False

        # ------------------------------------------------------------------ #
        #  Mid-execution message injection (thread-safe)                       #
        # ------------------------------------------------------------------ #
        self._message_queue: queue.Queue  = queue.Queue()
        self._loop_lock:     threading.Lock = threading.Lock()

    # ====================================================================== #
    #  Public API                                                             #
    # ====================================================================== #

    def run(self, task: str) -> Optional[str]:
        """
        Run a task within the current conversation context.

        Calling run() multiple times on the same Runtime instance is the
        intended pattern for multi-turn usage — each call appends to the
        shared history so the LLM always has full context.

        Args:
            task: Natural-language description of what to do.

        Returns:
            The summary string passed to done(), or None if the loop ended
            for any other reason (max_steps, abort).
        """
        self._done  = False
        self._abort = False

        self.messages.append({
            "role": _ROLE_USER,
            "content": f"{TAG_USER_INPUT}\n{task}",
        })

        self.hooks.emit(EventType.START, task=task)

        step = 0
        while step < self.config.runtime.max_steps and not self._done and not self._abort:
            step += 1
            self.hooks.emit(EventType.STEP, step=step, max_steps=self.config.runtime.max_steps)

            # 1. Drain mid-execution queue before next inference
            self._drain_message_queue()

            # 2. Build system prompt (snapshot refreshed every step)
            system_prompt = self._build_system_prompt()

            # 3. LLM inference
            try:
                response_text = self.provider.chat(
                    messages=self.messages,
                    system=system_prompt,
                    temperature=self.config.model.temperature,
                    max_tokens=self.config.model.max_tokens,
                )
            except Exception as exc:
                error_msg = f"LLM provider error: {exc}"
                self.hooks.emit(EventType.RUNTIME_ERROR, error=error_msg)
                self._append_execution_result(f"PROVIDER ERROR: {error_msg}")
                continue

            # LLM output → assistant role, no tag
            self.messages.append({"role": _ROLE_ASSISTANT, "content": response_text})

            # 4. Parse response
            control_block, payload_blocks = BlockParser.split(response_text)

            if control_block is None:
                self._append_execution_result("[No executable code block in response.]")
                continue

            if control_block.language not in ("python", "py", ""):
                self._append_execution_result(
                    f"[Control block language '{control_block.language}' is not Python — ignored.]"
                )
                continue

            # 5. Execute
            self._payload_queue    = list(payload_blocks)
            self._execution_buffer = []
            self._execute(control_block.content)

            execution_result = "\n\n".join(self._execution_buffer).strip()
            if not execution_result:
                execution_result = "[Control block executed with no output.]"
            self._append_execution_result(execution_result)

            if self._done:
                break

        # Persist after every run() call regardless of how the loop ended
        self.session.save(self.messages)

        if not self._done and not self._abort:
            self.hooks.emit(EventType.MAX_STEPS)

        return self._done_summary if self._done else None

    def reset(self):
        """
        Wipe the entire conversation history and start fresh.

        Clears in-memory messages and deletes the session file if using the
        file backend.  The next call to run() will start with a clean slate.
        """
        self.messages = []
        self.session.reset()
        self.hooks.emit(EventType.SESSION_RESET)

    def send_message(self, message: str):
        """
        Inject a user message into the running loop from any thread.

        The message is queued and tagged with [USER MESSAGE] — distinct from
        [USER INPUT] (the original task) — so the LLM can unambiguously
        recognise it as a mid-task course-correction or additional requirement.
        Injected at the step boundary; the agent is never interrupted mid-step.

        Thread-safe and non-blocking.
        """
        self._message_queue.put(message)
        self.hooks.emit(EventType.USER_MESSAGE_QUEUED, message=message)

    def abort(self):
        """Stop the loop cleanly after the current step completes."""
        self._abort = True

    def register_tool(self, name: str, func, replace: bool = False):
        """
        Register a custom tool into the agent's sandbox.

        The tool is callable by name in the agent's control block.
        Its docstring is automatically injected into the system prompt's
        TOOLS section — write a clear, concise docstring explaining when
        and how to use it.

        Args:
            name:    Name the agent uses to call the tool.
            func:    Any callable. Must call ``self.runtime._append_execution(result)``
                     (or equivalent) if it produces output the agent should see,
                     since return values from exec() are discarded.
            replace: Pass True to silently override an existing tool.

        Example::

            def web_search(query: str):
                \"""Search the web for current information.
                Use when the codebase snapshot can't answer a question about
                library APIs, recent changes, or external documentation.\"""
                result = my_search_api(query)
                runtime._append_execution(f"[web_search] {result}")

            runtime.register_tool("web_search", web_search)
        """
        if not replace and self.registry.get(name) is not None:
            raise ValueError(
                f"Tool '{name}' is already registered. Pass replace=True to override."
            )
        self.registry.register(name, func)

    # ====================================================================== #
    #  Internal helpers — used by tool classes                                #
    # ====================================================================== #

    def pop_next_payload_block(self) -> Optional[CodeBlock]:
        if self._payload_queue:
            return self._payload_queue.pop(0)
        return None

    def _append_execution(self, text: str):
        self._execution_buffer.append(text)

    def _tool_config(self, tool_name: str) -> dict:
        for tc in self.config.tools:
            if tc.name == tool_name:
                return tc.config
        return {}

    # ====================================================================== #
    #  Private implementation                                                 #
    # ====================================================================== #

    def _drain_message_queue(self):
        """
        Consume all queued send_message() calls and insert them into history
        as [USER MESSAGE] messages — distinct from [USER INPUT] so the LLM
        can tell them apart without relying on position.
        """
        while True:
            try:
                msg = self._message_queue.get_nowait()
            except queue.Empty:
                break
            self.messages.append({
                "role": _ROLE_USER,
                "content": f"{TAG_USER_INJECTION}\n{msg}",
            })
            self.hooks.emit(EventType.USER_MESSAGE_INJECTED, message=msg)

    def _register_enabled_tools(self):
        enabled = (
            {tc.name for tc in self.config.tools if tc.enabled}
            if self.config.tools
            else {"write_file", "read_file", "run_command", "ask_user"}
        )
        if "write_file"  in enabled: self.registry.register("write_file",  self._fs_tools.write_file)
        if "read_file"   in enabled: self.registry.register("read_file",   self._fs_tools.read_file)
        if "run_command" in enabled: self.registry.register("run_command", self._shell_tools.run_command)
        if "ask_user"    in enabled: self.registry.register("ask_user",    self._interaction_tools.ask_user)
        self.registry.register("done",  self._tool_done)
        self.registry.register("think", self._tool_think)

    def _tool_done(self, summary: str = "Task complete."):
        """
        Call when the task is fully complete and verified.
        Provide a thorough natural summary: everything created, changed, and
        confirmed working. This is the final message the user sees — make it
        a complete account, not a one-liner.
        """
        self._done         = True
        self._done_summary = summary
        self._append_execution(f"[done] {summary}")
        self.hooks.emit(EventType.FINISH, summary=summary)

    def _tool_think(self, message: str):
        """
        Narrate your reasoning to the user in real time — informal, specific, human.
        Side-channel only: shown to the user but NOT stored in conversation history.
        Write it like you're explaining your thinking to a developer sitting next to you.
        """
        self.hooks.emit(EventType.THINK, message=message)

    def _build_system_prompt(self) -> str:
        return self.prompt_manager.render(
            agent_name=self.config.name,
            agent_role=self.config.role or "",
            developer_prompt=self.config.system_prompt,
            tool_definitions=self.registry.get_definitions(),
            work_dir=self.config.runtime.work_dir,
            codebase_snapshot=self.context_manager.get_formatted_snapshot(),
        )

    def _build_sandbox(self) -> Dict[str, Any]:
        sandbox = {
            "__builtins__": {
                "print": print, "len": len, "range": range,
                "str": str, "int": int, "float": float, "bool": bool,
                "list": list, "dict": dict, "set": set, "tuple": tuple,
                "enumerate": enumerate, "zip": zip, "map": map,
                "filter": filter, "sorted": sorted, "reversed": reversed,
                "sum": sum, "min": min, "max": max, "abs": abs, "round": round,
                "isinstance": isinstance, "type": type, "repr": repr,
                "True": True, "False": False, "None": None,
                "Exception": Exception, "ValueError": ValueError,
                "TypeError": TypeError, "RuntimeError": RuntimeError,
            },
        }
        sandbox.update(self.registry.as_sandbox_dict())
        return sandbox

    def _execute(self, code: str):
        validator = ASTValidator(self.config.runtime.allowed_imports)
        try:
            validator.validate(code)
        except SecurityViolation as exc:
            self.hooks.emit(EventType.SECURITY_ERROR, error=str(exc))
            self._append_execution(f"SECURITY ERROR: {exc}")
            return

        captured_stdout = io.StringIO()
        old_stdout, sys.stdout = sys.stdout, captured_stdout

        try:
            exec(code, self._build_sandbox())  # noqa: S102
        except Exception:
            tb = traceback.format_exc()
            self.hooks.emit(EventType.RUNTIME_ERROR, error=tb)
            self._append_execution(f"EXECUTION ERROR:\n{tb}")
        finally:
            sys.stdout = old_stdout

        printed = captured_stdout.getvalue().strip()
        if printed:
            self._execution_buffer.insert(0, printed)

    def _append_execution_result(self, result: str):
        self.messages.append({
            "role": _ROLE_USER,
            "content": f"{TAG_EXECUTION_RESULT}\n{result}",
        })
