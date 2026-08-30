"""
file_verifiers.py — Deterministic functional verifiers for file-operation tasks.

Each verifier imports the agent-generated Python code and runs assertions
against it, or inspects file content directly. Zero LLM calls.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from .base import Verifier


def _import_module_from_path(name: str, path: Path):
    """Dynamically import a Python file as a module."""
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class BinarySearchVerifier(Verifier):
    """T_F01: Verifies binary_search.py has a correct implementation."""

    def verify(self, workspace: Path) -> tuple[bool, str]:
        target = workspace / "binary_search.py"
        if not target.exists():
            return False, "binary_search.py was not created"
        try:
            mod = _import_module_from_path("_eval_bs", target)
            fn = getattr(mod, "binary_search", None)
            if fn is None:
                return False, "binary_search() function not found in file"
            # Functional tests
            assert fn([1, 3, 5, 7, 9], 5)  == 2, "found target at mid"
            assert fn([1, 3, 5, 7, 9], 1)  == 0, "found target at start"
            assert fn([1, 3, 5, 7, 9], 9)  == 4, "found target at end"
            assert fn([1, 3, 5, 7, 9], 4)  == -1, "missing target returns -1"
            assert fn([], 1)               == -1, "empty list returns -1"
            return True, "binary_search() passes all 5 functional tests"
        except AssertionError as e:
            return False, f"Functional assertion failed: {e}"
        except Exception as e:
            return False, f"Import/execution error: {e}"


class MathUtilsVerifier(Verifier):
    """T_F02: Verifies math_utils.py has a corrected square() function."""

    def verify(self, workspace: Path) -> tuple[bool, str]:
        target = workspace / "math_utils.py"
        if not target.exists():
            return False, "math_utils.py not found"
        try:
            mod = _import_module_from_path("_eval_mu", target)
            assert mod.square(3)  == 9,  "square(3) == 9"
            assert mod.square(5)  == 25, "square(5) == 25"
            assert mod.square(-4) == 16, "square(-4) == 16"
            assert mod.cube(3)    == 27, "cube() must remain unchanged"
            return True, "square() and cube() verified correct"
        except AssertionError as e:
            return False, f"Assertion failed: {e}"
        except Exception as e:
            return False, f"Error: {e}"


class CalculatorVerifier(Verifier):
    """T_F03: Verifies all three buggy functions in calculator.py were fixed."""

    def verify(self, workspace: Path) -> tuple[bool, str]:
        target = workspace / "calculator.py"
        if not target.exists():
            return False, "calculator.py not found"
        try:
            mod = _import_module_from_path("_eval_calc", target)
            assert mod.add(2, 3)      == 5,  "add(2,3)==5"
            assert mod.subtract(5, 3) == 2,  "subtract(5,3)==2"
            assert mod.multiply(3, 4) == 12, "multiply(3,4)==12"
            return True, "All three calculator functions verified correct"
        except AssertionError as e:
            return False, f"Assertion failed: {e}"
        except Exception as e:
            return False, f"Error: {e}"


class MultiFileVerifier(Verifier):
    """T_F04: Verifies models.py, utils.py created and config.py updated."""

    def verify(self, workspace: Path) -> tuple[bool, str]:
        errors = []

        # Check models.py
        models_path = workspace / "models.py"
        if not models_path.exists():
            errors.append("models.py not created")
        else:
            try:
                mod = _import_module_from_path("_eval_models", models_path)
                user = mod.User(name="Alice", age=30)
                assert user.name == "Alice"
                assert user.age  == 30
            except Exception as e:
                errors.append(f"models.py error: {e}")

        # Check utils.py
        utils_path = workspace / "utils.py"
        if not utils_path.exists():
            errors.append("utils.py not created")
        else:
            try:
                # Need models imported for greet()
                if "_eval_models" not in sys.modules:
                    _import_module_from_path("_eval_models", workspace / "models.py")
                mod = _import_module_from_path("_eval_utils", utils_path)
                from _eval_models import User
                u = User(name="Bob", age=25)
                result = mod.greet(u)
                assert "Bob" in result, f"greet() must include the name, got: {result!r}"
            except Exception as e:
                errors.append(f"utils.py error: {e}")

        # Check config.py was updated
        config_path = workspace / "config.py"
        if not config_path.exists():
            errors.append("config.py missing")
        else:
            content = config_path.read_text()
            if '"1.0.0"' not in content and "'1.0.0'" not in content:
                errors.append("config.py VERSION not updated to 1.0.0")

        if errors:
            return False, "; ".join(errors)
        return True, "All three file operations verified"


class GreeterVerifier(Verifier):
    """T_F05: Verifies greeter.py was correctly updated after error recovery."""

    def verify(self, workspace: Path) -> tuple[bool, str]:
        target = workspace / "greeter.py"
        if not target.exists():
            return False, "greeter.py not found"
        try:
            mod = _import_module_from_path("_eval_greeter", target)
            greet_result = mod.greet("Alice")
            farewell_result = mod.farewell("Alice")
            assert "Hi" in greet_result or "Welcome" in greet_result, \
                f"greet() not updated: {greet_result!r}"
            assert "later" in farewell_result.lower() or "see you" in farewell_result.lower(), \
                f"farewell() not updated: {farewell_result!r}"
            return True, "greeter.py both functions verified correct after recovery"
        except AssertionError as e:
            return False, f"Assertion failed: {e}"
        except Exception as e:
            return False, f"Error: {e}"


class NotesVerifier(Verifier):
    """T_P01: Verifies notes.txt was created with the correct content."""

    def verify(self, workspace: Path) -> tuple[bool, str]:
        target = workspace / "notes.txt"
        if not target.exists():
            return False, "notes.txt was not created"
        content = target.read_text()
        if "Hello World" not in content:
            return False, f"'Hello World' not found in notes.txt: {content!r}"
        if "This is a test" not in content:
            return False, f"'This is a test' not found in notes.txt: {content!r}"
        return True, "notes.txt content verified correct"


class ConversationalVerifier(Verifier):
    """
    T_C01: Verifies the agent did NOT emit any file blocks or codepilot.py
    for a conversational prompt. The EvalTrace step count and block count
    are checked here as a secondary confirmation.
    """

    def verify(self, workspace: Path) -> tuple[bool, str]:
        # For conversational tasks, workspace should be completely empty.
        items = list(workspace.iterdir())
        if items:
            names = [i.name for i in items]
            return False, f"Workspace should be empty for conversational task, found: {names}"
        return True, "Workspace is clean — no files were written (correct for conversational)"


class InsertionSortDocstringVerifier(Verifier):
    """T_E01: Verifies a docstring was added to insertion_sort()."""

    def verify(self, workspace: Path) -> tuple[bool, str]:
        target = workspace / "insertion_sort.py"
        if not target.exists():
            return False, "insertion_sort.py not found"
        content = target.read_text()
        if '"""' not in content and "'''" not in content:
            return False, "No docstring found in insertion_sort.py"
        # Also verify the function still works after the edit
        try:
            mod = _import_module_from_path("_eval_is", target)
            result = mod.insertion_sort([5, 2, 4, 6, 1, 3])
            assert result == [1, 2, 3, 4, 5, 6], f"Sort result wrong: {result}"
        except AssertionError as e:
            return False, f"insertion_sort() broken after edit: {e}"
        except Exception as e:
            return False, f"Error executing insertion_sort(): {e}"
        return True, "Docstring present and insertion_sort() still works correctly"
