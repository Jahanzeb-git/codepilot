"""
File: watcher.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description:
Snapshot-based workspace change detector for the CodePilot runtime.

Architectural Notes:
Tracks only files the agent has explicitly interacted with (read or written).
Before each inference step the runtime calls check() to detect external
modifications caused by the human operator or other tools. Changed files
are reported with precise line-number ranges (via difflib) so the agent
knows exactly which parts of its mental model are stale and need re-reading.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

import os
import hashlib
import difflib
from datetime import datetime
from typing import Dict, List, Optional, Tuple


def _plural(count: int, noun: str) -> str:
    """Return ``"1 line"`` / ``"2 lines"`` without grammar drift."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


class WorkspaceWatcher:
    """
    Detects external file changes between agent steps.

    Only watches files registered via register().  Uses content hashing to
    detect changes and difflib to identify affected line ranges.

    Typical lifecycle inside the Runtime loop::

        # end of step N
        watcher.snapshot_all()     # baseline

        # user edits files externally ...

        # start of step N+1
        notification = watcher.check()
        if notification:
            inject_into_context(notification)
    """

    def __init__(
        self,
        work_dir: str = ".",
        max_diff_lines_per_file: int = 30,
        max_total_diff_lines: int = 100,
    ):
        self._tracked: Dict[str, _FileState] = {}
        self._work_dir = os.path.abspath(work_dir)
        self._max_per_file = max_diff_lines_per_file
        self._max_total = max_total_diff_lines

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, abs_path: str):
        """Start tracking a file.  Takes a snapshot of its current state."""
        self._tracked[abs_path] = _snapshot_file(abs_path)

    def snapshot_all(self):
        """Refresh snapshots for all tracked files (called after each step)."""
        for abs_path in list(self._tracked):
            self._tracked[abs_path] = _snapshot_file(abs_path)

    def check(self) -> Optional[str]:
        """
        Compare all tracked files against their snapshots.

        Returns a formatted ``[ENVIRONMENT CHANGE]`` notification string,
        or *None* if nothing changed. Modified text files include a bounded
        unified diff so the agent can often skip a full re-read.
        """
        notifications: List[str] = []
        total_diff_lines = 0

        for abs_path, old_state in list(self._tracked.items()):
            current = _snapshot_file(abs_path)

            # Unchanged
            if old_state.content_hash == current.content_hash:
                continue

            rel_path = os.path.relpath(abs_path, self._work_dir)

            # Deleted
            if old_state.exists and not current.exists:
                notifications.append(f"🗑️ Deleted: {rel_path}")
                continue

            # Created (was not there at snapshot time, now it is)
            if not old_state.exists and current.exists:
                detail = (
                    "binary" if current.binary
                    else _plural(current.line_count, "line")
                )
                notifications.append(f"📄 Created: {rel_path} ({detail})")
                continue

            # Binary content cannot be diffed meaningfully.
            if old_state.binary or current.binary:
                notifications.append(
                    f"📝 Modified: {rel_path}\n"
                    f"  (binary file — content not shown)"
                )
                continue

            # Modified — compute changed line ranges
            changed = _changed_line_ranges(old_state.lines, current.lines)
            if not changed:
                continue

            range_strs: List[str] = []
            for start, end in changed[:_MAX_RANGE_LABELS]:
                range_strs.append(f"{start}" if start == end else f"{start}-{end}")
            if len(changed) > _MAX_RANGE_LABELS:
                range_strs.append(f"… {len(changed) - _MAX_RANGE_LABELS} more ranges")

            remaining = max(
                0,
                min(self._max_per_file, self._max_total - total_diff_lines),
            )
            diff = _format_diff(
                old_state.lines, current.lines, rel_path, remaining,
            )

            notification = (
                f"📝 Modified: {rel_path}\n"
                f"  Changed lines: {', '.join(range_strs)}"
            )
            if diff:
                total_diff_lines += len(diff.splitlines())
                notification += "\n" + diff
            elif remaining == 0:
                notification += "\n  … diff omitted (budget exhausted)"

            notifications.append(notification)

        if not notifications:
            return None

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return (
            f"[ENVIRONMENT CHANGE] {timestamp}\n\n"
            + "\n".join(notifications)
        )


# -----------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------

#: Maximum number of changed-line ranges listed per file.
_MAX_RANGE_LABELS = 8
#: Maximum characters per diff line (protects against minified files).
_MAX_DIFF_LINE_CHARS = 300


def _format_diff(
    old_lines: List[str],
    new_lines: List[str],
    rel_path: str,
    max_lines: int,
) -> str:
    """Return an indented, bounded unified diff for one modified text file."""
    if max_lines <= 0:
        return ""

    diff = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            n=2,
            lineterm="",
        )
    )
    if not diff:
        return ""

    rendered: List[str] = []
    for line in diff:
        if len(line) > _MAX_DIFF_LINE_CHARS:
            line = line[:_MAX_DIFF_LINE_CHARS] + "…"
        rendered.append("  " + line)

    if len(rendered) > max_lines:
        hidden = len(rendered) - max_lines
        rendered = rendered[:max_lines]
        rendered.append(f"  … diff truncated ({hidden} more lines)")

    return "\n".join(rendered)

class _FileState:
    """Lightweight snapshot of a single file."""
    __slots__ = ("exists", "content_hash", "line_count", "lines", "binary")

    def __init__(
        self, exists: bool, content_hash: str, line_count: int, lines: List[str],
        binary: bool = False,
    ):
        self.exists = exists
        self.content_hash = content_hash
        self.line_count = line_count
        self.lines = lines
        self.binary = binary


def _snapshot_file(abs_path: str) -> _FileState:
    """Read a file and return its state; returns a 'not exists' state on error."""
    if not os.path.isfile(abs_path):
        return _FileState(exists=False, content_hash="", line_count=0, lines=[])
    try:
        with open(abs_path, "rb") as f:
            raw = f.read()
        h = hashlib.md5(raw, usedforsecurity=False).hexdigest()

        # Binary files cannot be diffed meaningfully. NUL bytes in the first
        # 8 KB are the same heuristic git uses.
        if b"\x00" in raw[:8192]:
            return _FileState(
                exists=True, content_hash=h, line_count=0, lines=[], binary=True,
            )

        content = raw.decode("utf-8", errors="replace")
        lines = content.splitlines()
        return _FileState(exists=True, content_hash=h, line_count=len(lines), lines=lines)
    except OSError:
        return _FileState(exists=False, content_hash="", line_count=0, lines=[])


def _changed_line_ranges(
    old_lines: List[str], new_lines: List[str],
) -> List[Tuple[int, int]]:
    """
    Return (start, end) 1-indexed line ranges that differ in the new file.

    Uses SequenceMatcher to find changed regions efficiently.
    """
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    ranges: List[Tuple[int, int]] = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            start = j1 + 1
            end = max(j1 + 1, j2)
            ranges.append((start, end))
    return ranges
