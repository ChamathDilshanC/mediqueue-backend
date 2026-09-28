"""Async SQLAlchemy engine, session dependency and declarative base."""
from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import event
from sqlalchemy.orm import DeclarativeBase
from .settings import get_settings

class Base(DeclarativeBase):
    """Base class for all owned database tables."""

engine = create_async_engine(get_settings().database_url, future=True)
if get_settings().database_url.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _attach_sqlite_schemas(dbapi_connection, _connection_record):
        """Emulate PostgreSQL namespaces for local SQLite connections."""
        cursor = dbapi_connection.cursor()
        for schema in ("iam", "queue", "notifications"):
            cursor.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cursor.close()
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async transaction session for a request."""
    async with SessionLocal() as session:
        yield session
