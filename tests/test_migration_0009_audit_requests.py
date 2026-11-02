from __future__ import annotations

from datetime import datetime

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from forwin.models.base import Base, alembic_config
from forwin.models.publisher import PublisherAuditRequest
from tests.postgres import postgres_empty_test_url


MIGRATION = "0009_audit_requests"
PREVIOUS = "0008_publish_hub"
TABLE = "publisher_audit_requests"
COLUMNS = {
    "id",
    "project_id",
    "platform_id",
    "work_binding_id",
    "book_name",
    "remote_book_id",
    "remote_url",
    "status",
    "extension_client_id",
    "attempt_count",
    "claimed_at",
    "lease_expires_at",
    "finished_at",
    "error_message",
    "risk_reason",
    "result_summary_json",
    "created_at",
    "updated_at",
}


@pytest.fixture
def database():
    url = postgres_empty_test_url("audit-requests-0009")
    config = alembic_config(url)
    engine = sa.create_engine(url)
    try:
        yield config, engine
    finally:
        engine.dispose()


def _revision(engine) -> str:
    with engine.connect() as connection:
        return connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()


def _insert(engine, request_id: str, **extra) -> None:
    values = {
        "id": request_id,
        "project_id": "p1",
        "platform_id": "fanqie",
        "work_binding_id": "w1",
        "book_name": "雾港",
        "remote_book_id": "7001",
        "remote_url": "https://fanqienovel.com/main/writer/book-info/7001",
        "status": "pending",
        "extension_client_id": "",
        "attempt_count": 0,
        "error_message": "",
        "risk_reason": "",
        "result_summary_json": "{}",
        "created_at": datetime(2026, 11, 1),
        "updated_at": datetime(2026, 11, 1),
        **extra,
    }
    table = sa.Table(TABLE, sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(**values))


def test_upgrade_creates_the_audit_request_table(database) -> None:
    config, engine = database
    command.upgrade(config, PREVIOUS)
    assert TABLE not in sa.inspect(engine).get_table_names()

    command.upgrade(config, "head")

    inspector = sa.inspect(engine)
    assert TABLE in inspector.get_table_names()
    columns = {column["name"]: column for column in inspector.get_columns(TABLE)}
    assert set(columns) == COLUMNS
    for name in ("claimed_at", "lease_expires_at", "finished_at"):
        assert columns[name]["nullable"] is True, name
    for name in ("platform_id", "work_binding_id", "status", "attempt_count"):
        assert columns[name]["nullable"] is False, name
    assert inspector.get_pk_constraint(TABLE)["constrained_columns"] == ["id"]
    assert _revision(engine) == MIGRATION


def test_only_one_open_request_per_book_may_exist(database) -> None:
    config, engine = database
    command.upgrade(config, "head")
    indexes = {i["name"]: i for i in sa.inspect(engine).get_indexes(TABLE)}
    assert indexes["ux_publisher_audit_requests_open_work"]["unique"]
    assert indexes["ux_publisher_audit_requests_open_work"]["column_names"] == ["work_binding_id"]

    _insert(engine, "r1")
    with pytest.raises(sa.exc.IntegrityError):
        _insert(engine, "r2", status="claimed")
    # finished requests do not count, any number of them may pile up
    _insert(engine, "r3", status="succeeded", work_binding_id="w1")
    _insert(engine, "r4", status="succeeded", work_binding_id="w1")
    _insert(engine, "r6", status="failed", work_binding_id="w1", error_message="x", risk_reason="captcha")
    # another book is independent
    _insert(engine, "r5", work_binding_id="w2")


def test_downgrade_drops_the_table_and_upgrade_runs_again(database) -> None:
    config, engine = database
    command.upgrade(config, "head")
    _insert(engine, "r1")

    command.downgrade(config, PREVIOUS)

    inspector = sa.inspect(engine)
    assert TABLE not in inspector.get_table_names()
    # nothing of the 0008 schema is touched
    assert "publish_hub_sync_state" in inspector.get_table_names()
    assert _revision(engine) == PREVIOUS

    command.upgrade(config, "head")

    assert TABLE in sa.inspect(engine).get_table_names()
    assert _revision(engine) == MIGRATION


def test_migrated_schema_matches_the_model(database) -> None:
    config, engine = database
    command.upgrade(config, "head")

    def include_object(obj, name, type_, reflected, compare_to):
        if type_ == "table":
            return name == TABLE
        table = getattr(obj, "table", None)
        return table is None or table.name == TABLE

    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"include_object": include_object, "compare_type": True},
        )
        differences = compare_metadata(context, Base.metadata)

    assert differences == []


def test_model_round_trips_through_the_migrated_table(database) -> None:
    config, engine = database
    command.upgrade(config, "head")

    with Session(engine) as session, session.begin():
        session.add(
            PublisherAuditRequest(
                id="r-model",
                project_id="p1",
                platform_id="fanqie",
                work_binding_id="w1",
                book_name="雾港",
                remote_book_id="7001",
            )
        )

    with Session(engine) as session:
        row = session.get(PublisherAuditRequest, "r-model")
        assert (row.status, row.attempt_count, row.extension_client_id) == ("pending", 0, "")
        assert (row.error_message, row.risk_reason) == ("", "")
        assert row.claimed_at is None and row.lease_expires_at is None
        assert row.result_summary_json == "{}"
        assert row.created_at is not None
