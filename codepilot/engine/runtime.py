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
from pathlib import Path
from dataclasses import replace
from typing import Dict, List, Optional, Any, Union

from ..core.prompt import SystemPromptParts

from ..core.agent_file import AgentConfig
from ..core.diff_protocol import DiffOperation, DiffProtocolError, apply_operation, parse_operations
from ..core.context import ContextManager
from ..core.memory import (
    MemoryManager, MemoryConfig,
    get_highest_task_position, TAG_USER_INPUT,
    count_messages_tokens,
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
from ..tools.mcp_tool import MCPTools


_ROLE_USER      = "user"
_ROLE_ASSISTANT = "assistant"

TAG_USER_INJECTION   = "[USER MESSAGE]"
TAG_EXECUTION_RESULT = "[EXECUTION RESULT]"
TAG_ENV_CHANGE       = "[ENVIRONMENT CHANGE]"
CONTROL_BLOCK_FILENAME = "~/.codepilot/runtime/codepilot.py"
RUNTIME_SCRIPT_NAME = "codepilot.py"

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
    Any natural text before the first diff header is emitted immediately via
    the STREAM hook, giving the user real-time feedback while file operations
    buffer until their complete diff records can be validated and applied.

    Streaming is enabled by default so natural Markdown reaches the UI while
    diff records are safely buffered. Set ``stream=False`` to suppress public
    STREAM hooks; inference still streams internally for protocol detection.

    Multi-turn
    ----------
     
    Calling ``runtime.run(task)`` multiple times continues the conversation.
    To start fresh, call ``runtime.reset()`` first.

    Session backends
    ----------------
    memory (default) — in-RAM, lost on exit.
    file             — persisted to ~/.codepilot/sessions/<id>.json.

    Multi-file diffs
    -----------------
    Any number of independent file diffs may appear in a step. Hunk ranges
    are ignored; edits are located by uniquely matching their old content.

    Parallel commands
    -----------------
    ``run_command(cmd, execution="parallel")`` queues commands; they are
    launched simultaneously after the runtime script finishes.
    """

    def __init__(
        self,
        agent_file: str,
        session: str = "memory",
        session_id: Optional[str] = None,
        session_dir=None,
        stream: bool = True,
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
        self._execution_buffer: List[str]       = []
        # A failed filesystem diff may be large.  Retain the parsed operation so
        # the next script can repair permissions and replay it without asking the
        # model to reproduce content.
        self._diff_cache: dict[int, DiffOperation] = {}
        self._diff_cache_counter: int = 0
        self._runtime_script_path = Path.home() / ".codepilot" / "runtime" / RUNTIME_SCRIPT_NAME
        self._reset_runtime_script()

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
        self._mcp_tools         = MCPTools(self) if self._mcp_enabled() else None
        self._mcp_setup_done    = False

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
                context_stress_multiplier=_mem_cfg.context_stress_multiplier,
                context_stress_trigger=_mem_cfg.context_stress_trigger,
                context_safety_margin_tokens=_mem_cfg.context_safety_margin_tokens,
                generation_reserve_tokens=(
                    self.config.model.max_tokens
                    + (self.config.model.thinking.budget_tokens
                       if self.config.model.thinking.enabled else 0)
                ),
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
        self._context_maintenance_active = False
        self._context_maintenance_completed = False

        # ------------------------------------------------------------------ #
        #  Per-step system-prompt cache                                        #
        # ------------------------------------------------------------------ #
        # _build_system_prompt() is measured once per step by
        # _maybe_start_context_maintenance() before the loop knows whether a
        # maintenance turn is needed. When it isn't, the loop's own prompt
        # build is identical (same context_stress="" content) — cached here
        # so it isn't rendered and tiktoken-counted twice per step.
        self._prompt_cache_step: Optional[int] = None
        self._prompt_cache: Optional[SystemPromptParts] = None
        self._prompt_cache_system_tokens: int = 0



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

        # MCP setup: connect servers and index tools on the very first run().
        # Deferred from __init__ because setup() is async.
        if self._mcp_tools is not None and not self._mcp_setup_done:
            await self._mcp_tools.setup()
            self._mcp_setup_done = True

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
            self._reset_runtime_script()

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

            await self._maybe_start_context_maintenance(step)

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

            # 3. LLM inference — always stream internally so the second-block
            # early-abort can fire in real time regardless of the public stream=
            # flag. _stream_inference suppresses user-facing STREAM events when
            # self._stream is False, preserving the external API contract exactly.
            try:
                response_text = await self._stream_inference(
                    system_prompt, messages=rendered_msgs, force_thinking=force_thinking
                )

            except Exception as exc:
                error_msg = f"LLM provider error: {exc}"
                self.hooks.emit(EventType.RUNTIME_ERROR, error=error_msg)
                self._append_execution_result(f"PROVIDER ERROR: {error_msg}")
                continue

            # 4. Parse self-contained diff operations.  Natural Markdown with no
            # diff is a conversational response; the old positional payload
            # protocol is intentionally no longer recognised.
            # parse_operations is fault-tolerant: it returns (ops, parse_errors)
            # so a single malformed diff block does not abort the good ones.
            operations, parse_errors = parse_operations(response_text)

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
            
            stored_response = stored_response.strip()
            
            self.messages.append({"role": _ROLE_ASSISTANT, "content": stored_response})

            # Schedule cache TTL refresh (Anthropic only)
            self._last_system_prompt = system_prompt
            self._schedule_cache_timer()

            if not operations and not parse_errors:
                # No diff at all — conversational reply. Already streamed to user.
                break

            # 5. Apply all workspace diffs, then execute the ephemeral script.
            self._execution_buffer = []

            # Feed back parse errors for each malformed diff block BEFORE applying
            # the good ones.  This preserves the left-to-right reading order in
            # [EXECUTION RESULT] so the model sees positional context.
            for pos, target, exc in parse_errors:
                err_msg = self._format_diff_error(str(exc))
                # Prepend position so the model can correlate with its own output.
                self._append_execution(
                    f"[diff #{pos} for '{target}'] PARSE FAILED: {err_msg}"
                )
                self.hooks.emit(EventType.RUNTIME_ERROR, error=err_msg)

            await self._apply_diff_operations(operations)
            script = self._runtime_script_path.read_text(encoding="utf-8")
            if script:
                await self._execute(script)
            self._reset_runtime_script()

            # 7. Assemble execution result and feed back as next user turn
            execution_result = "\n\n".join(self._execution_buffer).strip()
            if not execution_result:
                execution_result = "[Diff step completed with no output.]"
            self._append_execution_result(execution_result)

            if self._context_maintenance_completed:
                self._finish_context_maintenance()

            # 7. task(finish=True) was called during execution → loop terminates.
            #    Emit trailing text (after all parsed blocks) NOW so the user's
            #    stream reflects reality — summary appears after tools ran.
            if self._done:
                trailing = self._extract_trailing_text(
                    response_text, operations
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

        The tool is callable by name in the ephemeral runtime script.
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
        1. Emitting a valid ```diff-fenced unified diff for codepilot.py.
        2. Chaining terminal commands with '&&' instead of raw newlines.
        3. Properly closing a task with task(finish=True) in a separate step.
        The synthetic messages use the current diff protocol so the model
        pattern-matches the correct format from turn zero.
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
                    "```diff\n"
                    "diff --git a/codepilot.py b/codepilot.py\n"
                    "--- /dev/null\n"
                    "+++ b/codepilot.py\n"
                    "@@ -0,0 +1,1 @@\n"
                    f'+execute("main", "{cmd}", timeout=5)\n'
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
                    "```diff\n"
                    "diff --git a/codepilot.py b/codepilot.py\n"
                    "--- /dev/null\n"
                    "+++ b/codepilot.py\n"
                    "@@ -0,0 +1,1 @@\n"
                    "+task(finish=True)\n"
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

    def _append_execution(self, text: str):
        self._execution_buffer.append(text)

    def _cache_diff(self, operation: DiffOperation) -> int:
        """Store a failed, parsed diff operation for lossless retry."""
        self._diff_cache_counter += 1
        cache_id = self._diff_cache_counter
        self._diff_cache[cache_id] = operation
        return cache_id

    def _reset_runtime_script(self) -> None:
        self._runtime_script_path.parent.mkdir(parents=True, exist_ok=True)
        self._runtime_script_path.write_text("", encoding="utf-8")

    def _safe_diff_path(self, path: str) -> Path:
        work_dir = Path(self.config.runtime.work_dir).resolve()
        candidate = (work_dir / path).resolve()
        if not self.config.runtime.unsafe_mode and not candidate.is_relative_to(work_dir):
            raise PermissionError(f"'{path}' is outside workspace '{work_dir}'.")
        return candidate

    async def _apply_diff_operations(self, operations: list[DiffOperation]) -> None:
        """Apply each independent diff with existing TOOL_CALL/RESULT hooks."""
        for operation in operations:
            if operation.path.replace("\\", "/") == RUNTIME_SCRIPT_NAME:
                try:
                    source = apply_operation(operation, "", exists=False)
                    self._runtime_script_path.write_text(source, encoding="utf-8")
                    result = f"[diff] runtime script prepared ({len(source)} bytes)."
                    self._append_execution(result)
                    self.hooks.emit(EventType.TOOL_RESULT, tool="diff", result=result)
                except DiffProtocolError as exc:
                    # codepilot.py-specific: always a /dev/null creation diff,
                    # so only creation-specific errors can appear here.
                    result = (
                        f"[diff:codepilot.py] REJECTED: {exc} "
                        "Regenerate the complete codepilot.py diff using `--- /dev/null` "
                        "and only `+` lines. No script ran this step."
                    )
                    self._append_execution(result)
                    self.hooks.emit(EventType.TOOL_RESULT, tool="diff", result=result)
                except OSError as exc:
                    result = (
                        f"[diff:codepilot.py] OS ERROR: {exc}. "
                        "The runtime script directory may be missing. "
                        "Regenerate the codepilot.py /dev/null diff; no script ran."
                    )
                    self._append_execution(result)
                    self.hooks.emit(EventType.TOOL_RESULT, tool="diff", result=result)
                continue

            self.hooks.emit(
                EventType.TOOL_CALL, tool="diff",
                args={"path": operation.path, "hunks": len(operation.hunks)},
                label=f"Applying diff to {operation.path}...",
            )
            try:
                path = self._safe_diff_path(operation.path)
                exists = path.exists()

                if not exists and not operation.is_creation:
                    result = (
                        f"[diff] REJECTED: '{operation.path}' does not exist. "
                        "You cannot apply an edit diff to a non-existent file. "
                        "If you intended to create it, use a '--- /dev/null' creation diff."
                    )
                    self._append_execution(result)
                    self.hooks.emit(EventType.TOOL_RESULT, tool="diff", result=result)
                    continue

                current = path.read_text(encoding="utf-8") if exists else ""
                new_content = apply_operation(operation, current, exists)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(new_content, encoding="utf-8")
                if hasattr(self, "_watcher"):
                    self._watcher.register(str(path))
                
                if not exists:
                    result = f"[diff] '{operation.path}' created ({len(operation.hunks)} hunk(s), {len(new_content)} bytes). (Missing parent directories auto-created)."
                else:
                    result = f"[diff] '{operation.path}' updated ({len(operation.hunks)} hunk(s), {len(new_content)} bytes)."
                
                self._append_execution(result)
                self.hooks.emit(EventType.TOOL_RESULT, tool="diff", result=result)
            except DiffProtocolError as exc:
                e = str(exc).lower()
                if "not found" in e:
                    # Hunk content not in file — need more context lines.
                    # Cannot cache because the operation itself is flawed.
                    result = (
                        f"[diff] REJECTED: '{operation.path}' unchanged. {exc} "
                        "Regenerate this file's diff with more surrounding unchanged lines "
                        "(context lines starting with a space) so the old region matches exactly once."
                    )
                elif "ambiguous" in e:
                    # Multiple identical regions found.
                    # Cannot cache because the operation itself is flawed.
                    result = (
                        f"[diff] REJECTED: '{operation.path}' unchanged. {exc} "
                        "Add more unique context lines (lines with a space prefix) "
                        "above or below the changed block to make the match unambiguous."
                    )
                else:
                    # Structurally invalid (e.g., creation diff with wrong lines).
                    result = (
                        f"[diff] REJECTED: '{operation.path}' unchanged. {exc} "
                        "Regenerate it from scratch."
                    )
                self._append_execution(result)
                self.hooks.emit(EventType.TOOL_RESULT, tool="diff", result=result)
            except OSError as exc:
                cache_id = self._cache_diff(operation)
                result = (
                    f"[diff] OS ERROR: '{operation.path}' unchanged: {exc}\n"
                    f"Diff cached as diff_cache_id={cache_id} — do not regenerate content. "
                    f"Fix the OS condition in codepilot.py then call retry_diff({cache_id}) "
                    f"or retry_diff({cache_id}, path='correct/path') if the target path was wrong."
                )
                self._append_execution(result)
                self.hooks.emit(EventType.TOOL_RESULT, tool="diff", result=result)
            except Exception as exc:
                result = f"[diff] UNEXPECTED ERROR: '{operation.path}' unchanged: {type(exc).__name__}: {exc}."
                self._append_execution(result)
                self.hooks.emit(EventType.TOOL_RESULT, tool="diff", result=result)

    def retry_diff(self, diff_cache_id: int, path: Optional[str] = None) -> None:
        """Retry a cached diff after fixing an OS error; optional path corrects a bad target path."""
        operation = self._diff_cache.get(diff_cache_id)
        if operation is None:
            self._append_execution(f"[diff] ERROR: diff_cache_id={diff_cache_id} is not available.")
            return
        # This method runs from codepilot.py synchronously; use the same single
        # operation logic without an event-loop roundtrip.
        try:
            if path is not None:
                operation = replace(operation, path=path)
            path = self._safe_diff_path(operation.path)
            exists = path.exists()

            if not exists and not operation.is_creation:
                self._append_execution(
                    f"[diff] REJECTED: '{operation.path}' does not exist. "
                    "Cannot retry an edit diff on a non-existent file."
                )
                return

            current = path.read_text(encoding="utf-8") if exists else ""
            new_content = apply_operation(operation, current, exists)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_content, encoding="utf-8")
            self._diff_cache.pop(diff_cache_id, None)
            
            if not exists:
                result = f"[diff] '{operation.path}' created from diff_cache_id={diff_cache_id}."
            else:
                result = f"[diff] '{operation.path}' updated from diff_cache_id={diff_cache_id}."
                
            self._append_execution(result)
            self.hooks.emit(EventType.TOOL_RESULT, tool="diff", result=result)
        except DiffProtocolError as exc:
            self._append_execution(
                f"[diff] REJECTED: cached diff {diff_cache_id} no longer matches the current file: {exc} "
                "Read the current file with view_file() and generate a fresh diff; "
                "the cached operation was retained for reference."
            )
        except OSError as exc:
            self._append_execution(
                f"[diff] OS ERROR: cached diff {diff_cache_id} still failing: {exc}. "
                "Fix the environment condition and call retry_diff again."
            )

    def _tool_config(self, tool_name: str) -> dict:
        for tc in self.config.tools:
            if tc.name == tool_name:
                return tc.config
        return {}

    def _mcp_enabled(self) -> bool:
        """Return True if the 'mcp' tool is explicitly enabled in agent.yaml."""
        for tc in self.config.tools:
            if tc.name == "mcp" and tc.enabled:
                return True
        return False

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
            else {"view_file", "execute", "read_output",
                  "send_input", "terminate_terminal", "ask_user", "find"}
        )
        if "view_file"          in enabled: self.registry.register("view_file",           self._fs_tools.view_file)
        if "execute"             in enabled: self.registry.register("execute",             self._terminal_manager.execute)
        if "read_output"         in enabled: self.registry.register("read_output",         self._terminal_manager.read_output)
        if "send_input"          in enabled: self.registry.register("send_input",          self._terminal_manager.send_input)
        if "terminate_terminal"  in enabled: self.registry.register("terminate_terminal",  self._terminal_manager.terminate_terminal)
        if "ask_user"            in enabled: self.registry.register("ask_user",            self._interaction_tools.ask_user)
        if "semantic_search"     in enabled: self.registry.register("semantic_search",     self._semantic_tools.semantic_search)
        if "find"                in enabled: self.registry.register("find",                self._search_tools.find)
        if "mcp"                 in enabled and self._mcp_tools is not None:
            self.registry.register("mcp", self._mcp_tools.mcp)

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
        # Runtime control
        self.registry.register("task",                  self._task_control)
        self.registry.register("retry_diff",            self.retry_diff)

        # Sub-agent tools — only registered when enabled in config
        if self.config.sub_agents.enabled:
            self.registry.register("spawn_subagent", self._subagent_tools.spawn_subagent)
            self.registry.register("await_subagent",  self._subagent_tools.await_subagent)

    async def _maybe_start_context_maintenance(self, step: int) -> None:
        """Start one same-agent archival turn when measured stress requires it.

        Two outcomes when maintenance is required:
          1. There are completed, unarchived tasks the agent can reason
             about → hand it a forced maintenance turn (the common case).
          2. There are none (single long-running task, or everything is
             already archived) → nothing for the agent to judge, so fall
             back to the emergency backstop (see MemoryManager.process)
             instead of silently doing nothing while load keeps climbing.
        """
        if self._context_maintenance_active:
            return

        # This build is cached by _build_system_prompt() below and reused
        # by the loop's own prompt build later this same step if maintenance
        # turns out not to be needed — see the cache note in __init__.
        prompt = self._build_system_prompt(step, self.config.runtime.max_steps)
        system_tokens = self._prompt_cache_system_tokens
        pressure = self._memory.measure_context(self.messages, system_tokens)
        if not pressure.maintenance_required:
            return

        candidates = self._memory.build_archive_candidates(self.messages)
        if not candidates:
            before_stress = pressure.context_stress
            before_tokens = count_messages_tokens(self.messages, self._memory.config.provider_name)
            new_messages = await self._memory.process(self.messages, system_tokens)
            if new_messages is self.messages:
                return  # backstop declined — hard ceiling not actually crossed
            self.messages = new_messages
            after_tokens = count_messages_tokens(self.messages, self._memory.config.provider_name)
            after_stress = self._memory.measure_context(self.messages, system_tokens).context_stress
            self.hooks.emit(
                EventType.CONTEXT_DROP,
                before_pct=round(before_stress * 100),
                after_pct=round(after_stress * 100),
                tokens_saved=max(before_tokens - after_tokens, 0),
                tasks_archived=[],
            )
            return

        self._context_maintenance_active = True
        self._context_maintenance_completed = False
        self.registry.register("archive_context", self._context_tools.archive_context)
        self.messages.append({
            "role": _ROLE_USER,
            "content": self._build_maintenance_instruction(pressure, candidates),
        })
        self.hooks.emit(
            EventType.CONTEXT_MAINTENANCE_START,
            stress_pct=round(pressure.context_stress * 100),
            history_tokens=pressure.history_tokens,
            safe_budget=pressure.safe_history_budget,
            candidates=candidates,
        )

    @staticmethod
    def _build_maintenance_instruction(pressure, candidates: str) -> str:
        return (
            "[INTERNAL CONTEXT MAINTENANCE]\n\n"
            "Do NOT continue what you are doing. Do not perform normal task work.\n\n"
            f"Current history: {pressure.history_tokens:,} / "
            f"{pressure.safe_history_budget:,} safe tokens.\n"
            f"Context Stress: {pressure.context_stress * 100:.1f}%.\n\n"
            "Completed-task candidates:\n"
            f"{candidates}\n\n"
            "Decide whether completed tasks bear on the ACTIVE task. Archive every "
            "completed task that can be removed without affecting the ACTIVE task, "
            "and remove context noise. Use archive_context with dense factual "
            "summaries preserving exact files, decisions, commands, outcomes, and "
            "unresolved items. The ACTIVE task is protected."
        )

    def _finish_context_maintenance(self) -> None:
        """Return to the stable normal tool set after a successful archive."""
        self.registry.unregister("archive_context")
        self._context_maintenance_active = False
        self._context_maintenance_completed = False

    def _task_control(self, *, finish: bool = False):
        """Signal task lifecycle events.

        task(finish=True) — marks the current task as complete. The agentic
        loop terminates after this step finishes executing. Write your
        final summary as plain text before or after the diff records.
        """
        if finish:
            self._done = True
            self._append_execution("[task] Task marked as complete.")
        else:
            self._append_execution("[task] No action taken (finish=False).")

    def _build_system_prompt(self, step: int = 0, max_steps: int = 0) -> SystemPromptParts:
        # Build sub-agent status block (empty when none active)
        sub_agent_status = self._subagent_tools.manager.build_status_block()

        # Build MCP server block (empty string when MCP not configured)
        mcp_server_block = (
            self._mcp_tools.build_server_block()
            if self._mcp_tools is not None and self._mcp_setup_done
            else ""
        )

        # Append MCP server block to developer_prompt so it appears in the
        # static (cacheable) half of the system prompt, right after the
        # developer's own instructions.  It is token-cheap (server names
        # only) so caching it is safe.
        developer_prompt = self.config.system_prompt
        if mcp_server_block:
            developer_prompt = developer_prompt + "\n\n" + mcp_server_block

        common_kwargs = dict(
            agent_name=self.config.name,
            agent_role=self.config.role or "",
            developer_prompt=developer_prompt,
            tool_definitions=self.registry.get_definitions(),
            work_dir=self.config.runtime.work_dir,
            codebase_snapshot=self.context_manager.get_formatted_snapshot(),
            shell_info=self._terminal_manager.get_prompt_info(),
            step_info=self._build_step_info(step, max_steps),
            sub_agent_status=sub_agent_status,
        )

        if not self._context_maintenance_active:
            # This exact render (context_stress="") is what
            # _maybe_start_context_maintenance() needs for its pressure
            # check, and — when maintenance turns out not to be needed —
            # what the loop needs for the real inference call too. Cache
            # per-step so the ~4-5k token prompt isn't rendered and
            # tiktoken-counted twice for identical content.
            if self._prompt_cache_step == step:
                return self._prompt_cache
            prompt = self.prompt_manager.render(context_stress="", **common_kwargs)
            self._prompt_cache_step = step
            self._prompt_cache = prompt
            self._prompt_cache_system_tokens = self._memory.count_prompt_tokens(prompt.full)
            return prompt

        # Maintenance is active this step: build the real stress-annotated
        # prompt. system_tokens comes from this step's own context_stress=""
        # measurement (cached above, taken during the pressure check that
        # triggered maintenance in the first place) — no extra render needed
        # to get it, only one final render with the real stress text.
        if self._prompt_cache_step != step:
            base = self.prompt_manager.render(context_stress="", **common_kwargs)
            self._prompt_cache_step = step
            self._prompt_cache_system_tokens = self._memory.count_prompt_tokens(base.full)
        return self.prompt_manager.render(
            context_stress=self._memory.build_context_stress(
                self.messages, system_tokens=self._prompt_cache_system_tokens,
            ),
            **common_kwargs,
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
        operations: list[DiffOperation],
    ) -> str:
        """Extract raw text after the last parsed block.

        This text is the agent's final summary — streamed to the user after
        execution completes, replacing the old ```completion block.
        """
        if not operations:
            return ""
        last = operations[-1].source
        start = response_text.rfind(last)
        if start < 0:
            return ""
        trailing = response_text[start + len(last):].strip()
        return trailing.strip("`").strip()

    # ------------------------------------------------------------------ #
    #  Streaming inference                                                 #
    # ------------------------------------------------------------------ #

    # Primary trigger: the ```diff opening fence. Buffering starts here so the
    # fence itself is never streamed to the user as raw text.
    _DIFF_FENCE_OPEN_RE = re.compile(r"```diff\n")
    # Fallback trigger: bare diff --git header when the model omits the fence.
    # The parser handles fenceless diffs fine; we just need to stop streaming.
    _DIFF_HEADER_RE = re.compile(r"^diff --git a/.+? b/.+?$", re.MULTILINE)
    # Kept for backward compatibility with _find_control_fence / _emit_prefence_text.
    _CONTROL_FENCE_RE = _DIFF_HEADER_RE
    # Matches a standalone closing ``` fence line: newline + ``` + optional spaces + newline.
    # Used by the second-block early-abort logic to track the last clean clip point.
    _CLOSE_FENCE_RE = re.compile(r"\n```[ \t]*\n")
    # Hold-back: buffer enough chars to avoid prematurely emitting a partial marker.
    _HOLDBACK = len("diff --git a/x b/x")
    # The abort sentinel for a second codepilot.py action script in one generation.
    # Only codepilot.py diffs are restricted; multi-file workspace diffs are fine.
    _CODEPILOT_DIFF_HEADER = "diff --git a/codepilot.py b/codepilot.py"

    async def _stream_inference(
        self, system_prompt: Union[str, SystemPromptParts], messages: List[Dict] = None, force_thinking: bool = False
    ) -> str:
        """
        Stream the LLM response token by token — 3-state machine:

          'streaming'   — accumulate text; emit to the user in real time.
                          Watch for ```diff fence (primary) or bare diff --git
                          header (fallback) to transition to 'buffering'.
                          Also watches for <thinking> open tag.
          'thinking'    — inside <thinking>...</thinking>. Emit chunks to
                          THINKING_STREAM only. Never forward to STREAM.
                          Never include in the returned response text.
          'buffering'   — diff content detected: accumulate silently until
                          generation finishes OR a second codepilot.py diff
                          header is detected (early-abort fires, stream is
                          clipped at the last clean closing-fence boundary).

        Detection order (streaming → buffering transition):
          1. ```diff\n  — PRIMARY. The opening fence triggers buffering before
             the diff --git line arrives, so the fence itself is never leaked
             to the user stream as raw Markdown text.
          2. diff --git a/... b/... — FALLBACK. When the model emits a diff
             without a ```diff fence wrapper, the bare header triggers buffering
             so the parser can still find and apply the operation.

        Early-abort (second codepilot.py diff):
          Only a second `diff --git a/codepilot.py b/codepilot.py` triggers
          the abort — not every second diff --git, because emitting multiple
          independent workspace-file diffs in one step is intentional and valid.
          When the abort fires we clip the accumulated buffer at
          last_clean_clip_pos (the char position right after the closing ```
          of the last fully-completed diff block) so history always contains
          a syntactically complete response. The model never sees the cut.

        Returns the complete response text (thinking stripped) for pipeline processing.
        """
        msgs = messages if messages is not None else self.messages
        chunks:            list = []   # full response chunks (thinking stripped)
        pre_fence_emitted: int  = 0
        state:             str  = "streaming"

        # Holdback buffer for partial tag/fence detection
        holdback: str = ""

        # --- Second-block abort tracking (active only in 'buffering' state) ---
        # Tracks the position in the joined chunks string right after the last
        # closing ``` fence we have seen. Updated every time a line that is
        # exactly ``` (a closing fence) is completed inside 'buffering'.
        last_clean_clip_pos: int = 0
        # Sentinel string for the codepilot.py action-script diff header.
        # Only a SECOND occurrence of this exact header triggers the abort;
        # the first occurrence is what triggered buffering.
        _CODEPILOT_HDR     = self._CODEPILOT_DIFF_HEADER  # "diff --git a/codepilot.py b/codepilot.py"
        _SECOND_HDR_HOLDBACK = len(_CODEPILOT_HDR)  # safe look-ahead window
        # Whether the first codepilot.py diff has been seen yet.
        _first_codepilot_seen: bool = False
        # Position in accumulated past which we scan for the second header.
        _first_ctrl_end: int = 0

        def _second_codepilot_pos(buf: str, search_from: int) -> int:
            """Return start-index of the second codepilot.py diff header, or -1."""
            return buf.find(_CODEPILOT_HDR, search_from)

        # Whether buffering was triggered by a ```diff fence (vs. bare header fallback).
        # Tracked so we know what to look for as the primary detector.
        _fence_triggered: bool = False
        # The ```diff opening string for primary detection.
        _DIFF_FENCE = "```diff\n"

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
                                if self._stream and ctrl_pos > pre_fence_emitted:
                                    self.hooks.emit(EventType.STREAM,
                                                    text=accumulated_so_far[pre_fence_emitted:ctrl_pos])
                                state = "buffering"
                                chunks.append(pre_think)
                                pre_fence_emitted = len("".join(chunks))
                            else:
                                chunks.append(pre_think)
                                accumulated_so_far = "".join(chunks)
                                safe_end = len(accumulated_so_far) - self._HOLDBACK
                                if self._stream and safe_end > pre_fence_emitted:
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
                        accumulated = "".join(chunks)

                        # -------------------------------------------------------- #
                        # Second codepilot.py diff early-abort detection            #
                        #                                                            #
                        # Scan the safe window for a second occurrence of the       #
                        # codepilot.py diff header. Multiple workspace-file diffs   #
                        # in one step are ALLOWED; only a second codepilot.py       #
                        # action-script diff is forbidden (model hasn't seen the    #
                        # first one's execution result yet).                        #
                        # -------------------------------------------------------- #
                        safe_scan_end = len(accumulated) - _SECOND_HDR_HOLDBACK
                        if _first_codepilot_seen and safe_scan_end > _first_ctrl_end:
                            second_pos = _second_codepilot_pos(accumulated, _first_ctrl_end)
                            if second_pos != -1 and second_pos < safe_scan_end:
                                # Second codepilot.py diff detected — abort.
                                # Clip at the last clean fence boundary we recorded.
                                if last_clean_clip_pos > 0:
                                    clip_str = accumulated[:last_clean_clip_pos]
                                    chunks = [clip_str]
                                holdback = "\x00ABORT\x00"
                                break

                        # -------------------------------------------------------- #
                        # Mark first codepilot.py diff as seen (once)              #
                        # -------------------------------------------------------- #
                        if not _first_codepilot_seen:
                            cp_pos = accumulated.find(_CODEPILOT_HDR)
                            if cp_pos != -1:
                                _first_codepilot_seen = True
                                # Start scanning for the second one after this header
                                _first_ctrl_end = cp_pos + len(_CODEPILOT_HDR)

                        # -------------------------------------------------------- #
                        # Track last clean clip position                            #
                        #                                                            #
                        # A "clean clip point" is right after a closing ``` line.  #
                        # -------------------------------------------------------- #
                        for close_m in self._CLOSE_FENCE_RE.finditer(accumulated):
                            candidate = close_m.end()
                            if candidate > last_clean_clip_pos:
                                last_clean_clip_pos = candidate

                        break

                    # state == "streaming" — check for buffering trigger.
                    chunks.append(to_process)
                    accumulated = "".join(chunks)

                    # -------------------------------------------------------- #
                    # Primary trigger: ```diff\n fence                         #
                    # Buffering starts at the backtick, so the fence itself    #
                    # is never emitted to the user stream as raw text.         #
                    # -------------------------------------------------------- #
                    fence_m = self._DIFF_FENCE_OPEN_RE.search(accumulated)
                    if fence_m:
                        # Emit everything BEFORE the opening fence.
                        ctrl_pos = fence_m.start()
                        if self._stream and ctrl_pos > pre_fence_emitted:
                            self.hooks.emit(EventType.STREAM,
                                            text=accumulated[pre_fence_emitted:ctrl_pos])
                        state = "buffering"
                        _fence_triggered = True
                        # Scanner starts after the fence open; codepilot.py header
                        # will be detected inside the buffered block below.
                        _first_ctrl_end = fence_m.end()
                    else:
                        # ---------------------------------------------------- #
                        # Fallback trigger: bare diff --git header (no fence)   #
                        # ---------------------------------------------------- #
                        hdr_m = self._DIFF_HEADER_RE.search(accumulated)
                        if hdr_m:
                            ctrl_pos = hdr_m.start()
                            if self._stream and ctrl_pos > pre_fence_emitted:
                                self.hooks.emit(EventType.STREAM,
                                                text=accumulated[pre_fence_emitted:ctrl_pos])
                            state = "buffering"
                            _first_ctrl_end = hdr_m.end()
                            # If this bare header IS the codepilot.py header, mark it seen.
                            if _CODEPILOT_HDR in accumulated[hdr_m.start():hdr_m.end() + 1]:
                                _first_codepilot_seen = True
                                _first_ctrl_end = hdr_m.start() + len(_CODEPILOT_HDR)
                        else:
                            safe_end = len(accumulated) - self._HOLDBACK
                            if self._stream and safe_end > pre_fence_emitted:
                                self.hooks.emit(EventType.STREAM,
                                                text=accumulated[pre_fence_emitted:safe_end])
                                pre_fence_emitted = safe_end
                    break

            # ------------------------------------------------------------------ #
            # Check if the inner loop requested a stream abort                   #
            # ------------------------------------------------------------------ #
            if holdback == "\x00ABORT\x00":
                holdback = ""
                break

        # ------------------------------------------------------------------ #
        # Flush holdback remainder                                            #
        # ------------------------------------------------------------------ #
        if holdback and state != "thinking" and holdback != "\x00ABORT\x00":
            chunks.append(holdback)

        # ------------------------------------------------------------------ #
        # Final flush — only needed for pure conversational responses.       #
        # ------------------------------------------------------------------ #
        accumulated = "".join(chunks)

        if state == "streaming":
            # No codepilot block at all — pure conversation, flush everything.
            remaining = accumulated[pre_fence_emitted:]
            if self._stream and remaining:
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
    def _format_diff_error(error: str) -> str:
        """
        Return a terse, failure-class-aware correction for a DiffProtocolError
        raised by parse_operations().

        The model's response has already been stored in history.  This message
        appears as [EXECUTION RESULT] so the model sees exactly what it did wrong
        and what the minimum fix is.  Every word here costs a token on the retry.
        """
        e = error.lower()

        # --- No @@ hunk marker at all --- #
        if "no @@ hunk marker" in e:
            return (
                f"DIFF PARSE ERROR: {error}\n"
                "Every diff block needs at least one `@@ ... @@` line between "
                "the `--- / +++` headers and the hunk body. "
                "The @@ numbers are ignored; any value is accepted."
            )

        # --- Malformed hunk line (bad prefix) --- #
        if "malformed hunk line" in e:
            return (
                f"DIFF PARSE ERROR: {error}\n"
                "Every hunk body line must start with exactly one character: "
                "space (context), `+` (addition), or `-` (deletion). "
                "No other prefixes are valid."
            )

        # --- Missing or bad target path --- #
        if "missing a writable" in e or "missing" in e:
            return (
                f"DIFF PARSE ERROR: {error}\n"
                "The `+++ b/<path>` line is required and must contain a real workspace path. "
                "For file creation use `--- /dev/null`. For codepilot.py always use "
                "`--- /dev/null` and `+++ b/codepilot.py`."
            )

        # --- Deletion diff (not supported) --- #
        if "deletion diff" in e or "not supported" in e:
            return (
                f"DIFF PARSE ERROR: {error}\n"
                "File deletion via `+++ /dev/null` is not supported. "
                "To remove a file call `execute(\"main\", \"rm <path>\")` from codepilot.py."
            )

        # --- Creation diff has non-add lines --- #
        if "creation diff" in e and "only +" in e:
            return (
                f"DIFF PARSE ERROR: {error}\n"
                "A creation diff (`--- /dev/null`) must contain only `+` lines — "
                "no context (space) or deletion (`-`) lines."
            )

        # --- Empty hunk --- #
        if "empty hunk" in e:
            return (
                f"DIFF PARSE ERROR: {error}\n"
                "The hunk body between `@@` markers is empty. "
                "Include at least one `+` or `-` line."
            )

        # --- Generic fallback --- #
        return (
            f"DIFF PARSE ERROR: {error}\n"
            "Emit each file change as a self-contained `diff --git a/<p> b/<p>` block "
            "with `---`, `+++`, `@@`, and hunk lines prefixed by space/+/-. "
            "For codepilot.py use `--- /dev/null` and only `+` lines."
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
        origin = f"generated codepilot.py runtime script, line {line_no}"
        exception_name = type(exc).__name__
        exception_message = str(exc) or "(no exception message)"

        if ran_any_statements:
            semantics = (
                "Execution semantics:\n"
                "- Python started executing the runtime script.\n"
                "- Statements before the failing line may already have run and may have produced tool results or side effects.\n"
                "- The failing statement did not complete, and statements after it did not run.\n"
                "- Tool results printed before this error are still ground truth; inspect them before retrying."
            )
        else:
            semantics = (
                "Execution semantics:\n"
                "- Python could not compile the runtime script.\n"
            "- No statements in the runtime script ran.\n"
                "- No tool calls or file/terminal side effects came from this failed runtime script."
            )

        return (
            "EXECUTION ERROR: The assistant's codepilot.py runtime script raised a Python exception.\n\n"
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
