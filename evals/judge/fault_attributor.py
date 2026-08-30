"""
fault_attributor.py — LLM-as-Judge for CodePilot Eval Suite.

Called ONLY when:
  1. task.use_llm_judge == True, AND
  2. The EvalTrace verdict is UNKNOWN or the task has subjective judge_focus.

Uses Alibaba qwen3.8-max as the Oracle Judge Model.
The Oracle is NEVER the same model driving the agent (qwen3.8-flash).
This prevents self-preference bias.

The judge reads the full structured EvalTrace JSON + the task's judge_focus
rubric and returns exactly one verdict:
  PASS / RUNTIME_FAULT / MODEL_FAULT / INDUCED_HALLUCINATION

What the judge SEES:
  - The task ID and original prompt given to the agent.
  - The task-author's judge_focus rubric (what to specifically look for).
  - The full EvalTrace JSON: step-by-step record of what happened including
    total steps, tool errors, parse errors, protocol violations, and whether
    the task finished cleanly.

What the judge does NOT see:
  - The generated code itself (not stored in trace).
  - The raw LLM generation text (only a hash is stored).
  This keeps judge calls cheap and avoids giant prompts.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codepilot.engine.eval_trace import EvalTrace


_SYSTEM_RUBRIC = """\
You are a senior AI systems engineer auditing an autonomous coding agent called CodePilot.
You will receive a structured execution trace (JSON) and a task description.
Your job is to attribute the outcome to exactly ONE of these verdict domains:

PASS
  The agent completed the task correctly. Any extra steps or minor inefficiency
  are acceptable as long as the task finished cleanly and no runtime errors fired.

RUNTIME_FAULT
  The CodePilot runtime itself behaved incorrectly. Examples:
  - A valid SEARCH block was rejected despite matching the file content exactly.
  - The streaming buffer leaked a path line or markdown fence to the user.
  - A tool threw an unhandled exception that was not caused by the model's decision.
  - Error feedback from the runtime was factually wrong, leading the model astray.

MODEL_FAULT
  The language model made a wrong decision despite the runtime working correctly. Examples:
  - Took significantly more steps than necessary when the task was straightforward.
  - Hallucinated tool output instead of waiting for [EXECUTION RESULT].
  - Used a strategy that was provably suboptimal (e.g., sequential guessing instead of binary search).
  - Failed to complete the task despite multiple attempts.

INDUCED_HALLUCINATION
  The runtime gave back a confusing or misleading error message that caused
  the model to enter a bad reasoning loop. The model tried to follow the
  instructions correctly but the runtime UX led it astray.
  This is a runtime feedback quality bug.

IMPORTANT RULES:
- If the task finished cleanly with reasonable step count → PASS, even if not perfectly optimal.
- Only mark MODEL_FAULT if the model's decision was clearly wrong or inefficient.
- Only mark RUNTIME_FAULT if the runtime itself produced incorrect behavior.
- Do NOT penalize the model for taking one or two extra steps to verify work.

Reply with a JSON object ONLY (no surrounding text, no markdown):
{
  "verdict": "<PASS|RUNTIME_FAULT|MODEL_FAULT|INDUCED_HALLUCINATION>",
  "confidence": "<high|medium|low>",
  "reason": "<one concise sentence explaining the attribution>"
}
"""


def attribute_fault(
    trace: "EvalTrace",
    task_id: str,
    task_prompt: str,
    judge_focus: str = "",
    oracle_model: str = "qwen3-235b-a22b",
    oracle_provider: str = "alibaba",
) -> dict:
    """
    Call the Oracle LLM (qwen3.8-max via Alibaba) to attribute fault.

    Parameters
    ----------
    trace           : EvalTrace from the agent run.
    task_id         : Task identifier string.
    task_prompt     : The original prompt sent to the agent.
    judge_focus     : Rubric hint from the task YAML — what to specifically check.
    oracle_model    : Model to use as judge. Must NOT equal the agent model.
    oracle_provider : Provider for the oracle (alibaba).

    Returns
    -------
    dict with keys: verdict, confidence, reason
    """
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        return {
            "verdict": "UNKNOWN",
            "confidence": "low",
            "reason": "DASHSCOPE_API_KEY not set — cannot run LLM judge",
        }

    # Build user message — this is exactly what the judge sees
    user_message = f"""TASK ID: {task_id}

ORIGINAL PROMPT GIVEN TO AGENT:
{task_prompt.strip()}

SPECIFIC EVALUATION FOCUS (pay close attention to this):
{judge_focus.strip() if judge_focus else "General: did the agent complete the task correctly and efficiently?"}

EXECUTION TRACE (structured JSON — the complete record of what happened):
{trace.to_json()}

Based on the trace above and the evaluation focus, return your verdict as JSON."""

    # Use OpenAI-compatible API endpoint for Alibaba DashScope
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        response = client.chat.completions.create(
            model=oracle_model,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_RUBRIC},
                {"role": "user",   "content": user_message},
            ],
        )
        raw = response.choices[0].message.content
        result = json.loads(raw)
        verdict = result.get("verdict", "UNKNOWN")
        if verdict not in {"PASS", "RUNTIME_FAULT", "MODEL_FAULT", "INDUCED_HALLUCINATION"}:
            verdict = "UNKNOWN"
        return {
            "verdict":    verdict,
            "confidence": result.get("confidence", "low"),
            "reason":     result.get("reason", "No reason provided"),
            "raw_judge_input": {          # store what judge saw — for transparency
                "task_id":     task_id,
                "judge_focus": judge_focus,
                "trace_steps": trace.total_steps,
                "trace_errors": trace.total_tool_errors,
            },
        }
    except ImportError:
        return {
            "verdict": "UNKNOWN",
            "confidence": "low",
            "reason": "openai package not installed — pip install openai",
        }
    except Exception as e:
        return {
            "verdict":    "UNKNOWN",
            "confidence": "low",
            "reason":     f"LLM judge call failed: {type(e).__name__}: {e}",
        }
