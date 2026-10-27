"""
Tests for the compact codebase snapshot and the view_file read cap.

Run with:

    python3 -m pytest tests/context_snapshot_tests.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from codepilot.core.context import ContextManager  # noqa: E402
from codepilot.engine.hooks import EventType, HookSystem  # noqa: E402
from codepilot.tools.filesystem import FilesystemTools  # noqa: E402


class _Runtime:
    """Minimal runtime surface used by FilesystemTools."""

    def __init__(self, work_dir: str) -> None:
        self.config = SimpleNamespace(
            runtime=SimpleNamespace(work_dir=work_dir, unsafe_mode=False)
        )
        self.hooks = HookSystem()
        for event in EventType:
            self.hooks.clear(event)
        self.execution_log: list[str] = []

    def _tool_config(self, tool_name: str) -> dict:
        return {}

    def _append_execution(self, text: str) -> None:
        self.execution_log.append(text)


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("a\nb\nc\n")
    (tmp_path / "README.md").write_text("title\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")
    for name in (".grepai", ".codepilot"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "hidden.txt").write_text("no\n")
    return tmp_path


def test_snapshot_is_compact_and_lists_paths(tmp_path):
    root = _workspace(tmp_path)
    snapshot = ContextManager(str(root)).get_formatted_snapshot()

    assert str(root) in snapshot.splitlines()[0]
    assert "pkg/" in snapshot
    assert "mod.py 3" in snapshot
    assert "README.md 1" in snapshot
    # The old pretty-JSON shape must be gone.
    assert '"lines"' not in snapshot
    assert "{" not in snapshot


def test_snapshot_ignores_tool_state_directories(tmp_path):
    root = _workspace(tmp_path)
    snapshot = ContextManager(str(root)).get_formatted_snapshot()

    assert ".grepai" not in snapshot
    assert ".codepilot" not in snapshot


def test_snapshot_labels_binary_files(tmp_path):
    root = _workspace(tmp_path)
    snapshot = ContextManager(str(root)).get_formatted_snapshot()

    assert "logo.png [binary]" in snapshot
    # Three files exist, but only the two text files contribute line counts.
    assert "[3 files, 4 lines]" in snapshot.splitlines()[0]


def test_snapshot_honours_file_and_char_caps(tmp_path):
    root = tmp_path
    for index in range(40):
        (root / f"f{index:02d}.txt").write_text("x\n" * (index + 1))

    by_files = ContextManager(str(root), max_files=5).get_formatted_snapshot()
    assert "truncated" in by_files.splitlines()[0]
    assert sum(1 for line in by_files.splitlines()[1:] if ".txt " in line) == 5

    by_chars = ContextManager(str(root), max_chars=60).get_formatted_snapshot()
    assert "truncated" in by_chars.splitlines()[0]
    body = "\n".join(by_chars.splitlines()[1:])
    assert len(body) <= 60


def test_snapshot_reuses_cached_line_counts(tmp_path):
    root = _workspace(tmp_path)
    manager = ContextManager(str(root))

    first = manager.get_formatted_snapshot()
    second = manager.get_formatted_snapshot()

    assert first == second
    assert manager._line_cache


def test_view_file_caps_default_read(tmp_path):
    root = tmp_path
    (root / "big.py").write_text("".join(f"line {i}\n" for i in range(1, 1001)))
    tools = FilesystemTools(_Runtime(str(root)))

    result = tools.view_file("big.py")
    lines = result.splitlines()

    assert "lines 1–400 of 1000" in lines[0]
    assert len(lines) - 2 == 400
    assert "max_lines=400 reached" in lines[-1]
    assert "start_line=401" in lines[-1]


def test_view_file_respects_explicit_range_and_override(tmp_path):
    root = tmp_path
    (root / "big.py").write_text("".join(f"line {i}\n" for i in range(1, 1001)))
    tools = FilesystemTools(_Runtime(str(root)))

    ranged = tools.view_file("big.py", start_line=10, end_line=20)
    assert "lines 10–20 of 1000" in ranged.splitlines()[0]

    capped = tools.view_file("big.py", start_line=10, end_line=900)
    assert "lines 10–409 of 1000" in capped.splitlines()[0]

    full = tools.view_file("big.py", max_lines=1000)
    assert "[END OF FILE]" in full


def test_view_file_rejects_invalid_max_lines(tmp_path):
    root = tmp_path
    (root / "a.py").write_text("x\n")
    tools = FilesystemTools(_Runtime(str(root)))

    assert "max_lines must be a positive integer" in tools.view_file("a.py", max_lines=0)
    assert "max_lines must be a positive integer" in tools.view_file("a.py", max_lines="x")
