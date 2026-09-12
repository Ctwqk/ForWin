from __future__ import annotations

from pathlib import Path

import pytest

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from forwin.models import Base, Project
from forwin.models.projection import ProjectionCheckpoint
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


def test_fresh_postgres_upgrade_is_idempotent_and_refuses_provenance_loss() -> None:
    database_url = postgres_empty_test_url("v5-live-migration")
    config = _alembic_config(database_url)

    command.upgrade(config, "head")
    upgraded_tables, upgraded_revision = _database_state(database_url)
    assert set(Base.metadata.tables) <= upgraded_tables
    assert upgraded_revision == EXPECTED_REVISION

    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            project = Project(title="Preserved provenance", premise="Forward migrations")
            session.add(project)
            session.flush()
            session.add(ProjectionCheckpoint(project_id=project.id, projection_kind="chapter_memory", target_book_revision=0, projected_book_revision=0, source_digest="preserved-digest", status="healthy"))
            session.commit()
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO embedding_cache_entries (input_hash, embedding_identity, dimensions, vector_json) VALUES (:input, :identity, 2, '[0.0, 1.0]')"), {"input": "a" * 64, "identity": "b" * 64})
        with engine.connect() as connection:
            before_checkpoint = dict(connection.execute(text("SELECT * FROM projection_checkpoints")).mappings().one())
        with pytest.raises(RuntimeError, match="Cannot discard"):
            command.downgrade(config, "base")
        assert _database_state(database_url) == (upgraded_tables, upgraded_revision)
        command.upgrade(config, "head")
        assert _database_state(database_url) == (upgraded_tables, EXPECTED_REVISION)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT vector_json FROM embedding_cache_entries")) == "[0.0, 1.0]"
            assert dict(connection.execute(text("SELECT * FROM projection_checkpoints")).mappings().one()) == before_checkpoint
    finally:
        engine.dispose()


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
