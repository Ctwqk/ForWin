from hashlib import sha256

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, create_engine, text

from forwin.models.publisher import PublisherRawComment
from tests.postgres import postgres_empty_test_url
from tests.test_v5_live_migration import _alembic_config


def test_0005_preserves_raw_values_and_does_not_guess_historical_origin_or_completion():
    url = postgres_empty_test_url("comment-analysis-migration")
    engine = create_engine(url)
    config = _alembic_config(url)
    command.upgrade(config, "0004_revision_validation")
    with engine.begin() as connection:
        table = Table("publisher_raw_comments", MetaData(), autoload_with=connection)
        values = {
            column.name: column.default.arg
            for column in PublisherRawComment.__table__.columns
            if column.name in table.c
            and column.default is not None
            and (column.default.is_scalar or column.default.is_clause_element)
        }
        values.update(
            id="c",
            project_id="",
            platform_id="fanqie",
            remote_comment_id="r",
            work_name="同名书",
            chapter_title="第99章",
            body_text="太拖了",
        )
        connection.execute(table.insert().values(**values))
        before = dict(
            connection.execute(text("SELECT * FROM publisher_raw_comments"))
            .mappings()
            .one()
        )
    command.upgrade(config, "0005_comment_analysis")
    with engine.connect() as connection:
        after = dict(
            connection.execute(text("SELECT * FROM publisher_raw_comments"))
            .mappings()
            .one()
        )
        assert {key: after[key] for key in before} == before
        assert after["source_chapter_number"] is None
        assert after["source_status"] == "unknown"
        assert after["source_canon_commit_id"] == ""
        assert after["observed_at"] is None
        assert after["source_scope"] == "legacy:c"
        assert after["content_sha256"] == sha256("太拖了".encode()).hexdigest()
        assert (
            connection.scalar(text("SELECT count(*) FROM comment_analysis_records"))
            == 0
        )
    with pytest.raises(ValueError, match="cannot be discarded"):
        command.downgrade(config, "0004_revision_validation")
    engine.dispose()
