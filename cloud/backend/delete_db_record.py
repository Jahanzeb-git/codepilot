import asyncio
import os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

async def main():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url: return print("No DB URL")
    db_url = db_url.replace("postgres://", "postgresql+asyncpg://").split("?")[0]
    engine = create_async_engine(db_url, connect_args={"ssl": True})
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM machines WHERE fly_machine_id = '7845705c202ee8'"))
    print("Deleted from DB")

asyncio.run(main())
