from __future__ import annotations

import json

from sqlalchemy import inspect

from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.world_v4 import (
    WorldDeltaRow,
    WorldLineRow,
)


def test_init_db_exposes_world_v4_tables() -> None:
    engine = get_engine(postgres_test_url())

    init_db(engine)

    table_names = set(inspect(engine).get_table_names())
    assert {
        "world_lines",
        "world_deltas",
        "beliefs",
        "cognition_snapshots",
        "knowledge_gaps",
        "reveal_events",
        "knowledge_update_events",
        "reader_experience_deltas",
        "world_model_snapshots_v4",
        "world_compile_runs_v4",
        "arc_world_contracts",
        "band_world_contracts",
        "chapter_world_delta_intents",
    }.issubset(table_names)

    compile_run_columns = {
        column["name"] for column in inspect(engine).get_columns("world_compile_runs_v4")
    }
    assert "retrieval_pack_json" in compile_run_columns
    assert "projection_refresh_json" in compile_run_columns


def test_world_v4_rows_persist_nested_json_defaults() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(
            title="V4 测试书",
            premise="测试多层信息不对称世界模型",
            genre="科幻",
            setting_summary="殖民地与母星双线",
        )
        session.add(project)
        session.flush()

        world_line = WorldLineRow(
            project_id=project.id,
            world_line_id="line_homeworld_siege",
            line_type="hidden_parallel_line",
            title="母星围困线",
            objective_state_summary="母星通讯正在被切断",
            participants_json=json.dumps(["father", "enemy_fleet"], ensure_ascii=False),
        )
        session.add(world_line)
        world_delta = WorldDeltaRow(
            project_id=project.id,
            delta_id="delta_cut_array_3",
            world_line_id="line_homeworld_siege",
            delta_kind="offscreen",
            summary="敌方切断第三通讯阵列",
            objective_story_time="Day 32",
            narrative_chapter=23,
            source_type="faction_action",
            observer_states_json=json.dumps(
                {"reader": {"visibility": "hinted"}},
                ensure_ascii=False,
            ),
        )
        session.add(world_delta)

    with Session() as session:
        saved_line = session.query(WorldLineRow).filter_by(
            world_line_id="line_homeworld_siege"
        ).one()
        saved_delta = session.query(WorldDeltaRow).filter_by(
            delta_id="delta_cut_array_3"
        ).one()

    assert json.loads(saved_line.participants_json) == ["father", "enemy_fleet"]
    assert json.loads(saved_delta.observer_states_json)["reader"]["visibility"] == "hinted"
    assert json.loads(saved_delta.affected_entities_json) == []
    assert saved_delta.allowed_for_canon is True


def test_world_v4_compile_audit_columns_are_in_baseline() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)

    columns = {
        column["name"] for column in inspect(engine).get_columns("world_compile_runs_v4")
    }
    assert "retrieval_pack_json" in columns
    assert "projection_refresh_json" in columns
