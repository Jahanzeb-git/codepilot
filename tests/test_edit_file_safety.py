"""Safety regressions for the forgiving edit_file algorithm.

Anchor matching must tolerate small typos in old_str without silently
replacing a region whose middle lines came from somewhere else entirely.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from codepilot.core.tool_protocol import ToolCall
from codepilot.engine.hooks import HookSystem
from codepilot.tools.filesystem import FilesystemTools, _apply_str_replace
from codepilot.tools.registry import ToolRegistry


SERVICE = '''class Service:
    def create(self, customer: str, amount: float) -> Order:
        order = Order(customer, amount)
        self.repo.save(order)
        logger.info("created %s", customer)
        return order

    def other(self) -> int:
        return 1
'''


class _Runtime:
    def __init__(self, work_dir: Path) -> None:
        self.config = SimpleNamespace(
            runtime=SimpleNamespace(work_dir=str(work_dir), unsafe_mode=False)
        )
        self.hooks = HookSystem()
        self.executions: list[str] = []

    def _append_execution(self, text: str) -> None:
        self.executions.append(text)

    def _tool_config(self, name: str) -> dict:
        return {}


def _edit(root: Path, old: str, new: str) -> tuple[str, str]:
    target = root / "service.py"
    target.write_text(SERVICE, encoding="utf-8")
    tools = FilesystemTools(_Runtime(root))
    result = tools.edit_file("service.py", old_str=old, new_str=new)
    return result, target.read_text(encoding="utf-8")


def test_fabricated_middle_is_rejected() -> None:
    old = '''    def create(self, customer: str, amount: float) -> Order:
        self.percent = percent
        discount = amount * (self.percent / 100.0)
        return round(amount - discount, 2)
        return order'''
    with tempfile.TemporaryDirectory() as tmp:
        result, text = _edit(Path(tmp), old, "    def create(self, customer: str, amount: float) -> Order:\n        return order")
    assert result.startswith("[edit_file ERROR]")
    assert "closely enough" in result
    assert "self.repo.save(order)" in text


def test_elided_old_str_does_not_delete_code() -> None:
    old = '''    def create(self, customer: str, amount: float) -> Order:
        # ... existing code ...
        # ... existing code ...
        # ... existing code ...
        return order'''
    with tempfile.TemporaryDirectory() as tmp:
        result, text = _edit(Path(tmp), old, "    def create(self, customer: str, amount: float) -> Order:\n        return order")
    assert result.startswith("[edit_file ERROR]")
    assert "logger.info" in text


def test_small_middle_typo_still_applies() -> None:
    old = '''    def create(self, customer: str, amount: float) -> Order:
        order = Order(customer, amunt)
        self.repo.save(order)
        logger.info("created %s", customer)
        return order'''
    new = "    def create(self, customer: str, amount: float) -> Order:\n        return order"
    with tempfile.TemporaryDirectory() as tmp:
        result, text = _edit(Path(tmp), old, new)
    assert result.startswith("[edit_file] "), result
    assert "return order" in text


def test_crlf_file_stays_crlf() -> None:
    old = "        self.repo.save(order)\n        logger.info(\"created %s\", customer)"
    new = "        self.repo.save(order)\n        logger.info(\"saved %s\", customer)"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "service.py"
        target.write_bytes(SERVICE.replace("\n", "\r\n").encode())
        tools = FilesystemTools(_Runtime(root))
        result = tools.edit_file("service.py", old_str=old, new_str=new)
        raw = target.read_bytes()
    assert result.startswith("[edit_file] "), result
    assert b"saved %s" in raw
    assert b"\r\n" in raw
    assert b"saved %s\", customer)\n" not in raw


def test_lf_file_drops_crlf_from_new_str() -> None:
    old = "        self.repo.save(order)\n        logger.info(\"created %s\", customer)"
    new = "        self.repo.save(order)\r\n        logger.info(\"saved %s\", customer)"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "service.py"
        target.write_text(SERVICE, encoding="utf-8")
        tools = FilesystemTools(_Runtime(root))
        result = tools.edit_file("service.py", old_str=old, new_str=new)
        raw = target.read_bytes()
    assert result.startswith("[edit_file] "), result
    assert b"saved %s" in raw
    assert b"\r\n" not in raw


def test_ambiguous_duplicate_regions_are_refused() -> None:
    content = "def a():\n    return 1\n\ndef b():\n    return 1\n"
    result = _apply_str_replace(content, "    return 1", "    return 2")
    assert not result.ok
    assert "ambiguous" in result.error


def test_edit_file_through_registry_dispatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "service.py"
        target.write_text(SERVICE, encoding="utf-8")
        tools = FilesystemTools(_Runtime(root))
        registry = ToolRegistry()
        registry.register("edit_file", tools.edit_file)

        call = ToolCall(
            call_id="call_1",
            name="edit_file",
            arguments={
                "path": "service.py",
                "old_str": '        logger.info("created %s", customer)',
                "new_str": '        logger.info("saved %s", customer)',
            },
        )
        result = registry.dispatch(call)
        text = target.read_text(encoding="utf-8")

    assert result.startswith("[edit_file] "), result
    assert "saved %s" in text


def test_cache_id_retry_after_ambiguity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        tools = FilesystemTools(_Runtime(root))
        target = root / "dupes.py"
        target.write_text("def a():\n    return 1\n\ndef b():\n    return 1\n", encoding="utf-8")

        first = tools.edit_file(
            "dupes.py", old_str="    return 1", new_str="    return 2"
        )
        assert first.startswith("[edit_file ERROR]")
        cache_id = int(first.rsplit("cache_id=", 1)[1].split()[0])

        # The model fixes uniqueness, then retries using the cached payload.
        target.write_text("def a():\n    return 1\n\ndef b():\n    return 3\n", encoding="utf-8")
        retried = tools.edit_file("dupes.py", cache_id=cache_id)
        text = target.read_text(encoding="utf-8")

    assert retried.startswith("[edit_file] "), retried
    assert "return 2" in text
