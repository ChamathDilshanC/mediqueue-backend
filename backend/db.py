"""Async SQLAlchemy engine, session dependency and declarative base."""
from collections.abc import AsyncGenerator
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import event, inspect, text
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
        database = engine.url.database
        cursor.execute("PRAGMA foreign_keys=ON")
        for schema in ("iam", "queue", "notifications", "scheduling"):
            # Disk-backed namespaces survive reconnects and process restarts.
            filename = ":memory:" if database in (None, "", ":memory:") else str(Path(database).resolve()) + f".{schema}.db"
            cursor.execute(f"ATTACH DATABASE ? AS {schema}", (filename,))
        cursor.close()
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


def ensure_sqlite_discovery_columns(connection):
    """Add the discovery columns to existing local SQLite sidecars, without data loss."""
    if connection.dialect.name != "sqlite":
        return
    additions = {
        ("scheduling", "appointment"): {"source": "VARCHAR(20) NOT NULL DEFAULT 'STAFF'", "review_reason": "VARCHAR(500) NOT NULL DEFAULT ''", "reviewed_by": "VARCHAR(200)", "reviewed_at": "DATETIME"},
        ("iam", "branch"): {"address": "VARCHAR(500) NOT NULL DEFAULT ''", "phone": "VARCHAR(40) NOT NULL DEFAULT ''", "latitude": "FLOAT", "longitude": "FLOAT"},
        ("queue", "queue"): {"average_service_minutes": "INTEGER NOT NULL DEFAULT 5"},
    }
    inspector = inspect(connection)
    for (schema, table), columns in additions.items():
        if not inspector.has_table(table, schema=schema):
            continue
        existing = {column["name"] for column in inspector.get_columns(table, schema=schema)}
        for name, sql_type in columns.items():
            if name not in existing:
                connection.execute(text(f'ALTER TABLE {schema}."{table}" ADD COLUMN {name} {sql_type}'))

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async transaction session for a request."""
    async with SessionLocal() as session:
        yield session
