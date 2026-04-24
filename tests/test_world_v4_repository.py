from __future__ import annotations

import json

from sqlalchemy import func, select

from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.world_v4 import (
    KnowledgeUpdateEventRow,
    RevealEventRow,
    WorldDeltaRow,
)
from forwin.protocol.world_v4 import (
    DeltaKind,
    DeltaSource,
    DeltaSourceType,
    KnowledgeUpdateEvent,
    KnowledgeUpdateType,
    ObserverType,
    RevealEvent,
    VisibilityState,
    WorldDelta,
    WorldLine,
)
from forwin.world_model_v4.projection import WorldModelProjection
from forwin.world_model_v4.repository import WorldModelRepository


def test_repository_appends_ledgers_and_projection_rebuilds_snapshot() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(
            title="V4 仓储测试",
            premise="父亲在母星被围但读者尚不知情",
            genre="科幻",
            setting_summary="殖民地与母星双线",
        )
        session.add(project)
        session.flush()
        project_id = project.id

        repo = WorldModelRepository(session)
        projection = WorldModelProjection(session)
        repo.create_world_line(
            WorldLine(
                world_line_id="line_homeworld_siege",
                project_id=project_id,
                line_type="hidden_parallel_line",
                title="母星围困线",
                objective_state_summary="母星危机尚在幕后推进",
            )
        )
        repo.append_world_delta(
            WorldDelta(
                delta_id="delta_siege_begins",
                project_id=project_id,
                world_line_id="line_homeworld_siege",
                delta_kind=DeltaKind.OFFSCREEN,
                summary="敌方舰队开始围困母星",
                objective_story_time="Day 30",
                narrative_chapter=21,
                source=DeltaSource(source_type=DeltaSourceType.FACTION_ACTION),
            )
        )
        repo.append_world_delta(
            WorldDelta(
                delta_id="delta_array_cut",
                project_id=project_id,
                world_line_id="line_homeworld_siege",
                delta_kind=DeltaKind.HINT,
                summary="通讯出现乱码与父亲旧部呼号",
                objective_story_time="Day 32",
                narrative_chapter=23,
                source=DeltaSource(source_type=DeltaSourceType.INFORMATION_SPREAD),
            )
        )
        repo.append_reveal_event(
            RevealEvent(
                reveal_event_id="reveal_static_callsign",
                project_id=project_id,
                related_gap_id="gap_homeworld_siege",
                reveal_to_reader=True,
                reveal_to_characters=["protagonist"],
                reveal_method="hint",
                from_state=VisibilityState.HIDDEN,
                to_state=VisibilityState.HINTED,
                narrative_function="公平暗示母星异常",
            )
        )
        repo.append_knowledge_update(
            KnowledgeUpdateEvent(
                update_event_id="knowledge_reader_hint",
                project_id=project_id,
                update_type=KnowledgeUpdateType.HINT,
                observer_type=ObserverType.READER,
                observer_id="reader",
                related_gap_id="gap_homeworld_siege",
                from_state=VisibilityState.HIDDEN,
                to_state=VisibilityState.HINTED,
                chapter_number=23,
            )
        )

        snapshot = projection.rebuild_snapshot(project_id, as_of_chapter=23)

    with Session() as session:
        delta_count = session.scalar(
            select(func.count()).select_from(WorldDeltaRow).where(
                WorldDeltaRow.project_id == project_id
            )
        )
        reveal_count = session.scalar(
            select(func.count()).select_from(RevealEventRow).where(
                RevealEventRow.project_id == project_id
            )
        )
        knowledge_count = session.scalar(
            select(func.count()).select_from(KnowledgeUpdateEventRow).where(
                KnowledgeUpdateEventRow.project_id == project_id
            )
        )
        loaded = WorldModelRepository(session).get_snapshot_as_of_chapter(project_id, 23)

    assert delta_count == 2
    assert reveal_count == 1
    assert knowledge_count == 1
    assert snapshot.snapshot_id == loaded.snapshot_id
    assert snapshot.active_world_line_ids == ["line_homeworld_siege"]
    assert snapshot.source_delta_ids == ["delta_siege_begins", "delta_array_cut"]
    assert "敌方舰队开始围困母星" in snapshot.objective_state_summary


def test_repository_create_or_update_gap_preserves_latest_observer_state() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(
            title="V4 gap 测试",
            premise="gap lifecycle",
            genre="科幻",
            setting_summary="母星线",
        )
        session.add(project)
        session.flush()
        project_id = project.id

        repo = WorldModelRepository(session)
        repo.create_or_update_gap(
            gap_id="gap_homeworld_siege",
            project_id=project_id,
            objective_truth="Day 30 父亲在母星被围",
            related_world_line_id="line_homeworld_siege",
            observer_states={"reader": {"visibility": "hidden"}},
            status="open",
        )
        repo.create_or_update_gap(
            gap_id="gap_homeworld_siege",
            project_id=project_id,
            objective_truth="Day 30 父亲在母星被围",
            related_world_line_id="line_homeworld_siege",
            observer_states={"reader": {"visibility": "hinted"}},
            status="hinted",
        )
        gap_row = repo.get_gap(project_id, "gap_homeworld_siege")

    assert gap_row is not None
    assert gap_row.status == "hinted"
    assert json.loads(gap_row.observer_states_json)["reader"]["visibility"] == "hinted"
