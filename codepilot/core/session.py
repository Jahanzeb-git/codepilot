"""
File: session.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description:
Persistence layer for multi-turn agentic conversations.

Architectural Notes:
Implements three pluggable session backends under a common BaseSession ABC:
InMemorySession (ephemeral, zero config), FileSession (JSON on disk at
~/.codepilot/sessions/), and DatabaseSession (any SQLAlchemy-compatible DB).
The DatabaseSession auto-creates its table on first use — no migrations needed.
FileSession uses atomic temp-file rename writes to prevent corruption on crash.
All backends also persist extra state (memory archive, task counters) alongside
the message history.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

from __future__ import annotations

import json
import pathlib
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_session_dir() -> pathlib.Path:
    """
    Return a user-writable directory for session files.
    Created automatically if it doesn't exist.
    """
    session_dir = pathlib.Path.home() / ".codepilot" / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def _sanitise_id(session_id: str) -> str:
    """Strip characters that are unsafe in filenames."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in session_id)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseSession(ABC):

    is_async: bool = False  # Allows runtime to branch between sync and async calls

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

    def save_extra(self, data: Dict) -> None:
        """Persist additional session state (archive, counters)."""

    def load_extra(self) -> Dict:
        """Load additional session state."""
        return {}


# ---------------------------------------------------------------------------
# Async abstract base
# ---------------------------------------------------------------------------

class AsyncBaseSession(ABC):
    """
    Abstract base class for async-native session backends.

    All methods are coroutines so they can be safely awaited inside
    AsyncRuntime.run() without blocking the event loop.
    """

    is_async: bool = True  # Signals AsyncRuntime to use await on all session calls

    @abstractmethod
    async def load(self) -> List[Dict]:
        """Return the stored message list (empty list if no history yet)."""

    @abstractmethod
    async def save(self, messages: List[Dict]) -> None:
        """Persist the current message list."""

    @abstractmethod
    async def reset(self) -> None:
        """Wipe all stored history."""

    @property
    @abstractmethod
    def session_id(self) -> str:
        """Unique identifier for this session."""

    async def save_extra(self, data: Dict) -> None:
        """Persist additional session state (archive, counters)."""

    async def load_extra(self) -> Dict:
        """Load additional session state."""
        return {}


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
        self._extra: Dict = {}

    def load(self) -> List[Dict]:
        return list(self._messages)

    def save(self, messages: List[Dict]) -> None:
        self._messages = list(messages)

    def reset(self) -> None:
        self._messages = []
        self._extra = {}

    def save_extra(self, data: Dict) -> None:
        self._extra = dict(data)

    def load_extra(self) -> Dict:
        return dict(self._extra)

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

    The file is stored in ``~/.codepilot/sessions/`` which is always
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
            "extra":       existing.get("extra", {}),
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

    def save_extra(self, data: Dict) -> None:
        """Persist extra data into the session JSON file."""
        existing: Dict = {}
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        existing["extra"] = data
        existing["updated_at"] = time.time()

        tmp_path = self._path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        tmp_path.replace(self._path)

    def load_extra(self) -> Dict:
        """Load extra data from the session JSON file."""
        if not self._path.exists():
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("extra", {})
        except (json.JSONDecodeError, OSError):
            return {}

    def __repr__(self) -> str:
        return f"FileSession(id={self._session_id!r}, path={self._path})"


# ---------------------------------------------------------------------------
# Database-backed backend
# ---------------------------------------------------------------------------

class DatabaseSession(BaseSession):
    """
    Persists the conversation history to any SQLAlchemy-compatible database.

    Supported databases (anything SQLAlchemy supports):
        SQLite:     "sqlite:///./codepilot.db"
        PostgreSQL: "postgresql://user:pass@host/dbname"
        MySQL:      "mysql+pymysql://user:pass@host/dbname"

    The ``codepilot_sessions`` table is created automatically on first use —
    no migration scripts or manual schema setup required.

    Design (write-after-task, read-on-resume):
        - Messages accumulate in-memory during each run() call.
        - Written to the database atomically (UPSERT) when run() completes.
        - Read from the database only when a session is first loaded
          (i.e. on Runtime construction, or after reset()).

    This is the correct backend for web applications deployed in containers
    where multiple processes or replicas share the same database.

    Requires:
        pip install sqlalchemy
        pip install psycopg2-binary   # PostgreSQL only
    """

    _TABLE_NAME = "codepilot_sessions"

    def __init__(
        self,
        session_id: str,
        agent_name: str = "agent",
        db_url: str = "sqlite:///./codepilot.db",
        engine=None,
    ):
        """
        Args:
            session_id:  Unique name for this conversation.
            agent_name:  Stored as metadata.
            db_url:      SQLAlchemy connection string. Ignored when ``engine`` is
                         provided.
            engine:      Optional pre-built synchronous SQLAlchemy ``Engine``.
                         When supplied the caller's engine (and its connection
                         pool) is reused directly — no second pool is created.
                         Useful when your web application already owns the engine
                         and you want CodePilot to share the same pool.
        """
        try:
            import sqlalchemy as sa
        except ImportError:
            raise ImportError(
                "DatabaseSession requires SQLAlchemy.\n"
                "Install it with:  pip install sqlalchemy\n"
                "For PostgreSQL:   pip install psycopg2-binary"
            )

        self._session_id = session_id
        self._agent_name = agent_name
        self._sa         = sa

        if engine is not None:
            # Reuse the caller's engine — no new connection pool created.
            self._engine = engine
            self._db_url = repr(engine)   # stored for __repr__ only
        else:
            self._db_url = db_url
            # SQLite: enable WAL mode for safe concurrent reads.
            connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
            self._engine = sa.create_engine(db_url, connect_args=connect_args)

        self._table = self._build_table(sa)
        self._ensure_table()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _build_table(self, sa):
        meta = sa.MetaData()
        return sa.Table(
            self._TABLE_NAME, meta,
            sa.Column("session_id",  sa.String(255), primary_key=True),
            sa.Column("agent_name",  sa.String(255), nullable=False, default=""),
            sa.Column("messages",    sa.Text,         nullable=False, default="[]"),
            sa.Column("created_at",  sa.Float,        nullable=False),
            sa.Column("updated_at",  sa.Float,        nullable=False),
        )

    def _ensure_table(self) -> None:
        """Create the table if it doesn't exist. Idempotent — safe to call every time."""
        self._table.metadata.create_all(self._engine, checkfirst=True)

    # ------------------------------------------------------------------
    # BaseSession interface
    # ------------------------------------------------------------------

    def load(self) -> List[Dict]:
        """
        Fetch stored messages from the database for this session_id.
        Returns an empty list if no row exists yet (new session).
        """
        sa = self._sa
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(self._table.c.messages)
                .where(self._table.c.session_id == self._session_id)
            ).fetchone()

        if row is None:
            return []
        try:
            parsed = json.loads(row[0])
            # New format: {"messages": [...], "extra": {...}}
            if isinstance(parsed, dict) and "messages" in parsed:
                return parsed["messages"]
            # Old format: plain list
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def save(self, messages: List[Dict]) -> None:
        """
        Upsert the full message list for this session_id.
        Called once per run() completion — not per agentic step.
        Uses a transaction so either the full write succeeds or nothing changes.
        """
        sa   = self._sa
        now  = time.time()

        # Wrap messages with extra data for persistence
        existing_extra = self.load_extra()
        payload_obj = {
            "messages": messages,
            "extra": existing_extra,
        }
        data = json.dumps(payload_obj, ensure_ascii=False)

        with self._engine.begin() as conn:
            exists = conn.execute(
                sa.select(self._table.c.created_at)
                .where(self._table.c.session_id == self._session_id)
            ).fetchone()

            if exists:
                conn.execute(
                    sa.update(self._table)
                    .where(self._table.c.session_id == self._session_id)
                    .values(messages=data, updated_at=now)
                )
            else:
                conn.execute(
                    sa.insert(self._table).values(
                        session_id=self._session_id,
                        agent_name=self._agent_name,
                        messages=data,
                        created_at=now,
                        updated_at=now,
                    )
                )

    def reset(self) -> None:
        """Delete this session's row from the database."""
        sa = self._sa
        with self._engine.begin() as conn:
            conn.execute(
                sa.delete(self._table)
                .where(self._table.c.session_id == self._session_id)
            )

    # ------------------------------------------------------------------
    # Extra helpers
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    def exists(self) -> bool:
        """Return True if a row exists in the database for this session_id."""
        sa = self._sa
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(self._table.c.session_id)
                .where(self._table.c.session_id == self._session_id)
            ).fetchone()
        return row is not None

    def metadata(self) -> Optional[Dict]:
        """Return session metadata (without messages), or None if not found."""
        sa = self._sa
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(
                    self._table.c.session_id,
                    self._table.c.agent_name,
                    self._table.c.created_at,
                    self._table.c.updated_at,
                ).where(self._table.c.session_id == self._session_id)
            ).fetchone()

        if row is None:
            return None
        return {
            "session_id": row[0],
            "agent_name": row[1],
            "created_at": row[2],
            "updated_at": row[3],
        }

    def list_sessions(self) -> List[Dict]:
        """List all sessions stored in this database, newest first."""
        sa = self._sa
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(
                    self._table.c.session_id,
                    self._table.c.agent_name,
                    self._table.c.messages,
                    self._table.c.created_at,
                    self._table.c.updated_at,
                ).order_by(self._table.c.updated_at.desc())
            ).fetchall()

        sessions = []
        for row in rows:
            try:
                parsed = json.loads(row[2])
                if isinstance(parsed, dict) and isinstance(parsed.get("messages"), list):
                    msg_count = len(parsed["messages"])
                elif isinstance(parsed, list):
                    msg_count = len(parsed)
                else:
                    msg_count = 0
            except (json.JSONDecodeError, TypeError):
                msg_count = 0
            sessions.append({
                "session_id": row[0],
                "agent_name": row[1],
                "messages":   msg_count,
                "created_at": row[3],
                "updated_at": row[4],
            })
        return sessions

    def dispose(self) -> None:
        """
        Release all pooled connections. Call on application shutdown.
        Not required for SQLite.
        """
        self._engine.dispose()

    def save_extra(self, data: Dict) -> None:
        """Persist extra data alongside messages in the database."""
        sa = self._sa
        now = time.time()

        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(self._table.c.messages)
                .where(self._table.c.session_id == self._session_id)
            ).fetchone()

            if row is None:
                return

            try:
                parsed = json.loads(row[0])
                if isinstance(parsed, dict):
                    parsed["extra"] = data
                else:
                    # Old format migration
                    parsed = {"messages": parsed, "extra": data}
            except (json.JSONDecodeError, TypeError):
                return

            conn.execute(
                sa.update(self._table)
                .where(self._table.c.session_id == self._session_id)
                .values(
                    messages=json.dumps(parsed, ensure_ascii=False),
                    updated_at=now,
                )
            )

    def load_extra(self) -> Dict:
        """Load extra data from the database."""
        sa = self._sa
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(self._table.c.messages)
                .where(self._table.c.session_id == self._session_id)
            ).fetchone()

        if row is None:
            return {}
        try:
            parsed = json.loads(row[0])
            if isinstance(parsed, dict):
                return parsed.get("extra", {})
            return {}  # Old format — no extra
        except (json.JSONDecodeError, TypeError):
            return {}

    def __repr__(self) -> str:
        return f"DatabaseSession(id={self._session_id!r}, url={self._db_url!r})"


# ---------------------------------------------------------------------------
# Async database-backed backend
# ---------------------------------------------------------------------------

class AsyncDatabaseSession(AsyncBaseSession):
    """
    Persists conversation history using a caller-supplied SQLAlchemy
    ``AsyncEngine`` — the same engine the developer already created for their
    web application — so no second connection pool is ever created.

    The ``codepilot_sessions`` table is created automatically on the first
    database operation (lazy initialisation — safe inside an async context).

    Supported databases (anything SQLAlchemy async supports):
        PostgreSQL: create_async_engine("postgresql+asyncpg://user:pass@host/db")
        SQLite:     create_async_engine("sqlite+aiosqlite:///./db.sqlite3")

    Requires:
        pip install sqlalchemy
        pip install asyncpg      # PostgreSQL
        pip install aiosqlite    # SQLite async
    """

    _TABLE_NAME = "codepilot_sessions"

    def __init__(
        self,
        session_id: str,
        agent_name: str = "agent",
        engine=None,
    ):
        """
        Args:
            session_id:  Unique name for this conversation.
            agent_name:  Stored as metadata.
            engine:      A SQLAlchemy ``AsyncEngine``. Required.
        """
        try:
            from sqlalchemy.ext.asyncio import AsyncEngine
            import sqlalchemy as sa
        except ImportError:
            raise ImportError(
                "AsyncDatabaseSession requires SQLAlchemy.\n"
                "Install it with:  pip install sqlalchemy\n"
                "For PostgreSQL:   pip install asyncpg\n"
                "For SQLite:       pip install aiosqlite"
            )

        if engine is None:
            raise ValueError(
                "AsyncDatabaseSession requires a pre-built SQLAlchemy AsyncEngine.\n"
                "Example:\n"
                "  from sqlalchemy.ext.asyncio import create_async_engine\n"
                "  engine = create_async_engine('postgresql+asyncpg://user:pass@host/db')\n"
                "  session = AsyncDatabaseSession('my-session', engine=engine)"
            )

        if not isinstance(engine, AsyncEngine):
            raise TypeError(
                f"AsyncDatabaseSession expects a SQLAlchemy AsyncEngine, "
                f"got {type(engine).__name__!r}. "
                "For sync engines use DatabaseSession instead."
            )

        self._session_id  = session_id
        self._agent_name  = agent_name
        self._engine      = engine
        self._sa          = sa
        self._table       = self._build_table(sa)
        self._table_ready = False  # Lazy flag — table created on first DB call

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _build_table(self, sa):
        meta = sa.MetaData()
        return sa.Table(
            self._TABLE_NAME, meta,
            sa.Column("session_id",  sa.String(255), primary_key=True),
            sa.Column("agent_name",  sa.String(255), nullable=False, default=""),
            sa.Column("messages",    sa.Text,         nullable=False, default="[]"),
            sa.Column("created_at",  sa.Float,        nullable=False),
            sa.Column("updated_at",  sa.Float,        nullable=False),
        )

    async def _ensure_table(self) -> None:
        """Create the table if it does not exist. Idempotent — called lazily."""
        if self._table_ready:
            return
        async with self._engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: self._table.metadata.create_all(sync_conn, checkfirst=True)
            )
        self._table_ready = True

    # ------------------------------------------------------------------
    # AsyncBaseSession interface
    # ------------------------------------------------------------------

    async def load(self) -> List[Dict]:
        """Fetch stored messages. Returns [] for a brand-new session."""
        await self._ensure_table()
        sa = self._sa
        async with self._engine.connect() as conn:
            result = await conn.execute(
                sa.select(self._table.c.messages)
                .where(self._table.c.session_id == self._session_id)
            )
            row = result.fetchone()

        if row is None:
            return []
        try:
            parsed = json.loads(row[0])
            if isinstance(parsed, dict) and "messages" in parsed:
                return parsed["messages"]
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    async def save(self, messages: List[Dict]) -> None:
        """Upsert the full message list. Called once per run() completion."""
        await self._ensure_table()
        sa  = self._sa
        now = time.time()

        existing_extra = await self.load_extra()
        payload_obj = {"messages": messages, "extra": existing_extra}
        data = json.dumps(payload_obj, ensure_ascii=False)

        async with self._engine.begin() as conn:
            result = await conn.execute(
                sa.select(self._table.c.created_at)
                .where(self._table.c.session_id == self._session_id)
            )
            exists = result.fetchone()

            if exists:
                await conn.execute(
                    sa.update(self._table)
                    .where(self._table.c.session_id == self._session_id)
                    .values(messages=data, updated_at=now)
                )
            else:
                await conn.execute(
                    sa.insert(self._table).values(
                        session_id=self._session_id,
                        agent_name=self._agent_name,
                        messages=data,
                        created_at=now,
                        updated_at=now,
                    )
                )

    async def reset(self) -> None:
        """Delete this session's row from the database."""
        await self._ensure_table()
        sa = self._sa
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.delete(self._table)
                .where(self._table.c.session_id == self._session_id)
            )

    async def save_extra(self, data: Dict) -> None:
        """Persist extra state (memory archive, counters) alongside messages."""
        await self._ensure_table()
        sa  = self._sa
        now = time.time()

        async with self._engine.begin() as conn:
            result = await conn.execute(
                sa.select(self._table.c.messages)
                .where(self._table.c.session_id == self._session_id)
            )
            row = result.fetchone()

            if row is None:
                return

            try:
                parsed = json.loads(row[0])
                if isinstance(parsed, dict):
                    parsed["extra"] = data
                else:
                    # Old format migration: wrap plain list
                    parsed = {"messages": parsed, "extra": data}
            except (json.JSONDecodeError, TypeError):
                return

            await conn.execute(
                sa.update(self._table)
                .where(self._table.c.session_id == self._session_id)
                .values(
                    messages=json.dumps(parsed, ensure_ascii=False),
                    updated_at=now,
                )
            )

    async def load_extra(self) -> Dict:
        """Load extra state from the database."""
        await self._ensure_table()
        sa = self._sa
        async with self._engine.connect() as conn:
            result = await conn.execute(
                sa.select(self._table.c.messages)
                .where(self._table.c.session_id == self._session_id)
            )
            row = result.fetchone()

        if row is None:
            return {}
        try:
            parsed = json.loads(row[0])
            if isinstance(parsed, dict):
                return parsed.get("extra", {})
            return {}  # Old format — no extra
        except (json.JSONDecodeError, TypeError):
            return {}

    # ------------------------------------------------------------------
    # Extras
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    async def dispose(self) -> None:
        """Release all pooled connections. Call on application shutdown."""
        await self._engine.dispose()

    def __repr__(self) -> str:
        return f"AsyncDatabaseSession(id={self._session_id!r})"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_session(
    backend: str = "memory",
    session_id: str = "default",
    agent_name: str = "agent",
    session_dir: Optional[pathlib.Path] = None,
    db_url: Optional[str] = None,
    engine=None,
) -> "BaseSession | AsyncBaseSession":
    """
    Create a session backend.

    Args:
        backend:    'memory' (default), 'file', or 'db'.
        session_id: Unique name for this session.
        agent_name: Stored as metadata in file / db backends.
        session_dir: Override default ~/.codepilot/sessions/ (file only).
        db_url:     SQLAlchemy connection string. Used for backend='db' when
                    no ``engine`` is supplied.
                    e.g. "sqlite:///./codepilot.db"
                         "postgresql://user:pass@localhost/myapp"
        engine:     Optional pre-built SQLAlchemy Engine or AsyncEngine.
                    When provided, ``backend`` is inferred automatically:
                    - AsyncEngine  → AsyncDatabaseSession  (is_async=True)
                    - sync Engine  → DatabaseSession        (is_async=False)
                    The caller's connection pool is reused — no second pool
                    is created.  ``db_url`` is ignored when engine is set.

    Returns:
        A BaseSession or AsyncBaseSession instance ready to use.
    """
    # ------------------------------------------------------------------ #
    # Engine shortcut — detect sync vs async and bypass backend string    #
    # ------------------------------------------------------------------ #
    if engine is not None:
        try:
            from sqlalchemy.ext.asyncio import AsyncEngine
            if isinstance(engine, AsyncEngine):
                return AsyncDatabaseSession(
                    session_id=session_id,
                    agent_name=agent_name,
                    engine=engine,
                )
        except ImportError:
            pass  # sqlalchemy.ext.asyncio not available — fall through to sync

        # Must be a synchronous Engine
        return DatabaseSession(
            session_id=session_id,
            agent_name=agent_name,
            engine=engine,
        )

    # ------------------------------------------------------------------ #
    # String-based backends (existing behaviour — fully backward-compat)  #
    # ------------------------------------------------------------------ #
    if backend == "memory":
        return InMemorySession(session_id=session_id)

    elif backend == "file":
        return FileSession(
            session_id=session_id,
            agent_name=agent_name,
            session_dir=session_dir,
        )

    elif backend == "db":
        if not db_url:
            raise ValueError(
                "backend='db' requires a db_url. "
                "Example: db_url='sqlite:///./codepilot.db'"
            )
        return DatabaseSession(
            session_id=session_id,
            agent_name=agent_name,
            db_url=db_url,
        )

    else:
        raise ValueError(
            f"Unknown session backend '{backend}'. "
            "Choose 'memory', 'file', or 'db'."
        )
