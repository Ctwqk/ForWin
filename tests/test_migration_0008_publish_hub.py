from __future__ import annotations

from datetime import datetime

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from forwin import models
from forwin.models.base import Base, alembic_config
from forwin.models.publisher import PublisherUploadJob
from tests.postgres import postgres_empty_test_url


MIGRATION = "0008_publish_hub"
PREVIOUS = "0007_drop_codex"

JOB_COLUMNS = {
    "hub_job_id": "TEXT",
    "hub_status": "TEXT",
    "hub_submit_body_json": "JSONB",
    "hub_superseded_reason": "TEXT",
}
OWNED_TABLES = {
    "publisher_upload_jobs",
    "publisher_work_bindings",
    "publisher_chapter_bindings",
    "publish_hub_sync_state",
}


@pytest.fixture
def database():
    url = postgres_empty_test_url("publish-hub-0008")
    config = alembic_config(url)
    engine = sa.create_engine(url)
    try:
        yield config, engine
    finally:
        engine.dispose()


def _columns(engine, table: str) -> dict[str, dict]:
    return {column["name"]: column for column in sa.inspect(engine).get_columns(table)}


def _type_name(column: dict) -> str:
    return str(column["type"]).upper()


def _insert_job(engine, job_id: str, **extra) -> None:
    values = {
        "id": job_id,
        "project_id": "p1",
        "candidate_id": "",
        "chapter_number": 0,
        "idempotency_key": "",
        "platform_id": "fanqie",
        "task_kind": "chapter_upload",
        "status": "pending",
        "book_name": "雾港",
        "chapter_title": "第三章",
        "body_text": "正文",
        "body_sha256": "",
        "upload_url": "",
        "publish": True,
        "abort_requested": False,
        "extension_client_id": "",
        "current_attempt_id": "",
        "pause_reason": "",
        "current_url": "",
        "result_message": "",
        "error_message": "",
        "result_payload_json": "{}",
        "created_at": datetime(2026, 10, 31),
        "updated_at": datetime(2026, 10, 31),
        **extra,
    }
    table = sa.Table("publisher_upload_jobs", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(**values))


def test_upgrade_adds_the_hub_columns_and_the_sync_state_table(database) -> None:
    config, engine = database
    command.upgrade(config, PREVIOUS)
    assert "publish_hub_sync_state" not in sa.inspect(engine).get_table_names()

    command.upgrade(config, MIGRATION)

    jobs = _columns(engine, "publisher_upload_jobs")
    for name, type_name in JOB_COLUMNS.items():
        assert jobs[name]["nullable"] is True, name
        assert _type_name(jobs[name]) == type_name, name
    assert _columns(engine, "publisher_work_bindings")["hub_destination_id"]["nullable"]
    assert _columns(engine, "publisher_chapter_bindings")["hub_publication_id"]["nullable"]
    state = _columns(engine, "publish_hub_sync_state")
    assert set(state) == {"key", "cursor", "value_json", "updated_at"}
    assert _type_name(state["cursor"]) == "BIGINT" and not state["cursor"]["nullable"]
    assert _type_name(state["value_json"]) == "JSONB" and state["value_json"]["nullable"]
    assert not state["updated_at"]["nullable"]
    assert sa.inspect(engine).get_pk_constraint("publish_hub_sync_state")[
        "constrained_columns"
    ] == ["key"]


def test_hub_job_id_is_unique_but_may_be_null_many_times(database) -> None:
    config, engine = database
    command.upgrade(config, MIGRATION)

    _insert_job(engine, "job-1")
    _insert_job(engine, "job-2")
    _insert_job(engine, "job-3", hub_job_id="hub-1")
    with pytest.raises(sa.exc.IntegrityError):
        _insert_job(engine, "job-4", hub_job_id="hub-1")

    indexes = {i["name"]: i for i in sa.inspect(engine).get_indexes("publisher_upload_jobs")}
    assert indexes["ux_publisher_upload_jobs_hub_job_id"]["unique"]
    assert indexes["ux_publisher_upload_jobs_hub_job_id"]["column_names"] == ["hub_job_id"]


def test_status_superseded_is_accepted_by_the_database(database) -> None:
    config, engine = database
    command.upgrade(config, MIGRATION)

    _insert_job(
        engine,
        "job-1",
        status="superseded",
        hub_job_id="hub-1",
        hub_status="failed",
        hub_superseded_reason="publication_already_published",
    )

    with engine.connect() as connection:
        status, reason = connection.execute(
            sa.text(
                "SELECT status, hub_superseded_reason FROM publisher_upload_jobs "
                "WHERE id = 'job-1'"
            )
        ).one()
    assert (status, reason) == ("superseded", "publication_already_published")


def test_downgrade_removes_everything_and_upgrade_runs_again(database) -> None:
    config, engine = database
    command.upgrade(config, MIGRATION)

    command.downgrade(config, PREVIOUS)

    inspector = sa.inspect(engine)
    assert "publish_hub_sync_state" not in inspector.get_table_names()
    jobs = {c["name"] for c in inspector.get_columns("publisher_upload_jobs")}
    assert not set(JOB_COLUMNS) & jobs
    assert "hub_destination_id" not in {
        c["name"] for c in inspector.get_columns("publisher_work_bindings")
    }
    assert "hub_publication_id" not in {
        c["name"] for c in inspector.get_columns("publisher_chapter_bindings")
    }
    assert "ux_publisher_upload_jobs_hub_job_id" not in {
        i["name"] for i in inspector.get_indexes("publisher_upload_jobs")
    }
    with engine.connect() as connection:
        revision = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    assert revision == PREVIOUS

    command.upgrade(config, MIGRATION)

    assert "publish_hub_sync_state" in sa.inspect(engine).get_table_names()
    assert set(JOB_COLUMNS) <= {
        c["name"] for c in sa.inspect(engine).get_columns("publisher_upload_jobs")
    }
    with engine.connect() as connection:
        revision = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    assert revision == MIGRATION


def test_downgrade_turns_superseded_rows_into_cancelled(database) -> None:
    config, engine = database
    command.upgrade(config, MIGRATION)
    _insert_job(engine, "job-1", status="superseded", hub_job_id="hub-1")
    _insert_job(engine, "job-2", status="pending")

    command.downgrade(config, PREVIOUS)

    with engine.connect() as connection:
        statuses = dict(
            connection.execute(sa.text("SELECT id, status FROM publisher_upload_jobs")).all()
        )
    assert statuses == {"job-1": "cancelled", "job-2": "pending"}


def test_existing_job_rows_survive_the_round_trip_with_null_hub_columns(database) -> None:
    config, engine = database
    command.upgrade(config, PREVIOUS)
    _insert_job(engine, "legacy-job", status="succeeded")

    command.upgrade(config, MIGRATION)
    with engine.connect() as connection:
        row = connection.execute(
            sa.text(
                "SELECT status, hub_job_id, hub_status, hub_submit_body_json, "
                "hub_superseded_reason FROM publisher_upload_jobs WHERE id = 'legacy-job'"
            )
        ).one()
    assert tuple(row) == ("succeeded", None, None, None, None)

    command.downgrade(config, PREVIOUS)
    with engine.connect() as connection:
        status = connection.execute(
            sa.text("SELECT status FROM publisher_upload_jobs WHERE id = 'legacy-job'")
        ).scalar_one()
    assert status == "succeeded"


def test_migrated_schema_matches_the_models_for_the_tables_it_touches(database) -> None:
    config, engine = database
    command.upgrade(config, MIGRATION)

    def include_object(obj, name, type_, reflected, compare_to):
        if type_ == "table":
            return name in OWNED_TABLES
        table = getattr(obj, "table", None)
        return table is None or table.name in OWNED_TABLES

    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"include_object": include_object, "compare_type": True},
        )
        differences = compare_metadata(context, Base.metadata)

    assert differences == []


def test_model_stores_none_as_sql_null_not_json_null(database) -> None:
    config, engine = database
    command.upgrade(config, MIGRATION)

    with Session(engine) as session, session.begin():
        session.add(
            PublisherUploadJob(
                id="job-null",
                project_id="p1",
                platform_id="fanqie",
                body_text="正文",
                hub_submit_body_json=None,
            )
        )
        session.add(
            PublisherUploadJob(
                id="job-body",
                project_id="p1",
                platform_id="fanqie",
                body_text="正文",
                hub_submit_body_json={"b": 1, "a": ["第"]},
            )
        )
        session.add(models.PublishHubSyncState(key="changes", cursor=12))
        session.add(
            models.PublishHubSyncState(key="destinations", value_json=None)
        )

    with engine.connect() as connection:
        rows = dict(
            connection.execute(
                sa.text(
                    "SELECT id, hub_submit_body_json IS NULL FROM publisher_upload_jobs"
                )
            ).all()
        )
        state = {
            key: (cursor, value_is_null)
            for key, cursor, value_is_null in connection.execute(
                sa.text("SELECT key, cursor, value_json IS NULL FROM publish_hub_sync_state")
            )
        }
    assert rows == {"job-null": True, "job-body": False}
    assert state == {"changes": (12, True), "destinations": (0, True)}

    with Session(engine) as session:
        stored = session.get(PublisherUploadJob, "job-body")
        assert stored.hub_submit_body_json == {"b": 1, "a": ["第"]}
