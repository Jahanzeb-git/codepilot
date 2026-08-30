#!/usr/bin/env python3
"""
harness.py — CodePilot Eval Suite Orchestrator.

Usage
-----
  # Run full suite against baseline (CI mode)
  python evals/harness.py --baseline evals/baseline.json --fail-on-regression

  # Run a single task (development mode)
  python evals/harness.py --task T_F01_file_create_single

  # Dry run: validate all task YAMLs without calling the LLM
  python evals/harness.py --dry-run

Architecture
------------
1. Load all task YAMLs from evals/tasks/.
2. For each task: spin up a TempWorkspace, attach EvalTracer to the runtime,
   run the agent headlessly, then run the programmatic verifier.
3. If verdict is UNKNOWN and use_llm_judge is True, call fault_attributor.
4. Compare results against baseline.json.
5. Print report and exit 0 (all pass) or 1 (regression detected).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Ensure the repo root is on sys.path so codepilot can be imported
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from codepilot.engine.runtime import AsyncRuntime
from codepilot.engine.eval_trace import EvalTracer
from codepilot.engine.hooks import EventType

# Eval-local imports
_EVALS_DIR = Path(__file__).parent
sys.path.insert(0, str(_EVALS_DIR))

from judge.fault_attributor import attribute_fault
from report.formatter import print_report

_AGENT_FILE = str(_EVALS_DIR / "agent.yaml")
_TASKS_DIR  = _EVALS_DIR / "tasks"


# ---------------------------------------------------------------------------
# Task spec loader
# ---------------------------------------------------------------------------

def load_tasks(task_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """Load all task YAML files from evals/tasks/. Optionally filter by id."""
    tasks = []
    for path in sorted(_TASKS_DIR.glob("*.yaml")):
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        if task_filter and spec.get("id") != task_filter:
            continue
        tasks.append(spec)
    return tasks


def load_baseline(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Workspace management
# ---------------------------------------------------------------------------

class TempWorkspace:
    """Context manager that creates/tears down an isolated temp directory."""

    def __init__(self, setup: dict):
        self._setup = setup
        self._path: Optional[Path] = None

    def __enter__(self) -> Path:
        self._path = Path(tempfile.mkdtemp(prefix="codepilot_eval_"))
        for file_def in self._setup.get("files", []):
            target = self._path / file_def["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(file_def["content"], encoding="utf-8")
        return self._path

    def __exit__(self, *_):
        if self._path and self._path.exists():
            shutil.rmtree(self._path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Verifier loader
# ---------------------------------------------------------------------------

def load_verifier(verifier_spec: Optional[str], task_spec: dict):
    """Dynamically import and instantiate a verifier class from its dotted path."""
    if not verifier_spec:
        return None

    module_name, class_name = verifier_spec.rsplit(".", 1)
    full_module = f"verifiers.{module_name}"
    mod = importlib.import_module(full_module)
    cls = getattr(mod, class_name)

    # Some verifiers accept constructor args from the task spec
    if "server_port" in task_spec.get("assertions", {}):
        return cls(port=task_spec["assertions"]["server_port"])
    return cls()


# ---------------------------------------------------------------------------
# Baseline comparison
# ---------------------------------------------------------------------------

def compare_against_baseline(result: dict, baseline_task: dict) -> bool:
    """
    Check result metrics against baseline thresholds.
    Returns True if within bounds (pass), False if regression.
    """
    regressions = []
    if result["total_steps"] > baseline_task.get("max_steps", 999):
        regressions.append(
            f"steps {result['total_steps']} > baseline {baseline_task['max_steps']}"
        )
    if result["total_tool_errors"] > baseline_task.get("max_tool_errors", 999):
        regressions.append(
            f"tool_errors {result['total_tool_errors']} > baseline {baseline_task['max_tool_errors']}"
        )
    if result["total_parse_errors"] > baseline_task.get("max_parse_errors", 999):
        regressions.append(
            f"parse_errors {result['total_parse_errors']} > baseline {baseline_task['max_parse_errors']}"
        )
    if baseline_task.get("must_finish_cleanly") and not result["finished_cleanly"]:
        regressions.append("must_finish_cleanly violated")
    if baseline_task.get("max_protocol_violations") is not None:
        if result["total_protocol_violations"] > baseline_task["max_protocol_violations"]:
            regressions.append(
                f"protocol_violations {result['total_protocol_violations']} > "
                f"baseline {baseline_task['max_protocol_violations']}"
            )

    result["regression"] = bool(regressions)
    if regressions:
        result["verdict_reason"] = "REGRESSION: " + "; ".join(regressions)
    return not bool(regressions)


# ---------------------------------------------------------------------------
# Core task runner
# ---------------------------------------------------------------------------

async def run_task(task_spec: dict, dry_run: bool = False) -> dict:
    """Run a single eval task. Returns a result dict."""
    task_id = task_spec["id"]
    print(f"  ▶ {task_id}", end="", flush=True)

    if dry_run:
        print(f" [DRY RUN — skipped]")
        return {
            "task_id": task_id,
            "verdict": "PENDING",
            "verdict_reason": "dry-run",
            "total_steps": 0,
            "total_tool_errors": 0,
            "total_parse_errors": 0,
            "total_protocol_violations": 0,
            "finished_cleanly": False,
            "regression": False,
        }

    assertions = task_spec.get("assertions", {})
    setup      = task_spec.get("setup", {"files": []})
    prompt     = task_spec["prompt"]

    with TempWorkspace(setup) as workspace:
        # Override work_dir for this task's runtime
        os.environ["CODEPILOT_EVAL_WORKDIR"] = str(workspace)

        # Monkeypatch the agent file's work_dir at load time
        runtime = AsyncRuntime(
            agent_file=_AGENT_FILE,
            session="memory",
            stream=False,  # suppress streaming output during evals
        )
        # Override work_dir to point at the isolated temp workspace
        import codepilot.core.agent_file as _af_module
        object.__setattr__(runtime.config.runtime, "work_dir", str(workspace))
        runtime.context_manager = runtime.context_manager.__class__(str(workspace))

        # Suppress all default hook output (we want clean harness stdout)
        from codepilot.engine.hooks import EventType as _ET
        for _ev in (_ET.STREAM, _ET.THINKING_STREAM, _ET.TOOL_CALL, _ET.TOOL_RESULT,
                    _ET.FINISH, _ET.MAX_STEPS, _ET.RUNTIME_ERROR, _ET.SECURITY_ERROR):
            runtime.hooks.clear(_ev)


        # Attach tracer
        model_name = runtime.config.model.name
        tracer = EvalTracer(task_id=task_id, model=model_name)
        tracer.attach(runtime.hooks)

        # Attach raw transcript logger for debugging
        from transcript import TranscriptLogger
        report_dir = _EVALS_DIR / "report"
        t_logger = TranscriptLogger(task_id=task_id, report_dir=report_dir)
        t_logger.attach(runtime.hooks)

        # Run the agent
        try:
            await runtime.run(prompt)
        except Exception as exc:
            print(f" [RUNTIME EXCEPTION: {exc}]")
            trace = tracer.finalize()
            trace.verdict = "RUNTIME_FAULT"
            trace.verdict_reason = f"Unhandled runtime exception: {exc}"
        else:
            trace = tracer.finalize()

        # ── Programmatic assertions ─────────────────────────────────────
        files_ok = True
        for f in assertions.get("files_must_exist", []):
            if not (workspace / f).exists():
                trace.verdict = "MODEL_FAULT"
                trace.verdict_reason = f"Required file not created: {f}"
                files_ok = False
                break

        verifier_spec = assertions.get("verifier")
        if files_ok and verifier_spec and trace.verdict not in ("RUNTIME_FAULT",):
            try:
                verifier = load_verifier(verifier_spec, task_spec)
                v_passed, v_reason = verifier(workspace)
                if not v_passed:
                    trace.verdict = "MODEL_FAULT"
                    trace.verdict_reason = f"Functional verifier failed: {v_reason}"
                elif trace.verdict == "PASS":
                    trace.verdict_reason = f"Verified: {v_reason}"
            except Exception as ve:
                trace.verdict = "UNKNOWN"
                trace.verdict_reason = f"Verifier error: {ve}"

        # ── LLM Judge ──────────────────────────────────────────────────
        if trace.verdict == "UNKNOWN" and task_spec.get("use_llm_judge"):
            judge_result = attribute_fault(
                trace=trace,
                task_id=task_id,
                task_prompt=prompt,
                judge_focus=task_spec.get("judge_focus", ""),
            )
            trace.verdict       = judge_result["verdict"]
            trace.judge_output  = f"[{judge_result['confidence']}] {judge_result['reason']}"
            trace.verdict_reason = trace.judge_output

    result = asdict(trace)
    verdict_display = trace.verdict
    print(f" → {verdict_display}")
    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="CodePilot Eval Harness")
    parser.add_argument("--baseline",          default="evals/baseline.json",
                        help="Path to baseline.json")
    parser.add_argument("--task",              default=None,
                        help="Run a single task by ID")
    parser.add_argument("--dry-run",           action="store_true",
                        help="Validate YAML files without running the agent")
    parser.add_argument("--fail-on-regression",action="store_true",
                        help="Exit with code 1 if any regression is detected")
    parser.add_argument("--concurrency",       type=int, default=1,
                        help="Number of tasks to run in parallel (default: 1, safe default)")
    args = parser.parse_args()

    print("\n🔍 CodePilot Eval Suite")
    print(f"   Baseline: {args.baseline}")
    print(f"   Tasks dir: {_TASKS_DIR}\n")

    # Load
    tasks    = load_tasks(task_filter=args.task)
    baseline = load_baseline(args.baseline)
    baseline_tasks = baseline.get("tasks", {})

    if not tasks:
        print(f"No tasks found (filter: {args.task!r})")
        sys.exit(1)

    print(f"Running {len(tasks)} task(s)...\n")

    # Run tasks (sequential by default; increase --concurrency for parallelism)
    sem = asyncio.Semaphore(args.concurrency)

    async def run_with_sem(task_spec):
        async with sem:
            return await run_task(task_spec, dry_run=args.dry_run)

    results = await asyncio.gather(*[run_with_sem(t) for t in tasks])
    results = list(results)

    # Baseline comparison
    if not args.dry_run:
        for result in results:
            task_baseline = baseline_tasks.get(result["task_id"])
            if task_baseline:
                compare_against_baseline(result, task_baseline)
            else:
                result["regression"] = False
                result.setdefault("verdict_reason", "No baseline entry found")

    # Report
    all_passed = print_report(results, baseline)

    if args.fail_on_regression and not all_passed:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
