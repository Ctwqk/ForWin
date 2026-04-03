from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from sqlalchemy import Engine, event, text
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool


class Base(DeclarativeBase):
    pass


MigrationApplyFn = Callable[[object], None]


@dataclass(frozen=True)
class MigrationSpec:
    version: str
    apply_fn: MigrationApplyFn


def new_id() -> str:
    """Generate a new UUID4 hex string."""
    return uuid4().hex


def get_engine(db_path: str) -> Engine:
    """Create a SQLite engine with WAL journal mode enabled."""
    pool_kwargs: dict[str, object]
    if db_path == ":memory:":
        pool_kwargs = {"poolclass": StaticPool}
    else:
        pool_kwargs = {"poolclass": NullPool}
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        **pool_kwargs,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragmas(dbapi_connection, connection_record):  # type: ignore[misc]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create and return a sessionmaker bound to the given engine."""
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    """Create all tables defined in the metadata."""
    Base.metadata.create_all(engine)
    upgrade_db(engine)


def _migration_applied(conn, version: str) -> bool:
    return (
        conn.execute(
            text(
                """
                SELECT 1
                FROM schema_migrations
                WHERE version = :version
                """
            ),
            {"version": version},
        ).scalar_one_or_none()
        is not None
    )


def _mark_migration_applied(conn, version: str) -> None:
    conn.execute(
        text(
            """
            INSERT OR IGNORE INTO schema_migrations(version)
            VALUES (:version)
            """
        ),
        {"version": version},
    )


def _run_migration(conn, version: str, apply_fn) -> None:
    if _migration_applied(conn, version):
        return
    apply_fn(conn)
    _mark_migration_applied(conn, version)


def upgrade_db(engine: Engine) -> None:
    """Apply lightweight forward-only schema upgrades."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        migrations: list[MigrationSpec] = []

        def apply_entity_alias_index_v1(conn) -> None:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS entity_aliases (
                        id TEXT PRIMARY KEY,
                        entity_id TEXT NOT NULL,
                        project_id TEXT NOT NULL,
                        alias TEXT NOT NULL,
                        FOREIGN KEY(entity_id) REFERENCES entities(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_alias_project_alias
                    ON entity_aliases(project_id, alias)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_entity_aliases_project_id
                    ON entity_aliases(project_id)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_entity_aliases_alias
                    ON entity_aliases(alias)
                    """
                )
            )
            rows = conn.execute(
                text("SELECT id, project_id, aliases_json FROM entities")
            ).mappings()
            for row in rows:
                try:
                    aliases = json.loads(row["aliases_json"] or "[]") or []
                except (json.JSONDecodeError, TypeError):
                    aliases = []
                for alias in aliases:
                    alias_text = str(alias).strip()
                    if not alias_text:
                        continue
                    conn.execute(
                        text(
                            """
                            INSERT OR IGNORE INTO entity_aliases(id, entity_id, project_id, alias)
                            VALUES (:id, :entity_id, :project_id, :alias)
                            """
                        ),
                        {
                            "id": new_id(),
                            "entity_id": row["id"],
                            "project_id": row["project_id"],
                            "alias": alias_text,
                        },
                    )
        migrations.append(MigrationSpec("entity_alias_index_v1", apply_entity_alias_index_v1))

        def apply_phase3_analysis_v1(conn) -> None:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS project_stage_analyses (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        chapter_number INTEGER NOT NULL,
                        stage_label TEXT NOT NULL,
                        progress_ratio FLOAT NOT NULL DEFAULT 0,
                        timeline_label TEXT NOT NULL DEFAULT '',
                        timeline_ordinal INTEGER NOT NULL DEFAULT 0,
                        pacing_verdict TEXT NOT NULL DEFAULT 'steady',
                        pacing_summary TEXT NOT NULL DEFAULT '',
                        stale_threads_json TEXT NOT NULL DEFAULT '[]',
                        active_thread_count INTEGER NOT NULL DEFAULT 0,
                        unresolved_thread_count INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_project_stage_analyses_project_chapter
                    ON project_stage_analyses(project_id, chapter_number DESC)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS project_replan_events (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        trigger_chapter INTEGER NOT NULL,
                        risk_level TEXT NOT NULL DEFAULT 'low',
                        reason TEXT NOT NULL DEFAULT '',
                        focus_threads_json TEXT NOT NULL DEFAULT '[]',
                        strategy TEXT NOT NULL DEFAULT 'rearc',
                        status TEXT NOT NULL DEFAULT 'applied',
                        cooldown_until_chapter INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_project_replan_events_project_chapter
                    ON project_replan_events(project_id, trigger_chapter DESC)
                    """
                )
            )
        migrations.append(MigrationSpec("phase3_analysis_v1", apply_phase3_analysis_v1))

        def apply_phase24_arc_envelope_v1(conn) -> None:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS arc_envelopes (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        arc_id TEXT NOT NULL,
                        base_target_size INTEGER NOT NULL DEFAULT 0,
                        base_soft_min INTEGER NOT NULL DEFAULT 0,
                        base_soft_max INTEGER NOT NULL DEFAULT 0,
                        resolved_target_size INTEGER NOT NULL DEFAULT 0,
                        resolved_soft_min INTEGER NOT NULL DEFAULT 0,
                        resolved_soft_max INTEGER NOT NULL DEFAULT 0,
                        detailed_band_size INTEGER NOT NULL DEFAULT 0,
                        frozen_zone_size INTEGER NOT NULL DEFAULT 0,
                        current_projected_size INTEGER NOT NULL DEFAULT 0,
                        current_confidence FLOAT NOT NULL DEFAULT 0,
                        source_policy_tier TEXT NOT NULL DEFAULT 'short',
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id),
                        FOREIGN KEY(arc_id) REFERENCES arc_plan_versions(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_arc_envelopes_project_arc
                    ON arc_envelopes(project_id, arc_id)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS arc_structure_drafts (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        arc_id TEXT NOT NULL,
                        phase_layout_json TEXT NOT NULL DEFAULT '[]',
                        key_beats_json TEXT NOT NULL DEFAULT '[]',
                        thread_priorities_json TEXT NOT NULL DEFAULT '[]',
                        hotspot_candidates_json TEXT NOT NULL DEFAULT '[]',
                        compression_candidates_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id),
                        FOREIGN KEY(arc_id) REFERENCES arc_plan_versions(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_arc_structure_drafts_project_arc
                    ON arc_structure_drafts(project_id, arc_id)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS arc_envelope_analyses (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        arc_id TEXT NOT NULL,
                        based_on_band_id TEXT NOT NULL DEFAULT '',
                        recommendation TEXT NOT NULL DEFAULT 'keep',
                        evidence_json TEXT NOT NULL DEFAULT '[]',
                        expansion_signals_json TEXT NOT NULL DEFAULT '[]',
                        compression_signals_json TEXT NOT NULL DEFAULT '[]',
                        suggested_target INTEGER NOT NULL DEFAULT 0,
                        suggested_soft_min INTEGER NOT NULL DEFAULT 0,
                        suggested_soft_max INTEGER NOT NULL DEFAULT 0,
                        confidence FLOAT NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id),
                        FOREIGN KEY(arc_id) REFERENCES arc_plan_versions(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_arc_envelope_analyses_project_arc
                    ON arc_envelope_analyses(project_id, arc_id)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS provisional_promotion_records (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        arc_id TEXT NOT NULL,
                        band_id TEXT NOT NULL DEFAULT '',
                        promoted_chapter_ids_json TEXT NOT NULL DEFAULT '[]',
                        promotion_reason TEXT NOT NULL DEFAULT '',
                        based_on_analysis_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id),
                        FOREIGN KEY(arc_id) REFERENCES arc_plan_versions(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_provisional_promotions_project_arc_band
                    ON provisional_promotion_records(project_id, arc_id, band_id)
                    """
                )
            )
        migrations.append(MigrationSpec("phase24_arc_envelope_v1", apply_phase24_arc_envelope_v1))

        def apply_phase24_provisional_band_exec_v1(conn) -> None:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS provisional_band_executions (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        arc_id TEXT NOT NULL,
                        band_id TEXT NOT NULL DEFAULT '',
                        chapter_numbers_json TEXT NOT NULL DEFAULT '[]',
                        artifact_path TEXT NOT NULL DEFAULT '',
                        aggregate_verdict TEXT NOT NULL DEFAULT 'pass',
                        preview_char_count INTEGER NOT NULL DEFAULT 0,
                        issue_count INTEGER NOT NULL DEFAULT 0,
                        failure_count INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id),
                        FOREIGN KEY(arc_id) REFERENCES arc_plan_versions(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_provisional_band_exec_project_arc_band
                    ON provisional_band_executions(project_id, arc_id, band_id)
                    """
                )
            )
        migrations.append(
            MigrationSpec("phase24_provisional_band_exec_v1", apply_phase24_provisional_band_exec_v1)
        )

        def apply_phase24_provisional_chapter_ledger_v1(conn) -> None:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS provisional_chapter_ledgers (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        arc_id TEXT NOT NULL,
                        band_id TEXT NOT NULL DEFAULT '',
                        chapter_number INTEGER NOT NULL DEFAULT 0,
                        title TEXT NOT NULL DEFAULT '',
                        summary TEXT NOT NULL DEFAULT '',
                        verdict TEXT NOT NULL DEFAULT 'pass',
                        char_count INTEGER NOT NULL DEFAULT 0,
                        artifact_meta_path TEXT NOT NULL DEFAULT '',
                        draft_blob_path TEXT NOT NULL DEFAULT '',
                        current_time_label TEXT NOT NULL DEFAULT '',
                        projected_time_label TEXT NOT NULL DEFAULT '',
                        state_changes_json TEXT NOT NULL DEFAULT '[]',
                        events_json TEXT NOT NULL DEFAULT '[]',
                        thread_beats_json TEXT NOT NULL DEFAULT '[]',
                        time_advance_json TEXT NOT NULL DEFAULT '{}',
                        issues_json TEXT NOT NULL DEFAULT '[]',
                        error_text TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id),
                        FOREIGN KEY(arc_id) REFERENCES arc_plan_versions(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_provisional_chapter_ledgers_project_arc_band_chapter
                    ON provisional_chapter_ledgers(project_id, arc_id, band_id, chapter_number)
                    """
                )
            )
        migrations.append(
            MigrationSpec(
                "phase24_provisional_chapter_ledger_v1",
                apply_phase24_provisional_chapter_ledger_v1,
            )
        )

        def apply_performance_indexes_v1(conn) -> None:
            for statement in (
                """
                CREATE INDEX IF NOT EXISTS ix_entities_project_active
                ON entities(project_id, is_active)
                """,
                """
                CREATE INDEX IF NOT EXISTS ix_entities_project_name
                ON entities(project_id, name)
                """,
                """
                CREATE INDEX IF NOT EXISTS ix_entity_states_entity_chapter
                ON entity_states(entity_id, as_of_chapter DESC)
                """,
                """
                CREATE INDEX IF NOT EXISTS ix_arc_plan_versions_project_status
                ON arc_plan_versions(project_id, status)
                """,
                """
                CREATE INDEX IF NOT EXISTS ix_chapter_plans_project_chapter
                ON chapter_plans(project_id, chapter_number)
                """,
                """
                CREATE INDEX IF NOT EXISTS ix_chapter_plans_project_status
                ON chapter_plans(project_id, status)
                """,
                """
                CREATE INDEX IF NOT EXISTS ix_chapter_drafts_plan_version
                ON chapter_drafts(chapter_plan_id, version DESC)
                """,
                """
                CREATE INDEX IF NOT EXISTS ix_plot_threads_project_status
                ON plot_threads(project_id, status)
                """,
                """
                CREATE INDEX IF NOT EXISTS ix_plot_threads_project_name
                ON plot_threads(project_id, name)
                """,
                """
                CREATE INDEX IF NOT EXISTS ix_plot_thread_beats_thread_chapter
                ON plot_thread_beats(thread_id, chapter_number DESC)
                """,
                """
                CREATE INDEX IF NOT EXISTS ix_publisher_raw_comments_work_name
                ON publisher_raw_comments(work_name)
                """,
            ):
                conn.execute(text(statement))
        migrations.append(MigrationSpec("performance_indexes_v1", apply_performance_indexes_v1))

        def apply_phase4_simulation_v1(conn) -> None:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS npc_intent_snapshots (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        chapter_number INTEGER NOT NULL,
                        entity_id TEXT NOT NULL,
                        entity_name TEXT NOT NULL,
                        intent_kind TEXT NOT NULL DEFAULT 'pursue',
                        objective TEXT NOT NULL DEFAULT '',
                        tactic TEXT NOT NULL DEFAULT '',
                        urgency INTEGER NOT NULL DEFAULT 1,
                        notes TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id),
                        FOREIGN KEY(entity_id) REFERENCES entities(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_npc_intents_project_chapter
                    ON npc_intent_snapshots(project_id, chapter_number DESC)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS world_simulation_turns (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        chapter_number INTEGER NOT NULL,
                        pressure_level TEXT NOT NULL DEFAULT 'steady',
                        pressure_summary TEXT NOT NULL DEFAULT '',
                        notable_shifts_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_world_sim_turns_project_chapter
                    ON world_simulation_turns(project_id, chapter_number DESC)
                    """
                )
            )
        migrations.append(MigrationSpec("phase4_simulation_v1", apply_phase4_simulation_v1))

        def apply_phase3_replan_strategy_v1(conn) -> None:
            columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(project_replan_events)"))
            }
            if "strategy" not in columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE project_replan_events
                        ADD COLUMN strategy TEXT NOT NULL DEFAULT 'rearc'
                        """
                    )
                )
        migrations.append(MigrationSpec("phase3_replan_strategy_v1", apply_phase3_replan_strategy_v1))

        for migration in migrations:
            _run_migration(conn, migration.version, migration.apply_fn)
