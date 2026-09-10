import json

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, create_engine, text

from forwin.models import Project
from tests.postgres import postgres_empty_test_url
from tests.test_v5_live_migration import _alembic_config


def test_0007_preserves_historical_actions_without_inventing_execution_or_qualification():
    url = postgres_empty_test_url("feedback-action-migration")
    engine = create_engine(url)
    config = _alembic_config(url)
    command.upgrade(config, "0006_feedback_aggregation")
    with engine.begin() as connection:
        table = Table("projects", MetaData(), autoload_with=connection)
        values = {
            column.name: column.default.arg
            for column in Project.__table__.columns
            if column.name in table.c
            and column.default is not None
            and (column.default.is_scalar or column.default.is_clause_element)
        }
        connection.execute(
            table.insert().values(
                **(values | {"id": "book", "title": "Book", "premise": "Premise"})
            )
        )
        connection.execute(
            text(
                "INSERT INTO feedback_action_records (id,project_id,signal_key,signal_type,action_type,triggered_at_chapter,cooldown_until_chapter,notes,created_at) VALUES ('old','book','pacing:slow','pacing','boost_reward_density',4,7,'历史建议',CURRENT_TIMESTAMP)"
            )
        )
        before = dict(
            connection.execute(text("SELECT * FROM feedback_action_records"))
            .mappings()
            .one()
        )
    command.upgrade(config, "0007_feedback_actions")
    with engine.begin() as connection:
        after = dict(
            connection.execute(text("SELECT * FROM feedback_action_records"))
            .mappings()
            .one()
        )
        assert {key: after[key] for key in before} == before
        assert after["source_qualified"] is False
        assert after["status"] == "proposed"
        assert after["selected_at"] is None
        assert after["aggregate_id"] == ""
        assert after["hint_expires_at_chapter"] == 0
        assert json.loads(after["prompt_inclusions_json"]) == []
        assert json.loads(after["body_observation_json"]) == {}
        connection.execute(
            text(
                "UPDATE feedback_action_records SET prompt_inclusions_json=:evidence WHERE id='old'"
            ),
            {"evidence": '[{"input_status":"attempted_input"}]'},
        )
    with pytest.raises(RuntimeError, match="Cannot discard"):
        command.downgrade(config, "0006_feedback_aggregation")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT count(*) FROM feedback_action_records")) == 1
        )
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0007_feedback_actions"
        )
    engine.dispose()
