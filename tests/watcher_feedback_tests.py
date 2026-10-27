"""
Tests for [ENVIRONMENT CHANGE] diffs emitted by WorkspaceWatcher.

Run with:

    python3 -m pytest tests/watcher_feedback_tests.py -q
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from codepilot.core.watcher import WorkspaceWatcher  # noqa: E402


def test_modified_file_includes_unified_diff():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "app.py"
        path.write_text("x = 1\ny = 2\n", encoding="utf-8")

        watcher = WorkspaceWatcher(work_dir=tmp)
        watcher.register(str(path))
        path.write_text("x = 1\ny = 99\nz = 3\n", encoding="utf-8")

        notice = watcher.check()

        assert notice is not None
        assert "📝 Modified: app.py" in notice
        assert "Changed lines: 2-3" in notice
        assert "--- a/app.py" in notice
        assert "-y = 2" in notice
        assert "+y = 99" in notice
        assert "+z = 3" in notice


def test_binary_change_has_no_diff():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "blob.bin"
        path.write_bytes(b"\x00\x01\x02")

        watcher = WorkspaceWatcher(work_dir=tmp)
        watcher.register(str(path))
        path.write_bytes(b"\x00\x01\x03")

        notice = watcher.check()

        assert notice is not None
        assert "📝 Modified: blob.bin" in notice
        assert "binary file" in notice
        assert "--- a/blob.bin" not in notice


def test_diff_is_capped_and_marks_truncation():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "big.txt"
        path.write_text("".join(f"line {i}\n" for i in range(200)), encoding="utf-8")

        watcher = WorkspaceWatcher(work_dir=tmp, max_diff_lines_per_file=20)
        watcher.register(str(path))
        path.write_text("".join(f"changed {i}\n" for i in range(200)), encoding="utf-8")

        notice = watcher.check()

        assert notice is not None
        assert "diff truncated" in notice
        # 20 rendered diff lines + one truncation marker, indented by 2 spaces.
        diff_lines = [
            l for l in notice.splitlines()
            if l.startswith("  ") and not l.startswith("  Changed lines:")
        ]
        assert len(diff_lines) <= 21


def test_created_and_deleted_notifications_still_work():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "new.py"
        watcher = WorkspaceWatcher(work_dir=tmp)
        watcher.register(str(path))  # snapshot: does not exist

        path.write_text("print(1)\n", encoding="utf-8")
        created = watcher.check()
        assert created is not None and "📄 Created: new.py (1 lines)" in created

        watcher.snapshot_all()
        path.unlink()
        deleted = watcher.check()
        assert deleted is not None and "🗑️ Deleted: new.py" in deleted


def test_no_change_returns_none_and_snapshot_resets_baseline():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "stable.py"
        path.write_text("a = 1\n", encoding="utf-8")

        watcher = WorkspaceWatcher(work_dir=tmp)
        watcher.register(str(path))
        assert watcher.check() is None

        path.write_text("a = 2\n", encoding="utf-8")
        assert watcher.check() is not None

        watcher.snapshot_all()
        assert watcher.check() is None


def test_total_diff_budget_is_shared_across_files():
    with tempfile.TemporaryDirectory() as tmp:
        first = Path(tmp) / "one.txt"
        second = Path(tmp) / "two.txt"
        first.write_text("a\nb\n", encoding="utf-8")
        second.write_text("c\nd\n", encoding="utf-8")

        watcher = WorkspaceWatcher(
            work_dir=tmp,
            max_diff_lines_per_file=10,
            max_total_diff_lines=5,
        )
        watcher.register(str(first))
        watcher.register(str(second))

        first.write_text("a\nB\n", encoding="utf-8")
        second.write_text("c\nD\n", encoding="utf-8")

        notice = watcher.check()

        assert notice is not None
        assert "one.txt" in notice and "two.txt" in notice
        # The first file consumes the shared budget; the second is explicit
        # about the omission instead of silently showing nothing.
        assert "budget exhausted" in notice
