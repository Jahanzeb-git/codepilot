"""
Regression tests for abort + queued-message handling.

A tool call (notably MCP) can block longer than the user is willing to wait.
Abort must return promptly, the run must stop and persist, and a message the
user sent during the aborted turn must run next instead of staying queued
forever.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from codepilot.engine.runtime import AsyncRuntime  # noqa: E402
from codepilot.tools.mcp_tool import MCPTools  # noqa: E402


def test_abort_flag_and_visible_queue_drain():
    runtime = AsyncRuntime.__new__(AsyncRuntime)
    runtime._abort = False
    runtime._message_queue = queue.Queue()

    assert runtime.is_aborted() is False
    runtime.abort()
    assert runtime.is_aborted() is True

    runtime._message_queue.put(("what's done so far?", False))
    runtime._message_queue.put(("terminal environment noise", True))
    assert runtime._pop_queued_messages() == ["what's done so far?"]
    assert runtime._message_queue.empty()


class _Aborted:
    hooks = SimpleNamespace(emit=lambda *a, **k: None)

    def is_aborted(self) -> bool:
        return True

    def _append_execution(self, _text: str) -> None:
        pass


class _NotAborted(_Aborted):
    def is_aborted(self) -> bool:
        return False


def _mcp_with(runtime, future, monkeypatch):
    mcp = MCPTools.__new__(MCPTools)
    mcp._ready = True
    mcp._main_loop = object()
    mcp._runtime = runtime

    def fake_submit(coro, loop):
        coro.close()  # never scheduled; avoid a "never awaited" warning
        return future

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", fake_submit)
    return mcp


def test_mcp_abort_returns_without_waiting_for_the_120s_timeout(monkeypatch):
    future = concurrent.futures.Future()
    mcp = _mcp_with(_Aborted(), future, monkeypatch)

    started = time.monotonic()
    result = mcp.mcp(query="some tool")
    elapsed = time.monotonic() - started

    assert "Aborted" in result
    assert elapsed < 2.0
    assert future.cancelled()


def test_mcp_completed_future_still_returns_normally(monkeypatch):
    future = concurrent.futures.Future()
    future.set_result("tool result")
    mcp = _mcp_with(_NotAborted(), future, monkeypatch)

    assert mcp.mcp(query="some tool") == "tool result"
