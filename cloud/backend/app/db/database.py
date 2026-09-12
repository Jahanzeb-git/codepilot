"""
database.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Description: This file handles Database connection using Async SQLAlchemy and expose session dependency.
Licensed: MIT
"""
# --IMPORTS--
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from typing import AsyncIterator
import os
from dotenv import load_dotenv

# --CONFIG--
load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("Database URL not found.")

# Ensure asyncpg scheme
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# Strip incompatible query parameters that conflict with connect_args={"ssl": True}
import urllib.parse as urlparse
from urllib.parse import urlencode

url_parts = list(urlparse.urlparse(DATABASE_URL))
query = dict(urlparse.parse_qsl(url_parts[4]))
query.pop("sslmode", None)
query.pop("channel_binding", None)
url_parts[4] = urlencode(query)
DATABASE_URL = urlparse.urlunparse(url_parts)

# --ENGINE--
_async_engine = create_async_engine(
    DATABASE_URL,
    connect_args = {"ssl": True},
    pool_pre_ping = True,
    pool_recycle = 300
)

# --SESSION FACTORY--
SessionFactory = async_sessionmaker(
    _async_engine,
    expire_on_commit=False # Crucial for async: prevents SQLAlchemy from trying to lazily fetch attributes after a commit, which would cause a MissingGreenlet error.
)

# --BASE CLASS--
class Base(DeclarativeBase):
    pass

# --SESSION DEPENDENCY--
async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Asynchronously yield a database session"""
    try:
        db: AsyncSession = SessionFactory()
        yield db
    except:
        await db.rollback()
        raise # re-raise the exception to caller.
    finally:
        await db.close() # close the connection.

# --END OF FILE--