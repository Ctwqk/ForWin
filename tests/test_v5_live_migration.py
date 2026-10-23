from __future__ import annotations

import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from forwin.book_state.repository import BookStateRepository
from forwin.http.project_support import _delete_project
from forwin.models import Base, Project
from forwin.models.base import get_session_factory
from forwin.runtime.policy import RuntimePolicy
from tests.postgres import postgres_empty_test_url


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_REVISION = "0006_planning_v6"
PARALLEL_LEDGER_TABLES = {"character_state_transitions", "countdown_ledgers"}

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


def test_upgraded_columns_match_orm_metadata() -> None:
    database_url = postgres_empty_test_url("v5-column-drift")
    command.upgrade(_alembic_config(database_url), "head")

    engine = create_engine(database_url, future=True)
    try:
        inspector = inspect(engine)
        database_tables = set(inspector.get_table_names())
        drift: list[str] = []
        for table in Base.metadata.sorted_tables:
            if table.name not in database_tables:
                drift.append(f"{table.name}: table missing from database")
                continue
            database_columns = {
                column["name"]: column["nullable"]
                for column in inspector.get_columns(table.name)
            }
            orm_columns = {column.name: column.nullable for column in table.columns}
            if database_columns == orm_columns:
                continue
            extra = sorted(set(database_columns) - set(orm_columns))
            missing = sorted(set(orm_columns) - set(database_columns))
            nullability = sorted(
                name
                for name in set(database_columns) & set(orm_columns)
                if database_columns[name] != orm_columns[name]
            )
            drift.append(
                f"{table.name}: extra in database={extra}, "
                f"missing from database={missing}, nullability differs={nullability}"
            )
        assert drift == []
    finally:
        engine.dispose()


def _seed_populated_0002(connection, policies: dict[str, str]) -> None:
    """Rows written by 0002-era code: projects with stored policies and one node with a state row."""
    for project_id, policy_json in policies.items():
        connection.execute(
            text(
                "INSERT INTO projects (id, title, premise, genre, setting_summary, creation_status, "
                "active_genesis_revision_id, automation_json, runtime_policy_json, runtime_policy_version, "
                "created_at, updated_at) VALUES (:id, 't', 'p', 'g', '', 'writing', '', '{}', :policy, 1, now(), now())"
            ),
            {"id": project_id, "policy": policy_json},
        )
    connection.execute(
        text(
            "INSERT INTO world_nodes (id, project_id, node_type, name, aliases_json, summary, description, "
            "status, importance, scope, tags_json, created_at_chapter, valid_from_chapter, source_refs_json, "
            "is_active, profile_json, metadata_json, created_at, updated_at) VALUES ('char-1', 'p-layers', "
            "'character', 'Alice', '[\"Al\"]', '', '', 'active', 3, 'book', '[]', 0, 0, '[]', false, "
            "'{\"role\": \"lead\"}', '{}', now(), now())"
        )
    )
    connection.execute(
        text(
            "INSERT INTO world_node_states (id, project_id, node_id, node_type, as_of_chapter, "
            "as_of_story_time, state_json, source_delta_id, created_at) VALUES ('state-1', 'p-layers', "
            "'char-1', 'character', 0, '', '{\"location_id\": \"node-1\"}', 'delta-1', now())"
        )
    )


def test_populated_0002_upgrade_keeps_node_identity_and_loadable_policies() -> None:
    database_url = postgres_empty_test_url("v5-populated-0003")
    config = _alembic_config(database_url)
    command.upgrade(config, "0002_drop_dead_tables")
    layered = RuntimePolicy.for_profile("standard").model_dump(mode="json")
    layered["canon"]["book_state_layers"] = ["world", "map", "cognition", "narrative"]
    untouched = {
        "p-empty": "",
        "p-not-json": "not json",
        "p-list": "[1, 2]",
        "p-no-canon": json.dumps({"schema_version": 2}),
    }

    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            _seed_populated_0002(connection, {"p-layers": json.dumps(layered), **untouched})

        command.upgrade(config, "head")

        with Session(engine) as session:
            graph = BookStateRepository(session).load_base_world_graph("p-layers", as_of_chapter=0)
            node = graph.nodes_by_id["char-1"]
            assert (node.name, node.aliases, node.profile, node.is_active) == ("Alice", ["Al"], {"role": "lead"}, False)
            assert graph.get_state("char-1") == {"location_id": "node-1"}
            stored = dict(session.execute(text("SELECT id, runtime_policy_json FROM projects")).all())
        expected = json.loads(json.dumps(layered))
        del expected["canon"]["book_state_layers"]
        assert json.loads(stored["p-layers"]) == expected
        RuntimePolicy.model_validate_json(stored["p-layers"])
        assert {key: stored[key] for key in untouched} == untouched

        command.downgrade(config, "0002_drop_dead_tables")
        assert _database_state(database_url)[1] == "0002_drop_dead_tables"
    finally:
        engine.dispose()


def test_parallel_ledger_tables_are_gone() -> None:
    assert PARALLEL_LEDGER_TABLES.isdisjoint(Base.metadata.tables)

    database_url = postgres_empty_test_url("v5-parallel-ledgers")
    config = _alembic_config(database_url)
    command.upgrade(config, "0003_book_state_v6")
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO projects (id, title, premise, genre, setting_summary, creation_status, "
                    "active_genesis_revision_id, automation_json, runtime_policy_json, runtime_policy_version, "
                    "created_at, updated_at) VALUES ('p-ledgers', 't', 'p', 'g', '', 'writing', '', '{}', '{}', 1, "
                    "now(), now())"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO countdown_ledgers (id, project_id, countdown_key, label, chapter_number, "
                    "normalized_remaining_minutes, raw_mention, is_reset_event, is_branch_clock, is_resolution_event, "
                    "status, evidence_refs_json, payload_json, created_at) VALUES ('row-1', 'p-ledgers', 'k', 'k', 1, "
                    "30, '', 'false', 'false', 'false', 'consistent', '[]', '{}', now())"
                )
            )

        command.upgrade(config, "head")
        tables, revision = _database_state(database_url)
        assert revision == EXPECTED_REVISION
        assert PARALLEL_LEDGER_TABLES.isdisjoint(tables)

        command.downgrade(config, "0003_book_state_v6")
        tables, revision = _database_state(database_url)
        assert revision == "0003_book_state_v6"
        assert PARALLEL_LEDGER_TABLES <= tables
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM countdown_ledgers")) == 0
    finally:
        engine.dispose()
