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
    # Ensure every model module is imported so metadata includes all tables.
    from forwin import models as _models  # noqa: F401

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

        def apply_generation_tasks_v1(conn) -> None:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS generation_tasks (
                        id TEXT PRIMARY KEY,
                        task_kind TEXT NOT NULL DEFAULT 'generation',
                        status TEXT NOT NULL DEFAULT 'starting',
                        title TEXT NOT NULL DEFAULT '',
                        subtitle TEXT NOT NULL DEFAULT '',
                        project_id TEXT NOT NULL DEFAULT '',
                        extension_client_id TEXT NOT NULL DEFAULT '',
                        error_message TEXT NOT NULL DEFAULT '',
                        message TEXT NOT NULL DEFAULT '',
                        current_stage TEXT NOT NULL DEFAULT 'queued',
                        stage_history_json TEXT NOT NULL DEFAULT '[]',
                        requested_chapters INTEGER NOT NULL DEFAULT 0,
                        current_chapter INTEGER NOT NULL DEFAULT 0,
                        completed_chapters_json TEXT NOT NULL DEFAULT '[]',
                        failed_chapters_json TEXT NOT NULL DEFAULT '[]',
                        paused_chapters_json TEXT NOT NULL DEFAULT '[]',
                        frozen_artifacts_json TEXT NOT NULL DEFAULT '[]',
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        pause_requested INTEGER NOT NULL DEFAULT 0,
                        paused_at TEXT NULL,
                        started_at TEXT NULL,
                        finished_at TEXT NULL,
                        deleted_at TEXT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_generation_tasks_status_updated
                    ON generation_tasks(status, updated_at)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_generation_tasks_project_updated
                    ON generation_tasks(project_id, updated_at)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_generation_tasks_deleted_updated
                    ON generation_tasks(deleted_at, updated_at)
                    """
                )
            )

        migrations.append(MigrationSpec("generation_tasks_v1", apply_generation_tasks_v1))

        def apply_generation_task_pause_v1(conn) -> None:
            columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(generation_tasks)")).fetchall()
            }
            if "pause_requested" not in columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE generation_tasks
                        ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0
                        """
                    )
                )
            if "paused_at" not in columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE generation_tasks
                        ADD COLUMN paused_at TEXT NULL
                        """
                    )
                )

        migrations.append(MigrationSpec("generation_task_pause_v1", apply_generation_task_pause_v1))

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

        def apply_experience_overlay_v1(conn) -> None:
            chapter_plan_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(chapter_plans)"))
            }
            if "experience_plan_json" not in chapter_plan_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE chapter_plans
                        ADD COLUMN experience_plan_json TEXT NOT NULL DEFAULT '{}'
                        """
                    )
                )

            arc_structure_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(arc_structure_drafts)"))
            }
            if "reader_promise_json" not in arc_structure_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE arc_structure_drafts
                        ADD COLUMN reader_promise_json TEXT NOT NULL DEFAULT '{}'
                        """
                    )
                )
            if "arc_payoff_map_json" not in arc_structure_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE arc_structure_drafts
                        ADD COLUMN arc_payoff_map_json TEXT NOT NULL DEFAULT '{}'
                        """
                    )
                )

            chapter_review_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(chapter_reviews)"))
            }
            if "review_meta_json" not in chapter_review_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE chapter_reviews
                        ADD COLUMN review_meta_json TEXT NOT NULL DEFAULT '{}'
                        """
                    )
                )

            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS band_experience_plans (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        arc_id TEXT NOT NULL,
                        band_id TEXT NOT NULL,
                        chapter_start INTEGER NOT NULL DEFAULT 1,
                        chapter_end INTEGER NOT NULL DEFAULT 1,
                        stall_guard_max_gap INTEGER NOT NULL DEFAULT 0,
                        schedule_json TEXT NOT NULL DEFAULT '{}',
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
                    CREATE INDEX IF NOT EXISTS ix_band_experience_plans_project_arc_band
                    ON band_experience_plans(project_id, arc_id, band_id)
                    """
                )
            )

            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS chapter_rewrite_attempts (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        chapter_number INTEGER NOT NULL,
                        attempt_no INTEGER NOT NULL,
                        trigger_review_id TEXT NOT NULL,
                        repair_scope TEXT NOT NULL DEFAULT '',
                        design_patch_json TEXT NOT NULL DEFAULT '{}',
                        source_draft_id TEXT NOT NULL,
                        result_draft_id TEXT NOT NULL,
                        result_verdict TEXT NOT NULL DEFAULT '',
                        forced_accept_applied INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id),
                        FOREIGN KEY(trigger_review_id) REFERENCES chapter_reviews(id),
                        FOREIGN KEY(source_draft_id) REFERENCES chapter_drafts(id),
                        FOREIGN KEY(result_draft_id) REFERENCES chapter_drafts(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_chapter_rewrite_attempts_project_chapter_attempt
                    ON chapter_rewrite_attempts(project_id, chapter_number, attempt_no)
                    """
                )
            )
        migrations.append(MigrationSpec("experience_overlay_v1", apply_experience_overlay_v1))

        def apply_audience_feedback_v1(conn) -> None:
            raw_comment_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(publisher_raw_comments)"))
            }
            if "like_count" not in raw_comment_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE publisher_raw_comments
                        ADD COLUMN like_count INTEGER NOT NULL DEFAULT 0
                        """
                    )
                )
            if "reply_count" not in raw_comment_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE publisher_raw_comments
                        ADD COLUMN reply_count INTEGER NOT NULL DEFAULT 0
                        """
                    )
                )

            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS comment_signal_candidates (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        source_comment_id TEXT NOT NULL,
                        signal_type TEXT NOT NULL,
                        target_type TEXT NOT NULL DEFAULT '',
                        target_name TEXT NOT NULL DEFAULT '',
                        severity INTEGER NOT NULL DEFAULT 1,
                        confidence FLOAT NOT NULL DEFAULT 0.5,
                        evidence_span TEXT NOT NULL DEFAULT '',
                        signal_level TEXT NOT NULL DEFAULT 'noise',
                        chapter_number INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id),
                        FOREIGN KEY(source_comment_id) REFERENCES publisher_raw_comments(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_comment_signals_source
                    ON comment_signal_candidates(source_comment_id)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_comment_signals_project_type
                    ON comment_signal_candidates(project_id, signal_type)
                    """
                )
            )

            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS signal_window_aggregates (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        signal_key TEXT NOT NULL,
                        signal_type TEXT NOT NULL DEFAULT '',
                        target_type TEXT NOT NULL DEFAULT '',
                        target_name TEXT NOT NULL DEFAULT '',
                        window_type TEXT NOT NULL DEFAULT 'short',
                        window_chapter_start INTEGER NOT NULL DEFAULT 0,
                        window_chapter_end INTEGER NOT NULL DEFAULT 0,
                        hit_comment_count INTEGER NOT NULL DEFAULT 0,
                        unique_user_count INTEGER NOT NULL DEFAULT 0,
                        total_comment_count INTEGER NOT NULL DEFAULT 0,
                        reader_estimate INTEGER NOT NULL DEFAULT 0,
                        reader_tier INTEGER NOT NULL DEFAULT 0,
                        estimation_method TEXT NOT NULL DEFAULT 'comment_proxy',
                        scale_confidence FLOAT NOT NULL DEFAULT 0.35,
                        max_severity INTEGER NOT NULL DEFAULT 0,
                        avg_confidence FLOAT NOT NULL DEFAULT 0,
                        signal_level TEXT NOT NULL DEFAULT 'noise',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_signal_window_agg_project_key_window
                    ON signal_window_aggregates(project_id, signal_key, window_type)
                    """
                )
            )

            existing_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(signal_window_aggregates)")).fetchall()
            }
            if "estimation_method" not in existing_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE signal_window_aggregates
                        ADD COLUMN estimation_method TEXT NOT NULL DEFAULT 'comment_proxy'
                        """
                    )
                )
            if "scale_confidence" not in existing_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE signal_window_aggregates
                        ADD COLUMN scale_confidence FLOAT NOT NULL DEFAULT 0.35
                        """
                    )
                )

            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS reader_scale_snapshots (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        chapter_number INTEGER NOT NULL DEFAULT 0,
                        reader_estimate INTEGER NOT NULL DEFAULT 0,
                        estimation_method TEXT NOT NULL DEFAULT 'comment_proxy',
                        tier INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_reader_scale_project_chapter
                    ON reader_scale_snapshots(project_id, chapter_number)
                    """
                )
            )

            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS feedback_action_records (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        signal_key TEXT NOT NULL,
                        signal_type TEXT NOT NULL DEFAULT '',
                        action_type TEXT NOT NULL DEFAULT '',
                        triggered_at_chapter INTEGER NOT NULL DEFAULT 0,
                        cooldown_until_chapter INTEGER NOT NULL DEFAULT 0,
                        notes TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_feedback_actions_project_key
                    ON feedback_action_records(project_id, signal_key)
                    """
                )
            )
        migrations.append(MigrationSpec("audience_feedback_v1", apply_audience_feedback_v1))

        def apply_audience_feedback_scale_meta_v1(conn) -> None:
            existing_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(signal_window_aggregates)")).fetchall()
            }
            if existing_columns and "estimation_method" not in existing_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE signal_window_aggregates
                        ADD COLUMN estimation_method TEXT NOT NULL DEFAULT 'comment_proxy'
                        """
                    )
                )
            if existing_columns and "scale_confidence" not in existing_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE signal_window_aggregates
                        ADD COLUMN scale_confidence FLOAT NOT NULL DEFAULT 0.35
                        """
                    )
                )

            reader_scale_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(reader_scale_snapshots)")).fetchall()
            }
            if reader_scale_columns and "estimation_method" not in reader_scale_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE reader_scale_snapshots
                        ADD COLUMN estimation_method TEXT NOT NULL DEFAULT 'comment_proxy'
                        """
                    )
                )

        migrations.append(
            MigrationSpec(
                "audience_feedback_scale_meta_v1",
                apply_audience_feedback_scale_meta_v1,
            )
        )

        def apply_audience_feedback_project_scope_v1(conn) -> None:
            raw_comment_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(publisher_raw_comments)"))
            }
            if "project_id" not in raw_comment_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE publisher_raw_comments
                        ADD COLUMN project_id TEXT NOT NULL DEFAULT ''
                        """
                    )
                )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_publisher_raw_comments_project
                    ON publisher_raw_comments(project_id)
                    """
                )
            )

            sync_job_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(publisher_comment_sync_jobs)"))
            }
            if "project_id" not in sync_job_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE publisher_comment_sync_jobs
                        ADD COLUMN project_id TEXT NOT NULL DEFAULT ''
                        """
                    )
                )

            exact_title_map: dict[str, str] = {}
            duplicate_titles: set[str] = set()
            for project_id, title in conn.execute(
                text("SELECT id, title FROM projects")
            ).all():
                normalized_title = str(title or "").strip()
                if not normalized_title:
                    continue
                if normalized_title in exact_title_map:
                    duplicate_titles.add(normalized_title)
                    exact_title_map.pop(normalized_title, None)
                    continue
                exact_title_map[normalized_title] = str(project_id)
            for title in duplicate_titles:
                exact_title_map.pop(title, None)

            if exact_title_map:
                raw_rows = conn.execute(
                    text("SELECT id, work_name, project_id FROM publisher_raw_comments")
                ).all()
                for row_id, work_name, project_id in raw_rows:
                    normalized_title = str(work_name or "").strip()
                    if str(project_id or "").strip() or normalized_title not in exact_title_map:
                        continue
                    conn.execute(
                        text(
                            """
                            UPDATE publisher_raw_comments
                            SET project_id = :project_id
                            WHERE id = :row_id
                            """
                        ),
                        {
                            "project_id": exact_title_map[normalized_title],
                            "row_id": row_id,
                        },
                    )

                job_rows = conn.execute(
                    text("SELECT id, work_name, project_id FROM publisher_comment_sync_jobs")
                ).all()
                for row_id, work_name, project_id in job_rows:
                    normalized_title = str(work_name or "").strip()
                    if str(project_id or "").strip() or normalized_title not in exact_title_map:
                        continue
                    conn.execute(
                        text(
                            """
                            UPDATE publisher_comment_sync_jobs
                            SET project_id = :project_id
                            WHERE id = :row_id
                            """
                        ),
                        {
                            "project_id": exact_title_map[normalized_title],
                            "row_id": row_id,
                        },
                    )

        migrations.append(
            MigrationSpec(
                "audience_feedback_project_scope_v1",
                apply_audience_feedback_project_scope_v1,
            )
        )

        def apply_publisher_extension_platform_state_v1(conn) -> None:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS publisher_extension_platform_states (
                        client_id TEXT NOT NULL,
                        platform_id TEXT NOT NULL,
                        connected INTEGER NOT NULL DEFAULT 0,
                        login_method TEXT NOT NULL DEFAULT '',
                        status_json TEXT NOT NULL DEFAULT '{}',
                        last_error TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_heartbeat_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (client_id, platform_id),
                        FOREIGN KEY(client_id) REFERENCES publisher_extension_clients(client_id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_publisher_extension_platform_states_platform
                    ON publisher_extension_platform_states(platform_id, connected)
                    """
                )
            )
        migrations.append(
            MigrationSpec(
                "publisher_extension_platform_state_v1",
                apply_publisher_extension_platform_state_v1,
            )
        )

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

        def apply_project_automation_v1(conn) -> None:
            columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(projects)"))
            }
            if "automation_json" not in columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE projects
                        ADD COLUMN automation_json TEXT NOT NULL DEFAULT '{}'
                        """
                    )
                )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_projects_updated_at
                    ON projects(updated_at DESC)
                    """
                )
            )

        migrations.append(MigrationSpec("project_automation_v1", apply_project_automation_v1))

        def apply_publisher_upload_job_abort_v1(conn) -> None:
            columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(publisher_upload_jobs)"))
            }
            if "abort_requested" not in columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE publisher_upload_jobs
                        ADD COLUMN abort_requested INTEGER NOT NULL DEFAULT 0
                        """
                    )
                )
            if "deleted_at" not in columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE publisher_upload_jobs
                        ADD COLUMN deleted_at TEXT NULL
                        """
                    )
                )
        migrations.append(
            MigrationSpec("publisher_upload_job_abort_v1", apply_publisher_upload_job_abort_v1)
        )

        def apply_publisher_upload_job_project_v1(conn) -> None:
            columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(publisher_upload_jobs)"))
            }
            if "project_id" not in columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE publisher_upload_jobs
                        ADD COLUMN project_id TEXT NOT NULL DEFAULT ''
                        """
                    )
                )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_publisher_upload_jobs_project
                    ON publisher_upload_jobs(project_id, updated_at DESC)
                    """
                )
            )

            rows = conn.execute(
                text(
                    """
                    SELECT id, book_name, project_id
                    FROM publisher_upload_jobs
                    """
                )
            ).fetchall()
            for row in rows:
                existing_project_id = str(row[2] or "").strip()
                if existing_project_id:
                    continue
                book_name = str(row[1] or "").strip()
                if not book_name:
                    continue
                matches = conn.execute(
                    text(
                        """
                        SELECT id
                        FROM projects
                        WHERE title = :title
                        LIMIT 2
                        """
                    ),
                    {"title": book_name},
                ).fetchall()
                if len(matches) != 1:
                    continue
                conn.execute(
                    text(
                        """
                        UPDATE publisher_upload_jobs
                        SET project_id = :project_id
                        WHERE id = :job_id
                        """
                    ),
                    {
                        "project_id": str(matches[0][0] or ""),
                        "job_id": str(row[0] or ""),
                    },
                )

        migrations.append(
            MigrationSpec("publisher_upload_job_project_v1", apply_publisher_upload_job_project_v1)
        )

        def apply_publisher_browser_session_entries_v1(conn) -> None:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS publisher_browser_session_entries (
                        client_id TEXT NOT NULL,
                        platform_id TEXT NOT NULL,
                        cookie_count INTEGER NOT NULL DEFAULT 0,
                        cookies_json TEXT NOT NULL DEFAULT '[]',
                        synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_verified_at TEXT NULL,
                        last_error TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (client_id, platform_id),
                        FOREIGN KEY(client_id) REFERENCES publisher_extension_clients(client_id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_publisher_browser_session_entries_platform_synced
                    ON publisher_browser_session_entries(platform_id, synced_at)
                    """
                )
            )
            existing_rows = conn.execute(
                text(
                    """
                    SELECT platform_id, extension_client_id, cookie_count, cookies_json,
                           synced_at, last_verified_at, last_error, updated_at
                    FROM publisher_browser_sessions
                    """
                )
            ).mappings()
            for row in existing_rows:
                client_id = str(row["extension_client_id"] or "").strip() or "legacy-browser-session"
                try:
                    conn.execute(
                        text(
                            """
                            INSERT OR IGNORE INTO publisher_extension_clients(
                                client_id,
                                extension_version,
                                browser_name,
                                browser_version,
                                backend_base_url
                            )
                            VALUES (
                                :client_id,
                                '',
                                'legacy',
                                '',
                                ''
                            )
                            """
                        ),
                        {"client_id": client_id},
                    )
                    conn.execute(
                        text(
                            """
                            INSERT OR IGNORE INTO publisher_browser_session_entries(
                                client_id,
                                platform_id,
                                cookie_count,
                                cookies_json,
                                synced_at,
                                last_verified_at,
                                last_error,
                                updated_at
                            )
                            VALUES (
                                :client_id,
                                :platform_id,
                                :cookie_count,
                                :cookies_json,
                                :synced_at,
                                :last_verified_at,
                                :last_error,
                                :updated_at
                            )
                            """
                        ),
                        {
                            "client_id": client_id,
                            "platform_id": row["platform_id"],
                            "cookie_count": row["cookie_count"],
                            "cookies_json": row["cookies_json"],
                            "synced_at": row["synced_at"],
                            "last_verified_at": row["last_verified_at"],
                            "last_error": row["last_error"],
                            "updated_at": row["updated_at"],
                        },
                    )
                except Exception:
                    continue

        migrations.append(
            MigrationSpec(
                "publisher_browser_session_entries_v1",
                apply_publisher_browser_session_entries_v1,
            )
        )

        def apply_project_target_total_chapters_v1(conn) -> None:
            columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(projects)")).fetchall()
            }
            if "target_total_chapters" not in columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE projects
                        ADD COLUMN target_total_chapters INTEGER NOT NULL DEFAULT 3
                        """
                    )
                )
            conn.execute(
                text(
                    """
                    UPDATE projects
                    SET target_total_chapters = 3
                    WHERE target_total_chapters IS NULL OR target_total_chapters <= 0
                    """
                )
            )

        migrations.append(
            MigrationSpec(
                "project_target_total_chapters_v1",
                apply_project_target_total_chapters_v1,
            )
        )

        def apply_project_target_total_chapters_consistency_v1(conn) -> None:
            columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(projects)")).fetchall()
            }
            if "target_total_chapters" not in columns:
                return
            conn.execute(
                text(
                    """
                    UPDATE projects
                    SET target_total_chapters = (
                        SELECT COUNT(*)
                        FROM chapter_plans
                        WHERE chapter_plans.project_id = projects.id
                    )
                    WHERE COALESCE(target_total_chapters, 0) < (
                        SELECT COUNT(*)
                        FROM chapter_plans
                        WHERE chapter_plans.project_id = projects.id
                    )
                    """
                )
            )

        migrations.append(
            MigrationSpec(
                "project_target_total_chapters_consistency_v1",
                apply_project_target_total_chapters_consistency_v1,
            )
        )

        def apply_governance_layer_v1(conn) -> None:
            project_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(projects)"))
            }
            if "governance_json" not in project_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE projects
                        ADD COLUMN governance_json TEXT NOT NULL DEFAULT '{}'
                        """
                    )
                )

            chapter_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(chapter_plans)"))
            }
            if "task_contract_json" not in chapter_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE chapter_plans
                        ADD COLUMN task_contract_json TEXT NOT NULL DEFAULT '[]'
                        """
                    )
                )

            band_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(band_experience_plans)"))
            }
            if "task_contract_json" not in band_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE band_experience_plans
                        ADD COLUMN task_contract_json TEXT NOT NULL DEFAULT '[]'
                        """
                    )
                )

            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS band_checkpoints (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        arc_id TEXT NOT NULL,
                        band_id TEXT NOT NULL DEFAULT '',
                        chapter_start INTEGER NOT NULL DEFAULT 0,
                        chapter_end INTEGER NOT NULL DEFAULT 0,
                        trigger_source TEXT NOT NULL DEFAULT 'auto_band_end',
                        boundary_kind TEXT NOT NULL DEFAULT 'band_end',
                        boundary_chapter INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'pending',
                        summary TEXT NOT NULL DEFAULT '',
                        reason TEXT NOT NULL DEFAULT '',
                        issues_json TEXT NOT NULL DEFAULT '[]',
                        related_task_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        resolved_at TEXT NULL,
                        FOREIGN KEY(project_id) REFERENCES projects(id),
                        FOREIGN KEY(arc_id) REFERENCES arc_plan_versions(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_band_checkpoints_project_band_created
                    ON band_checkpoints(project_id, band_id, created_at)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_band_checkpoints_project_status_created
                    ON band_checkpoints(project_id, status, created_at)
                    """
                )
            )

            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS narrative_constraints (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        arc_id TEXT NOT NULL DEFAULT '',
                        band_id TEXT NOT NULL DEFAULT '',
                        constraint_type TEXT NOT NULL DEFAULT 'character_availability',
                        level TEXT NOT NULL DEFAULT 'hard',
                        subject_name TEXT NOT NULL DEFAULT '',
                        description TEXT NOT NULL DEFAULT '',
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        effective_from_chapter INTEGER NOT NULL DEFAULT 1,
                        protect_until_chapter INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_narrative_constraints_project_status
                    ON narrative_constraints(project_id, status)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_narrative_constraints_project_band
                    ON narrative_constraints(project_id, band_id)
                    """
                )
            )

            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS decision_events (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        task_id TEXT NOT NULL DEFAULT '',
                        band_id TEXT NOT NULL DEFAULT '',
                        chapter_number INTEGER NOT NULL DEFAULT 0,
                        scope TEXT NOT NULL DEFAULT 'project',
                        event_family TEXT NOT NULL DEFAULT 'business_event',
                        event_type TEXT NOT NULL DEFAULT '',
                        actor_type TEXT NOT NULL DEFAULT 'system',
                        actor_id TEXT NOT NULL DEFAULT '',
                        summary TEXT NOT NULL DEFAULT '',
                        reason TEXT NOT NULL DEFAULT '',
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        related_object_type TEXT NOT NULL DEFAULT '',
                        related_object_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_decision_events_project_created
                    ON decision_events(project_id, created_at)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_decision_events_project_scope_created
                    ON decision_events(project_id, scope, created_at)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_decision_events_project_band_created
                    ON decision_events(project_id, band_id, created_at)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_decision_events_project_chapter_created
                    ON decision_events(project_id, chapter_number, created_at)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_decision_events_task_created
                    ON decision_events(task_id, created_at)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_decision_events_related_object
                    ON decision_events(related_object_type, related_object_id, created_at)
                    """
                )
            )

        migrations.append(MigrationSpec("governance_layer_v1", apply_governance_layer_v1))

        def apply_decision_event_causality_v1(conn) -> None:
            decision_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(decision_events)"))
            }
            if "parent_event_id" not in decision_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE decision_events
                        ADD COLUMN parent_event_id TEXT NOT NULL DEFAULT ''
                        """
                    )
                )
            if "causal_root_id" not in decision_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE decision_events
                        ADD COLUMN causal_root_id TEXT NOT NULL DEFAULT ''
                        """
                    )
                )
            conn.execute(
                text(
                    """
                    UPDATE decision_events
                    SET causal_root_id = id
                    WHERE IFNULL(causal_root_id, '') = ''
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_decision_events_causal_root_created
                    ON decision_events(causal_root_id, created_at)
                    """
                )
            )

        migrations.append(MigrationSpec("decision_event_causality_v1", apply_decision_event_causality_v1))

        def apply_subworld_control_v1(conn) -> None:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS sub_worlds (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        origin_arc_id TEXT NULL,
                        parent_subworld_id TEXT NULL,
                        name TEXT NOT NULL DEFAULT '',
                        purpose TEXT NOT NULL DEFAULT '',
                        scope TEXT NOT NULL DEFAULT 'arc_local',
                        status TEXT NOT NULL DEFAULT 'active',
                        introduced_at_chapter INTEGER NOT NULL DEFAULT 0,
                        retired_at_chapter INTEGER NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_sub_worlds_project_status
                    ON sub_worlds(project_id, status)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_sub_worlds_project_scope
                    ON sub_worlds(project_id, scope)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_sub_worlds_project_origin_arc
                    ON sub_worlds(project_id, origin_arc_id)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS sub_world_roster_items (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        subworld_id TEXT NOT NULL,
                        entity_id TEXT NULL,
                        entity_kind TEXT NOT NULL DEFAULT 'character',
                        display_name TEXT NOT NULL DEFAULT '',
                        slot_key TEXT NOT NULL DEFAULT '',
                        role_hint TEXT NOT NULL DEFAULT '',
                        description TEXT NOT NULL DEFAULT '',
                        is_core INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'planned_slot',
                        activation_chapter INTEGER NOT NULL DEFAULT 0,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id),
                        FOREIGN KEY(subworld_id) REFERENCES sub_worlds(id),
                        FOREIGN KEY(entity_id) REFERENCES entities(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_sub_world_roster_project_subworld
                    ON sub_world_roster_items(project_id, subworld_id)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_sub_world_roster_project_status
                    ON sub_world_roster_items(project_id, status)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_sub_world_roster_project_display
                    ON sub_world_roster_items(project_id, display_name)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_sub_world_roster_project_slot
                    ON sub_world_roster_items(project_id, slot_key)
                    """
                )
            )

            projects = conn.execute(text("SELECT id FROM projects")).mappings().all()
            for project in projects:
                project_id = str(project["id"])
                global_row = conn.execute(
                    text(
                        """
                        SELECT id
                        FROM sub_worlds
                        WHERE project_id = :project_id
                          AND scope = 'global_core'
                        ORDER BY created_at ASC, id ASC
                        LIMIT 1
                        """
                    ),
                    {"project_id": project_id},
                ).mappings().first()
                if global_row is None:
                    global_id = new_id()
                    conn.execute(
                        text(
                            """
                            INSERT INTO sub_worlds(
                                id, project_id, origin_arc_id, parent_subworld_id,
                                name, purpose, scope, status, introduced_at_chapter,
                                retired_at_chapter, metadata_json, created_at, updated_at
                            ) VALUES (
                                :id, :project_id, NULL, NULL,
                                :name, :purpose, 'global_core', 'active', 0,
                                NULL, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                            )
                            """
                        ),
                        {
                            "id": global_id,
                            "project_id": project_id,
                            "name": "global_core",
                            "purpose": "项目级核心常驻角色池",
                        },
                    )
                else:
                    global_id = str(global_row["id"])

                rostered_entity_ids = {
                    str(row["entity_id"])
                    for row in conn.execute(
                        text(
                            """
                            SELECT entity_id
                            FROM sub_world_roster_items
                            WHERE project_id = :project_id
                              AND entity_id IS NOT NULL
                            """
                        ),
                        {"project_id": project_id},
                    ).mappings().all()
                    if str(row["entity_id"] or "").strip()
                }
                active_characters = conn.execute(
                    text(
                        """
                        SELECT id, name, description
                        FROM entities
                        WHERE project_id = :project_id
                          AND kind = 'character'
                          AND is_active = 1
                        """
                    ),
                    {"project_id": project_id},
                ).mappings().all()
                for entity in active_characters:
                    entity_id = str(entity["id"])
                    if entity_id in rostered_entity_ids:
                        continue
                    conn.execute(
                        text(
                            """
                            INSERT INTO sub_world_roster_items(
                                id, project_id, subworld_id, entity_id, entity_kind,
                                display_name, slot_key, role_hint, description,
                                is_core, status, activation_chapter, metadata_json,
                                created_at, updated_at
                            ) VALUES (
                                :id, :project_id, :subworld_id, :entity_id, 'character',
                                :display_name, '', '', :description,
                                1, 'seeded_named', 0, '{}',
                                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                            )
                            """
                        ),
                        {
                            "id": new_id(),
                            "project_id": project_id,
                            "subworld_id": global_id,
                            "entity_id": entity_id,
                            "display_name": str(entity["name"] or ""),
                            "description": str(entity["description"] or ""),
                        },
                    )

        migrations.append(MigrationSpec("subworld_control_v1", apply_subworld_control_v1))

        def apply_book_genesis_v1(conn) -> None:
            def _column_names(table_name: str) -> set[str]:
                rows = conn.execute(text(f"PRAGMA table_info({table_name})")).mappings().all()
                return {str(row["name"]) for row in rows}

            project_columns = _column_names("projects")
            if "creation_status" not in project_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE projects
                        ADD COLUMN creation_status TEXT NOT NULL DEFAULT 'legacy'
                        """
                    )
                )
            if "active_genesis_revision_id" not in project_columns:
                conn.execute(
                    text(
                        """
                        ALTER TABLE projects
                        ADD COLUMN active_genesis_revision_id TEXT NOT NULL DEFAULT ''
                        """
                    )
                )

            arc_columns = _column_names("arc_plan_versions")
            for column_name, ddl in (
                ("arc_number", "ALTER TABLE arc_plan_versions ADD COLUMN arc_number INTEGER NOT NULL DEFAULT 1"),
                ("chapter_start", "ALTER TABLE arc_plan_versions ADD COLUMN chapter_start INTEGER NOT NULL DEFAULT 1"),
                ("chapter_end", "ALTER TABLE arc_plan_versions ADD COLUMN chapter_end INTEGER NOT NULL DEFAULT 0"),
                (
                    "planned_target_size",
                    "ALTER TABLE arc_plan_versions ADD COLUMN planned_target_size INTEGER NOT NULL DEFAULT 0",
                ),
                (
                    "planned_soft_min",
                    "ALTER TABLE arc_plan_versions ADD COLUMN planned_soft_min INTEGER NOT NULL DEFAULT 0",
                ),
                (
                    "planned_soft_max",
                    "ALTER TABLE arc_plan_versions ADD COLUMN planned_soft_max INTEGER NOT NULL DEFAULT 0",
                ),
            ):
                if column_name not in arc_columns:
                    conn.execute(text(ddl))

            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS book_genesis_revisions (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        revision INTEGER NOT NULL DEFAULT 1,
                        based_on_revision_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'draft',
                        pack_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_book_genesis_revisions_project_created
                    ON book_genesis_revisions(project_id, created_at)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS prompt_traces (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        genesis_revision_id TEXT NOT NULL DEFAULT '',
                        decision_event_id TEXT NOT NULL DEFAULT '',
                        parent_trace_id TEXT NOT NULL DEFAULT '',
                        trace_scope TEXT NOT NULL DEFAULT 'genesis',
                        stage_key TEXT NOT NULL DEFAULT '',
                        template_id TEXT NOT NULL DEFAULT '',
                        template_version TEXT NOT NULL DEFAULT 'v1',
                        effective_system_prompt TEXT NOT NULL DEFAULT '',
                        prompt_layers_json TEXT NOT NULL DEFAULT '[]',
                        input_snapshot_json TEXT NOT NULL DEFAULT '{}',
                        model_profile_json TEXT NOT NULL DEFAULT '{}',
                        attempts_json TEXT NOT NULL DEFAULT '[]',
                        output_summary_json TEXT NOT NULL DEFAULT '{}',
                        backend TEXT NOT NULL DEFAULT '',
                        codex_job_id TEXT NOT NULL DEFAULT '',
                        permission_profile TEXT NOT NULL DEFAULT '',
                        fallback_used INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_prompt_traces_project_stage_created
                    ON prompt_traces(project_id, stage_key, created_at)
                    """
                )
            )

            conn.execute(
                text(
                    """
                    UPDATE projects
                    SET creation_status = CASE
                        WHEN COALESCE(creation_status, '') = '' THEN 'legacy'
                        ELSE creation_status
                    END
                    """
                )
            )

        migrations.append(MigrationSpec("book_genesis_v1", apply_book_genesis_v1))

        def apply_prompt_trace_codex_metadata_v1(conn) -> None:
            columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(prompt_traces)"))
            }
            for column_name, ddl in (
                ("backend", "ALTER TABLE prompt_traces ADD COLUMN backend TEXT NOT NULL DEFAULT ''"),
                ("codex_job_id", "ALTER TABLE prompt_traces ADD COLUMN codex_job_id TEXT NOT NULL DEFAULT ''"),
                ("permission_profile", "ALTER TABLE prompt_traces ADD COLUMN permission_profile TEXT NOT NULL DEFAULT ''"),
                ("fallback_used", "ALTER TABLE prompt_traces ADD COLUMN fallback_used INTEGER NOT NULL DEFAULT 0"),
            ):
                if column_name not in columns:
                    conn.execute(text(ddl))

        migrations.append(MigrationSpec("prompt_trace_codex_metadata_v1", apply_prompt_trace_codex_metadata_v1))

        def apply_review_repair_chain_v1(conn) -> None:
            chapter_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(chapter_plans)"))
            }
            for column_name, ddl in (
                (
                    "acceptance_mode",
                    """
                    ALTER TABLE chapter_plans
                    ADD COLUMN acceptance_mode TEXT NOT NULL DEFAULT ''
                    """,
                ),
                (
                    "repair_attempt_count",
                    """
                    ALTER TABLE chapter_plans
                    ADD COLUMN repair_attempt_count INTEGER NOT NULL DEFAULT 0
                    """,
                ),
                (
                    "residual_review_issues_json",
                    """
                    ALTER TABLE chapter_plans
                    ADD COLUMN residual_review_issues_json TEXT NOT NULL DEFAULT '[]'
                    """,
                ),
                (
                    "canon_risk_level",
                    """
                    ALTER TABLE chapter_plans
                    ADD COLUMN canon_risk_level TEXT NOT NULL DEFAULT ''
                    """,
                ),
            ):
                if column_name not in chapter_columns:
                    conn.execute(text(ddl))

            rewrite_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(chapter_rewrite_attempts)"))
            }
            for column_name, ddl in (
                (
                    "result_review_id",
                    """
                    ALTER TABLE chapter_rewrite_attempts
                    ADD COLUMN result_review_id TEXT NOT NULL DEFAULT ''
                    """,
                ),
                (
                    "failure_reason",
                    """
                    ALTER TABLE chapter_rewrite_attempts
                    ADD COLUMN failure_reason TEXT NOT NULL DEFAULT ''
                    """,
                ),
                (
                    "verification_json",
                    """
                    ALTER TABLE chapter_rewrite_attempts
                    ADD COLUMN verification_json TEXT NOT NULL DEFAULT '{}'
                    """,
                ),
                (
                    "source_chapter_plan_json",
                    """
                    ALTER TABLE chapter_rewrite_attempts
                    ADD COLUMN source_chapter_plan_json TEXT NOT NULL DEFAULT '{}'
                    """,
                ),
                (
                    "result_chapter_plan_json",
                    """
                    ALTER TABLE chapter_rewrite_attempts
                    ADD COLUMN result_chapter_plan_json TEXT NOT NULL DEFAULT '{}'
                    """,
                ),
                (
                    "source_band_plan_json",
                    """
                    ALTER TABLE chapter_rewrite_attempts
                    ADD COLUMN source_band_plan_json TEXT NOT NULL DEFAULT '{}'
                    """,
                ),
                (
                    "result_band_plan_json",
                    """
                    ALTER TABLE chapter_rewrite_attempts
                    ADD COLUMN result_band_plan_json TEXT NOT NULL DEFAULT '{}'
                    """,
                ),
            ):
                if column_name not in rewrite_columns:
                    conn.execute(text(ddl))

        migrations.append(MigrationSpec("review_repair_chain_v1", apply_review_repair_chain_v1))

        def apply_world_model_v1(conn) -> None:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS world_model_snapshots (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        as_of_chapter INTEGER NOT NULL DEFAULT 0,
                        version INTEGER NOT NULL DEFAULT 1,
                        status TEXT NOT NULL DEFAULT 'live',
                        snapshot_json TEXT NOT NULL DEFAULT '{}',
                        source_digest TEXT NOT NULL DEFAULT '',
                        compiled_from_event_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_world_model_snapshots_project_chapter
                    ON world_model_snapshots(project_id, as_of_chapter)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_world_model_snapshots_project_status
                    ON world_model_snapshots(project_id, status)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_world_model_snapshots_project_digest
                    ON world_model_snapshots(project_id, source_digest)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS world_model_pages (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        page_key TEXT NOT NULL,
                        page_type TEXT NOT NULL DEFAULT 'overview',
                        title TEXT NOT NULL DEFAULT '',
                        vault_path TEXT NOT NULL DEFAULT '',
                        markdown TEXT NOT NULL DEFAULT '',
                        frontmatter_json TEXT NOT NULL DEFAULT '{}',
                        content_hash TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1,
                        status TEXT NOT NULL DEFAULT 'canon_live',
                        as_of_chapter INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_world_model_pages_project_key
                    ON world_model_pages(project_id, page_key)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_world_model_pages_project_type
                    ON world_model_pages(project_id, page_type)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_world_model_pages_project_status
                    ON world_model_pages(project_id, status)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS world_model_links (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        source_page_id TEXT NOT NULL,
                        target_page_id TEXT NOT NULL,
                        relation_type TEXT NOT NULL DEFAULT 'related',
                        evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id),
                        FOREIGN KEY(source_page_id) REFERENCES world_model_pages(id),
                        FOREIGN KEY(target_page_id) REFERENCES world_model_pages(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_world_model_links_project_source
                    ON world_model_links(project_id, source_page_id)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_world_model_links_project_target
                    ON world_model_links(project_id, target_page_id)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS world_edit_proposals (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT 'obsidian',
                        target_page_key TEXT NOT NULL DEFAULT '',
                        target_field TEXT NOT NULL DEFAULT '',
                        proposed_patch_json TEXT NOT NULL DEFAULT '{}',
                        reason TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_by TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        reviewed_at TEXT,
                        FOREIGN KEY(project_id) REFERENCES projects(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_world_edit_proposals_project_status
                    ON world_edit_proposals(project_id, status)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_world_edit_proposals_project_page
                    ON world_edit_proposals(project_id, target_page_key)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS world_model_conflicts (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        conflict_type TEXT NOT NULL DEFAULT '',
                        severity TEXT NOT NULL DEFAULT 'warning',
                        subject_key TEXT NOT NULL DEFAULT '',
                        description TEXT NOT NULL DEFAULT '',
                        evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                        status TEXT NOT NULL DEFAULT 'open',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        resolved_at TEXT,
                        FOREIGN KEY(project_id) REFERENCES projects(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_world_model_conflicts_project_status
                    ON world_model_conflicts(project_id, status)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_world_model_conflicts_project_type
                    ON world_model_conflicts(project_id, conflict_type)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS world_model_compile_runs (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        trigger TEXT NOT NULL DEFAULT '',
                        as_of_chapter INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'started',
                        source_refs_json TEXT NOT NULL DEFAULT '[]',
                        source_digest TEXT NOT NULL DEFAULT '',
                        snapshot_id TEXT NOT NULL DEFAULT '',
                        error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(project_id) REFERENCES projects(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_world_model_compile_runs_project_chapter
                    ON world_model_compile_runs(project_id, as_of_chapter)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_world_model_compile_runs_project_status
                    ON world_model_compile_runs(project_id, status)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_world_model_compile_runs_project_digest
                    ON world_model_compile_runs(project_id, source_digest)
                    """
                )
            )

        migrations.append(MigrationSpec("world_model_v1", apply_world_model_v1))

        for migration in migrations:
            _run_migration(conn, migration.version, migration.apply_fn)
