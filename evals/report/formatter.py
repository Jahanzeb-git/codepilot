"""
formatter.py — CLI and GitHub Actions report formatter for the eval harness.

Produces:
  - A color-coded terminal table after each run.
  - A GitHub Actions step summary (Markdown) written to GITHUB_STEP_SUMMARY.
  - A machine-readable JSON report saved to evals/report/latest_run.json.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List


# ANSI colours (stripped automatically in CI environments without TTY)
_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_CYAN   = "\033[96m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"

_VERDICT_COLOUR = {
    "PASS":                  _GREEN,
    "RUNTIME_FAULT":         _RED,
    "MODEL_FAULT":           _YELLOW,
    "INDUCED_HALLUCINATION": _RED,
    "UNKNOWN":               _YELLOW,
    "PENDING":               _CYAN,
}

_VERDICT_EMOJI = {
    "PASS":                  "✅",
    "RUNTIME_FAULT":         "🔴",
    "MODEL_FAULT":           "🟡",
    "INDUCED_HALLUCINATION": "🔴",
    "UNKNOWN":               "🟠",
    "PENDING":               "⬜",
}


def print_report(results: List[dict], baseline: dict) -> bool:
    """
    Print a formatted eval report to stdout.
    Returns True if all tasks passed (for CI exit code).
    """
    passed  = [r for r in results if r["verdict"] == "PASS"]
    failed  = [r for r in results if r["verdict"] != "PASS"]
    all_pass = len(failed) == 0

    print(f"\n{_BOLD}{'─' * 72}{_RESET}")
    print(f"{_BOLD}  CodePilot Eval Suite — Results{_RESET}")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Model: {baseline.get('_pinned_model', 'unknown')}")
    print(f"{'─' * 72}{_RESET}")

    col = "{:<38} {:>6} {:>7} {:>7}  {}"
    print(col.format("TASK", "STEPS", "T_ERR", "P_ERR", "VERDICT"))
    print("─" * 72)

    for r in results:
        v  = r.get("verdict", "PENDING")
        c  = _VERDICT_COLOUR.get(v, "")
        em = _VERDICT_EMOJI.get(v, "")
        regression_flag = " ⚠️ REGRESSION" if r.get("regression") else ""
        print(col.format(
            r["task_id"][:38],
            r.get("total_steps", "?"),
            r.get("total_tool_errors", "?"),
            r.get("total_parse_errors", "?"),
            f"{c}{em} {v}{_RESET}{regression_flag}",
        ))

    print("─" * 72)
    total = len(results)
    summary_colour = _GREEN if all_pass else _RED
    print(f"\n{summary_colour}{_BOLD}  {len(passed)}/{total} PASSED{_RESET}")
    if failed:
        print(f"{_RED}  Failed: {', '.join(r['task_id'] for r in failed)}{_RESET}")
    print()

    # Write GitHub Actions step summary
    _write_gha_summary(results, baseline, passed, failed)

    # Write machine-readable JSON
    _write_json_report(results, baseline)

    return all_pass


def _write_gha_summary(results, baseline, passed, failed):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    lines = [
        "## CodePilot Eval Suite Results",
        f"**Model**: `{baseline.get('_pinned_model', 'unknown')}`  ",
        f"**Time**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
        f"**{len(passed)}/{len(results)} tasks passed**",
        "",
        "| Task | Steps | ToolErr | ParseErr | Verdict |",
        "|------|------:|--------:|---------:|---------|",
    ]
    for r in results:
        v  = r.get("verdict", "PENDING")
        em = _VERDICT_EMOJI.get(v, "")
        reg = " ⚠️" if r.get("regression") else ""
        lines.append(
            f"| `{r['task_id']}` | {r.get('total_steps','?')} | "
            f"{r.get('total_tool_errors','?')} | {r.get('total_parse_errors','?')} | "
            f"{em} {v}{reg} |"
        )
    if failed:
        lines += ["", "### Failed Tasks"]
        for r in failed:
            lines.append(f"- **{r['task_id']}**: {r.get('verdict_reason', r.get('judge_output', ''))}")

    Path(summary_path).write_text("\n".join(lines), encoding="utf-8")


def _write_json_report(results, baseline):
    report_dir = Path(__file__).parent
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pinned_model":  baseline.get("_pinned_model"),
        "total":         len(results),
        "passed":        sum(1 for r in results if r["verdict"] == "PASS"),
        "results":       results,
    }
    (report_dir / "latest_run.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
