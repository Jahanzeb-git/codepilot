"""
codepilot.core.session
~~~~~~~~~~~~~~~~~~~~~~

Persistence layer for multi-turn agentic conversations.

Two backends are supported:

    InMemorySession  — keeps the message history in a Python list for the
                       lifetime of the process.  Zero configuration, zero I/O.
                       Works perfectly for CLI tools running in a while-loop or
                       for any use-case where you don't need history to survive
                       a process restart.

    FileSession      — serialises the message history to a JSON file on disk
                       so the context survives process restarts.  The file is
                       written to a user-writable directory that requires no
                       elevated permissions on any platform:

                           Windows : %USERPROFILE%\\.codepilot\\sessions\\
                           macOS   : ~/.codepilot/sessions/
                           Linux   : ~/.codepilot/sessions/

Usage (via Runtime — the preferred interface):

    # in-memory (default)
    runtime = Runtime("agent.yaml")

    # file-backed, auto-named from agent name
    runtime = Runtime("agent.yaml", session="file")

    # file-backed, explicit session id (resumes if file exists)
    runtime = Runtime("agent.yaml", session="file", session_id="project-x")

    # wipe history and start fresh
    runtime.reset()
"""

from __future__ import annotations

import json
import os
import pathlib
import platform
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_session_dir() -> pathlib.Path:
    """
    Return a platform-appropriate, user-writable directory for session files.
    Created automatically if it doesn't exist.
    """
    if platform.system() == "Windows":
        base = pathlib.Path(os.environ.get("USERPROFILE", pathlib.Path.home()))
    else:
        base = pathlib.Path.home()

    session_dir = base / ".codepilot" / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def _sanitise_id(session_id: str) -> str:
    """Strip characters that are unsafe in filenames."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in session_id)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseSession(ABC):

    @abstractmethod
    def load(self) -> List[Dict]:
        """Return the stored message list (empty list if no history yet)."""

    @abstractmethod
    def save(self, messages: List[Dict]) -> None:
        """Persist the current message list."""

    @abstractmethod
    def reset(self) -> None:
        """Wipe all stored history."""

    @property
    @abstractmethod
    def session_id(self) -> str:
        """Unique identifier for this session."""


# ---------------------------------------------------------------------------
# In-memory backend
# ---------------------------------------------------------------------------

class InMemorySession(BaseSession):
    """
    Stores the conversation history in a plain Python list.

    - No I/O, no files, no configuration needed.
    - History is lost when the process exits.
    - Perfect for CLI while-loops or ephemeral API handlers.
    """

    def __init__(self, session_id: str = "default"):
        self._session_id = session_id
        self._messages: List[Dict] = []

    def load(self) -> List[Dict]:
        return list(self._messages)

    def save(self, messages: List[Dict]) -> None:
        self._messages = list(messages)

    def reset(self) -> None:
        self._messages = []

    @property
    def session_id(self) -> str:
        return self._session_id

    def __repr__(self) -> str:
        return f"InMemorySession(id={self._session_id!r}, messages={len(self._messages)})"


# ---------------------------------------------------------------------------
# File-backed backend
# ---------------------------------------------------------------------------

class FileSession(BaseSession):
    """
    Persists the conversation history as a JSON file on disk.

    The file is stored in ``~/.codepilot/sessions/`` (or
    ``%USERPROFILE%\\.codepilot\\sessions\\`` on Windows) which is always
    user-writable without elevated permissions.

    File format::

        {
          "session_id": "my-project",
          "agent_name": "BackendEngineer",
          "created_at": 1712345678.0,
          "updated_at": 1712349999.0,
          "messages": [ ... ]
        }

    If the file already exists when the Runtime starts, the previous
    conversation is automatically resumed — the LLM sees the full history
    and can reason about what was done in prior sessions.
    """

    def __init__(
        self,
        session_id: str,
        agent_name: str = "agent",
        session_dir: Optional[pathlib.Path] = None,
    ):
        self._session_id  = _sanitise_id(session_id)
        self._agent_name  = agent_name
        self._session_dir = session_dir or _default_session_dir()
        self._path        = self._session_dir / f"{self._session_id}.json"

    # ------------------------------------------------------------------
    # BaseSession interface
    # ------------------------------------------------------------------

    def load(self) -> List[Dict]:
        if not self._path.exists():
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("messages", [])
        except (json.JSONDecodeError, OSError):
            # Corrupted or unreadable — start fresh rather than crash.
            return []

    def save(self, messages: List[Dict]) -> None:
        existing: Dict = {}
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        payload = {
            "session_id":  self._session_id,
            "agent_name":  self._agent_name,
            "created_at":  existing.get("created_at", time.time()),
            "updated_at":  time.time(),
            "messages":    messages,
        }

        # Atomic write: write to a temp file then rename so a crash mid-write
        # never leaves a corrupted session file.
        tmp_path = self._path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        tmp_path.replace(self._path)

    def reset(self) -> None:
        if self._path.exists():
            self._path.unlink()

    # ------------------------------------------------------------------
    # Extra helpers
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def path(self) -> pathlib.Path:
        return self._path

    def exists(self) -> bool:
        """Return True if a saved session file exists on disk."""
        return self._path.exists()

    def metadata(self) -> Optional[Dict]:
        """Return session metadata (without messages) or None if no file."""
        if not self._path.exists():
            return None
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {k: v for k, v in data.items() if k != "messages"}
        except (json.JSONDecodeError, OSError):
            return None

    def list_sessions(self) -> List[Dict]:
        """List all saved sessions in the session directory."""
        sessions = []
        for p in sorted(self._session_dir.glob("*.json")):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                sessions.append({
                    "session_id": data.get("session_id", p.stem),
                    "agent_name": data.get("agent_name", ""),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                    "messages":   len(data.get("messages", [])),
                    "path":       str(p),
                })
            except (json.JSONDecodeError, OSError):
                continue
        return sessions

    def __repr__(self) -> str:
        return f"FileSession(id={self._session_id!r}, path={self._path})"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_session(
    backend: str = "memory",
    session_id: str = "default",
    agent_name: str = "agent",
    session_dir: Optional[pathlib.Path] = None,
) -> BaseSession:
    """
    Create a session backend.

    Args:
        backend:    'memory' (default) or 'file'.
        session_id: Unique name for this session.  Used as the filename for
                    file-backed sessions.  Safe to include hyphens and dots.
        agent_name: Used only for metadata in file-backed sessions.
        session_dir: Override the default ~/.codepilot/sessions/ directory.

    Returns:
        A BaseSession instance ready to use.
    """
    if backend == "memory":
        return InMemorySession(session_id=session_id)
    elif backend == "file":
        return FileSession(
            session_id=session_id,
            agent_name=agent_name,
            session_dir=session_dir,
        )
    else:
        raise ValueError(
            f"Unknown session backend '{backend}'. Choose 'memory' or 'file'."
        )
