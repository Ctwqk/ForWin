from __future__ import annotations

from collections import defaultdict
from contextlib import nullcontext
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from forwin import models
from forwin.models import Base
from forwin.models import base as model_base


ROOT = Path(__file__).resolve().parents[1]
BASELINE_MODULE = "forwin.migrations.versions.0001_v5_baseline"
RECOVERY_TABLES = {
    "outbox_events",
    "projection_checkpoints",
    "post_canon_maintenance_runs",
    "publisher_upload_jobs",
    "publisher_upload_attempts",
    "publisher_upload_receipts",
    "publisher_operator_actions",
}
# Schema added by migrations after 0001. The baseline comparison below only
# checks what 0001 itself creates, so these are removed from the ORM side.
TABLES_ADDED_AFTER_BASELINE = {
    "story_calendars", "book_lexicons", "world_plan_conflicts",
    "arc_beat_sheets", "band_plans", "world_conflict_result_refs",
}
COLUMNS_ADDED_AFTER_BASELINE = {
    "graph_deltas": {"story_time_start", "story_time_end", "tick_id", "beat_sheet_id", "seq"},
    "world_node_states": {"name", "aliases_json", "profile_json", "is_active", "seq"},
    "map_snapshots": {"seq"},
    "book_cognition_snapshots": {"seq"},
    "projection_checkpoints": {"run_seq"},
    "cognition_overlays": {"knowledge_by_ref_json"},
    "world_snapshots": {"narrative_node_index_json", "narrative_edge_index_json", "seq"},
    "arc_plan_versions": {"title", "goal", "stakes", "payoff_direction"},
    "chapter_plans": {
        "band_plan_id", "pov_ref", "onstage_refs_json", "location_refs_json",
        "world_bindings_json", "reveal_directives_json", "hint_directives_json",
        "intended_changes_json", "milestone_ids_json", "expected_span_minutes", "hook",
    },
}


def _table(name: str) -> sa.Table:
    assert name in Base.metadata.tables, f"{name} is not registered in Base.metadata"
    return Base.metadata.tables[name]


def _column_default(table: sa.Table, name: str) -> object:
    default = table.c[name].default
    assert default is not None, f"{table.name}.{name} has no ORM default"
    return default.arg


def _assert_server_defaults(table: sa.Table, expected: dict[str, str]) -> None:
    assert {
        name: _server_default(table.c[name]) for name in expected
    } == expected


def _server_default(column: sa.Column[Any]) -> str | None:
    if column.server_default is None:
        return None
    return str(column.server_default.arg)


def _foreign_keys(table: sa.Table) -> dict[str, set[str]]:
    return {
        column.name: {foreign_key.target_fullname for foreign_key in column.foreign_keys}
        for column in table.columns
        if column.foreign_keys
    }


def _unique_constraints(table: sa.Table) -> set[tuple[str | None, tuple[str, ...]]]:
    return {
        (constraint.name, tuple(column.name for column in constraint.columns))
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }


def _metadata_indexes(
    table: sa.Table,
) -> set[tuple[str | None, tuple[str, ...], bool, str | None]]:
    indexes = set()
    for index in table.indexes:
        where = index.dialect_options["postgresql"].get("where")
        indexes.add(
            (
                index.name,
                tuple(expression.name for expression in index.expressions),
                index.unique,
                str(where) if where is not None else None,
            )
        )
    return indexes


@pytest.fixture
def baseline_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    migration = import_module(BASELINE_MODULE)
    tables: dict[str, sa.Table] = {}
    indexes: dict[
        str, set[tuple[str, tuple[str, ...], bool, str | None]]
    ] = defaultdict(set)
    create_order: list[str] = []
    drop_order: list[str] = []

    def create_table(name: str, *elements: Any, **_: Any) -> sa.Table:
        table = sa.Table(name, sa.MetaData(), *elements)
        tables[name] = table
        create_order.append(name)
        return table

    def create_index(
        name: str,
        table_name: str,
        columns: list[str],
        *,
        unique: bool = False,
        **kwargs: Any,
    ) -> None:
        where = kwargs.get("postgresql_where")
        indexes[table_name].add(
            (
                name,
                tuple(columns),
                unique,
                str(where) if where is not None else None,
            )
        )

    monkeypatch.setattr(migration.op, "f", lambda name: name)
    monkeypatch.setattr(migration.op, "create_table", create_table)
    monkeypatch.setattr(migration.op, "create_index", create_index)
    monkeypatch.setattr(migration.op, "drop_index", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        migration.op,
        "drop_table",
        lambda name, **_kwargs: drop_order.append(name),
    )

    migration.upgrade()
    migration.downgrade()
    return SimpleNamespace(
        migration=migration,
        tables=tables,
        indexes=indexes,
        create_order=create_order,
        drop_order=drop_order,
    )


def _created_schema(
    monkeypatch: pytest.MonkeyPatch, run: Any
) -> tuple[dict[str, sa.Table], dict[str, set[tuple[str, tuple[str, ...], bool]]]]:
    """Tables and indexes that ``run()`` creates through alembic ``op``."""
    tables: dict[str, sa.Table] = {}
    indexes: dict[str, set[tuple[str, tuple[str, ...], bool]]] = defaultdict(set)

    def create_table(name: str, *elements: Any, **_: Any) -> sa.Table:
        tables[name] = sa.Table(name, sa.MetaData(), *elements)
        return tables[name]

    def create_index(name: str, table_name: str, columns: list[str], *, unique: bool = False, **_: Any) -> None:
        indexes[table_name].add((name, tuple(columns), unique))

    op = import_module("alembic.op")
    monkeypatch.setattr(op, "f", lambda name: name)
    monkeypatch.setattr(op, "create_table", create_table)
    monkeypatch.setattr(op, "create_index", create_index)
    monkeypatch.setattr(op, "drop_index", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(op, "drop_table", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(op, "execute", lambda *_args, **_kwargs: None)
    run()
    return tables, indexes


def _column_shape(table: sa.Table) -> list[tuple[str, type, bool, bool]]:
    return [
        (column.name, type(column.type), column.nullable, column.primary_key)
        for column in table.columns
    ]


def test_recovery_models_are_registered_and_exported() -> None:
    assert RECOVERY_TABLES <= set(Base.metadata.tables)
    for name in (
        "ProjectionCheckpoint",
        "PostCanonMaintenanceRun",
        "PublisherUploadAttempt",
        "PublisherUploadReceipt",
        "PublisherOperatorAction",
    ):
        assert hasattr(models, name)


def test_book_frame_models_are_registered() -> None:
    assert hasattr(models, "StoryCalendarRow")
    assert hasattr(models, "BookLexiconRow")
    assert TABLES_ADDED_AFTER_BASELINE <= set(Base.metadata.tables)


def test_outbox_has_fenced_lease_columns_and_claim_indexes() -> None:
    table = _table("outbox_events")

    assert table.c.worker_id.nullable is False
    assert _column_default(table, "worker_id") == ""
    assert _server_default(table.c.worker_id) == ""
    assert table.c.lease_epoch.nullable is False
    assert _column_default(table, "lease_epoch") == 0
    assert _server_default(table.c.lease_epoch) == "0"
    assert table.c.lease_expires_at.nullable is True
    assert table.c.heartbeat_at.nullable is True
    assert "locked_by" not in table.c
    assert "locked_at" not in table.c

    index_names = {index.name for index in table.indexes}
    assert "ix_outbox_events_status_available" in index_names
    assert "ix_outbox_events_status_lease_expires" in index_names


def test_projection_checkpoint_contract() -> None:
    table = _table("projection_checkpoints")

    expected_columns = {
        "id",
        "project_id",
        "projection_kind",
        "status",
        "target_canon_commit_id",
        "target_chapter_number",
        "projected_canon_commit_id",
        "projected_chapter_number",
        "last_event_id",
        "source_digest",
        "last_error",
        "run_seq",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
    }
    assert set(table.c.keys()) == expected_columns
    assert _foreign_keys(table) == {
        "project_id": {"projects.id"},
        "target_canon_commit_id": {"canon_commit_records.id"},
        "projected_canon_commit_id": {"canon_commit_records.id"},
    }
    assert table.c.target_canon_commit_id.nullable is True
    assert table.c.projected_canon_commit_id.nullable is True
    assert table.c.started_at.nullable is True
    assert table.c.completed_at.nullable is True
    assert {
        name: _column_default(table, name)
        for name in (
            "status",
            "target_chapter_number",
            "projected_chapter_number",
            "last_event_id",
            "source_digest",
            "last_error",
        )
    } == {
        "status": "never",
        "target_chapter_number": 0,
        "projected_chapter_number": 0,
        "last_event_id": "",
        "source_digest": "",
        "last_error": "",
    }
    _assert_server_defaults(
        table,
        {
            "status": "never",
            "target_chapter_number": "0",
            "projected_chapter_number": "0",
            "last_event_id": "",
            "source_digest": "",
            "last_error": "",
        },
    )
    assert (
        "uq_projection_checkpoints_project_kind",
        ("project_id", "projection_kind"),
    ) in _unique_constraints(table)
    assert {index.name for index in table.indexes} == {
        "ix_projection_checkpoints_project_status",
        "ix_projection_checkpoints_status_updated",
    }
    assert not any(isinstance(column.type, sa.Enum) for column in table.columns)


def test_post_canon_maintenance_contract() -> None:
    table = _table("post_canon_maintenance_runs")

    assert set(table.c.keys()) == {
        "id",
        "canon_commit_id",
        "project_id",
        "chapter_number",
        "candidate_id",
        "step_name",
        "idempotency_key",
        "status",
        "attempts",
        "available_at",
        "worker_id",
        "lease_epoch",
        "lease_expires_at",
        "heartbeat_at",
        "result_json",
        "last_error",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
    }
    assert _foreign_keys(table) == {
        "canon_commit_id": {"canon_commit_records.id"},
        "project_id": {"projects.id"},
        "candidate_id": {"candidate_draft_records.id"},
    }
    assert {
        name: _column_default(table, name)
        for name in (
            "status",
            "attempts",
            "worker_id",
            "lease_epoch",
            "result_json",
            "last_error",
        )
    } == {
        "status": "pending",
        "attempts": 0,
        "worker_id": "",
        "lease_epoch": 0,
        "result_json": "{}",
        "last_error": "",
    }
    _assert_server_defaults(
        table,
        {
            "status": "pending",
            "attempts": "0",
            "worker_id": "",
            "lease_epoch": "0",
            "result_json": "{}",
            "last_error": "",
        },
    )
    for name in (
        "available_at",
        "lease_expires_at",
        "heartbeat_at",
        "started_at",
        "completed_at",
    ):
        assert table.c[name].nullable is True
    assert _unique_constraints(table) == {
        (
            "uq_post_canon_maintenance_canon_step",
            ("canon_commit_id", "step_name"),
        ),
        ("uq_post_canon_maintenance_idempotency_key", ("idempotency_key",)),
    }
    assert {index.name for index in table.indexes} == {
        "ix_post_canon_maintenance_project_chapter_status",
        "ix_post_canon_maintenance_status_available",
        "ix_post_canon_maintenance_status_lease_expires",
    }
    assert not any(isinstance(column.type, sa.Enum) for column in table.columns)


def test_publisher_recovery_contract() -> None:
    jobs = _table("publisher_upload_jobs")
    attempts = _table("publisher_upload_attempts")
    receipts = _table("publisher_upload_receipts")

    assert {
        "canon_commit_id",
        "candidate_id",
        "chapter_number",
        "idempotency_key",
        "body_sha256",
        "current_attempt_id",
        "available_at",
        "reconcile_after",
        "paused_at",
        "pause_reason",
    } <= set(jobs.c.keys())
    assert _foreign_keys(jobs) == {"canon_commit_id": {"canon_commit_records.id"}}
    assert jobs.c.canon_commit_id.nullable is True
    for name in ("available_at", "reconcile_after", "paused_at"):
        assert jobs.c[name].nullable is True
    assert {
        name: _column_default(jobs, name)
        for name in (
            "candidate_id",
            "chapter_number",
            "idempotency_key",
            "body_sha256",
            "current_attempt_id",
            "pause_reason",
        )
    } == {
        "candidate_id": "",
        "chapter_number": 0,
        "idempotency_key": "",
        "body_sha256": "",
        "current_attempt_id": "",
        "pause_reason": "",
    }
    _assert_server_defaults(
        jobs,
        {
            "candidate_id": "",
            "chapter_number": "0",
            "idempotency_key": "",
            "body_sha256": "",
            "current_attempt_id": "",
            "pause_reason": "",
        },
    )
    idempotency_index = next(
        index
        for index in jobs.indexes
        if index.name == "ux_publisher_upload_jobs_idempotency_key"
    )
    assert idempotency_index.unique is True
    assert tuple(column.name for column in idempotency_index.columns) == (
        "idempotency_key",
    )
    assert (
        str(idempotency_index.dialect_options["postgresql"]["where"])
        == "idempotency_key <> ''"
    )

    assert _foreign_keys(attempts) == {
        "upload_job_id": {"publisher_upload_jobs.id"}
    }
    assert set(attempts.c.keys()) == {
        "id",
        "upload_job_id",
        "attempt_number",
        "attempt_kind",
        "worker_id",
        "lease_epoch",
        "status",
        "phase",
        "claimed_at",
        "heartbeat_at",
        "lease_expires_at",
        "finished_at",
        "content_sha256",
        "error_code",
        "error_message",
        "result_json",
        "created_at",
        "updated_at",
    }
    assert (
        "uq_publisher_upload_attempts_job_number",
        ("upload_job_id", "attempt_number"),
    ) in _unique_constraints(attempts)
    assert {index.name for index in attempts.indexes} == {
        "ix_publisher_upload_attempts_job_status",
        "ix_publisher_upload_attempts_status_lease_expires",
    }

    assert _foreign_keys(receipts) == {
        "upload_job_id": {"publisher_upload_jobs.id"},
        "upload_attempt_id": {"publisher_upload_attempts.id"},
    }
    assert set(receipts.c.keys()) == {
        "id",
        "upload_job_id",
        "upload_attempt_id",
        "receipt_key",
        "idempotency_key",
        "platform_id",
        "remote_book_id",
        "remote_chapter_id",
        "remote_url",
        "official_state",
        "content_sha256",
        "evidence_json",
        "source",
        "observed_at",
        "created_at",
    }
    assert (
        "uq_publisher_upload_receipts_receipt_key",
        ("receipt_key",),
    ) in _unique_constraints(receipts)
    assert {index.name for index in receipts.indexes} == {
        "ix_publisher_upload_receipts_idempotency_key",
        "ix_publisher_upload_receipts_job_created",
        "ix_publisher_upload_receipts_platform_remote",
    }


def test_baseline_matches_recovery_metadata_and_dependency_order(
    baseline_operations: SimpleNamespace,
) -> None:
    capture = baseline_operations

    for table_name in RECOVERY_TABLES:
        metadata_table = _table(table_name)
        migration_table = capture.tables[table_name]
        baseline_columns = [
            column_name
            for column_name in metadata_table.c.keys()
            if column_name not in COLUMNS_ADDED_AFTER_BASELINE.get(table_name, set())
        ]
        assert list(migration_table.c.keys()) == baseline_columns
        for column_name in baseline_columns:
            metadata_column = metadata_table.c[column_name]
            migration_column = migration_table.c[column_name]
            assert type(migration_column.type) is type(metadata_column.type)
            assert getattr(migration_column.type, "length", None) == getattr(
                metadata_column.type, "length", None
            )
            assert migration_column.nullable is metadata_column.nullable
            assert migration_column.primary_key is metadata_column.primary_key
            assert _server_default(migration_column) == _server_default(metadata_column)
        assert _foreign_keys(migration_table) == _foreign_keys(metadata_table)
        assert _unique_constraints(migration_table) == _unique_constraints(
            metadata_table
        )
        assert capture.indexes[table_name] == _metadata_indexes(metadata_table)

    create_position = {
        table_name: capture.create_order.index(table_name)
        for table_name in capture.create_order
    }
    for parent, dependent in (
        ("projects", "projection_checkpoints"),
        ("canon_commit_records", "projection_checkpoints"),
        ("projects", "post_canon_maintenance_runs"),
        ("candidate_draft_records", "post_canon_maintenance_runs"),
        ("canon_commit_records", "post_canon_maintenance_runs"),
        ("canon_commit_records", "publisher_upload_jobs"),
        ("publisher_upload_jobs", "publisher_upload_attempts"),
        ("publisher_upload_jobs", "publisher_upload_receipts"),
        ("publisher_upload_attempts", "publisher_upload_receipts"),
        ("publisher_upload_jobs", "publisher_operator_actions"),
    ):
        assert create_position[parent] < create_position[dependent]

    drop_position = {
        table_name: capture.drop_order.index(table_name)
        for table_name in capture.drop_order
    }
    for dependent, parent in (
        ("projection_checkpoints", "canon_commit_records"),
        ("projection_checkpoints", "projects"),
        ("post_canon_maintenance_runs", "canon_commit_records"),
        ("post_canon_maintenance_runs", "candidate_draft_records"),
        ("post_canon_maintenance_runs", "projects"),
        ("publisher_upload_receipts", "publisher_upload_attempts"),
        ("publisher_upload_receipts", "publisher_upload_jobs"),
        ("publisher_upload_attempts", "publisher_upload_jobs"),
        ("publisher_operator_actions", "publisher_upload_jobs"),
        ("publisher_upload_jobs", "canon_commit_records"),
    ):
        assert drop_position[dependent] < drop_position[parent]


def test_post_baseline_schema_is_absent_from_0001_and_present_in_orm(
    baseline_operations: SimpleNamespace,
) -> None:
    capture = baseline_operations

    for table_name in TABLES_ADDED_AFTER_BASELINE:
        assert table_name not in capture.tables
        assert table_name in Base.metadata.tables
    for table_name, column_names in COLUMNS_ADDED_AFTER_BASELINE.items():
        assert column_names <= set(_table(table_name).c.keys())
        assert not column_names & set(capture.tables[table_name].c.keys())


def test_baseline_revision_is_rotated_and_followed_only_by_later_migrations() -> None:
    migration = import_module(BASELINE_MODULE)
    versions = sorted(
        path.name
        for path in (ROOT / "forwin/migrations/versions").glob("*.py")
        if path.name != "__init__.py"
    )

    assert versions == [
        "0001_v5_baseline.py",
        "0002_drop_dead_tables.py",
        "0003_book_state_v6.py",
        "0004_drop_parallel_ledgers.py",
        "0005_world_plan_conflicts.py",
        "0006_planning_v6.py",
    ]
    assert migration.revision == "0001_v5_recovery"
    assert migration.down_revision is None
    assert "Revision ID: 0001_v5_recovery" in migration.__doc__


def test_require_v5_schema_rejects_old_baseline_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SimpleNamespace(scalar_one_or_none=lambda: "0001_v5_baseline")
    connection = SimpleNamespace(execute=lambda _statement: result)
    engine = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        url=SimpleNamespace(
            render_as_string=lambda **_kwargs: (
                "postgresql+psycopg://forwin:forwin@localhost:5432/forwin"
            )
        ),
        connect=lambda: nullcontext(connection),
    )
    monkeypatch.setattr(
        model_base.ScriptDirectory,
        "from_config",
        lambda _config: SimpleNamespace(
            get_current_head=lambda: "0001_v5_recovery"
        ),
    )
    monkeypatch.setattr(
        model_base,
        "inspect",
        lambda _engine: SimpleNamespace(has_table=lambda _name: True),
    )

    with pytest.raises(
        model_base.SchemaRevisionMismatchError,
        match=(
            r"\[FORWIN_SCHEMA_REVISION_MISMATCH\].*"
            "0001_v5_baseline, expected 0001_v5_recovery"
        ),
    ) as raised:
        model_base.require_v5_schema(engine)

    assert raised.value.code == "FORWIN_SCHEMA_REVISION_MISMATCH"
    assert raised.value.current_revision == "0001_v5_baseline"
    assert raised.value.expected_revision == "0001_v5_recovery"


def test_parallel_ledger_downgrade_recreates_the_0001_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    drop_ledgers = import_module("forwin.migrations.versions.0004_drop_parallel_ledgers")
    baseline_tables, baseline_indexes = _created_schema(monkeypatch, import_module(BASELINE_MODULE).upgrade)
    recreated_tables, recreated_indexes = _created_schema(monkeypatch, drop_ledgers.downgrade)

    assert drop_ledgers.revision == "0004_drop_parallel_ledgers"
    assert drop_ledgers.down_revision == "0003_book_state_v6"
    assert set(recreated_tables) == {"character_state_transitions", "countdown_ledgers"}
    for name, table in recreated_tables.items():
        assert _column_shape(table) == _column_shape(baseline_tables[name])
        assert _foreign_keys(table) == _foreign_keys(baseline_tables[name])
        assert recreated_indexes[name] == baseline_indexes[name]


def test_world_conflict_table_matches_migration_0005(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = import_module("forwin.migrations.versions.0005_world_plan_conflicts")
    tables, indexes = _created_schema(monkeypatch, migration.upgrade)
    assert migration.revision == "0005_world_plan_conflicts"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0004_drop_parallel_ledgers"
    assert set(tables) == {"world_plan_conflicts"}
    assert hasattr(models, "WorldPlanConflictRow")
    orm = _table("world_plan_conflicts")
    # 0006 adds origin_key and its index; tests/test_planning_v6_schema.py pins them.
    added_in_0006 = {"origin_key", "decision_option", "superseded_by_delta"}
    assert set(orm.c.keys()) - added_in_0006 == {
        "id", "project_id", "source", "detected_after_chapter", "story_time",
        "involved_refs_json", "violated", "proposed_change_json", "impact_level",
        "options_json", "required_decider", "blocks_from_chapter", "status",
        "decision_json", "result_refs_json", "summary", "intent_ref", "actor_ref",
        "created_at", "updated_at",
    }
    assert _column_shape(tables[orm.name]) == [
        shape for shape in _column_shape(orm) if shape[0] not in added_in_0006
    ]
    assert _foreign_keys(tables[orm.name]) == _foreign_keys(orm)
    assert indexes[orm.name] == {
        (index.name, tuple(column.name for column in index.columns), index.unique)
        for index in orm.indexes
        if not added_in_0006 & {column.name for column in index.columns}
    } == {
        ("ix_world_plan_conflicts_project_status", ("project_id", "status"), False),
        ("ix_world_plan_conflicts_project_blocks", ("project_id", "blocks_from_chapter"), False),
    }
    dropped = []
    monkeypatch.setattr(migration.op, "drop_table", lambda name: dropped.append(name))
    migration.downgrade()
    assert dropped == ["world_plan_conflicts"]
