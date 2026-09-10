from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, create_engine, text

from forwin.models import Project
from forwin.models.publisher import (
    CommentSignalCandidate,
    PublisherRawComment,
    SignalWindowAggregate,
)
from tests.postgres import postgres_empty_test_url
from tests.test_v5_live_migration import _alembic_config


def old_insert(connection, model, **specific):
    table = Table(model.__tablename__, MetaData(), autoload_with=connection)
    values = {
        c.name: c.default.arg
        for c in model.__table__.columns
        if c.name in table.c
        and c.default is not None
        and (c.default.is_scalar or c.default.is_clause_element)
    }
    values.update(specific)
    connection.execute(table.insert().values(**values))


def test_0006_preserves_old_evidence_without_guessing_direction_or_qualification():
    url = postgres_empty_test_url("comment-aggregate-migration")
    engine = create_engine(url)
    config = _alembic_config(url)
    command.upgrade(config, "0005_comment_analysis")
    with engine.begin() as connection:
        old_insert(
            connection,
            Project,
            id="project",
            title="book",
            premise="story",
            genre="genre",
        )
        old_insert(
            connection,
            PublisherRawComment,
            id="comment",
            project_id="project",
            platform_id="fanqie",
            remote_comment_id="r",
        )
        old_insert(
            connection,
            CommentSignalCandidate,
            id="signal",
            project_id="project",
            source_comment_id="comment",
            signal_type="pacing",
            evidence_span="太快了",
            signal_level="confirmed",
        )
        for number in (1, 2):
            old_insert(
                connection,
                SignalWindowAggregate,
                id=f"old{number}",
                project_id="project",
                signal_key="pacing:arc:节奏",
                signal_type="pacing",
                signal_level="confirmed",
                hit_comment_count=7,
            )
        before = list(
            connection.execute(
                text("SELECT * FROM signal_window_aggregates ORDER BY id")
            ).mappings()
        )
    command.upgrade(config, "0006_feedback_aggregation")
    with engine.begin() as connection:
        after = list(
            connection.execute(
                text("SELECT * FROM signal_window_aggregates ORDER BY id")
            ).mappings()
        )
        assert [
            {k: row[k] for k in old} for row, old in zip(after, before, strict=True)
        ] == before
        assert all(
            row["direction"] == "unknown"
            and not row["source_qualified"]
            and row["evidence_sha256"] == ""
            for row in after
        )
        assert (
            connection.scalar(text("SELECT direction FROM comment_signal_candidates"))
            == "unknown"
        )
    command.downgrade(config, "0005_comment_analysis")
    command.upgrade(config, "0006_feedback_aggregation")
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE signal_window_aggregates SET evidence_sha256='new-evidence' WHERE id='old1'"
            )
        )
    with pytest.raises(RuntimeError, match="Cannot discard"):
        command.downgrade(config, "0005_comment_analysis")
    engine.dispose()
