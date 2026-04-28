from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def new_id() -> str:
    """Generate a new UUID4 hex string."""
    return uuid4().hex


def _normalize_database_url(database_url: str) -> str:
    normalized = str(database_url or "").strip()
    if not normalized:
        raise ValueError("FORWIN_DATABASE_URL is required and must use postgresql+psycopg://")
    legacy_sqlite_prefix = "sqlite" + ":"
    legacy_memory_url = ":" + "memory" + ":"
    if (
        normalized.startswith(legacy_sqlite_prefix)
        or normalized == legacy_memory_url
        or normalized.endswith(".db")
    ):
        raise ValueError(
            "SQLite database paths are no longer supported. Set FORWIN_DATABASE_URL "
            "to a postgresql+psycopg:// URL."
        )
    parsed = make_url(normalized)
    if parsed.drivername != "postgresql+psycopg":
        raise ValueError("ForWin requires a postgresql+psycopg:// database URL.")
    return normalized


def get_engine(database_url: str) -> Engine:
    """Create a Postgres engine for the ForWin primary state database."""
    return create_engine(
        _normalize_database_url(database_url),
        pool_pre_ping=True,
        future=True,
    )


def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create and return a sessionmaker bound to the given engine."""
    return sessionmaker(bind=engine, expire_on_commit=False)


def alembic_config(database_url: str) -> AlembicConfig:
    """Build an Alembic config without relying on a cwd-specific ini file."""
    root = Path(__file__).resolve().parents[2]
    ini_path = root / "alembic.ini"
    config = AlembicConfig(str(ini_path)) if ini_path.exists() else AlembicConfig()
    config.set_main_option("script_location", "forwin:migrations")
    config.set_main_option("sqlalchemy.url", _normalize_database_url(database_url))
    return config


def init_db(engine: Engine) -> None:
    """Upgrade the Postgres schema to the latest Alembic revision."""
    url = str(engine.url.render_as_string(hide_password=False))
    command.upgrade(alembic_config(url), "head")
