from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from forwin.models import Base
from tests.postgres import postgres_empty_test_url


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_REVISION = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()


def _alembic_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _database_state(database_url: str) -> tuple[set[str], str]:
    engine = create_engine(database_url, future=True)
    try:
        tables = set(inspect(engine).get_table_names())
        revision = ""
        if "alembic_version" in tables:
            with engine.connect() as connection:
                revision = str(
                    connection.scalar(text("SELECT version_num FROM alembic_version"))
                    or ""
                )
        return tables, revision
    finally:
        engine.dispose()


def test_fresh_postgres_upgrade_downgrade_upgrade_cycle() -> None:
    database_url = postgres_empty_test_url("v5-live-migration")
    config = _alembic_config(database_url)

    command.upgrade(config, "head")
    upgraded_tables, upgraded_revision = _database_state(database_url)
    assert set(Base.metadata.tables) <= upgraded_tables
    assert upgraded_revision == EXPECTED_REVISION

    command.downgrade(config, "base")
    downgraded_tables, downgraded_revision = _database_state(database_url)
    assert downgraded_tables <= {"alembic_version"}
    assert downgraded_revision == ""

    command.upgrade(config, "head")
    restored_tables, restored_revision = _database_state(database_url)
    assert restored_tables == upgraded_tables
    assert restored_revision == EXPECTED_REVISION


def test_migration_preserves_application_logger_availability() -> None:
    import logging

    logger = logging.getLogger("forwin.migration_observability_probe")
    previous_disabled = logger.disabled
    logger.disabled = False
    try:
        url = postgres_empty_test_url("migration-logging")
        command.upgrade(_alembic_config(url), "head")
        assert logger.disabled is False
    finally:
        logger.disabled = previous_disabled
