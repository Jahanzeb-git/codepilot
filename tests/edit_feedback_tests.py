"""
Tests for edit_file feedback: human-readable counts and bounded diffs.

These are tracked tests (the ``test_*.py`` files in this directory are
gitignored scratch tests). Run with:

    python3 -m pytest tests/edit_feedback_tests.py -q
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from codepilot.engine.hooks import EventType, HookSystem  # noqa: E402
from codepilot.tools.filesystem import FilesystemTools  # noqa: E402


class _DummyRuntime:
    """Minimal runtime surface required by FilesystemTools."""

    def __init__(self, work_dir: str) -> None:
        self.config = SimpleNamespace(
            runtime=SimpleNamespace(work_dir=work_dir, unsafe_mode=True),
        )
        self.hooks = HookSystem()
        for event in EventType:
            self.hooks.clear(event)
        self.hooks.register(EventType.PERMISSION_REQUEST, lambda **kwargs: True)
        self.execution_log: list[str] = []

    def _tool_config(self, tool_name: str) -> dict:
        return {}

    def _append_execution(self, text: str) -> None:
        self.execution_log.append(text)


def _make(tmp: str):
    rt = _DummyRuntime(work_dir=tmp)
    return rt, FilesystemTools(rt)


def test_single_line_edit_uses_singular_and_includes_diff():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.py"
        path.write_text(
            '"""doc"""\n\nif __name__ == "__main__":::::::\n    print(1)\n',
            encoding="utf-8",
        )
        _, fs = _make(tmp)

        result = fs.edit_file(
            "sample.py",
            'if __name__ == "__main__":::::::',
            'if __name__ == "__main__":',
        )

        assert "replaced 1 line at line 3 with 1 line (Δ0)" in result
        assert "1 lines" not in result
        assert "--- a/sample.py" in result
        assert '-if __name__ == "__main__":::::::' in result
        assert '+if __name__ == "__main__":' in result


def test_diff_is_capped_per_edit_call():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "big.py"
        path.write_text("".join(f"line_{i} = {i}\n" for i in range(300)), encoding="utf-8")
        _, fs = _make(tmp)

        old = "".join(f"line_{i} = {i}\n" for i in range(150)).rstrip("\n")
        new = "".join(f"line_{i} = {i + 1000}\n" for i in range(150)).rstrip("\n")

        result = fs.edit_file("big.py", old, new)

        assert "replaced 150 lines" in result
        assert "diff truncated" in result
        # 40 diff lines + one truncation notice (plus the header line).
        diff_lines = result.splitlines()[1:]
        assert len(diff_lines) <= 41


def test_each_edit_call_gets_its_own_diff():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "multi.py"
        path.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
        _, fs = _make(tmp)

        first = fs.edit_file("multi.py", "a = 1", "a = 10")
        second = fs.edit_file("multi.py", "c = 3", "c = 30")

        assert "-a = 1" in first and "+a = 10" in first
        assert "+c = 30" not in first
        assert "-c = 3" in second and "+c = 30" in second
        assert "+a = 10" not in second


def test_no_diff_when_replacement_is_identical():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "same.py"
        path.write_text("x = 1\n", encoding="utf-8")
        _, fs = _make(tmp)

        result = fs.edit_file("same.py", "x = 1", "x = 1")

        assert "updated" in result
        assert "--- a/same.py" not in result
