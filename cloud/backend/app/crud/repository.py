"""
repository.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Description: This file handles utilities for authentication and authorization.
Licensed: MIT
"""
# --IMPORTS--
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select, update
from models.models import UserDB, MachineDB
from fastapi import HTTPException 
from core.security import verify_pass
from datetime import datetime, timezone, time, timedelta

# --UTILITIES--
async def create_user(db: AsyncSession, email: str, hashed_password: str):
    """Create user and register in database"""
    new_user = UserDB(email=email, hashed_password=hashed_password)
    db.add(new_user)
    try:
        await db.commit()
        await db.refresh(new_user)
        return new_user
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="User already exists")


async def get_user_by_email(db: AsyncSession, email: str):
    """Get current user by email from database"""
    query = select(UserDB).where(UserDB.email == email)
    result = await db.execute(query)
    return result.scalar_one_or_none()

async def is_exists(db: AsyncSession, email: str, password: str) -> bool:
    """Check if user exists in database and password is correct"""
    query = select(UserDB).where(UserDB.email == email)
    exec = await db.execute(query)
    result = exec.scalar_one_or_none()
    if result and verify_pass(password, result.hashed_password):
        return True
    return False

async def add_machine(db: AsyncSession, machine_id: str, id: int, status: str, machine_secret: str) -> bool: 
    try:
        new_machine = MachineDB(user_id=id, fly_machine_id=machine_id, status=status, machine_secret=machine_secret)
        db.add(new_machine)
        await db.commit()
        await db.refresh(new_machine)
        return new_machine
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Machine already exists"
        )

async def is_machine_exists(db: AsyncSession, id: int):
    query = select(MachineDB).where(MachineDB.user_id == id)
    exec = await db.execute(query)
    machine = exec.scalar_one_or_none()
    if not machine:
        return None
    return machine

async def get_machine_by_secret(db: AsyncSession, x_machine_secret: str):
    # Guard against empty or missing secret (e.g. from a tombstoned/deleted machine)
    if not x_machine_secret:
        return None
    query = select(MachineDB).where(MachineDB.machine_secret == x_machine_secret)
    exec = await db.execute(query)
    machine = exec.scalar_one_or_none()
    if not machine:
        return None
    return machine
    
async def delete_machine_record(db: AsyncSession, machine: MachineDB):
    """Soft-delete a machine record by clearing the Fly machine ID and secret.

    The DB row is intentionally PRESERVED so that quota counters
    (daily_usage_seconds, total_usage_seconds) survive machine deletion.
    This prevents the delete-and-recreate quota bypass: a user cannot
    reset their usage by deleting their machine and launching a new one.

    The next /launch call will detect the tombstoned row (fly_machine_id=None)
    and reuse it with a fresh machine ID and secret.
    """
    machine.fly_machine_id = None
    import secrets
    machine.machine_secret = f"deleted_{secrets.token_urlsafe(16)}"
    machine.status = "stopped"
    machine.last_started_at = None
    await db.commit()

DAILY_LIMIT_SECONDS = 10800   # 3 hours
TOTAL_LIMIT_SECONDS = 324000  # 90 hours
IDLE_TIMEOUT_SECONDS = 120    # no WS activity for this long => treat tab as closed

async def reconcile_usage(db: AsyncSession, machine: MachineDB) -> None:
    """
    Bring daily_usage_seconds/total_usage_seconds/last_started_at up to date
    as of *now*, correctly handling a running session that spans a UTC day
    boundary. This MUST be the only place daily_usage_seconds is ever reset,
    and it must be called before every single quota comparison anywhere in
    the app (launch, status, ws/status, the background enforcer).

    Why this matters: the previous version just did
        if machine.last_usage_date != today: daily_usage_seconds = 0
    which is exploitable. If a session is *actively running* when the UTC
    date rolls over, last_started_at is untouched by that reset — so
    live_usage = now - last_started_at keeps counting from before midnight,
    but gets compared against a freshly-zeroed daily budget. Starting a
    session in the last minute before UTC midnight was enough to stack a
    full second 3-hour allowance onto one continuous session, forever,
    every single day.

    Fix: when a day boundary is crossed under an active session, we credit
    the pre-boundary elapsed time to total_usage_seconds (so the 90-hour
    lifetime cap stays exactly correct — nothing is lost or double-counted)
    and then re-baseline last_started_at to the boundary itself. Everything
    computed after this point is measured from *today's* start, so the
    fresh daily budget can never be stacked on top of unaccounted-for prior
    usage. A loop handles the (practically rare, but possible if the
    enforcer was ever down) case of multiple boundaries being crossed at
    once.
    """
    today = datetime.now(timezone.utc).date()
    if machine.last_usage_date == today:
        return

    if machine.last_started_at is not None:
        cursor = machine.last_started_at
        while cursor.date() < today:
            boundary = datetime.combine(cursor.date() + timedelta(days=1), time.min)
            elapsed = int((boundary - cursor).total_seconds())
            if elapsed > 0:
                machine.total_usage_seconds += elapsed
            cursor = boundary
        machine.last_started_at = cursor

    machine.daily_usage_seconds = 0
    machine.last_usage_date = today
    await db.commit()


async def update_usage_on_suspend(db: AsyncSession, machine: MachineDB):
    """Calculate elapsed time since last_started_at and add to accumulators."""
    if machine.last_started_at:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        elapsed = int((now - machine.last_started_at).total_seconds())
        if elapsed > 0:
            machine.daily_usage_seconds += elapsed
            machine.total_usage_seconds += elapsed
        machine.last_started_at = None
        await db.commit()

async def get_running_machines(db: AsyncSession):
    """Get all machines that are currently marked as running (last_started_at is not None)."""
    query = select(MachineDB).where(MachineDB.last_started_at.isnot(None))
    result = await db.execute(query)
    return result.scalars().all()

async def touch_machine_activity(db: AsyncSession, fly_machine_id: str) -> None:
    """
    Cheap, race-safe "I'm still here" heartbeat for a live WS proxy
    connection. Uses a bare UPDATE (no SELECT/ORM load) so many concurrent
    WS connections for the same machine (agent socket, several terminal
    sockets, control socket) can all call this without stepping on each
    other or needing to hold a loaded ORM object for the connection's
    entire lifetime.
    """
    if not fly_machine_id:
        return
    await db.execute(
        update(MachineDB)
        .where(MachineDB.fly_machine_id == fly_machine_id)
        .values(last_active_at=datetime.now(timezone.utc).replace(tzinfo=None))
    )
    await db.commit()

async def get_idle_running_machines(db: AsyncSession):
    """
    Machines that are marked running but have had no WS activity in over
    IDLE_TIMEOUT_SECONDS — our signal that the workspace tab was closed
    (or the connection died) without a clean disconnect event ever
    reaching any single backend process. Machines that were just started
    and have never had a WS connect yet (last_active_at is NULL) are
    included too, using last_started_at as the baseline, so "launch and
    never actually open the workspace" also gets caught instead of running
    forever.
    """
    query = select(MachineDB).where(MachineDB.last_started_at.isnot(None))
    result = await db.execute(query)
    machines = result.scalars().all()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    idle = []
    for m in machines:
        baseline = m.last_active_at or m.last_started_at
        if baseline and (now - baseline).total_seconds() >= IDLE_TIMEOUT_SECONDS:
            idle.append(m)
    return idle

# --END OF FILE--