from __future__ import annotations

from datetime import datetime
import importlib
import json
from types import SimpleNamespace

from alembic import command
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from forwin import models
from forwin.generation.task_payload import execution_payload, payload_from_json
from forwin.models.base import alembic_config
from forwin.runtime.policy import RuntimePolicy
from tests.postgres import postgres_empty_test_url


MIGRATION = "0007_drop_codex"
PREVIOUS = "0006_planning_v6"


@pytest.fixture
def legacy_database():
    url = postgres_empty_test_url("drop-codex-0007")
    config = alembic_config(url)
    command.upgrade(config, PREVIOUS)
    engine = sa.create_engine(url)
    try:
        yield config, engine
    finally:
        engine.dispose()


def _seed_project(session: Session, *, runtime_policy_json: str = "") -> None:
    session.add(
        models.Project(
            id="project-1",
            title="title",
            premise="premise",
            genre="genre",
            runtime_policy_json=runtime_policy_json,
        )
    )
    session.flush()


def _legacy_policy_json() -> str:
    policy = RuntimePolicy.for_profile("standard").model_dump(mode="json")
    policy["pause"]["gate_delegate"] = "spark"
    policy["world"]["conflict_delegate"] = "codex"
    policy["models"]["role_preferences"] = {
        "review.world_conflict": ["gpt-6-luna"]
    }
    policy["review"]["repair_models"] = ["deepseek-reasoner"]
    return json.dumps(policy)


def _legacy_task_payload_json() -> str:
    payload = execution_payload(
        mode="continue",
        policy=RuntimePolicy.for_profile("standard"),
        policy_version=7,
        root_event_id="root-before-cutover",
        num_chapters=3,
        max_chapters=12,
    ).model_dump(mode="json")
    payload["policy_snapshot"] = json.loads(_legacy_policy_json())
    payload["policy_snapshot"]["models"].pop("chains", None)
    return json.dumps(payload)


@pytest.mark.parametrize("status", ["queued", "running"])
def test_0007_legacy_task_payload_loads_after_upgrade(legacy_database, status) -> None:
    config, engine = legacy_database
    before = _legacy_task_payload_json()
    with Session(engine) as session, session.begin():
        _seed_project(session, runtime_policy_json=_legacy_policy_json())
        session.add(models.GenerationTask(
            id="task-1", project_id="project-1", status=status,
            execution_payload_json=before,
        ))

    command.upgrade(config, MIGRATION)

    with engine.connect() as connection:
        raw = connection.execute(sa.text(
            "SELECT execution_payload_json FROM generation_tasks WHERE id = 'task-1'"
        )).scalar_one()
    _assert_migrated_task_payload(before, raw)

    command.downgrade(config, PREVIOUS)
    with engine.connect() as connection:
        raw = connection.execute(sa.text(
            "SELECT execution_payload_json FROM generation_tasks WHERE id = 'task-1'"
        )).scalar_one()
    policy = json.loads(raw)["policy_snapshot"]
    assert policy["pause"]["gate_delegate"] == "spark"
    assert policy["world"]["conflict_delegate"] == "codex"


def _assert_migrated_task_payload(before: str, after: str) -> None:
    payload = payload_from_json(after)
    assert payload.policy_version == 7
    assert payload.policy_snapshot.pause.gate_delegate == "llm"
    assert payload.policy_snapshot.world.conflict_delegate == "llm"
    expected = json.loads(before)
    actual = json.loads(after)
    expected.pop("policy_snapshot")
    actual.pop("policy_snapshot")
    assert actual == expected


def test_0007_task_payload_rewrite_without_database(monkeypatch) -> None:
    migration = importlib.import_module("forwin.migrations.versions.0007_drop_codex")
    before = _legacy_task_payload_json()
    rows = {"task-1": before, "blank": "", "invalid": "not-json", "absent": "{}"}

    class Bind:
        def execute(self, statement, params=None):
            sql = str(statement)
            if sql.startswith("SELECT"):
                return SimpleNamespace(all=lambda: list(rows.items()) if "generation_tasks" in sql else [])
            assert sql.startswith("UPDATE generation_tasks SET execution_payload_json")
            rows[params["id"]] = params["payload"]

    monkeypatch.setattr(migration.op, "get_bind", lambda: Bind())
    monkeypatch.setattr(migration.op, "drop_column", lambda *args: None)
    monkeypatch.setattr(migration.op, "add_column", lambda *args: None)
    migration.upgrade()
    _assert_migrated_task_payload(before, rows["task-1"])
    assert {key: rows[key] for key in ("blank", "invalid", "absent")} == {
        "blank": "", "invalid": "not-json", "absent": "{}",
    }
    migration.downgrade()
    policy = json.loads(rows["task-1"])["policy_snapshot"]
    assert policy["pause"]["gate_delegate"] == "spark"
    assert policy["world"]["conflict_delegate"] == "codex"
    assert "repair_models" not in policy["review"]
    assert "role_preferences" not in policy["models"]


def _seed_conflict(session: Session) -> None:
    table = sa.Table(
        "world_plan_conflicts", sa.MetaData(), autoload_with=session.connection()
    )
    session.execute(
        table.insert().values(
            id="conflict-1",
            project_id="project-1",
            source="world_tick",
            detected_after_chapter=1,
            story_time=60,
            involved_refs_json="[]",
            violated="impact_review",
            proposed_change_json="{}",
            impact_level="major",
            options_json="[]",
            required_decider="codex",
            blocks_from_chapter=2,
            status="open",
            decision_json=json.dumps(
                {"option": "veto_world", "decided_by": "codex", "rationale": "kept"}
            ),
            result_refs_json="[]",
            summary="A held change.",
            intent_ref="",
            actor_ref="",
            created_at=datetime(2026, 10, 25),
            updated_at=datetime(2026, 10, 25),
        )
    )


def _seed_prompt_trace(session: Session) -> None:
    table = sa.Table(
        "prompt_traces", sa.MetaData(), autoload_with=session.connection()
    )
    session.execute(
        table.insert().values(
            id="trace-1",
            project_id="project-1",
            genesis_revision_id="",
            decision_event_id="",
            parent_trace_id="",
            trace_scope="writer",
            stage_key="spark_pause_gate",
            template_id="writer:pause_gate",
            template_version="v1",
            effective_system_prompt="",
            prompt_layers_json="[]",
            input_snapshot_json="{}",
            model_profile_json="{}",
            attempts_json=json.dumps(
                [{"attempt_no": 1, "task_family": "codex_delegate"}]
            ),
            output_summary_json="{}",
            backend="legacy",
            codex_job_id="job-1",
            permission_profile="prompt_only_readonly",
            fallback_used=False,
            created_at=datetime(2026, 10, 25),
        )
    )


def test_0007_rewrites_policy_literals(legacy_database) -> None:
    config, engine = legacy_database
    with Session(engine) as session, session.begin():
        _seed_project(session, runtime_policy_json=_legacy_policy_json())

    command.upgrade(config, MIGRATION)

    with engine.connect() as connection:
        raw_policy = connection.execute(
            sa.text(
                "SELECT runtime_policy_json FROM projects WHERE id = 'project-1'"
            )
        ).scalar_one()
    policy = json.loads(raw_policy)
    assert policy["pause"]["gate_delegate"] == "llm"
    assert policy["world"]["conflict_delegate"] == "llm"
    assert "role_preferences" not in policy["models"]
    assert "repair_models" not in policy["review"]
    assert RuntimePolicy.model_validate_json(raw_policy)


def test_0007_rewrites_conflict_deciders(legacy_database) -> None:
    config, engine = legacy_database
    with Session(engine) as session, session.begin():
        _seed_project(session)
        _seed_conflict(session)

    command.upgrade(config, MIGRATION)

    with engine.connect() as connection:
        required_decider, decision_json = connection.execute(
            sa.text(
                "SELECT required_decider, decision_json FROM world_plan_conflicts "
                "WHERE id = 'conflict-1'"
            )
        ).one()
    assert required_decider == "llm"
    decision = json.loads(decision_json)
    assert decision["decided_by"] == "llm"
    assert decision["option"] == "veto_world"


def test_0007_drops_codex_trace_columns(legacy_database) -> None:
    config, engine = legacy_database
    with Session(engine) as session, session.begin():
        _seed_project(session)
        _seed_prompt_trace(session)

    command.upgrade(config, MIGRATION)

    columns = {
        column["name"] for column in sa.inspect(engine).get_columns("prompt_traces")
    }
    assert "codex_job_id" not in columns
    assert "permission_profile" not in columns
    with engine.connect() as connection:
        stage_key, attempts_json = connection.execute(
            sa.text(
                "SELECT stage_key, attempts_json FROM prompt_traces "
                "WHERE id = 'trace-1'"
            )
        ).one()
    assert stage_key == "pause_gate"
    assert json.loads(attempts_json)[0]["task_family"] == "delegate"


def test_0007_downgrade_restores_columns_and_literals(legacy_database) -> None:
    config, engine = legacy_database
    with Session(engine) as session, session.begin():
        _seed_project(session, runtime_policy_json=_legacy_policy_json())
        _seed_conflict(session)
        _seed_prompt_trace(session)

    command.upgrade(config, MIGRATION)
    command.downgrade(config, PREVIOUS)

    trace_columns = {
        column["name"]: column
        for column in sa.inspect(engine).get_columns("prompt_traces")
    }
    for name in ("codex_job_id", "permission_profile"):
        assert trace_columns[name]["nullable"] is True
        assert str(trace_columns[name]["default"]).split("::")[0] == "''"

    with engine.connect() as connection:
        raw_policy = connection.execute(
            sa.text(
                "SELECT runtime_policy_json FROM projects WHERE id = 'project-1'"
            )
        ).scalar_one()
        required_decider, decision_json = connection.execute(
            sa.text(
                "SELECT required_decider, decision_json FROM world_plan_conflicts "
                "WHERE id = 'conflict-1'"
            )
        ).one()
        stage_key, attempts_json, codex_job_id, permission_profile = connection.execute(
            sa.text(
                "SELECT stage_key, attempts_json, codex_job_id, permission_profile "
                "FROM prompt_traces WHERE id = 'trace-1'"
            )
        ).one()

    policy = json.loads(raw_policy)
    assert policy["pause"]["gate_delegate"] == "spark"
    assert policy["world"]["conflict_delegate"] == "codex"
    assert required_decider == "codex"
    assert json.loads(decision_json)["decided_by"] == "codex"
    assert stage_key == "spark_pause_gate"
    assert json.loads(attempts_json)[0]["task_family"] == "codex_delegate"
    assert codex_job_id == permission_profile == ""
