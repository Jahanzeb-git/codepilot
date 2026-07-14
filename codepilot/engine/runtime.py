"""
File: runtime.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description: 
This module handles the core agentic execution runtime, including loop 
state management, and LLM provider integration.

Architectural Notes:
- The runtime uses Python's `exec()` to give the agent full, unrestricted
  Python as its control interface (code-as-interface pattern).
- The LLM is the trust boundary. No runtime-level import restrictions or
  filesystem monkey-patching are applied.
- Sandboxing is an external deployment concern: run the process inside
  bwrap (bare-metal) or a Docker container (cloud/CI) as appropriate.
- File write/read tools enforce work_dir boundaries unless unsafe_mode
  is enabled in the AgentFile.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

import asyncio
import os
import queue
import re
import threading
import traceback
from typing import Dict, List, Optional, Any, Union

from ..core.prompt import SystemPromptParts

from ..core.agent_file import AgentConfig
from ..core.block_parser import BlockParser, CodeBlock
from ..core.context import ContextManager
from ..core.memory import (
    MemoryManager, MemoryConfig,
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
from ..tools.subagent import SubAgentTools, FILE_LOCK_COORDINATOR
from ..tools.terminal import TerminalManager
from ..tools.semantic import SemanticTools, SemanticConfigError


_ROLE_USER      = "user"
_ROLE_ASSISTANT = "assistant"

TAG_USER_INJECTION   = "[USER MESSAGE]"
TAG_EXECUTION_RESULT = "[EXECUTION RESULT]"
TAG_ENV_CHANGE       = "[ENVIRONMENT CHANGE]"
CONTROL_BLOCK_FILENAME = "<codepilot-control-block>"

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
    Any number of ``write_file()`` calls per step (mode='w' / 'a').
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
        db=None,
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

        # ------------------------------------------------------------------ #
        #  Per-step ephemeral state                                            #
        # ------------------------------------------------------------------ #
        self._payload_queue:    List[CodeBlock] = []
        self._execution_buffer: List[str]       = []
        self._step_write_count:  int = 0
        self._step_edited_files: set[str] = set()

        # Payload cache: populated at parse-time (before execution) so OS-level
        # failures can offer from_cache_id=<N> recovery.
        # Keys are auto-incrementing integer IDs, values are content strings.
        # Decoupled from file paths so the LLM can retry with a corrected path.
        # Entries are evicted on successful write/edit to prevent stale reuse.
        self._payload_cache: dict[int, str] = {}
        self._payload_cache_counter: int = 0

        self.context_manager = ContextManager(self.config.runtime.work_dir)
        self.prompt_manager  = PromptManager()

        self._fs_tools          = FilesystemTools(self)
        self._terminal_manager  = TerminalManager(self)
        self._interaction_tools = InteractionTools(self)
        self._semantic_tools    = SemanticTools(self)
        self._search_tools      = SearchTools(self)
        self._context_tools     = ContextTools(self)
        self._subagent_tools    = SubAgentTools(self, depth=0)
        self._file_lock_coordinator = FILE_LOCK_COORDINATOR

        self.registry = ToolRegistry()
        self._register_enabled_tools()
        self._validate_semantic_config()
        self._register_context_tools()

        # Start default terminal session (cross-platform)
        self._terminal_manager.start_default_terminal()

        # ------------------------------------------------------------------ #
        #  Workspace file change detection                                     #
        # ------------------------------------------------------------------ #
        self._watcher = WorkspaceWatcher(work_dir=self.config.runtime.work_dir)

        # ------------------------------------------------------------------ #
        #  Session / persistence                                               #
        # ------------------------------------------------------------------ #
        _sid = session_id or self.config.name.lower().replace(" ", "-")

        # ``db`` takes precedence over the legacy ``db_url`` string.
        # If db is an Engine or AsyncEngine, pass it as engine=; otherwise
        # fall back to the db_url string for backward compatibility.
        _engine = db if db is not None else None
        _db_url = db_url if _engine is None else None

        self.session: BaseSession = create_session(
            backend=session,
            session_id=_sid,
            agent_name=self.config.name,
            session_dir=session_dir,
            db_url=_db_url,
            engine=_engine,
        )

        # For async sessions, initial load is deferred to the first run() call
        # because __init__ is synchronous and cannot await coroutines.
        if self.session.is_async:
            self.messages: List[Dict[str, str]] = []
            self._session_bootstrapped = False
        else:
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
        extra = self.session.load_extra() if not self.session.is_async else {}
        if extra.get("memory_state"):
            self._memory.restore_state(extra["memory_state"])
        self._raw_llm_generations: List[Dict[str, Any]] = list(extra.get("raw_llm_generations", []))

        # ------------------------------------------------------------------ #
        #  Task position counter                                               #
        # ------------------------------------------------------------------ #
        self._task_counter = get_highest_task_position(self.messages)

        # Set for sync sessions only; async sessions set this in run() bootstrap
        self._session_bootstrapped = not self.session.is_async

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
            The summary string from the completion block, or None if the loop ended
            for any other reason (max_steps, abort).
        """
        self._done  = False
        self._abort = False
        self._terminal_manager.ensure_default_terminal()

        # Async session bootstrap: load history on the very first run() call.
        # This is deferred from __init__ because __init__ is not async.
        if not self._session_bootstrapped:
            self.messages = await self.session.load()
            extra = await self.session.load_extra()
            if extra.get("memory_state"):
                self._memory.restore_state(extra["memory_state"])
            self._raw_llm_generations = list(extra.get("raw_llm_generations", []))
            self._task_counter = get_highest_task_position(self.messages)
            self._session_bootstrapped = True

        # Safety-net: global summarization if context is dangerously high
        self.messages = await self._memory.process(self.messages)

        # Few-Shot Bootstrap: If history is empty, inject a perfect interaction
        # to teach the LLM Markdown protocol syntax and terminal usage.
        if not self.messages:
            self._inject_few_shot_bootstrap()

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

            force_thinking = False
            if len(self.messages) == 1 and not self.config.model.thinking.enabled:
                if self.config.model.provider.lower() == "deepseek":
                    force_thinking = True

            # 3. LLM inference (streaming or blocking)
            try:
                if self._stream:
                    # _stream_inference handles thinking interception inline:
                    # - thinking chunks → THINKING_STREAM (never STREAM)
                    # - returned text has thinking stripped → BlockParser-safe
                    response_text = await self._stream_inference(
                        system_prompt, messages=rendered_msgs, force_thinking=force_thinking
                    )
                else:
                    response_text = await self.provider.chat(
                        messages=rendered_msgs,
                        system=system_prompt,
                        temperature=self.config.model.temperature,
                        max_tokens=self.config.model.max_tokens,
                        force_thinking=force_thinking,
                    )
                    # Non-streaming: extract and emit thinking separately,
                    # then strip it so BlockParser never sees it.
                    import re as _re
                    think_match = _re.search(r'<thinking>(.*?)</thinking>\n?', response_text, flags=_re.DOTALL)
                    if think_match:
                        self.hooks.emit(EventType.THINKING_STREAM, thinking=think_match.group(1))
                        response_text = response_text[:think_match.start()] + response_text[think_match.end():]
                    self._emit_prefence_text(response_text)

            except Exception as exc:
                error_msg = f"LLM provider error: {exc}"
                self.hooks.emit(EventType.RUNTIME_ERROR, error=error_msg)
                self._append_execution_result(f"PROVIDER ERROR: {error_msg}")
                continue

            # 4. Parse response first so we can extract payload blocks.
            # Parser errors are recoverable model-format mistakes. Preserve the
            # assistant response in history, then feed back a protocol-level
            # correction message so the next inference can fix its block shape.
            try:
                control_block, payload_blocks, protocol_warning = BlockParser.split(response_text)
            except ValueError as exc:
                self.messages.append({"role": _ROLE_ASSISTANT, "content": response_text})
                error_msg = self._format_parser_error(str(exc))
                # Attempt to salvage payload content by position even though
                # the response had protocol violations. This allows the LLM to
                # retry failed writes using from_cache_id=<N> without regenerating.
                salvaged = BlockParser.salvage_payloads_for_cache(response_text)
                if salvaged:
                    salvage_lines = []
                    retry_lines = []
                    for target_path, content in salvaged:
                        cache_id = self._cache_payload(content)
                        salvage_lines.append(f"  - '{target_path}' → cache_id={cache_id}")
                        retry_lines.append(f'write_file("{target_path}", from_cache_id={cache_id})')
                    system_msg = (
                        "\n\n[SYSTEM] Despite the error, file content was salvaged by "
                        "positional matching and cached:\n"
                        + "\n".join(salvage_lines)
                        + "\n\nIn the next step, retry WITHOUT payload blocks — "
                        "content will be loaded from cache:\n"
                        "```codepilot\n"
                        + "\n".join(retry_lines)
                        + "\n```\n"
                        "Important: DO NOT regenerate file content. No payload block(s) needed — "
                        "call write_file() directly in control block without associating payload block."
                    )
                    error_msg = error_msg + system_msg
                self.hooks.emit(EventType.RUNTIME_ERROR, error=error_msg)
                self._append_execution_result(error_msg)
                continue

            # 4.5 Fire observability hook with the exact raw LLM generation
            # BEFORE any execution side effects. This is the place in the pipeline
            # where the full, unmodified response_text is available. The CLI
            # uses this to write a faithful trace JSON for debugging.
            self.hooks.emit(EventType.LLM_RESPONSE, step=step, response=response_text)
            self._record_raw_llm_generation(step, response_text)

            # LLM output → assistant role. Strip any hallucinated </task_N> closing
            # tags before storing. The model sees <task_N>...</task_N> wrappers for
            # completed tasks in the rendered context and pattern-matches by emitting
            # a spurious </task_N> to "close" the current (unwrapped) active task.
            # These tags are never valid in an assistant turn — strip them so the
            # model does not reinforce the pattern on the next inference step.
            stored_response = re.sub(r"\s*</task_\d+>", "", response_text)
            
            # Self-Correcting History: If the LLM glued the fence to the previous sentence
            # or used only one newline, auto-correct it to \n\n before saving to history.
            # This ensures the LLM learns the perfect pattern for future turns.
            stored_response = re.sub(r"([^\n])\s*(```codepilot\b)", r"\1\n\n\2", stored_response).strip()
            
            self.messages.append({"role": _ROLE_ASSISTANT, "content": stored_response})

            # Schedule cache TTL refresh (Anthropic only)
            self._last_system_prompt = system_prompt
            self._schedule_cache_timer()

            if control_block is None:
                # No ```codepilot block → conversational reply (may include
                # display ```python blocks). Already streamed to user.
                break

            # 5. Populate payload cache BEFORE execution.
            # This ensures content is cached even if the OS rejects the write.
            # Cache IDs are auto-assigned; the mapping from tool-call position
            # to cache ID is stored so filesystem tools can look up by ID.
            # Only calls with payload_count==1 are cached (from_cache_id calls
            # have payload_count==0 and are excluded).
            try:
                write_calls, _, _ = BlockParser._extract_write_file_calls(control_block.content)
                cache_targets = [fp for fp, cnt in write_calls if cnt == 1]
                self._step_precache_ids: dict[str, int] = {}
                for target, block in zip(cache_targets, payload_blocks):
                    cache_id = self._cache_payload(block.content)
                    self._step_precache_ids[target] = cache_id
            except Exception:
                pass  # Cache population is best-effort; never block execution

            # 6. Execute
            # (Post-execution [SYSTEM] cache notice removed — now inline in tools)
            self._payload_queue    = list(payload_blocks)
            self._execution_buffer = []
            await self._execute(control_block.content)

            # 5.5 If surplus payload blocks were detected, the warning is appended
            # AFTER the tool outputs in its own [PROTOCOL WARNING] section so
            # the model can clearly distinguish factual tool results from
            # meta-feedback about its own generation quality.
            # The warning explicitly states the operation succeeded so the
            # model does NOT attempt to redo the step.

            # 7. Assemble execution result and feed back as next user turn
            execution_result = "\n\n".join(self._execution_buffer).strip()
            if not execution_result:
                execution_result = "[Control block executed with no output.]"
            if protocol_warning:
                self.hooks.emit(EventType.RUNTIME_ERROR, error=protocol_warning)
                execution_result = (
                    f"{execution_result}\n\n"
                    f"[PROTOCOL WARNING]\n{protocol_warning}"
                )
            # Note: [SYSTEM] retry notices for cached content are now emitted
            # inline by the filesystem tools (write_file/edit_file) themselves
            # with failure-type-specific guidance. No global notice needed here.
            self._append_execution_result(execution_result)

            # 7. task(finish=True) was called during execution → loop terminates.
            #    Emit trailing text (after all parsed blocks) NOW so the user's
            #    stream reflects reality — summary appears after tools ran.
            if self._done:
                trailing = self._extract_trailing_text(
                    response_text, control_block, payload_blocks
                )
                if trailing:
                    self.hooks.emit(EventType.STREAM, text=trailing)
                self._done_summary = trailing
                self.hooks.emit(EventType.FINISH, summary=self._done_summary)
                break

            # 8. Update watcher snapshots (baseline for next step's check)
            self._watcher.snapshot_all()

        # Persist after every run() call
        if self.session.is_async:
            await self.session.save(self.messages)
            await self.session.save_extra({
                "memory_state": self._memory.serialize_state(),
                "raw_llm_generations": list(self._raw_llm_generations),
            })
        else:
            self.session.save(self.messages)
            self.session.save_extra({
                "memory_state": self._memory.serialize_state(),
                "raw_llm_generations": list(self._raw_llm_generations),
            })

        # Note: do NOT cancel the cache timer here.
        # It should fire between tasks to upgrade TTL to 1h.

        if not self._done and not self._abort:
            if step >= self.config.runtime.max_steps:
                self.hooks.emit(EventType.MAX_STEPS)
            else:
                # Stream was naturally closed due to a conversational reply rather than a completion block.
                # Emit a FINISH event so the UI gracefully restores its state.
                self.hooks.emit(EventType.FINISH, summary="Agent replied. Standing by for next task.")

        return self._done_summary if self._done else None

    def reset(self):
        """Wipe the entire conversation history and start fresh (sync sessions)."""
        self._cancel_cache_timer()
        self.messages = []
        self._raw_llm_generations = []
        if not self.session.is_async:
            self.session.reset()
        self._session_bootstrapped = not self.session.is_async
        self._terminal_manager.cleanup_all()
        self._terminal_manager.start_default_terminal()
        self.hooks.emit(EventType.SESSION_RESET)

    async def areset(self):
        """Wipe conversation history — use this when the session is async."""
        self._cancel_cache_timer()
        self.messages = []
        self._raw_llm_generations = []
        if self.session.is_async:
            await self.session.reset()
        else:
            self.session.reset()
        self._session_bootstrapped = not self.session.is_async
        self._terminal_manager.cleanup_all()
        self._terminal_manager.start_default_terminal()
        self.hooks.emit(EventType.SESSION_RESET)

    def raw_llm_generations(self) -> List[Dict[str, Any]]:
        """Return raw, uncompressed LLM generations captured for this session."""
        return list(self._raw_llm_generations)

    def _record_raw_llm_generation(self, step: int, response: str) -> None:
        """Persist a faithful raw generation record for observability."""
        self._raw_llm_generations.append({
            "task_position": self._task_counter,
            "step": step,
            "response": response,
        })

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

    def _inject_few_shot_bootstrap(self):
        """
        Inject a synthetic 'Pre-Flight Diagnostic' sequence into an empty session.
        This provides the LLM with real environment context (OS, Python) while
        serving as a flawless few-shot example of:
        1. Emitting perfectly fenced ```codepilot blocks (with blank lines).
        2. Chaining terminal commands with '&&' instead of raw newlines.
        3. Properly closing a task with task(finish=True).
        """
        import platform
        import sys
        
        is_windows = platform.system() == "Windows"
        cmd = "python --version && ver" if is_windows else "python3 --version && uname -s -r -m"
        
        py_ver = sys.version.split()[0]
        os_info = f"{platform.system()} {platform.release()} {platform.machine()}"
        
        output_body = (
            f"Python {py_ver}\n"
            f"{os_info}\n"
            f"[status: completed | return_code: 0 | pid: 9999 | cwd: {self.config.runtime.work_dir}]"
        )

        self.messages.extend([
            {
                "role": _ROLE_USER,
                "content": "[SYSTEM] Agent sandbox initialized. Please verify the environment context before proceeding."
            },
            {
                "role": _ROLE_ASSISTANT,
                "content": (
                    "I will check the operating system and Python environment to ensure I have the correct context before proceeding.\n\n"
                    "```codepilot\n"
                    f'execute("main", "{cmd}", timeout=5)\n'
                    "```"
                )
            },
            {
                "role": _ROLE_USER,
                "content": f"[EXECUTION RESULT]\n[terminal:main:cmd1] $ {cmd}\n{output_body}"
            },
            {
                "role": _ROLE_ASSISTANT,
                "content": (
                    "Environment verified successfully. I am standing by for the user's first task.\n\n"
                    "```codepilot\n"
                    "task(finish=True)\n"
                    "```"
                )
            },
            {
                "role": _ROLE_USER,
                "content": "[EXECUTION RESULT]\n[task] Task marked as complete."
            }
        ])
        
        # Reset task counter so the user's first actual prompt becomes [Task 1]
        self._task_counter = 0

    # ====================================================================== #
    #  Internal helpers — used by tool classes                                #
    # ====================================================================== #

    def pop_next_payload_block(self) -> Optional[CodeBlock]:
        if self._payload_queue:
            return self._payload_queue.pop(0)
        return None

    def _append_execution(self, text: str):
        self._execution_buffer.append(text)

    def _cache_payload(self, content: str) -> int:
        """Store content in the payload cache and return its unique ID."""
        self._payload_cache_counter += 1
        cache_id = self._payload_cache_counter
        self._payload_cache[cache_id] = content
        return cache_id

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
        # If the user supplies a tools: list in agent.yaml, honour it exactly.
        # If no tools: block is present, fall back to a safe default set.
        # NOTE: semantic_search is intentionally ABSENT from the default set.
        # It is an opt-in tool that requires explicit configuration in agent.yaml.
        enabled = (
            {tc.name for tc in self.config.tools if tc.enabled}
            if self.config.tools
            else {"view_file", "write_file", "edit_file", "execute", "read_output",
                  "send_input", "terminate_terminal", "ask_user", "find"}
        )
        if "view_file"          in enabled: self.registry.register("view_file",           self._fs_tools.view_file)
        if "write_file"         in enabled: self.registry.register("write_file",          self._fs_tools.write_file)
        if "edit_file"          in enabled: self.registry.register("edit_file",           self._fs_tools.edit_file)
        if "execute"             in enabled: self.registry.register("execute",             self._terminal_manager.execute)
        if "read_output"         in enabled: self.registry.register("read_output",         self._terminal_manager.read_output)
        if "send_input"          in enabled: self.registry.register("send_input",          self._terminal_manager.send_input)
        if "terminate_terminal"  in enabled: self.registry.register("terminate_terminal",  self._terminal_manager.terminate_terminal)
        if "ask_user"            in enabled: self.registry.register("ask_user",            self._interaction_tools.ask_user)
        if "semantic_search"     in enabled: self.registry.register("semantic_search",     self._semantic_tools.semantic_search)
        if "find"                in enabled: self.registry.register("find",                self._search_tools.find)

    def _validate_semantic_config(self):
        """Pre-flight check for semantic_search when it is explicitly enabled.

        If the tool is registered but misconfigured (wrong provider, missing
        API key, etc.), we:
        1. Unregister it so the LLM never sees it in the system prompt.
        2. Raise a RuntimeError so the user gets an immediate, clear message
           at startup rather than a confusing mid-task failure.
        """
        if self.registry.get("semantic_search") is None:
            # Tool not enabled — nothing to validate.
            return
        try:
            self._semantic_tools.validate_config()
        except SemanticConfigError as exc:
            # Hide the tool from the LLM.
            self.registry.unregister("semantic_search")
            raise RuntimeError(str(exc)) from exc

    def _register_context_tools(self):
        """Register context management and runtime control tools.

        These tools are independent of agent.yaml's optional tools list because
        they are part of the runtime's core protocol, not external workspace
        capabilities like filesystem, terminal, or semantic search.
        """
        # Context management tools (always enabled)
        self.registry.register("archive_context",       self._context_tools.archive_context)
        self.registry.register("reveal_context",        self._context_tools.reveal_context)
        self.registry.register("list_archived_context", self._context_tools.list_archived_context)
        # Runtime control
        self.registry.register("task",                  self._task_control)

        # Sub-agent tools — only registered when enabled in config
        if self.config.sub_agents.enabled:
            self.registry.register("spawn_subagent", self._subagent_tools.spawn_subagent)
            self.registry.register("await_subagent",  self._subagent_tools.await_subagent)

    def _task_control(self, *, finish: bool = False):
        """Signal task lifecycle events.

        task(finish=True) — marks the current task as complete. The agentic
        loop terminates after this step finishes executing. Write your
        final summary as plain text before or after the ```codepilot block.
        """
        if finish:
            self._done = True
            self._append_execution("[task] Task marked as complete.")
        else:
            self._append_execution("[task] No action taken (finish=False).")

    def _build_system_prompt(self, step: int = 0, max_steps: int = 0) -> SystemPromptParts:
        # Build sub-agent status block (empty when none active)
        sub_agent_status = self._subagent_tools.manager.build_status_block()
        return self.prompt_manager.render(
            agent_name=self.config.name,
            agent_role=self.config.role or "",
            developer_prompt=self.config.system_prompt,
            tool_definitions=self.registry.get_definitions(),
            work_dir=self.config.runtime.work_dir,
            codebase_snapshot=self.context_manager.get_formatted_snapshot(),
            shell_info=self._terminal_manager.get_prompt_info(),
            step_info=self._build_step_info(step, max_steps),
            context_stress=self._memory.build_context_stress(self.messages),
            sub_agent_status=sub_agent_status,
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
    #  Task prefix rendering for LLM visibility                            #
    # ------------------------------------------------------------------ #


    def _render_messages_for_llm(self) -> List[Dict]:
        """
        Create a copy of messages for LLM inference.

        The internal [Task N] prefix on each task's first user message is
        preserved as-is. This gives the model clear task boundary markers
        and the numeric position it needs for archive_context(position=N)
        without introducing any XML open/close structure that the model
        would pattern-match and attempt to close in its own responses.

        Archived tasks remain as a single [ARCHIVED TASK N] message.
        """
        rendered = []
        for msg in self.messages:
            rendered.append(dict(msg))
        return rendered


    # ------------------------------------------------------------------ #
    #  Streaming inference                                                 #
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    #  Trailing text extraction                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_trailing_text(
        response_text: str,
        control_block,
        payload_blocks: list,
    ) -> str:
        """Extract raw text after the last parsed block.

        This text is the agent's final summary — streamed to the user after
        execution completes, replacing the old ```completion block.
        """
        last_block = payload_blocks[-1] if payload_blocks else control_block
        if last_block is None:
            return ""
        return response_text[last_block.end_pos:].strip()

    # ------------------------------------------------------------------ #
    #  Streaming inference                                                 #
    # ------------------------------------------------------------------ #

    _CONTROL_FENCE_RE = re.compile(r"```codepilot\n")
    # Hold-back: buffer enough chars to avoid prematurely emitting a
    # partial fence marker split across streaming chunks.
    _HOLDBACK = len("```codepilot")  # 12 chars

    async def _stream_inference(
        self, system_prompt: Union[str, SystemPromptParts], messages: List[Dict] = None, force_thinking: bool = False
    ) -> str:
        """
        Stream the LLM response token by token — 3-state machine:

          'streaming'   — emit text to the user in real time. Watch for a
                          line-anchored ```codepilot fence OR <thinking> tag.
          'thinking'    — inside <thinking>...</thinking>. Emit chunks to
                          THINKING_STREAM only. Never forward to STREAM.
                          Never include in the returned response text.
          'buffering'   — ```codepilot fence detected: accumulate silently
                          until generation finishes.

        Returning accumulated WITHOUT the thinking block ensures BlockParser
        never sees code fences inside chain-of-thought, eliminating the
        payload-mismatch parser error on the first prompt.

        A hold-back buffer prevents premature emission when fence/tag markers
        are split across streaming chunks.

        Returns the complete response text (thinking stripped) for pipeline processing.
        """
        msgs = messages if messages is not None else self.messages
        chunks:            list = []   # full response chunks (thinking stripped)
        pre_fence_emitted: int  = 0
        state:             str  = "streaming"

        # Holdback buffer for partial tag/fence detection
        holdback: str = ""

        async for chunk in self.provider.chat_stream(
            messages=msgs,
            system=system_prompt,
            temperature=self.config.model.temperature,
            max_tokens=self.config.model.max_tokens,
            force_thinking=force_thinking,
        ):
            holdback += chunk

            # Strip hallucinated <codepilot>/<\/codepilot> XML wrapper tags.
            # Some models (e.g. deepseek-v4-flash) wrap their ```codepilot fence in
            # XML tags despite the system prompt prohibiting it. We strip them here
            # before the state machine runs so they never reach the STREAM event /
            # user terminal. The tags carry no information — the fence itself is the
            # signal the parser needs.
            holdback = holdback.replace("<codepilot>", "").replace("</codepilot>", "")

            while holdback:
                if state == "thinking":
                    end_idx = holdback.find("</thinking>")
                    if end_idx != -1:
                        # Emit whatever thinking text precedes the closing tag
                        thinking_chunk = holdback[:end_idx]
                        if thinking_chunk:
                            self.hooks.emit(EventType.THINKING_STREAM, thinking=thinking_chunk)
                        # Consume the closing tag and everything after
                        holdback = holdback[end_idx + len("</thinking>"):]
                        # Strip leading newline that follows </thinking>
                        if holdback.startswith("\n"):
                            holdback = holdback[1:]
                        state = "streaming"
                    else:
                        # Check for a partial closing tag at the end of holdback
                        # (e.g. holdback ends with "</think" — wait for more chunks)
                        partial = "</thinking>"
                        safe_end = len(holdback)
                        for k in range(1, len(partial)):
                            if holdback.endswith(partial[:k]):
                                safe_end = len(holdback) - k
                                break
                        if safe_end > 0:
                            self.hooks.emit(EventType.THINKING_STREAM, thinking=holdback[:safe_end])
                        holdback = holdback[safe_end:]
                        break

                else:  # state == "streaming" or "buffering"
                    # Check for <thinking> open tag first
                    think_idx = holdback.find("<thinking>")
                    if think_idx != -1:
                        # Emit/buffer everything before the tag
                        pre_think = holdback[:think_idx]
                        if pre_think and state == "streaming":
                            accumulated_so_far = "".join(chunks) + pre_think
                            m = self._CONTROL_FENCE_RE.search(accumulated_so_far)
                            if m:
                                ctrl_pos = m.start()
                                if ctrl_pos > pre_fence_emitted:
                                    self.hooks.emit(EventType.STREAM,
                                                    text=accumulated_so_far[pre_fence_emitted:ctrl_pos])
                                state = "buffering"
                                chunks.append(pre_think)
                                pre_fence_emitted = len("".join(chunks))
                            else:
                                chunks.append(pre_think)
                                accumulated_so_far = "".join(chunks)
                                safe_end = len(accumulated_so_far) - self._HOLDBACK
                                if safe_end > pre_fence_emitted:
                                    self.hooks.emit(EventType.STREAM,
                                                    text=accumulated_so_far[pre_fence_emitted:safe_end])
                                    pre_fence_emitted = safe_end
                        elif pre_think:
                            chunks.append(pre_think)

                        holdback = holdback[think_idx + len("<thinking>"):]
                        # Strip leading newline inside thinking tag
                        if holdback.startswith("\n"):
                            holdback = holdback[1:]
                        state = "thinking"
                        continue

                    # No <thinking> tag — process normally
                    # Check for partial <thinking> at end of holdback
                    tag = "<thinking>"
                    safe_end_hold = len(holdback)
                    for k in range(1, len(tag)):
                        if holdback.endswith(tag[:k]):
                            safe_end_hold = len(holdback) - k
                            break

                    to_process = holdback[:safe_end_hold]
                    holdback = holdback[safe_end_hold:]

                    if not to_process:
                        break

                    if state == "buffering":
                        chunks.append(to_process)
                        break

                    # state == "streaming"
                    chunks.append(to_process)
                    accumulated = "".join(chunks)
                    m = self._CONTROL_FENCE_RE.search(accumulated)
                    if m:
                        ctrl_pos = m.start()
                        if ctrl_pos > pre_fence_emitted:
                            self.hooks.emit(EventType.STREAM,
                                            text=accumulated[pre_fence_emitted:ctrl_pos])
                        state = "buffering"
                    else:
                        safe_end = len(accumulated) - self._HOLDBACK
                        if safe_end > pre_fence_emitted:
                            self.hooks.emit(EventType.STREAM,
                                            text=accumulated[pre_fence_emitted:safe_end])
                            pre_fence_emitted = safe_end
                    break

        # ------------------------------------------------------------------ #
        # Flush holdback remainder                                            #
        # ------------------------------------------------------------------ #
        if holdback and state != "thinking":
            chunks.append(holdback)

        # ------------------------------------------------------------------ #
        # Final flush — only needed for pure conversational responses.       #
        # ------------------------------------------------------------------ #
        accumulated = "".join(chunks)

        if state == "streaming":
            # No codepilot block at all — pure conversation, flush everything.
            remaining = accumulated[pre_fence_emitted:]
            if remaining:
                self.hooks.emit(EventType.STREAM, text=remaining)

        return accumulated

    @classmethod
    def _find_control_fence(cls, text: str) -> int:
        """Find the character position of a line-anchored ```codepilot fence.

        Returns the position of the opening ```, or -1 if not found.
        """
        m = cls._CONTROL_FENCE_RE.search(text)
        return m.start() if m else -1

    def _emit_prefence_text(self, response_text: str):
        """Emit pre-fence text as STREAM events (non-streaming mode).

        Trailing text after the codepilot/payload blocks is emitted by run()
        after _execute() completes, not here.
        """
        fence_pos = self._find_control_fence(response_text)
        if fence_pos > 0:
            pre_text = response_text[:fence_pos].strip()
            if pre_text:
                self.hooks.emit(EventType.STREAM, text=pre_text)
        elif fence_pos == -1 and response_text.strip():
            # No control block at all — emit everything (display/chat)
            self.hooks.emit(EventType.STREAM, text=response_text.strip())

    @staticmethod
    def _format_parser_error(error: str) -> str:
        return (
            "PARSER ERROR: The previous assistant response was not executed because "
            "it violated CodePilot's block protocol.\n\n"
            f"Specific parser failure: {error}\n\n"
            "How to fix your next response:\n"
            "1. Emit exactly one ```codepilot fenced Control Block if you want tools to run.\n"
            "2. For every write_file(...) call, provide the required Payload Block(s) immediately "
            "after the Control Block.\n"
            "3. Every Payload Block must include a filename= annotation that exactly matches the "
            "corresponding write_file path, for example: ```python filename=src/app.py.\n"
            "4. Payload Blocks are consumed strictly in write_file call order. For mode='multi_edit', "
            "provide one Payload Block per tuple in edits=[...], in the same order as the tuples.\n"
            "5. Use task(finish=True) in the control block to signal task completion — do not "
            "combine it with execute() or read_output() in the same step.\n\n"
            "No tool code from the previous response ran. Re-emit a corrected CodePilot response now."
        )

    # ------------------------------------------------------------------ #
    #  Sandbox + execution                                                 #
    # ------------------------------------------------------------------ #

    def _build_sandbox(self, captured_print=None) -> Dict[str, Any]:
        import builtins
        b_dict = {k: getattr(builtins, k) for k in dir(builtins)}
        b_dict["print"] = captured_print if captured_print is not None else print
        sandbox = {
            "__builtins__": b_dict,
            "WORK_DIR": self.config.runtime.work_dir,
        }
        sandbox.update(self.registry.as_sandbox_dict())
        return sandbox

    async def _execute(self, code: str):
        """Async wrapper — runs the CPU-bound + PTY-bound execution in a thread."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._execute_sync, code)

    def _execute_sync(self, code: str):
        _print_lines: list = []

        def _captured_print(*args, sep=" ", end="\n", file=None, flush=False):
            _print_lines.append(sep.join(str(a) for a in args) + end)

        try:
            compiled = compile(code, CONTROL_BLOCK_FILENAME, "exec")
        except SyntaxError as exc:
            tb = traceback.format_exc()
            error_text = self._format_execution_error(exc, tb, ran_any_statements=False)
            self.hooks.emit(EventType.RUNTIME_ERROR, error=error_text)
            self._append_execution(error_text)
            return
            
        has_tools = any(tool in code for tool in self.registry._tools)
        if not has_tools:
            self.hooks.emit(EventType.TOOL_CALL, tool="python", args={}, label="Executing raw python block...")

        try:
            exec(compiled, self._build_sandbox(captured_print=_captured_print))  # noqa: S102
        except Exception as exc:
            tb = traceback.format_exc()
            error_text = self._format_execution_error(exc, tb, ran_any_statements=True)
            self.hooks.emit(EventType.RUNTIME_ERROR, error=error_text)
            self._append_execution(error_text)

        printed = "".join(_print_lines).strip()
        if printed:
            self._execution_buffer.insert(0, printed)
            
        if not has_tools:
            self.hooks.emit(EventType.TOOL_RESULT, tool="python", result=printed)

    @staticmethod
    def _format_execution_error(exc: BaseException, traceback_text: str, ran_any_statements: bool) -> str:
        line_no = getattr(exc, "lineno", None)
        if line_no is None:
            match = re.search(
                rf'File "{re.escape(CONTROL_BLOCK_FILENAME)}", line (\d+)',
                traceback_text,
            )
            if match:
                line_no = match.group(1)
        line_no = line_no or "unknown"
        origin = f"generated ```codepilot Control Block, line {line_no}"
        exception_name = type(exc).__name__
        exception_message = str(exc) or "(no exception message)"

        if ran_any_statements:
            semantics = (
                "Execution semantics:\n"
                "- Python started executing the Control Block.\n"
                "- Statements before the failing line may already have run and may have produced tool results or side effects.\n"
                "- The failing statement did not complete, and statements after it did not run.\n"
                "- Tool results printed before this error are still ground truth; inspect them before retrying."
            )
        else:
            semantics = (
                "Execution semantics:\n"
                "- Python could not compile the Control Block.\n"
                "- No statements in the Control Block ran.\n"
                "- No tool calls or file/terminal side effects came from this failed Control Block."
            )

        return (
            "EXECUTION ERROR: The assistant's ```codepilot Control Block raised a Python exception.\n\n"
            f"Origin: {origin}.\n"
            f"Exception: {exception_name}: {exception_message}\n\n"
            f"{semantics}\n\n"
            "Raw traceback:\n"
            f"{traceback_text}"
        )

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
    Synchronous wrapper over AsyncRuntime with a **persistent** event loop.

    A dedicated daemon thread runs an asyncio event loop for the lifetime of
    this Runtime instance.  All async operations (LLM inference, streaming,
    cache timers, session persistence) execute on that loop — they survive
    across multiple ``run()`` calls, fixing issues with ``asyncio.run()``
    destroying the loop (and all its tasks/timers) after every invocation.

    Suitable for CLI usage and scripts. For web apps / FastAPI, use
    AsyncRuntime directly with ``await``.
    """

    def __init__(self, agent_file: str, **kwargs):
        # ------------------------------------------------------------------ #
        #  Persistent event loop on a daemon thread                            #
        # ------------------------------------------------------------------ #
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            name="codepilot-runtime-loop",
            daemon=True,
        )
        self._loop_thread.start()

        # Create AsyncRuntime on the loop thread so any loop-bound resources
        # (cache timers, etc.) bind to the correct loop.
        async def _init_async():
            return AsyncRuntime(agent_file, **kwargs)

        future = asyncio.run_coroutine_threadsafe(_init_async(), self._loop)
        self._async = future.result()

    # ── internal helper ──────────────────────────────────────────────────────

    def _run_coro(self, coro):
        """Submit a coroutine to the persistent loop and block until done."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    # ── public API mirrors AsyncRuntime ──────────────────────────────────────

    def run(self, task: str) -> Optional[str]:
        """Run a task synchronously. Blocks the calling thread until complete."""
        return self._run_coro(self._async.run(task))

    def reset(self):
        """Wipe the entire conversation history and start fresh."""
        if self._async.session.is_async:
            self._run_coro(self._async.areset())
        else:
            self._async.reset()

    def abort(self):
        """Stop the loop cleanly after the current step completes."""
        self._async.abort()

    def send_message(self, message: str):
        """Inject a user message into the running loop from any thread."""
        self._async.send_message(message)

    def raw_llm_generations(self) -> List[Dict[str, Any]]:
        """Return raw, uncompressed LLM generations captured for this session."""
        return self._async.raw_llm_generations()

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
