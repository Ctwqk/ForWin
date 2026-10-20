from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from forwin.http.project_support import _delete_project
from forwin.models import Base, Project
from forwin.models.base import get_session_factory
from tests.postgres import postgres_empty_test_url


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_REVISION = "0002_drop_dead_tables"

# Tables that exist only in the production database (left over from v4 and
# the scenario rehearsal work, never part of 0001). Each maps to the foreign
# keys it carries there (2026-10-19 backup dump); none cascade.
PRODUCTION_LEGACY_TABLE_FKS = {
    "beliefs": {"project_id": "projects"},
    "cognition_snapshots": {"project_id": "projects"},
    "knowledge_gaps": {"project_id": "projects"},
    "knowledge_update_events": {"project_id": "projects"},
    "npc_intent_snapshots": {"project_id": "projects", "entity_id": "entities"},
    "provisional_band_executions": {
        "project_id": "projects",
        "arc_id": "arc_plan_versions",
    },
    "provisional_chapter_ledgers": {
        "project_id": "projects",
        "arc_id": "arc_plan_versions",
    },
    "reader_experience_deltas": {"project_id": "projects"},
    "reveal_events": {"project_id": "projects"},
    "scenario_rehearsal_runs": {"project_id": "projects"},
    "scenario_plan_patches": {
        "project_id": "projects",
        "run_id": "scenario_rehearsal_runs",
    },
    "world_compile_runs_v4": {"project_id": "projects"},
    "world_deltas": {"project_id": "projects"},
    "world_lines": {"project_id": "projects"},
    "world_model_snapshots_v4": {"project_id": "projects"},
}


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


def _create_production_legacy_tables(connection) -> None:
    # Dict order creates scenario_rehearsal_runs before scenario_plan_patches.
    for table, fks in PRODUCTION_LEGACY_TABLE_FKS.items():
        columns = ["id varchar PRIMARY KEY"] + [
            f"{column} varchar REFERENCES {target}(id)"
            for column, target in fks.items()
        ]
        connection.execute(text(f"CREATE TABLE {table} ({', '.join(columns)})"))


def test_upgrade_drops_production_legacy_tables_so_projects_can_be_deleted() -> None:
    database_url = postgres_empty_test_url("v5-legacy-tables")
    config = _alembic_config(database_url)
    command.upgrade(config, "0001_v5_recovery")

    engine = create_engine(database_url, future=True)
    try:
        session_factory = get_session_factory(engine)
        with session_factory.begin() as session:
            session.add(
                Project(
                    id="legacy-project",
                    title="遗留表",
                    premise="测试 premise",
                    genre="玄幻",
                    setting_summary="",
                )
            )
        with engine.begin() as connection:
            _create_production_legacy_tables(connection)
            connection.execute(
                text(
                    "INSERT INTO scenario_rehearsal_runs (id, project_id) "
                    "VALUES ('run-1', 'legacy-project')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO scenario_plan_patches (id, project_id, run_id) "
                    "VALUES ('patch-1', 'legacy-project', 'run-1')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO world_lines (id, project_id) "
                    "VALUES ('line-1', 'legacy-project')"
                )
            )

        command.upgrade(config, "head")

        tables, revision = _database_state(database_url)
        assert revision == EXPECTED_REVISION
        assert sorted(set(PRODUCTION_LEGACY_TABLE_FKS) & tables) == []
        assert set(Base.metadata.tables) <= tables

        with session_factory.begin() as session:
            _delete_project(session, "legacy-project")
        with session_factory() as session:
            assert session.query(Project).count() == 0
    finally:
        engine.dispose()
