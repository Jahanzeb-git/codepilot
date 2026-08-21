"""
File: subagent.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-06-06

Description:
Sub-agent tools for the CodePilot runtime.

Architectural Notes:
- spawn_subagent() creates a new Runtime instance on a daemon thread (same
  process, full isolation via separate event loop and message queues). The main
  agent is NOT blocked — it receives the agent_id immediately and continues.
- await_subagent(agent_id) blocks until the sub-agent finishes (or times out).
- ask_main_agent() is registered only on sub-agent runtimes. It puts a message
  on the main agent's event queue and blocks until a reply is injected.
- FileLockCoordinator serialises writes to the same absolute path across all
  agent threads. Two agents can write to different files in parallel; they
  queue up if they target the same file.
- Depth guard: spawn_subagent() on a depth-1 runtime returns an error string
  instead of recursing. The tool schema is identical (cache preserved).

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

from __future__ import annotations

import queue
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ..engine.runtime import Runtime


# ── Global file-write coordinator ──────────────────────────────────────────────

class FileLockCoordinator:
    """
    Per-path write serialisation across all agent threads.

    Two agents can write to different files simultaneously.
    If both target the same resolved path, the second waits until
    the first has finished writing.
    """

    def __init__(self):
        self._lock = threading.Lock()          # guards _path_locks dict
        self._path_locks: Dict[str, threading.Lock] = {}

    def acquire(self, abs_path: str) -> None:
        with self._lock:
            if abs_path not in self._path_locks:
                self._path_locks[abs_path] = threading.Lock()
        self._path_locks[abs_path].acquire()

    def release(self, abs_path: str) -> None:
        lock = self._path_locks.get(abs_path)
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass  # already released — no-op


# Singleton coordinator shared by all Runtime instances in this process
FILE_LOCK_COORDINATOR = FileLockCoordinator()


# ── Sub-agent handle ────────────────────────────────────────────────────────────

class SubAgentHandle:
    """
    Tracks a single sub-agent: its thread, queues, and status metadata.
    """

    def __init__(self, agent_id: int, task_summary: str):
        self.agent_id: int        = agent_id
        self.task_summary: str    = task_summary
        self.started_at: float    = time.monotonic()

        # Sub-agent → main: questions (ask_main_agent calls)
        self.to_main: queue.Queue   = queue.Queue()
        # Main → sub-agent: answers
        self.from_main: queue.Queue = queue.Queue()

        # Result tracking
        self._done_event: threading.Event = threading.Event()
        self.result_summary: str          = ""
        self.files_written: List[str]     = []
        self.error: Optional[str]         = None

        self._thread: Optional[threading.Thread] = None

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def is_done(self) -> bool:
        return self._done_event.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until done. Returns True if done, False if timed out."""
        return self._done_event.wait(timeout=timeout)

    def mark_done(
        self,
        summary: str = "",
        files_written: Optional[List[str]] = None,
        error: Optional[str] = None,
    ) -> None:
        self.result_summary = summary
        self.files_written  = files_written or []
        self.error          = error
        self._done_event.set()

    def status_line(self) -> str:
        """One-line status for injection into main agent's system prompt."""
        elapsed = int(self.elapsed_seconds)
        files   = ", ".join(self.files_written) if self.files_written else "none"
        task_short = self.task_summary[:60] + ("…" if len(self.task_summary) > 60 else "")
        status  = "done" if self.is_done else "running"
        return (
            f"Agent #{self.agent_id} [{status}] elapsed:{elapsed}s "
            f"task:\"{task_short}\" files:{files}"
        )


# ── Sub-agent manager ───────────────────────────────────────────────────────────

class SubAgentManager:
    """
    Registry of all active and completed sub-agents for a Runtime instance.
    Thread-safe.
    """

    def __init__(self):
        self._lock: threading.Lock = threading.Lock()
        self._agents: Dict[int, SubAgentHandle] = {}
        self._next_id: int = 1

    def create(self, task_summary: str) -> SubAgentHandle:
        with self._lock:
            aid = self._next_id
            self._next_id += 1
            handle = SubAgentHandle(aid, task_summary)
            self._agents[aid] = handle
        return handle

    def get(self, agent_id: int) -> Optional[SubAgentHandle]:
        return self._agents.get(agent_id)

    def active_handles(self) -> List[SubAgentHandle]:
        return [h for h in self._agents.values() if not h.is_done]

    def all_handles(self) -> List[SubAgentHandle]:
        return list(self._agents.values())

    def build_status_block(self) -> str:
        """Build the <sub_agent_status> block for injection into system prompt."""
        handles = self.all_handles()
        if not handles:
            return ""
        lines = [h.status_line() for h in handles]
        return "\n".join(lines)


# ── Tools class ─────────────────────────────────────────────────────────────────

class SubAgentTools:
    """
    Provides spawn_subagent, await_subagent tools for main-agent runtimes,
    and ask_main_agent for sub-agent runtimes.
    """

    def __init__(self, runtime: "Runtime", depth: int = 0):
        self.runtime = runtime
        self.depth   = depth
        self.manager = SubAgentManager()

    # ── spawn_subagent ─────────────────────────────────────────────────────────

    def spawn_subagent(
        self,
        task: str,
        context: str = "",
        tools: Optional[List[str]] = None,
        model: Optional[str] = None,
    ) -> str:
        """
        Spawn an independent worker agent. Returns agent_id immediately.

        Use when: task is self-contained, bounded, generates many tool steps,
        and does not need access to your conversation history.
        INJECT everything into `context` — the worker has no history access.
        Include: relevant file contents, constraints, expected output format.

        Watch for [Sub-Agent #N →] markers in your next turn if the worker
        has a question. Use await_subagent(agent_id) to get the final result.

        Args:
            task:    Clear task description for the worker agent.
            context: All context the worker needs (file contents, constraints).
            tools:   Allowlist of tool names (default: view_file, execute,
                     read_output, send_input, find, ask_user). Workspace changes
                     are always emitted as diffs, not tool calls.
            model:   Override model (default: same as main agent). Use a
                     faster/cheaper variant for focused tasks.

        Returns:
            "agent_id:<N>" on success, or error string if sub-agents disabled.
        """
        # Depth guard — preserves tool schema (cache intact), rejects at runtime
        if self.depth > 0:
            return (
                "Error: Nested sub-agents are not supported. "
                "Complete this task yourself."
            )

        # Check config flag
        sa_config = getattr(self.runtime.config, "sub_agents", None)
        if sa_config is not None and not getattr(sa_config, "enabled", True):
            return "Error: sub_agents.enabled is false in agent.yaml."

        handle = self.manager.create(task_summary=task[:120])

        # Emit hook so CLI can show spawn event
        from ..engine.hooks import EventType
        self.runtime.hooks.emit(
            EventType.SUBAGENT_SPAWN,
            agent_id=handle.agent_id,
            task_summary=handle.task_summary,
        )

        # Launch worker thread
        t = threading.Thread(
            target=self._run_subagent,
            args=(handle, task, context, tools, model),
            daemon=True,
            name=f"codepilot-subagent-{handle.agent_id}",
        )
        handle._thread = t
        t.start()

        return f"agent_id:{handle.agent_id}"

    # ── await_subagent ─────────────────────────────────────────────────────────

    def await_subagent(self, agent_id: int, timeout: int = 600) -> str:
        """
        Wait for a sub-agent to finish and return its result summary.

        Blocks until the sub-agent completes or the timeout (seconds) expires.
        While waiting, the main agent's step loop is paused — only call this
        when you genuinely need the result before proceeding.

        Args:
            agent_id: The agent_id returned by spawn_subagent().
            timeout:  Max seconds to wait (default 600 / 10 min).

        Returns:
            Result summary string, or error/timeout message.
        """
        handle = self.manager.get(agent_id)
        if handle is None:
            return f"Error: No sub-agent with agent_id {agent_id}."

        finished = handle.wait(timeout=float(timeout))
        if not finished:
            return f"Timeout: sub-agent #{agent_id} did not finish within {timeout}s."

        if handle.error:
            return (
                f"Sub-agent #{agent_id} failed after {int(handle.elapsed_seconds)}s.\n"
                f"Error: {handle.error}"
            )

        files = ", ".join(handle.files_written) if handle.files_written else "none"
        return (
            f"Sub-agent #{agent_id} completed in {int(handle.elapsed_seconds)}s.\n"
            f"Files written: {files}\n"
            f"Summary: {handle.result_summary}"
        )

    # ── ask_main_agent (registered on sub-agent runtime only) ──────────────────

    @staticmethod
    def make_ask_main_agent(handle: "SubAgentHandle", main_runtime: "Runtime"):
        """
        Returns a closure that, when called by the sub-agent, blocks until the
        main agent replies. The question is also injected into main context with
        a [Sub-Agent #N →] marker.
        """
        def ask_main_agent(message: str, timeout: int = 300) -> str:
            """
            Send a question to the main agent and wait for its reply.

            Use when you need clarification before continuing. The main agent
            will see your message with a [Sub-Agent #N →] marker. This call
            blocks until the main agent replies or the timeout expires.

            Args:
                message: Your question or status message.
                timeout: Seconds to wait for a reply (default 300 / 5 min).

            Returns:
                Main agent's reply, or timeout message.
            """
            # Put question into main agent's context via send_message
            marker = f"[Sub-Agent #{handle.agent_id} →] {message}"
            main_runtime.send_message(marker)

            # Emit hook for CLI display
            from ..engine.hooks import EventType
            main_runtime.hooks.emit(
                EventType.SUBAGENT_MESSAGE,
                agent_id=handle.agent_id,
                message=message,
            )

            # Also signal to_main queue (for programmatic reply via handle)
            handle.to_main.put(message)

            # Block waiting for a reply
            try:
                reply = handle.from_main.get(timeout=float(timeout))
                return reply
            except queue.Empty:
                return f"Timeout: no reply from main agent within {timeout}s. Proceeding."

        return ask_main_agent

    # ── Internal: sub-agent execution ──────────────────────────────────────────

    def _run_subagent(
        self,
        handle: SubAgentHandle,
        task: str,
        context: str,
        tool_names: Optional[List[str]],
        model_override: Optional[str],
    ) -> None:
        """Runs on a daemon thread. Creates a fresh Runtime, runs the task."""
        from ..engine.runtime import Runtime as SyncRuntime
        from ..engine.hooks import EventType

        try:
            # Build a temporary agent.yaml for the sub-agent
            config_path = self._build_subagent_config(tool_names, model_override)

            # Track files changed by sub-agent diff operations.
            files_tracker: List[str] = []

            sub_rt = SyncRuntime(
                str(config_path),
                session="memory",
                stream=False,
            )

            # Register ask_main_agent tool (sub-agent → main communication)
            ask_fn = self.make_ask_main_agent(handle, self.runtime)
            sub_rt.register_tool("ask_main_agent", ask_fn, replace=False)

            # Track files written by the sub-agent via TOOL_RESULT hook
            @sub_rt.hooks.register  # type: ignore
            def _noop(): pass

            sub_rt.hooks.clear(
                __import__(
                    "codepilot.engine.hooks", fromlist=["EventType"]
                ).EventType.TOOL_RESULT
            )

            def _track_tool_result(tool: str, result: str, **_):
                if tool == "diff" and "ERROR" not in result and "REJECTED" not in result:
                    match = re.search(r"'([^']+)'", result)
                    if match and match.group(1) != "codepilot.py":
                        path = match.group(1)
                        if path not in files_tracker:
                            files_tracker.append(path)

            sub_rt.hooks.register(
                __import__(
                    "codepilot.engine.hooks", fromlist=["EventType"]
                ).EventType.TOOL_RESULT,
                _track_tool_result,
            )

            # Build full task with injected context
            full_task = task
            if context:
                full_task = f"{task}\n\n--- Context provided by main agent ---\n{context}"

            result_summary = sub_rt.run(full_task) or "Task completed."
            handle.mark_done(
                summary=result_summary,
                files_written=files_tracker,
                error=None,
            )

        except Exception as exc:
            handle.mark_done(
                summary="",
                files_written=[],
                error=str(exc),
            )
        finally:
            # Emit finish event on main runtime hooks (thread-safe)
            from ..engine.hooks import EventType
            self.runtime.hooks.emit(
                EventType.SUBAGENT_FINISH,
                agent_id=handle.agent_id,
                summary=handle.result_summary,
                files_written=handle.files_written,
                elapsed_seconds=handle.elapsed_seconds,
                error=handle.error,
            )
            # Clean up temp config
            try:
                if hasattr(self, "_last_subagent_config"):
                    Path(self._last_subagent_config).unlink(missing_ok=True)
            except Exception:
                pass

    def _build_subagent_config(
        self,
        tool_names: Optional[List[str]],
        model_override: Optional[str],
    ) -> Path:
        """Write a temporary agent.yaml for the sub-agent runtime."""
        import yaml

        main_cfg = self.runtime.config
        model_name = model_override or main_cfg.model.name
        provider   = main_cfg.model.provider
        api_key_env = main_cfg.model.api_key_env

        default_tools = [
            "view_file", "execute",
            "read_output", "send_input", "find",
        ]
        enabled_tools = tool_names or default_tools

        tools_yaml = [{"name": t, "enabled": True} for t in enabled_tools]

        data = {
            "agent": {
                "name": f"sub-agent-{id(self)}",
                "role": "A focused worker agent. Complete the assigned task precisely.",
                "system_prompt": (
                    "You are a focused worker agent completing a bounded task. "
                    "Complete it precisely and efficiently. "
                    "Use ask_main_agent() if you need clarification. "
                    "Call task(finish=True) when done with a clear summary."
                ),
                "model": {
                    "provider": provider,
                    "name": model_name,
                    "api_key_env": api_key_env,
                    "temperature": 0.0,
                    "max_tokens": main_cfg.model.max_tokens,
                },
                "runtime": {
                    "work_dir": main_cfg.runtime.work_dir,
                    "max_steps": getattr(
                        getattr(main_cfg, "sub_agents", None), "max_steps", 20
                    ),
                    "unsafe_mode": main_cfg.runtime.unsafe_mode,
                },
                "tools": tools_yaml,
            }
        }

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, prefix="codepilot_subagent_"
        )
        yaml.dump(data, tmp, default_flow_style=False, sort_keys=False)
        tmp.close()
        self._last_subagent_config = tmp.name
        return Path(tmp.name)
