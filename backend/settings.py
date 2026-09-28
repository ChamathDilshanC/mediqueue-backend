"""Typed environment configuration for the API and worker."""
from functools import lru_cache
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite+aiosqlite:///./mediqueue.db"
    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    supabase_jwt_secret: str | None = None
    supabase_jwks_url: str | None = None
    auth_dev_header_enabled: bool = False
    dev_tenant_id: str = "00000000-0000-0000-0000-000000000001"
    dev_branch_id: str = "00000000-0000-0000-0000-000000000002"
    config_path: str = "configuration/defaults.json"

    @field_validator("database_url")
    @classmethod
    def use_async_postgres_driver(cls, value: str) -> str:
        """Ensure PostgreSQL URLs use the async driver required by SQLAlchemy."""
        if not value.startswith(("postgresql://", "postgresql+psycopg://", "postgresql+asyncpg://")):
            return value
        url = make_url(value)
        url = url.set(drivername="postgresql+asyncpg")
        if "sslmode" in url.query and "ssl" not in url.query:
            url = url.update_query_dict({"ssl": url.query["sslmode"]}).difference_update_query(["sslmode"])
        if "ssl" not in url.query:
            url = url.update_query_dict({"ssl": "require"})
        return url.render_as_string(hide_password=False)

@lru_cache
def get_settings() -> Settings:
    """Return the process-wide immutable settings object."""
    return Settings()
