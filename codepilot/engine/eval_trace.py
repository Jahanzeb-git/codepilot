"""
File: eval_trace.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-08-30

Description:
    Structured telemetry collector for the CodePilot Eval Suite.

    EvalTracer subscribes to the existing HookSystem event bus and builds a
    rich, structured EvalTrace JSON after each agent run. It is entirely
    non-invasive: zero changes to runtime.py are required.

    The EvalTrace is the primary source of truth for:
      - Programmatic pass/fail gating (step count, error counts).
      - Fault attribution (RUNTIME_FAULT / MODEL_FAULT / INDUCED_HALLUCINATION).
      - Baseline regression detection in CI.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from ..engine.hooks import EventType, HookSystem


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class StepRecord:
    """Telemetry captured for a single agentic step."""
    step: int
    blocks_emitted: int = 0
    parse_errors: int = 0        # ConflictProtocolError / ParseError count
    tool_errors: int = 0         # OS errors, permission denials, rejected blocks
    protocol_violations: int = 0 # Markdown fences detected around conflict blocks
    used_retry_block: bool = False
    generation_hash: str = ""    # SHA-256 prefix of raw LLM generation


@dataclass
class EvalTrace:
    """
    Complete structured record of a single eval task run.

    Verdict Values
    --------------
    PASS                  All programmatic assertions passed.
    RUNTIME_FAULT         A runtime component (parser, tool executor) failed.
    MODEL_FAULT           Model made a wrong decision; runtime behaved correctly.
    INDUCED_HALLUCINATION Runtime feedback confused the model into bad decisions.
    UNKNOWN               Requires LLM judge to disambiguate.
    PENDING               Not yet evaluated.
    """
    task_id: str
    model: str
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    total_steps: int = 0
    total_parse_errors: int = 0
    total_tool_errors: int = 0
    total_protocol_violations: int = 0
    finished_cleanly: bool = False
    hit_max_steps: bool = False
    steps: List[StepRecord] = field(default_factory=list)
    verdict: str = "PENDING"
    verdict_reason: str = ""
    judge_output: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# ---------------------------------------------------------------------------
# Tracer
# ---------------------------------------------------------------------------

class EvalTracer:
    """
    Non-invasive telemetry collector that hooks into the HookSystem event bus.

    Usage
    -----
        tracer = EvalTracer(task_id="T_F01", model="qwen3-8b")
        tracer.attach(runtime.hooks)
        await runtime.run(prompt)
        trace = tracer.finalize()
    """

    def __init__(self, task_id: str, model: str):
        self._task_id = task_id
        self._model = model
        self._trace = EvalTrace(task_id=task_id, model=model)
        self._current_step: Optional[StepRecord] = None

    def attach(self, hooks: HookSystem) -> None:
        """Register all event listeners on the provided HookSystem."""
        hooks.register(EventType.STEP,         self._on_step)
        hooks.register(EventType.RUNTIME_ERROR, self._on_runtime_error)
        hooks.register(EventType.TOOL_RESULT,   self._on_tool_result)
        hooks.register(EventType.FINISH,        self._on_finish)
        hooks.register(EventType.MAX_STEPS,     self._on_max_steps)
        hooks.register(EventType.LLM_RESPONSE,  self._on_llm_response)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_step(self, step: int, **_) -> None:
        if self._current_step is not None:
            self._trace.steps.append(self._current_step)
        self._current_step = StepRecord(step=step)
        self._trace.total_steps = step

    def _on_runtime_error(self, error: str, **_) -> None:
        if self._current_step is None:
            return
        error_lower = error.lower()
        if any(k in error_lower for k in ("search", "replace", "marker", "block", "parse")):
            self._current_step.parse_errors += 1
            self._trace.total_parse_errors += 1
        else:
            self._current_step.tool_errors += 1
            self._trace.total_tool_errors += 1

    def _on_tool_result(self, tool: str, result: str, **_) -> None:
        if self._current_step is None:
            return
        result_lower = result.lower()
        if tool == "block":
            self._current_step.blocks_emitted += 1
            if any(k in result_lower for k in ("rejected", "os error", "unexpected error")):
                self._current_step.tool_errors += 1
                self._trace.total_tool_errors += 1
            if "retry_block" in result_lower:
                self._current_step.used_retry_block = True

    def _on_llm_response(self, step: int, response: str, **_) -> None:
        if self._current_step is None:
            return
        self._current_step.generation_hash = hashlib.sha256(
            response.encode("utf-8", errors="replace")
        ).hexdigest()[:16]

        # Protocol violation: markdown fence within 5 lines before a SEARCH marker
        if "<<<<<<< SEARCH" in response:
            lines = response.splitlines()
            for i, line in enumerate(lines):
                if "<<<<<<< SEARCH" in line:
                    window = lines[max(0, i - 5): i]
                    if any(ln.strip().startswith("```") for ln in window):
                        self._current_step.protocol_violations += 1
                        self._trace.total_protocol_violations += 1
                        break

    def _on_finish(self, **_) -> None:
        self._trace.finished_cleanly = True
        self._trace.finished_at = time.time()

    def _on_max_steps(self, **_) -> None:
        self._trace.hit_max_steps = True
        self._trace.finished_at = time.time()

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    def finalize(self) -> EvalTrace:
        """Commit the last in-progress step and return the complete EvalTrace."""
        if self._current_step is not None:
            self._trace.steps.append(self._current_step)
            self._current_step = None
        if self._trace.finished_at == 0.0:
            self._trace.finished_at = time.time()
        self._trace.verdict = self._compute_preliminary_verdict()
        return self._trace

    def _compute_preliminary_verdict(self) -> str:
        """
        Deterministic preliminary verdict from the trace telemetry.

        Protocol violations (markdown fences) are intentionally excluded from
        verdict logic. The runtime's 100-char holdback buffer already strips
        them before they reach the user, so they cause zero functional breakage.
        They are recorded in total_protocol_violations as a SOFT METRIC for
        trend analysis — not as a hard pass/fail gate.
        """
        t = self._trace
        if t.total_parse_errors > 0 and not t.finished_cleanly:
            # Parse errors that prevented completion are runtime faults
            return "RUNTIME_FAULT"
        if t.hit_max_steps or not t.finished_cleanly:
            # Could be model inefficiency OR runtime-induced loop — needs judge
            return "UNKNOWN"
        return "PASS"

