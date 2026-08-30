"""
base.py — Abstract Verifier base class for the CodePilot Eval Suite.

A Verifier receives the workspace path after the agent finishes and returns
(passed: bool, reason: str). All checks are deterministic — no LLM calls here.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path


class Verifier(ABC):
    """Abstract base for all eval task verifiers."""

    @abstractmethod
    def verify(self, workspace: Path) -> tuple[bool, str]:
        """
        Run assertions against the final workspace state.

        Parameters
        ----------
        workspace : Path
            Absolute path to the temp workspace directory.

        Returns
        -------
        (passed, reason) : tuple[bool, str]
            passed  — True if all assertions pass.
            reason  — Human-readable explanation of the result.
        """

    def __call__(self, workspace: Path) -> tuple[bool, str]:
        return self.verify(workspace)
