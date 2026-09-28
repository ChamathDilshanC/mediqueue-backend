"""Alembic migration environment for MediQueue-owned schemas."""

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

from backend import models
from backend.db import Base
from backend.settings import get_settings

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    """Run migrations against the configured PostgreSQL database."""
    database_url = make_url(get_settings().database_url).set(
        drivername="postgresql+psycopg"
    )
    ssl = database_url.query.get("ssl")
    database_url = database_url.difference_update_query(["ssl"])
    if ssl:
        database_url = database_url.update_query_dict({"sslmode": "require"})
    connectable = create_engine(database_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
