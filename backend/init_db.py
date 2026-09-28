"""Create a local SQLite database: python -m backend.init_db."""
import asyncio
from . import models
from .db import Base, engine


async def main():
    if engine.dialect.name != "sqlite":
        raise SystemExit("Use alembic upgrade head for PostgreSQL")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    print("Local SQLite schemas initialized.")


if __name__ == "__main__":
    asyncio.run(main())
