from __future__ import annotations

import json

from sqlalchemy import select

from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.world_v4 import CognitionSnapshotRow, WorldLineRow
from forwin.state.updater import StateUpdater
from forwin.world_model_v4.bootstrap import bootstrap_initial_world_model
from forwin.world_model_v4.repository import WorldModelRepository


def test_bootstrap_initial_world_model_creates_primary_line_and_observers() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project(
            title="母星危机",
            premise="主角在殖民地建设防线，母星危机暗线推进。",
            genre="科幻",
            setting_summary="新星殖民地与母星远程通讯体系",
            target_total_chapters=40,
            creation_status="genesis_complete",
        )

        result = bootstrap_initial_world_model(session, project.id)

    with Session() as session:
        line = session.execute(
            select(WorldLineRow).where(
                WorldLineRow.project_id == project.id,
                WorldLineRow.world_line_id == "primary_visible_line",
            )
        ).scalar_one()
        cognition_rows = list(
            session.execute(
                select(CognitionSnapshotRow).where(
                    CognitionSnapshotRow.project_id == project.id
                )
            )
            .scalars()
            .all()
        )
        snapshot = WorldModelRepository(session).get_snapshot_as_of_chapter(project.id, 0)

    assert result.snapshot_id == snapshot.snapshot_id
    assert line.line_type == "primary_visible_line"
    assert "新星殖民地" in line.objective_state_summary
    assert {(row.observer_type, row.observer_id) for row in cognition_rows} == {
        ("reader", "reader"),
        ("character", "protagonist"),
    }
    assert all(json.loads(row.beliefs_json) == [] for row in cognition_rows)
    assert snapshot.active_world_line_ids == ["primary_visible_line"]
