from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class SchemaRevisionMismatchError(RuntimeError):
    code = "FORWIN_SCHEMA_REVISION_MISMATCH"

    def __init__(self, *, current_revision: str, expected_revision: str) -> None:
        self.current_revision = current_revision
        self.expected_revision = expected_revision
        super().__init__(
            f"[{self.code}] ForWin database schema is "
            f"{current_revision or 'unstamped'}, expected {expected_revision}. "
            "Back up the database, verify its migration history, and apply the "
            "supported forward migrations with `python -m forwin.migrations`. "
            "Do not recreate an existing database."
        )


def new_id() -> str:
    return uuid4().hex


def _coerce_postgres_url(database_url: str) -> str:
    value = str(database_url or "").strip()
    if not value or value == ":memory:" or "://" not in value:
        test_url = os.environ.get("FORWIN_TEST_DATABASE_URL", "").strip()
        if test_url:
            value = test_url
    try:
        url = make_url(value)
    except Exception as exc:
        raise ValueError(
            "ForWin requires FORWIN_DATABASE_URL to be a PostgreSQL SQLAlchemy URL."
        ) from exc
    if url.get_backend_name() != "postgresql":
        raise ValueError(
            "ForWin no longer supports SQLite as a runtime database. "
            "Set FORWIN_DATABASE_URL to a PostgreSQL URL such as "
            "postgresql+psycopg://forwin:forwin@localhost:5432/forwin."
        )
    if url.drivername == "postgresql":
        return url.set(drivername="postgresql+psycopg").render_as_string(
            hide_password=False
        )
    return value


def get_engine(database_url: str) -> Engine:
    resolved_url = _coerce_postgres_url(database_url)
    connect_timeout = int(
        os.environ.get("FORWIN_POSTGRES_CONNECT_TIMEOUT_SECONDS", "10")
    )
    pool_size = int(os.environ.get("FORWIN_POSTGRES_POOL_SIZE", "5"))
    max_overflow = int(os.environ.get("FORWIN_POSTGRES_MAX_OVERFLOW", "10"))
    return create_engine(
        resolved_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        connect_args={"connect_timeout": connect_timeout},
    )


def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def alembic_config(database_url: str) -> AlembicConfig:
    root = Path(__file__).resolve().parents[2]
    ini_path = root / "alembic.ini"
    config = AlembicConfig(str(ini_path)) if ini_path.exists() else AlembicConfig()
    config.set_main_option("script_location", "forwin:migrations")
    config.set_main_option("sqlalchemy.url", _coerce_postgres_url(database_url))
    return config


def run_migrations(database_url: str) -> None:
    """Explicit deployment operation; application startup never mutates schema."""
    engine = get_engine(database_url)
    try:
        # One transaction covers the legacy bridge and the current chain. A
        # later identity preflight refusal also rolls back the bridge changes.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "SELECT pg_advisory_xact_lock(hashtext('forwin:schema-migration'))"
                )
            )
            tables = set(inspect(connection).get_table_names())
            revisions = []
            if "alembic_version" in tables:
                revisions = list(
                    connection.scalars(text("SELECT version_num FROM alembic_version"))
                )
            if not revisions and tables - {"alembic_version"}:
                raise RuntimeError(
                    "Existing unstamped database requires migration-history inspection; refusing to create or stamp over it."
                )
            config = alembic_config(database_url)
            config.attributes["connection"] = connection
            if revisions == ["0001_v5_baseline"]:
                legacy = alembic_config(database_url)
                legacy.set_main_option("script_location", "forwin:legacy_migrations")
                legacy.attributes["connection"] = connection
                alembic_command.upgrade(legacy, "0001_v5_recovery")
            alembic_command.upgrade(config, "head")
    finally:
        engine.dispose()


def require_v5_schema(engine: Engine) -> None:
    """Refuse startup unless the database is stamped at the current baseline."""
    if engine.dialect.name != "postgresql":
        raise ValueError("ForWin requires a PostgreSQL engine.")
    expected = ScriptDirectory.from_config(
        alembic_config(str(engine.url.render_as_string(hide_password=False)))
    ).get_current_head()
    if not expected:
        raise RuntimeError("ForWin migration head is missing.")
    if not inspect(engine).has_table("alembic_version"):
        raise RuntimeError(
            "ForWin database has no v5 schema marker. Back up the database and "
            "inspect its migration history before applying a supported forward "
            "migration. Do not recreate or blindly stamp an existing database."
        )
    with engine.connect() as conn:
        current = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
    if current != expected:
        raise SchemaRevisionMismatchError(
            current_revision=str(current or ""),
            expected_revision=str(expected),
        )


def init_db(engine: Engine) -> None:
    """Create an isolated test schema from current metadata.

    Production code must call :func:`require_v5_schema`; this helper is retained
    for disposable test databases only.
    """
    if engine.dialect.name != "postgresql":
        raise ValueError("ForWin requires a PostgreSQL engine.")
    from forwin import models as _models  # noqa: F401

    Base.metadata.create_all(engine)
    _stamp_test_schema(engine)


def _stamp_test_schema(engine: Engine) -> None:
    database_url = str(engine.url.render_as_string(hide_password=False))
    head = ScriptDirectory.from_config(alembic_config(database_url)).get_current_head()
    if not head:
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS alembic_version (
                    version_num VARCHAR(32) NOT NULL PRIMARY KEY
                )
                """
            )
        )
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(
            text("INSERT INTO alembic_version(version_num) VALUES (:version)"),
            {"version": head},
        )


__all__ = [
    "Base",
    "alembic_config",
    "get_engine",
    "get_session_factory",
    "init_db",
    "new_id",
    "require_v5_schema",
    "run_migrations",
]
