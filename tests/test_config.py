from configuration.validate import validate_file
def test_defaults_valid(): validate_file("configuration/defaults.json")


def test_serverless_database_connections_are_not_reused(monkeypatch):
    from backend.db import engine_options
    from sqlalchemy.pool import NullPool
    monkeypatch.setenv("VERCEL", "1")
    options = engine_options("postgresql+asyncpg://example")
    assert options["poolclass"] is NullPool
    assert options["connect_args"]["prepared_statement_cache_size"] == 0
    assert "poolclass" not in engine_options("sqlite+aiosqlite://")
    monkeypatch.delenv("VERCEL")
    assert engine_options("postgresql+asyncpg://example")["pool_pre_ping"]
