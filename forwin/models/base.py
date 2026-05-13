from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


POSTGRES_BASELINE_MIGRATIONS = (
    "entity_alias_index_v1",
    "phase3_analysis_v1",
    "generation_tasks_v1",
    "generation_task_pause_v1",
    "phase24_arc_envelope_v1",
    "phase24_provisional_band_exec_v1",
    "phase24_provisional_chapter_ledger_v1",
    "performance_indexes_v1",
    "experience_overlay_v1",
    "audience_feedback_v1",
    "audience_feedback_scale_meta_v1",
    "audience_feedback_project_scope_v1",
    "publisher_extension_platform_state_v1",
    "phase4_simulation_v1",
    "phase3_replan_strategy_v1",
    "project_automation_v1",
    "publisher_upload_job_abort_v1",
    "publisher_upload_job_project_v1",
    "publisher_browser_session_entries_v1",
    "project_target_total_chapters_v1",
    "project_target_total_chapters_consistency_v1",
    "governance_layer_v1",
    "decision_event_causality_v1",
    "subworld_control_v1",
    "book_genesis_v1",
    "prompt_trace_codex_metadata_v1",
    "review_repair_chain_v1",
    "world_model_v1",
    "world_v4_schema_v1",
    "world_v4_compile_audit_v1",
    "scenario_rehearsal_v1",
    "candidate_draft_and_scenario_patches_v1",
    "book_state_schema_v1",
    "knowledge_system_v46",
    "book_reader_experience_v46",
    "map_graph_schema_v1",
    "map_graph_schema_v2",
    "performance_spans_v1",
    "legacy_checkpoint_statuses_v1",
    "canon_quality_v1",
    "projection_cache_fields_v1",
)


def new_id() -> str:
    """Generate a new UUID4 hex string."""
    return uuid4().hex


def _coerce_postgres_url(database_url: str) -> str:
    value = str(database_url or "").strip()
    if not value or value == ":memory:" or "://" not in value:
        test_url = os.environ.get("FORWIN_TEST_DATABASE_URL", "").strip()
        if test_url:
            value = test_url
    try:
        url = make_url(value)
    except Exception as exc:  # noqa: BLE001
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
    """Create a PostgreSQL SQLAlchemy engine."""
    resolved_url = _coerce_postgres_url(database_url)
    connect_timeout = int(os.environ.get("FORWIN_POSTGRES_CONNECT_TIMEOUT_SECONDS", "10"))
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
    """Create and return a sessionmaker bound to the given engine."""
    return sessionmaker(bind=engine, expire_on_commit=False)


def alembic_config(database_url: str) -> AlembicConfig:
    """Build an Alembic config without relying on a cwd-specific ini file."""
    root = Path(__file__).resolve().parents[2]
    ini_path = root / "alembic.ini"
    config = AlembicConfig(str(ini_path)) if ini_path.exists() else AlembicConfig()
    config.set_main_option("script_location", "forwin:migrations")
    config.set_main_option("sqlalchemy.url", _coerce_postgres_url(database_url))
    return config


def init_db(engine: Engine) -> None:
    """Create all tables defined in the metadata."""
    if engine.dialect.name != "postgresql":
        raise ValueError("ForWin requires a PostgreSQL engine.")
    # Ensure every model module is imported so metadata includes all tables.
    from forwin import models as _models  # noqa: F401

    Base.metadata.create_all(engine)
    upgrade_db(engine)
    _stamp_alembic_head(engine)


def _mark_migration_applied(conn, version: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO schema_migrations(version)
            VALUES (:version)
            ON CONFLICT (version) DO NOTHING
            """
        ),
        {"version": version},
    )


def _upgrade_postgresql_database(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        for version in POSTGRES_BASELINE_MIGRATIONS:
            _mark_migration_applied(conn, version)
        conn.execute(
            text(
                """
                UPDATE band_checkpoints
                SET status = 'overridden',
                    resolved_at = COALESCE(resolved_at, updated_at, created_at)
                WHERE status = 'approved'
                """
            )
        )


def upgrade_db(engine: Engine) -> None:
    """Apply lightweight forward-only schema upgrades."""
    if engine.dialect.name != "postgresql":
        raise ValueError("ForWin requires a PostgreSQL engine.")
    _upgrade_postgresql_database(engine)


def _stamp_alembic_head(engine: Engine) -> None:
    """Keep db_update migration tooling able to verify a create_all database."""
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
        current = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        if current is None:
            conn.execute(
                text("INSERT INTO alembic_version(version_num) VALUES (:version)"),
                {"version": head},
            )
        else:
            conn.execute(
                text("UPDATE alembic_version SET version_num = :version"),
                {"version": head},
            )
