import os
import queue
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
    The CodePilot agentic loop with multi-turn, session persistence, and
    optional streaming support.

    Streaming
    ---------
    When ``stream=True``, the LLM response is received token by token.
    Any natural text before the first code fence is emitted immediately via
    the STREAM hook, giving the user real-time feedback while the rest of the
    response (code blocks, payload blocks) buffers silently.  Once the full
    response is received the normal parse, validate, execute pipeline runs
    unchanged.

    When ``stream=False`` (default), the full response is fetched in one call.
    Pre-fence text is still emitted as a single ``STREAM`` event for
    consistency — hook handlers work identically in both modes.

    Multi-turn
    ----------
    Calling ``runtime.run(task)`` multiple times continues the conversation.
    To start fresh, call ``runtime.reset()`` first.

    Session backends
    ----------------
    memory (default) — in-RAM, lost on exit.
    file             — persisted to ~/.codepilot/sessions/<id>.json.

    Multi-file writes
    -----------------
    Up to 5 ``write_file()`` calls per step (mode='w' / 'a').
    Edits and inserts: one per step to prevent line-number drift.
    Each ``write_file()`` consumes the next Payload Block in order.

    Parallel commands
    -----------------
    ``run_command(cmd, execution="parallel")`` queues commands; they are
    launched simultaneously after the control block finishes.
    """

    def __init__(
        self,
        agent_file: str,
        session: str = "memory",
        session_id: Optional[str] = None,
        session_dir=None,
        stream: bool = False,
    ):
        """
        Args:
            agent_file:  Path to the AgentFile YAML.
            session:     'memory' (default) or 'file'.
            session_id:  Unique name for this session.
            session_dir: Override default session directory for file backend.
            stream:      Enable token-level streaming (pre-block text is
                         emitted in real time via STREAM hooks).
        """
        self.config: AgentConfig = AgentConfig.load(agent_file)
        self.provider = get_provider(self.config.model)
        self.hooks    = HookSystem()
        self._stream  = stream

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

        self.messages: List[Dict[str, str]] = self.session.load()

        # ------------------------------------------------------------------ #
        #  Per-step ephemeral state                                            #
        # ------------------------------------------------------------------ #
        self._payload_queue:    List[CodeBlock] = []
        self._execution_buffer: List[str]       = []
        self._step_write_count: int = 0
        self._step_edit_count:  int = 0

        # ------------------------------------------------------------------ #
        #  Control flags                                                       #
        # ------------------------------------------------------------------ #
        self._done:         bool = False
        self._done_summary: str  = ""
        self._abort:        bool = False

        # ------------------------------------------------------------------ #
        #  Mid-execution message injection (thread-safe)                       #
        # ------------------------------------------------------------------ #
        self._message_queue: queue.Queue    = queue.Queue()
        self._loop_lock:     threading.Lock = threading.Lock()

    # ====================================================================== #
    #  Public API                                                             #
    # ====================================================================== #

    def run(self, task: str) -> Optional[str]:
        """
        Run a task within the current conversation context.

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

            # Reset per-step state
            self._step_write_count = 0
            self._step_edit_count  = 0
            self._shell_tools.reset_step()

            # 1. Drain mid-execution queue before next inference
            self._drain_message_queue()

            # 2. Build system prompt (snapshot refreshed every step)
            system_prompt = self._build_system_prompt()

            # 3. LLM inference (streaming or blocking)
            try:
                if self._stream:
                    response_text = self._stream_inference(system_prompt)
                else:
                    response_text = self.provider.chat(
                        messages=self.messages,
                        system=system_prompt,
                        temperature=self.config.model.temperature,
                        max_tokens=self.config.model.max_tokens,
                    )
                    # Non-streaming: still emit pre-fence text as single event
                    self._emit_prefence_text(response_text)
            except Exception as exc:
                error_msg = f"LLM provider error: {exc}"
                self.hooks.emit(EventType.RUNTIME_ERROR, error=error_msg)
                self._append_execution_result(f"PROVIDER ERROR: {error_msg}")
                continue

            # LLM output → assistant role
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

            # 6. Flush parallel commands (if any were queued)
            self._shell_tools.flush_parallel()

            execution_result = "\n\n".join(self._execution_buffer).strip()
            if not execution_result:
                execution_result = "[Control block executed with no output.]"
            self._append_execution_result(execution_result)

            if self._done:
                break

        # Persist after every run() call
        self.session.save(self.messages)

        if not self._done and not self._abort:
            self.hooks.emit(EventType.MAX_STEPS)

        return self._done_summary if self._done else None

    def reset(self):
        """Wipe the entire conversation history and start fresh."""
        self.messages = []
        self.session.reset()
        self.hooks.emit(EventType.SESSION_RESET)

    def send_message(self, message: str):
        """
        Inject a user message into the running loop from any thread.
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
        Its docstring is automatically injected into the system prompt.

        Args:
            name:    Name the agent uses to call the tool.
            func:    Any callable.
            replace: Pass True to silently override an existing tool.
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
        """Consume all queued send_message() calls and insert into history."""
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
        self.registry.register("done", self._tool_done)

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

    def _build_system_prompt(self) -> str:
        return self.prompt_manager.render(
            agent_name=self.config.name,
            agent_role=self.config.role or "",
            developer_prompt=self.config.system_prompt,
            tool_definitions=self.registry.get_definitions(),
            work_dir=self.config.runtime.work_dir,
            codebase_snapshot=self.context_manager.get_formatted_snapshot(),
        )

    # ------------------------------------------------------------------ #
    #  Streaming inference                                                 #
    # ------------------------------------------------------------------ #

    def _stream_inference(self, system_prompt: str) -> str:
        """
        Stream the LLM response token by token.

        Text before the first code fence is emitted immediately via STREAM
        hooks for real-time user feedback.  Once the fence is detected, all
        subsequent tokens are buffered silently.

        A 2-character hold-back prevents incorrect emission when the fence
        marker is split across two streaming chunks (e.g. chunk ends with
        '`' and next starts with '``').

        Returns the complete response text for normal pipeline processing.
        """
        chunks: list            = []
        pre_fence_emitted: int  = 0
        fence_found: bool       = False

        for chunk in self.provider.chat_stream(
            messages=self.messages,
            system=system_prompt,
            temperature=self.config.model.temperature,
            max_tokens=self.config.model.max_tokens,
        ):
            chunks.append(chunk)

            if fence_found:
                continue

            accumulated = "".join(chunks)
            fence_pos   = accumulated.find("```")

            if fence_pos == -1:
                # No fence yet — emit new text but hold back 2 chars so a
                # fence split across chunks isn't prematurely streamed.
                safe_end = len(accumulated) - 2
                if safe_end > pre_fence_emitted:
                    self.hooks.emit(
                        EventType.STREAM,
                        text=accumulated[pre_fence_emitted:safe_end],
                    )
                    pre_fence_emitted = safe_end
            else:
                # Fence detected — emit any remaining pre-fence text
                if fence_pos > pre_fence_emitted:
                    self.hooks.emit(
                        EventType.STREAM,
                        text=accumulated[pre_fence_emitted:fence_pos],
                    )
                fence_found = True

        # Flush any held-back text if the response was purely prose (no fence)
        if not fence_found:
            accumulated = "".join(chunks)
            remaining   = accumulated[pre_fence_emitted:]
            if remaining:
                self.hooks.emit(EventType.STREAM, text=remaining)

        return "".join(chunks)

    def _emit_prefence_text(self, response_text: str):
        """Emit pre-fence text as a single STREAM event (non-streaming mode)."""
        fence_pos = response_text.find("```")
        if fence_pos > 0:
            pre_text = response_text[:fence_pos].strip()
            if pre_text:
                self.hooks.emit(EventType.STREAM, text=pre_text)
        elif fence_pos == -1 and response_text.strip():
            # No code block at all — still emit the text
            self.hooks.emit(EventType.STREAM, text=response_text.strip())

    # ------------------------------------------------------------------ #
    #  Sandbox + execution                                                 #
    # ------------------------------------------------------------------ #

    def _build_sandbox(self, captured_print=None) -> Dict[str, Any]:
        sandbox = {
            "__builtins__": {
                "print": captured_print if captured_print is not None else print,
                "__import__": __import__,
                "len": len, "range": range,
                "str": str, "int": int, "float": float, "bool": bool,
                "list": list, "dict": dict, "set": set, "tuple": tuple,
                "enumerate": enumerate, "zip": zip, "map": map,
                "filter": filter, "sorted": sorted, "reversed": reversed,
                "sum": sum, "min": min, "max": max, "abs": abs, "round": round,
                "isinstance": isinstance, "issubclass": issubclass,
                "type": type, "repr": repr,
                "True": True, "False": False, "None": None,
                "Exception": Exception, "ValueError": ValueError,
                "TypeError": TypeError, "RuntimeError": RuntimeError,
                "KeyError": KeyError, "IndexError": IndexError,
                "StopIteration": StopIteration,
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

        _print_lines: list = []

        def _captured_print(*args, sep=" ", end="\n", file=None, flush=False):
            _print_lines.append(sep.join(str(a) for a in args) + end)

        try:
            exec(code, self._build_sandbox(captured_print=_captured_print))  # noqa: S102
        except Exception:
            tb = traceback.format_exc()
            self.hooks.emit(EventType.RUNTIME_ERROR, error=tb)
            self._append_execution(f"EXECUTION ERROR:\n{tb}")

        printed = "".join(_print_lines).strip()
        if printed:
            self._execution_buffer.insert(0, printed)

    def _append_execution_result(self, result: str):
        self.messages.append({
            "role": _ROLE_USER,
            "content": f"{TAG_EXECUTION_RESULT}\n{result}",
        })
