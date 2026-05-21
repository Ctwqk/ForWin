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
    "world_model_canonical_pages_v1",
    "character_identity_map_v1",
    "legacy_checkpoint_statuses_v1",
    "canon_quality_v1",
    "projection_cache_fields_v1",
    "narrative_obligations_v1",
    "canon_admission_obligation_fields_v1",
    "future_plan_audit_runs_v1",
    "generation_task_leases_v1",
    "trope_usage_records_v1",
    "project_target_total_default_v1",
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
        _upgrade_world_model_canonical_pages(conn)
        _upgrade_world_model_projection_cache_fields(conn)
        _upgrade_character_identity_map(conn)
        _drop_obsolete_character_identity_legacy_bridge(conn)
        _upgrade_narrative_obligations(conn)
        _upgrade_canon_admission_obligation_fields(conn)
        _upgrade_future_plan_audit_runs(conn)
        _upgrade_generation_task_leases(conn)
        _upgrade_trope_usage_records(conn)
        _upgrade_project_target_total_default(conn)
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


def _upgrade_world_model_canonical_pages(conn) -> None:
    conn.execute(text("ALTER TABLE world_model_pages ADD COLUMN IF NOT EXISTS logical_identity_key VARCHAR NOT NULL DEFAULT ''"))
    conn.execute(text("ALTER TABLE world_model_pages ADD COLUMN IF NOT EXISTS canonical_source_type VARCHAR NOT NULL DEFAULT ''"))
    conn.execute(text("ALTER TABLE world_model_pages ADD COLUMN IF NOT EXISTS canonical_source_id VARCHAR NOT NULL DEFAULT ''"))
    conn.execute(text("ALTER TABLE world_model_pages ADD COLUMN IF NOT EXISTS supersedes_page_id VARCHAR NOT NULL DEFAULT ''"))
    conn.execute(text("ALTER TABLE world_model_pages ADD COLUMN IF NOT EXISTS canonical_rank INTEGER NOT NULL DEFAULT 0"))
    conn.execute(
        text(
            """
            UPDATE world_model_pages
            SET logical_identity_key = CASE
                    WHEN page_type IN ('character','faction','organization','family','institution','resource','region','node','location')
                         AND trim(title) <> ''
                    THEN page_type || ':name:' || regexp_replace(lower(trim(title)), '[[:space:]]+', '', 'g')
                    ELSE page_type || ':page:' || COALESCE(NULLIF(page_key, ''), regexp_replace(lower(trim(title)), '[[:space:]]+', '', 'g'))
                END,
                canonical_source_type = CASE
                    WHEN frontmatter_json LIKE '%"node_id"%' THEN 'book_state_node'
                    WHEN page_key LIKE '%:genesis:%' OR page_key LIKE 'genesis:%' THEN 'genesis'
                    ELSE 'world_model_page'
                END,
                canonical_source_id = COALESCE(NULLIF(page_key, ''), id),
                canonical_rank = CASE
                    WHEN frontmatter_json LIKE '%"node_id"%' THEN 30000
                    WHEN page_key LIKE '%:genesis:%' OR page_key LIKE 'genesis:%' THEN 10000
                    ELSE 20000
                END + LEAST(GREATEST(COALESCE(as_of_chapter, 0), 0), 9999)
            WHERE logical_identity_key = ''
            """
        )
    )
    conn.execute(
        text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    first_value(id) OVER (
                        PARTITION BY project_id, page_type, logical_identity_key
                        ORDER BY canonical_rank DESC, as_of_chapter DESC, revision DESC, updated_at DESC, id DESC
                    ) AS canonical_id,
                    row_number() OVER (
                        PARTITION BY project_id, page_type, logical_identity_key
                        ORDER BY canonical_rank DESC, as_of_chapter DESC, revision DESC, updated_at DESC, id DESC
                    ) AS rn
                FROM world_model_pages
                WHERE status = 'canon_live'
                  AND logical_identity_key <> ''
            )
            UPDATE world_model_pages AS page
            SET status = 'superseded',
                supersedes_page_id = ranked.canonical_id
            FROM ranked
            WHERE page.id = ranked.id
              AND ranked.rn > 1
            """
        )
    )
    conn.execute(text("UPDATE world_model_pages SET supersedes_page_id = '' WHERE status = 'canon_live'"))
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_world_model_pages_project_identity "
            "ON world_model_pages (project_id, page_type, logical_identity_key)"
        )
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_world_model_pages_live_identity "
            "ON world_model_pages (project_id, page_type, logical_identity_key) "
            "WHERE status = 'canon_live' AND logical_identity_key <> ''"
        )
    )


def _upgrade_world_model_projection_cache_fields(conn) -> None:
    for column, ddl in (
        ("projection_kind", "VARCHAR NOT NULL DEFAULT 'world_studio'"),
        ("projection_version", "VARCHAR NOT NULL DEFAULT ''"),
        ("source_digest", "VARCHAR NOT NULL DEFAULT ''"),
        ("section_digest_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("observer_type", "VARCHAR NOT NULL DEFAULT ''"),
        ("observer_id", "VARCHAR NOT NULL DEFAULT ''"),
        ("role_scope", "VARCHAR NOT NULL DEFAULT ''"),
        ("visibility_scope", "VARCHAR NOT NULL DEFAULT ''"),
        ("canon_status", "VARCHAR NOT NULL DEFAULT 'canon_projection'"),
    ):
        conn.execute(text(f"ALTER TABLE world_model_pages ADD COLUMN IF NOT EXISTS {column} {ddl}"))
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_world_model_pages_project_projection "
            "ON world_model_pages (project_id, projection_kind, projection_version)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_world_model_pages_project_source_digest "
            "ON world_model_pages (project_id, source_digest)"
        )
    )


def _upgrade_character_identity_map(conn) -> None:
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS character_identity_map (
                id VARCHAR PRIMARY KEY,
                project_id VARCHAR NOT NULL REFERENCES projects(id),
                canonical_character_id VARCHAR NOT NULL DEFAULT '',
                book_state_node_id VARCHAR NOT NULL DEFAULT '',
                genesis_ref_id VARCHAR NOT NULL DEFAULT '',
                roster_item_ids_json TEXT DEFAULT '[]',
                aliases_json TEXT DEFAULT '[]',
                display_name TEXT DEFAULT '',
                status VARCHAR DEFAULT 'active',
                metadata_json TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    for column, ddl in (
        ("canonical_character_id", "VARCHAR NOT NULL DEFAULT ''"),
        ("book_state_node_id", "VARCHAR NOT NULL DEFAULT ''"),
        ("genesis_ref_id", "VARCHAR NOT NULL DEFAULT ''"),
        ("roster_item_ids_json", "TEXT DEFAULT '[]'"),
        ("aliases_json", "TEXT DEFAULT '[]'"),
        ("display_name", "TEXT DEFAULT ''"),
        ("status", "VARCHAR DEFAULT 'active'"),
        ("metadata_json", "TEXT DEFAULT '{}'"),
        ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
    ):
        conn.execute(text(f"ALTER TABLE character_identity_map ADD COLUMN IF NOT EXISTS {column} {ddl}"))
    conn.execute(
        text(
            """
            INSERT INTO character_identity_map (
                id,
                project_id,
                canonical_character_id,
                book_state_node_id,
                genesis_ref_id,
                roster_item_ids_json,
                aliases_json,
                display_name,
                status,
                metadata_json,
                created_at,
                updated_at
            )
            SELECT
                'char_identity_' || id,
                project_id,
                id,
                id,
                COALESCE((metadata_json::jsonb ->> 'genesis_ref_id'), ''),
                COALESCE((metadata_json::jsonb -> 'roster_item_ids')::text, '[]'),
                CASE
                    WHEN aliases_json IS NULL OR aliases_json = '' OR aliases_json = '[]'
                    THEN jsonb_build_array(name)::text
                    ELSE (aliases_json::jsonb || to_jsonb(name))::text
                END::text,
                name,
                'active',
                jsonb_build_object('backfilled_from', 'world_nodes')::text,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            FROM world_nodes
            WHERE node_type = 'character'
              AND NOT EXISTS (
                SELECT 1
                FROM character_identity_map existing
                WHERE existing.project_id = world_nodes.project_id
                  AND existing.book_state_node_id = world_nodes.id
              )
            """
        )
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_character_identity_project_canonical ON character_identity_map (project_id, canonical_character_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_character_identity_project_book_node ON character_identity_map (project_id, book_state_node_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_character_identity_project_genesis ON character_identity_map (project_id, genesis_ref_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_character_identity_project_status ON character_identity_map (project_id, status)"))


def _drop_obsolete_character_identity_legacy_bridge(conn) -> None:
    conn.execute(text("DROP INDEX IF EXISTS ix_character_identity_project_legacy"))
    conn.execute(text("ALTER TABLE character_identity_map DROP COLUMN IF EXISTS legacy_entity_id"))
    conn.execute(
        text(
            "ALTER TABLE IF EXISTS relation_edges "
            "DROP CONSTRAINT IF EXISTS relation_edges_source_entity_id_fkey"
        )
    )
    conn.execute(
        text(
            "ALTER TABLE IF EXISTS relation_edges "
            "DROP CONSTRAINT IF EXISTS relation_edges_target_entity_id_fkey"
        )
    )


def _upgrade_narrative_obligations(conn) -> None:
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS narrative_obligations (
                id VARCHAR PRIMARY KEY,
                project_id VARCHAR NOT NULL REFERENCES projects(id),
                origin_chapter_number INTEGER NOT NULL DEFAULT 0,
                origin_draft_id VARCHAR NOT NULL DEFAULT '',
                origin_review_id VARCHAR NOT NULL DEFAULT '',
                origin_signal_ids_json TEXT NOT NULL DEFAULT '[]',
                origin_plan_snapshot_id VARCHAR NOT NULL DEFAULT '',
                obligation_type VARCHAR NOT NULL DEFAULT '',
                priority VARCHAR NOT NULL DEFAULT 'P1',
                status VARCHAR NOT NULL DEFAULT 'proposed',
                summary TEXT NOT NULL DEFAULT '',
                deferral_reason TEXT NOT NULL DEFAULT '',
                hardness VARCHAR NOT NULL DEFAULT 'soft_gap',
                subject_refs_json TEXT NOT NULL DEFAULT '[]',
                evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                deadline_chapter INTEGER NOT NULL DEFAULT 0,
                deadline_policy VARCHAR NOT NULL DEFAULT 'block_at_deadline',
                payoff_test TEXT NOT NULL DEFAULT '',
                resolution_conditions_json TEXT NOT NULL DEFAULT '[]',
                linked_plan_patch_ids_json TEXT NOT NULL DEFAULT '[]',
                linked_future_chapters_json TEXT NOT NULL DEFAULT '[]',
                blocking_policy VARCHAR NOT NULL DEFAULT 'block_at_deadline',
                created_by VARCHAR NOT NULL DEFAULT 'system',
                resolved_at TIMESTAMP,
                resolution_chapter INTEGER NOT NULL DEFAULT 0,
                resolution_evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                waive_reason TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    for column, ddl in (
        ("origin_chapter_number", "INTEGER NOT NULL DEFAULT 0"),
        ("origin_draft_id", "VARCHAR NOT NULL DEFAULT ''"),
        ("origin_review_id", "VARCHAR NOT NULL DEFAULT ''"),
        ("origin_signal_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("origin_plan_snapshot_id", "VARCHAR NOT NULL DEFAULT ''"),
        ("obligation_type", "VARCHAR NOT NULL DEFAULT ''"),
        ("priority", "VARCHAR NOT NULL DEFAULT 'P1'"),
        ("status", "VARCHAR NOT NULL DEFAULT 'proposed'"),
        ("summary", "TEXT NOT NULL DEFAULT ''"),
        ("deferral_reason", "TEXT NOT NULL DEFAULT ''"),
        ("hardness", "VARCHAR NOT NULL DEFAULT 'soft_gap'"),
        ("subject_refs_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("evidence_refs_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("deadline_chapter", "INTEGER NOT NULL DEFAULT 0"),
        ("deadline_policy", "VARCHAR NOT NULL DEFAULT 'block_at_deadline'"),
        ("payoff_test", "TEXT NOT NULL DEFAULT ''"),
        ("resolution_conditions_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("linked_plan_patch_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("linked_future_chapters_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("blocking_policy", "VARCHAR NOT NULL DEFAULT 'block_at_deadline'"),
        ("created_by", "VARCHAR NOT NULL DEFAULT 'system'"),
        ("resolved_at", "TIMESTAMP"),
        ("resolution_chapter", "INTEGER NOT NULL DEFAULT 0"),
        ("resolution_evidence_refs_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("waive_reason", "TEXT NOT NULL DEFAULT ''"),
        ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
    ):
        conn.execute(text(f"ALTER TABLE narrative_obligations ADD COLUMN IF NOT EXISTS {column} {ddl}"))

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS narrative_plan_patches (
                id VARCHAR PRIMARY KEY,
                project_id VARCHAR NOT NULL REFERENCES projects(id),
                patch_type VARCHAR NOT NULL DEFAULT 'defer_acceptance',
                target_scope VARCHAR NOT NULL DEFAULT 'chapter',
                target_plan_id VARCHAR NOT NULL DEFAULT '',
                target_arc_id VARCHAR NOT NULL DEFAULT '',
                target_band_id VARCHAR NOT NULL DEFAULT '',
                affected_chapters_json TEXT NOT NULL DEFAULT '[]',
                source_obligation_ids_json TEXT NOT NULL DEFAULT '[]',
                source_signal_ids_json TEXT NOT NULL DEFAULT '[]',
                old_plan_digest VARCHAR NOT NULL DEFAULT '',
                new_plan_digest VARCHAR NOT NULL DEFAULT '',
                old_contract_json TEXT NOT NULL DEFAULT '{}',
                new_contract_json TEXT NOT NULL DEFAULT '{}',
                diff_summary TEXT NOT NULL DEFAULT '',
                must_preserve_json TEXT NOT NULL DEFAULT '[]',
                must_not_change_json TEXT NOT NULL DEFAULT '[]',
                new_constraints_json TEXT NOT NULL DEFAULT '[]',
                writer_context_injections_json TEXT NOT NULL DEFAULT '[]',
                reviewer_context_injections_json TEXT NOT NULL DEFAULT '[]',
                expected_resolution_tests_json TEXT NOT NULL DEFAULT '[]',
                validation_status VARCHAR NOT NULL DEFAULT 'pending',
                validation_errors_json TEXT NOT NULL DEFAULT '[]',
                applied BOOLEAN NOT NULL DEFAULT FALSE,
                applied_at TIMESTAMP,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    for column, ddl in (
        ("patch_type", "VARCHAR NOT NULL DEFAULT 'defer_acceptance'"),
        ("target_scope", "VARCHAR NOT NULL DEFAULT 'chapter'"),
        ("target_plan_id", "VARCHAR NOT NULL DEFAULT ''"),
        ("target_arc_id", "VARCHAR NOT NULL DEFAULT ''"),
        ("target_band_id", "VARCHAR NOT NULL DEFAULT ''"),
        ("affected_chapters_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("source_obligation_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("source_signal_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("old_plan_digest", "VARCHAR NOT NULL DEFAULT ''"),
        ("new_plan_digest", "VARCHAR NOT NULL DEFAULT ''"),
        ("old_contract_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("new_contract_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("diff_summary", "TEXT NOT NULL DEFAULT ''"),
        ("must_preserve_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("must_not_change_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("new_constraints_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("writer_context_injections_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("reviewer_context_injections_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("expected_resolution_tests_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("validation_status", "VARCHAR NOT NULL DEFAULT 'pending'"),
        ("validation_errors_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("applied", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("applied_at", "TIMESTAMP"),
        ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
    ):
        conn.execute(text(f"ALTER TABLE narrative_plan_patches ADD COLUMN IF NOT EXISTS {column} {ddl}"))

    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_narrative_obligations_project_status ON narrative_obligations (project_id, status, priority)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_narrative_obligations_project_deadline ON narrative_obligations (project_id, deadline_chapter)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_narrative_obligations_origin_chapter ON narrative_obligations (project_id, origin_chapter_number)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_narrative_plan_patches_project_scope ON narrative_plan_patches (project_id, target_scope)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_narrative_plan_patches_project_applied ON narrative_plan_patches (project_id, applied)"))


def _upgrade_canon_admission_obligation_fields(conn) -> None:
    for column, ddl in (
        ("admission_mode", "VARCHAR NOT NULL DEFAULT 'clean'"),
        ("obligation_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("required_plan_patch_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("blocking_reasons_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("expired_obligation_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("over_budget", "VARCHAR NOT NULL DEFAULT 'false'"),
    ):
        conn.execute(text(f"ALTER TABLE canon_admission_runs ADD COLUMN IF NOT EXISTS {column} {ddl}"))


def _upgrade_future_plan_audit_runs(conn) -> None:
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS future_plan_audit_runs (
                id VARCHAR PRIMARY KEY,
                project_id VARCHAR NOT NULL REFERENCES projects(id),
                current_chapter_number INTEGER NOT NULL DEFAULT 0,
                trigger_stage VARCHAR NOT NULL DEFAULT '',
                inspected_chapters_json TEXT NOT NULL DEFAULT '[]',
                status VARCHAR NOT NULL DEFAULT 'pass',
                issues_json TEXT NOT NULL DEFAULT '[]',
                applied_plan_patch_ids_json TEXT NOT NULL DEFAULT '[]',
                blocking_reasons_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    for column, ddl in (
        ("current_chapter_number", "INTEGER NOT NULL DEFAULT 0"),
        ("trigger_stage", "VARCHAR NOT NULL DEFAULT ''"),
        ("inspected_chapters_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("status", "VARCHAR NOT NULL DEFAULT 'pass'"),
        ("issues_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("applied_plan_patch_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("blocking_reasons_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
    ):
        conn.execute(text(f"ALTER TABLE future_plan_audit_runs ADD COLUMN IF NOT EXISTS {column} {ddl}"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_future_plan_audit_project_created ON future_plan_audit_runs (project_id, created_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_future_plan_audit_project_chapter ON future_plan_audit_runs (project_id, current_chapter_number)"))


def _upgrade_generation_task_leases(conn) -> None:
    for column, ddl in (
        ("lease_owner", "VARCHAR NOT NULL DEFAULT ''"),
        ("lease_expires_at", "TIMESTAMP NULL"),
        ("heartbeat_at", "TIMESTAMP NULL"),
        ("resume_from_chapter", "INTEGER NOT NULL DEFAULT 0"),
        ("run_until_chapter", "INTEGER NOT NULL DEFAULT 0"),
        ("max_chapters", "INTEGER NOT NULL DEFAULT 0"),
    ):
        conn.execute(text(f"ALTER TABLE generation_tasks ADD COLUMN IF NOT EXISTS {column} {ddl}"))
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_generation_tasks_lease "
            "ON generation_tasks (status, lease_expires_at)"
        )
    )


def _upgrade_trope_usage_records(conn) -> None:
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS trope_usage_records (
                id VARCHAR PRIMARY KEY,
                project_id VARCHAR NOT NULL REFERENCES projects(id),
                arc_id VARCHAR NOT NULL DEFAULT '',
                band_id VARCHAR NOT NULL DEFAULT '',
                chapter_number INTEGER NOT NULL DEFAULT 0,
                template_id VARCHAR NOT NULL,
                category VARCHAR NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_trope_usage_project_band "
            "ON trope_usage_records (project_id, band_id, created_at)"
        )
    )


def _upgrade_project_target_total_default(conn) -> None:
    conn.execute(
        text("ALTER TABLE projects ALTER COLUMN target_total_chapters SET DEFAULT 50")
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
