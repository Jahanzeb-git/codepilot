"""
models.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Description: This file handles the ORM models for table creation in database.
Licensed: MIT
"""
# --IMPORTS--
from sqlalchemy.orm import Mapped, mapped_column, relationship
from db.database import Base
from sqlalchemy import ForeignKey, Enum
import enum
from datetime import datetime, date

# --ENUM--
class MachineStatus(enum.Enum):
    starting = "starting"
    running = "running"
    stopped = "stopped"
    failed = "failed"


# --ORM--
class UserDB(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(index=True, unique=True)
    hashed_password: Mapped[str] = mapped_column(nullable=False)

    machine: Mapped["MachineDB"] = relationship(back_populates="user")

class MachineDB(Base):
    __tablename__ = "machines"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)
    fly_machine_id: Mapped[str] = mapped_column(nullable=True)
    status: Mapped[MachineStatus] = mapped_column(
        Enum(MachineStatus),
        nullable=False,
        default=MachineStatus.starting,
        server_default="starting"
    )
    machine_secret: Mapped[str] = mapped_column(nullable=True, unique=True)

    last_started_at: Mapped[datetime] = mapped_column(nullable=True)
    # Last time we saw a live WebSocket (agent/terminal/control) proxied for
    # this machine — refreshed on connect, on a periodic heartbeat while any
    # such connection stays open, and on disconnect. Used by the background
    # enforcer to detect "the workspace tab was closed" without depending on
    # any single backend process's in-memory state (see main.py).
    last_active_at: Mapped[datetime] = mapped_column(nullable=True)
    daily_usage_seconds: Mapped[int] = mapped_column(default=0, server_default="0")
    total_usage_seconds: Mapped[int] = mapped_column(default=0, server_default="0")
    last_usage_date: Mapped[date] = mapped_column(nullable=True)

    user: Mapped["UserDB"] = relationship(back_populates="machine")


# --END OF FILE--