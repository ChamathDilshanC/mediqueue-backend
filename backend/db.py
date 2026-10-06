"""Async SQLAlchemy engine, session dependency and declarative base."""
from collections.abc import AsyncGenerator
from pathlib import Path
import os
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import event, inspect, text
from sqlalchemy.orm import DeclarativeBase
from .settings import get_settings

class Base(DeclarativeBase):
    """Base class for all owned database tables."""


# Latest Alembic revision this code requires. PostgreSQL schemas are changed only by
# `alembic upgrade head`; /health/ready reports a database that has not been migrated.
SCHEMA_REVISION = "0017_payment_integrity"

def engine_options(database_url: str) -> dict:
    """Avoid stale sockets and cross-request connection reuse in serverless runtimes."""
    options = {"future": True, "pool_pre_ping": True}
    if database_url.startswith("postgresql+asyncpg") and os.getenv("VERCEL"):
        options["poolclass"] = NullPool
        options["connect_args"] = {"timeout": 10, "prepared_statement_cache_size": 0}
    return options


engine = create_async_engine(get_settings().database_url, **engine_options(get_settings().database_url))
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
        ("scheduling", "appointment"): {"source": "VARCHAR(20) NOT NULL DEFAULT 'STAFF'", "review_reason": "VARCHAR(500) NOT NULL DEFAULT ''", "reviewed_by": "VARCHAR(200)", "reviewed_at": "DATETIME", "quotation": "TEXT NOT NULL DEFAULT '[]'", "payment_method": "VARCHAR(30)", "payment_status": "VARCHAR(20) NOT NULL DEFAULT 'UNPAID'", "payment_reference": "VARCHAR(200)",
                                    "checkout_session_id": "VARCHAR(255)", "payment_amount": "INTEGER"},
        ("scheduling", "room"): {"ward_id": "TEXT"},
        ("scheduling", "bed"): {"room_id": "TEXT"},
        ("scheduling", "doctor"): {"quotation_template": "TEXT NOT NULL DEFAULT '[]'"},
        ("iam", "branch"): {"address": "VARCHAR(500) NOT NULL DEFAULT ''", "phone": "VARCHAR(40) NOT NULL DEFAULT ''", "latitude": "FLOAT", "longitude": "FLOAT"},
        ("queue", "queue"): {"average_service_minutes": "INTEGER NOT NULL DEFAULT 5"},
        ("notifications", "outbox_event"): {"dead_lettered_at": "DATETIME"},
    }
    attached = {row[1] for row in connection.exec_driver_sql("PRAGMA database_list")}
    inspector = inspect(connection)
    for (schema, table), columns in additions.items():
        if schema not in attached or not inspector.has_table(table, schema=schema):
            continue
        existing = {column["name"] for column in inspector.get_columns(table, schema=schema)}
        for name, sql_type in columns.items():
            if name not in existing:
                connection.execute(text(f'ALTER TABLE {schema}."{table}" ADD COLUMN {name} {sql_type}'))
    if "notifications" not in attached:
        return
    connection.execute(text(
        'CREATE TABLE IF NOT EXISTS notifications."stripe_webhook_event" '
        '(event_id VARCHAR(255) PRIMARY KEY, event_type VARCHAR(120) NOT NULL, '
        'received_at DATETIME DEFAULT CURRENT_TIMESTAMP)'
    ))

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async transaction session for a request."""
    async with SessionLocal() as session:
        yield session
