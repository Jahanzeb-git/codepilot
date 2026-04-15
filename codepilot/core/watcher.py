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
        max_diff_lines_per_file: int = 30,
        max_total_diff_lines: int = 100,
    ):
        self._tracked: Dict[str, _FileState] = {}
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
        or *None* if nothing changed.
        """
        notifications: List[str] = []
        total_lines = 0

        for abs_path, old_state in list(self._tracked.items()):
            current = _snapshot_file(abs_path)

            # Unchanged
            if old_state.content_hash == current.content_hash:
                continue

            basename = os.path.basename(abs_path)

            # Deleted
            if old_state.exists and not current.exists:
                notifications.append(f"🗑️ Deleted: {basename}")
                continue

            # Created (was not there at snapshot time, now it is)
            if not old_state.exists and current.exists:
                notifications.append(
                    f"📄 Created: {basename} ({current.line_count} lines)"
                )
                continue

            # Modified — compute changed line ranges
            changed = _changed_line_ranges(old_state.lines, current.lines)
            if not changed:
                continue

            remaining_budget = min(
                self._max_per_file,
                self._max_total - total_lines,
            )
            range_strs: List[str] = []
            lines_reported = 0

            for start, end in changed:
                span = end - start + 1
                if lines_reported + span > remaining_budget:
                    leftover = sum(e - s + 1 for s, e in changed) - lines_reported
                    range_strs.append(f"(… {leftover} more changed lines)")
                    break
                range_strs.append(f"{start}" if start == end else f"{start}-{end}")
                lines_reported += span

            total_lines += lines_reported
            notifications.append(
                f"📝 Modified: {basename}\n"
                f"  Changed lines: {', '.join(range_strs)}"
            )

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

class _FileState:
    """Lightweight snapshot of a single file."""
    __slots__ = ("exists", "content_hash", "line_count", "lines")

    def __init__(
        self, exists: bool, content_hash: str, line_count: int, lines: List[str],
    ):
        self.exists = exists
        self.content_hash = content_hash
        self.line_count = line_count
        self.lines = lines


def _snapshot_file(abs_path: str) -> _FileState:
    """Read a file and return its state; returns a 'not exists' state on error."""
    if not os.path.isfile(abs_path):
        return _FileState(exists=False, content_hash="", line_count=0, lines=[])
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        lines = content.splitlines()
        h = hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()
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
