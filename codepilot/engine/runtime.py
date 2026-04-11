import asyncio
import os
import queue
import re
import threading
import traceback
from typing import Dict, List, Optional, Any, Union

from ..core.prompt import SystemPromptParts

from ..core.agent_file import AgentConfig
from ..core.ast_validator import ASTValidator, SecurityViolation
from ..core.block_parser import BlockParser, CodeBlock
from ..core.context import ContextManager
from ..core.memory import (
    MemoryManager, MemoryConfig, find_task_map,
    get_highest_task_position, TAG_USER_INPUT,
)
from ..core.prompt import PromptManager
from ..core.session import BaseSession, create_session
from ..core.watcher import WorkspaceWatcher
from ..engine.hooks import EventType, HookSystem
from ..engine.provider import get_provider, AnthropicProvider
from ..tools.context import ContextTools
from ..tools.filesystem import FilesystemTools
from ..tools.interaction import InteractionTools
from ..tools.registry import ToolRegistry
from ..tools.search import SearchTools
from ..tools.shell import ShellManager
from ..tools.semantic import SemanticTools


_ROLE_USER      = "user"
_ROLE_ASSISTANT = "assistant"

TAG_USER_INJECTION   = "[USER MESSAGE]"
TAG_EXECUTION_RESULT = "[EXECUTION RESULT]"
TAG_ENV_CHANGE       = "[ENVIRONMENT CHANGE]"

# Background timer delay for upgrading Anthropic cache TTL to 1h.
# Set to 4.5 minutes — just before the default 5min TTL expires.
_CACHE_REFRESH_DELAY = 4.5 * 60  # 270 seconds


class AsyncRuntime:
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
        db_url: Optional[str] = None,
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
        self._shell_manager     = ShellManager(self)
        self._interaction_tools = InteractionTools(self)
        self._semantic_tools    = SemanticTools(self)
        self._search_tools      = SearchTools(self)
        self._context_tools     = ContextTools(self)

        self.registry = ToolRegistry()
        self._register_enabled_tools()

        # Start default shell session (POSIX only)
        self._shell_manager.start_default_shell()

        # ------------------------------------------------------------------ #
        #  Workspace file change detection                                     #
        # ------------------------------------------------------------------ #
        self._watcher = WorkspaceWatcher()

        # ------------------------------------------------------------------ #
        #  Session / persistence                                               #
        # ------------------------------------------------------------------ #
        _sid = session_id or self.config.name.lower().replace(" ", "-")
        self.session: BaseSession = create_session(
            backend=session,
            session_id=_sid,
            agent_name=self.config.name,
            session_dir=session_dir,
            db_url=db_url,
        )

        self.messages: List[Dict[str, str]] = self.session.load()

        # ------------------------------------------------------------------ #
        #  Context memory manager                                              #
        # ------------------------------------------------------------------ #
        _mem_cfg = self.config.memory
        self._memory = MemoryManager(
            config=MemoryConfig(
                max_context_tokens=_mem_cfg.max_context_tokens,
                global_summary_threshold=_mem_cfg.global_summary_threshold,
                global_summary_max_tokens=_mem_cfg.global_summary_max_tokens,
                provider_name=self.config.model.provider.lower(),
            ),
            provider=self.provider,
        )

        # Restore memory state (archive + global state) from session
        extra = self.session.load_extra()
        if extra.get("memory_state"):
            self._memory.restore_state(extra["memory_state"])

        # ------------------------------------------------------------------ #
        #  Task position counter                                               #
        # ------------------------------------------------------------------ #
        self._task_counter = get_highest_task_position(self.messages)

        # ------------------------------------------------------------------ #
        #  Per-step ephemeral state                                            #
        # ------------------------------------------------------------------ #
        self._payload_queue:    List[CodeBlock] = []
        self._execution_buffer: List[str]       = []
        self._step_write_count:  int = 0
        self._step_edited_files: set[str] = set()

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

        # ------------------------------------------------------------------ #
        #  Cache TTL refresh timer (Anthropic only)                            #
        # ------------------------------------------------------------------ #
        self._cache_timer: Optional[threading.Timer] = None
        self._last_system_prompt: Optional[SystemPromptParts] = None

    # ====================================================================== #
    #  Public API                                                             #
    # ====================================================================== #

    async def run(self, task: str) -> Optional[str]:
        """
        Run a task within the current conversation context.

        Returns:
            The summary string passed to done(), or None if the loop ended
            for any other reason (max_steps, abort).
        """
        self._done  = False
        self._abort = False
        self._shell_manager.ensure_default_shell()

        # Safety-net: global summarization if context is dangerously high
        self.messages = await self._memory.process(self.messages)

        # Assign a stable task position and append the new task
        self._task_counter += 1
        task_pos = self._task_counter
        self.messages.append({
            "role": _ROLE_USER,
            "content": f"[Task {task_pos}]{TAG_USER_INPUT}\n{task}",
        })

        self.hooks.emit(EventType.START, task=task)

        step = 0
        while step < self.config.runtime.max_steps and not self._done and not self._abort:
            step += 1
            self.hooks.emit(EventType.STEP, step=step, max_steps=self.config.runtime.max_steps)

            # Reset per-step state
            self._step_write_count = 0
            self._step_edited_files = set()

            # 1. Drain mid-execution queue before next inference
            self._drain_message_queue()

            # 1.5 Check for external workspace changes since last step
            changes = self._watcher.check()
            if changes:
                self.messages.append({
                    "role": _ROLE_USER,
                    "content": changes,
                })
                self._watcher.snapshot_all()

            # 2. Build system prompt (with context stress signal)
            system_prompt = self._build_system_prompt(step, self.config.runtime.max_steps)

            # 2.5 Render messages with XML task wrappers for LLM visibility
            rendered_msgs = self._render_messages_for_llm()

            # 2.9 Cancel any pending cache refresh timer before inference
            self._cancel_cache_timer()

            # 3. LLM inference (streaming or blocking)
            try:
                if self._stream:
                    response_text = await self._stream_inference(
                        system_prompt, messages=rendered_msgs
                    )
                else:
                    response_text = await self.provider.chat(
                        messages=rendered_msgs,
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

            # 4. Parse response first so we can extract payload blocks
            control_block, payload_blocks, completion_block = BlockParser.split(response_text)

            # 4.5 Strip payload block contents from memory to save massive tokens
            memory_text = response_text
            if payload_blocks:
                for pb in payload_blocks:
                    placeholder = "# [Content successfully written to file on disk. Use read_file() to view line numbers. Note: This literal comment is a placeholder it's confirms the content exactly wrote as you write.]"
                    memory_text = memory_text.replace(pb.content, placeholder)

            # LLM output → assistant role (using compressed text)
            self.messages.append({"role": _ROLE_ASSISTANT, "content": memory_text})

            # Schedule cache TTL refresh (Anthropic only)
            self._last_system_prompt = system_prompt
            self._schedule_cache_timer()

            if control_block is None:
                # No ```codepilot block → conversational reply (may include
                # display ```python blocks). Already streamed to user.
                break

            # 5. Execute
            self._payload_queue    = list(payload_blocks)
            self._execution_buffer = []
            await self._execute(control_block.content)

            # 6. Assemble execution result and feed back as next user turn
            execution_result = "\n\n".join(self._execution_buffer).strip()
            if not execution_result:
                execution_result = "[Control block executed with no output.]"
            self._append_execution_result(execution_result)

            # 7. Completion block → task is done, loop terminates
            if completion_block is not None:
                self._done         = True
                self._done_summary = completion_block.content.strip()
                self.hooks.emit(EventType.FINISH, summary=self._done_summary)
                break

            # 8. Update watcher snapshots (baseline for next step's check)
            self._watcher.snapshot_all()

        # Persist after every run() call
        self.session.save(self.messages)
        self.session.save_extra({
            "memory_state": self._memory.serialize_state(),
        })

        # Note: do NOT cancel the cache timer here.
        # It should fire between tasks to upgrade TTL to 1h.

        if not self._done and not self._abort:
            if step >= self.config.runtime.max_steps:
                self.hooks.emit(EventType.MAX_STEPS)
            else:
                # Stream was naturally closed due to a conversational reply rather than a done() block.
                # Emit a FINISH event so the UI gracefully restores its state.
                self.hooks.emit(EventType.FINISH, summary="Agent replied. Standing by for next task.")

        return self._done_summary if self._done else None

    def reset(self):
        """Wipe the entire conversation history and start fresh."""
        self._cancel_cache_timer()
        self.messages = []
        self.session.reset()
        self._shell_manager.cleanup_all()
        self._shell_manager.start_default_shell()
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
            else {"write_file", "read_file", "execute", "read_output",
                  "send_input", "send_signal", "kill_shell",
                  "ask_user", "semantic_search", "find"}
        )
        if "write_file"      in enabled: self.registry.register("write_file",      self._fs_tools.write_file)
        if "read_file"       in enabled: self.registry.register("read_file",       self._fs_tools.read_file)
        if "execute"         in enabled: self.registry.register("execute",         self._shell_manager.execute)
        if "read_output"     in enabled: self.registry.register("read_output",     self._shell_manager.read_output)
        if "send_input"      in enabled: self.registry.register("send_input",      self._shell_manager.send_input)
        if "send_signal"     in enabled: self.registry.register("send_signal",     self._shell_manager.send_signal)
        if "kill_shell"      in enabled: self.registry.register("kill_shell",      self._shell_manager.kill_shell)
        if "ask_user"        in enabled: self.registry.register("ask_user",        self._interaction_tools.ask_user)
        if "semantic_search" in enabled: self.registry.register("semantic_search", self._semantic_tools.semantic_search)
        if "find"            in enabled: self.registry.register("find",            self._search_tools.find)

        # Context management tools (always enabled)
        self.registry.register("archive_context",       self._context_tools.archive_context)
        self.registry.register("reveal_context",        self._context_tools.reveal_context)
        self.registry.register("list_archived_context", self._context_tools.list_archived_context)

    def _build_system_prompt(self, step: int = 0, max_steps: int = 0) -> SystemPromptParts:
        return self.prompt_manager.render(
            agent_name=self.config.name,
            agent_role=self.config.role or "",
            developer_prompt=self.config.system_prompt,
            tool_definitions=self.registry.get_definitions(),
            work_dir=self.config.runtime.work_dir,
            codebase_snapshot=self.context_manager.get_formatted_snapshot(),
            shell_info=self._shell_manager.get_prompt_info(),
            step_info=self._build_step_info(step, max_steps),
            context_stress=self._memory.build_context_stress(self.messages),
        )

    @staticmethod
    def _build_step_info(step: int, max_steps: int) -> str:
        """Generate a step-awareness line with progressive urgency signals."""
        if not max_steps:
            return ""
        pct  = round(step / max_steps * 100)
        base = f"Agentic step {step} / {max_steps}"
        if pct >= 90:
            return f"{base} — {pct}% agentic steps consumed! Hard Limit Near!"
        if pct >= 75:
            return f"{base} — {pct}% agentic steps consumed. Approaching step limit!"
        if pct >= 33:
            return f"{base} — {pct}% agentic steps consumed!"
        return base

    # ------------------------------------------------------------------ #
    #  Cache TTL refresh timer (Anthropic only)                            #
    # ------------------------------------------------------------------ #

    def _cancel_cache_timer(self) -> None:
        """Cancel any pending cache refresh timer (threading.Timer or asyncio.Task)."""
        if self._cache_timer is not None:
            self._cache_timer.cancel()  # works for both Timer and Task
            self._cache_timer = None

    def _schedule_cache_timer(self) -> None:
        """
        Start a background timer that fires after 4.5 minutes of inactivity.

        When it fires, it sends a minimal (1-token) inference call to upgrade
        the conversational cache breakpoint TTL from 5 minutes to 1 hour.

        Only activates for AnthropicProvider — other providers do automatic
        caching and don't benefit from explicit TTL management.
        """
        if not isinstance(self.provider, AnthropicProvider):
            return

        self._cancel_cache_timer()

        async def _refresh_task():
            await asyncio.sleep(_CACHE_REFRESH_DELAY)
            try:
                await self.provider.refresh_cache_ttl(
                    messages=self._render_messages_for_llm(),
                    system=self._last_system_prompt,
                )
            except Exception:
                pass  # Best-effort — failure just means cache expires naturally.
            finally:
                self._cache_timer = None

        self._cache_timer = asyncio.create_task(_refresh_task())

    # ------------------------------------------------------------------ #
    #  XML task wrappers for LLM visibility                                #
    # ------------------------------------------------------------------ #

    _TASK_PREFIX_RE = re.compile(r"^\[Task \d+\]")

    def _render_messages_for_llm(self) -> List[Dict]:
        """
        Create a copy of messages with XML task wrappers added.

        Non-archived tasks are wrapped:
            <task_N>
            [USER INPUT]
            ...messages...
            </task_N>

        Archived tasks remain as-is (single [ARCHIVED TASK N] message).
        The internal [Task N] prefix is stripped — the XML tag replaces it.
        """
        tmap = find_task_map(self.messages)

        # Build fast lookups: which message indices open/close a task?
        opens:  Dict[int, int] = {}   # msg_idx → task_position
        closes: Dict[int, int] = {}   # msg_idx → task_position

        for pos, (start, end, is_archived) in tmap.items():
            if not is_archived:
                opens[start] = pos
                closes[end - 1] = pos

        rendered = []
        for i, msg in enumerate(self.messages):
            content = msg.get("content", "")

            # Strip the internal [Task N] prefix — XML tag replaces it
            if self._TASK_PREFIX_RE.match(content):
                content = self._TASK_PREFIX_RE.sub("", content, count=1)

            # Add opening XML tag
            if i in opens:
                content = f"<task_{opens[i]}>\n{content}"

            # Add closing XML tag
            if i in closes:
                content = f"{content}\n</task_{closes[i]}>"

            rendered.append({**msg, "content": content})

        return rendered

    # ------------------------------------------------------------------ #
    #  Streaming inference                                                 #
    # ------------------------------------------------------------------ #

    _CONTROL_FENCE    = "```codepilot"
    _COMPLETION_FENCE = "```completion"
    _HOLDBACK         = max(len(_CONTROL_FENCE), len(_COMPLETION_FENCE))  # 13 chars

    async def _stream_inference(
        self, system_prompt: Union[str, SystemPromptParts], messages: List[Dict] = None
    ) -> str:
        """
        Stream the LLM response token by token — 3-state machine:

          'streaming'  — before the codepilot fence: emit to user in real time.
          'buffering'  — after codepilot fence, before completion fence: buffer silently.
          'completing' — inside the completion fence: emit to user in real time.

        A 13-character hold-back prevents premature emission when fence markers
        are split across streaming chunks.

        Returns the complete response text for normal pipeline processing.
        """
        msgs = messages if messages is not None else self.messages
        chunks:              list = []
        pre_fence_emitted:   int  = 0
        compl_emitted:       int  = 0  # absolute offset of last emitted completion char
        compl_content_start: int  = 0  # absolute offset where completion block content begins
        state:               str  = "streaming"

        async for chunk in self.provider.chat_stream(
            messages=msgs,
            system=system_prompt,
            temperature=self.config.model.temperature,
            max_tokens=self.config.model.max_tokens,
        ):
            chunks.append(chunk)
            accumulated = "".join(chunks)

            if state == "streaming":
                fence_pos = accumulated.find(self._CONTROL_FENCE)
                if fence_pos == -1:
                    # No control fence yet — emit but hold back to avoid
                    # prematurely streaming a fence split across chunks.
                    safe_end = len(accumulated) - self._HOLDBACK
                    if safe_end > pre_fence_emitted:
                        self.hooks.emit(EventType.STREAM,
                                        text=accumulated[pre_fence_emitted:safe_end])
                        pre_fence_emitted = safe_end
                else:
                    # Control fence found — flush remaining pre-fence text.
                    if fence_pos > pre_fence_emitted:
                        self.hooks.emit(EventType.STREAM,
                                        text=accumulated[pre_fence_emitted:fence_pos])
                    state = "buffering"

            # Use `if` (not elif) so a transition in the same chunk is handled immediately.
            if state == "buffering":
                comp_pos = accumulated.find(self._COMPLETION_FENCE)
                if comp_pos != -1:
                    newline_pos = accumulated.find("\n", comp_pos)
                    if newline_pos != -1:
                        compl_content_start = newline_pos + 1
                        compl_emitted       = compl_content_start
                        state = "completing"

            if state == "completing":
                # Emit completion content with holdback (avoids emitting
                # the closing ``` which appears at the very end).
                safe_end = len(accumulated) - self._HOLDBACK
                if safe_end > compl_emitted:
                    self.hooks.emit(EventType.STREAM,
                                    text=accumulated[compl_emitted:safe_end])
                    compl_emitted = safe_end

        # ------------------------------------------------------------------ #
        # Final flush — emit any content held back by the sliding window.    #
        # ------------------------------------------------------------------ #
        accumulated = "".join(chunks)

        if state == "streaming":
            # No codepilot block at all — pure conversation, flush everything.
            remaining = accumulated[pre_fence_emitted:]
            if remaining:
                self.hooks.emit(EventType.STREAM, text=remaining)

        elif state == "completing":
            # Use BlockParser to get the clean, trimmed completion block content
            # and emit whatever the holdback window was still sitting on.
            _, _, completion_block = BlockParser.split(accumulated)
            if completion_block:
                already_emitted = compl_emitted - compl_content_start
                remaining = completion_block.content[already_emitted:]
                if remaining:
                    self.hooks.emit(EventType.STREAM, text=remaining)

        return accumulated

    def _emit_prefence_text(self, response_text: str):
        """Emit pre-fence text and completion block content as STREAM events (non-streaming mode)."""
        fence_pos = response_text.find(self._CONTROL_FENCE)
        if fence_pos > 0:
            pre_text = response_text[:fence_pos].strip()
            if pre_text:
                self.hooks.emit(EventType.STREAM, text=pre_text)
        elif fence_pos == -1 and response_text.strip():
            # No control block at all — emit everything (display/chat)
            self.hooks.emit(EventType.STREAM, text=response_text.strip())
            return  # No completion block possible without a control block

        # Emit completion block content if present
        _, _, completion_block = BlockParser.split(response_text)
        if completion_block and completion_block.content.strip():
            self.hooks.emit(EventType.STREAM, text=completion_block.content.strip())

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

    async def _execute(self, code: str):
        """Async wrapper — runs the CPU-bound + pexpect-bound execution in a thread."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._execute_sync, code)

    def _execute_sync(self, code: str):
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


# ============================================================================ #
#  Sync wrapper — for CLI users / scripts that don't run an event loop          #
# ============================================================================ #

class Runtime:
    """
    Thin synchronous wrapper over AsyncRuntime.

    Suitable for CLI usage and scripts. Calls asyncio.run() internally so the
    caller doesn't need to manage an event loop.

    For web apps / FastAPI, use AsyncRuntime directly with ``await``.
    """

    def __init__(self, agent_file: str, **kwargs):
        self._async = AsyncRuntime(agent_file, **kwargs)

    # ── public API mirrors AsyncRuntime ──────────────────────────────────────

    def run(self, task: str) -> Optional[str]:
        """Run a task synchronously. Blocks the calling thread until complete."""
        return asyncio.run(self._async.run(task))

    def reset(self):
        """Wipe the entire conversation history and start fresh."""
        self._async.reset()

    def abort(self):
        """Stop the loop cleanly after the current step completes."""
        self._async.abort()

    def send_message(self, message: str):
        """Inject a user message into the running loop from any thread."""
        self._async.send_message(message)

    def register_tool(self, name: str, func, replace: bool = False):
        """Register a custom tool into the agent's sandbox."""
        self._async.register_tool(name, func, replace)

    # ── expose underlying state for advanced users ────────────────────────────

    @property
    def messages(self):
        return self._async.messages

    @property
    def hooks(self):
        return self._async.hooks

    @property
    def config(self):
        return self._async.config
