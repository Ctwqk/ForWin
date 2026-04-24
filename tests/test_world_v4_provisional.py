from __future__ import annotations

from sqlalchemy import func, select

from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.world_v4 import WorldDeltaRow
from forwin.protocol.world_v4 import (
    DeltaKind,
    DeltaSource,
    DeltaSourceType,
    WorldDelta,
    WorldLine,
)
from forwin.world_model_v4.compiler import WorldModelCompiler
from forwin.world_model_v4.provisional import ProjectionLayer, WorldModelProvisionalStore
from forwin.world_model_v4.repository import WorldModelRepository


def test_planned_and_provisional_deltas_do_not_enter_actual_canon_until_promoted() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(
            title="Provisional v4",
            premise="母星危机压力测试",
            genre="科幻",
            setting_summary="殖民地与母星",
        )
        session.add(project)
        session.flush()
        project_id = project.id
        repo = WorldModelRepository(session)
        repo.create_world_line(
            WorldLine(
                world_line_id="line_homeworld_siege",
                project_id=project_id,
                line_type="hidden_parallel_line",
                title="母星围困线",
            )
        )
        store = WorldModelProvisionalStore(session)
        planned = store.record_delta(
            project_id=project_id,
            projection_id="arc2-pressure",
            layer=ProjectionLayer.PLANNED_PROJECTION,
            world_delta=WorldDelta(
                delta_id="planned_hint_ch22",
                project_id=project_id,
                world_line_id="line_homeworld_siege",
                delta_kind=DeltaKind.HINT,
                summary="计划在第22章出现通讯延迟",
                narrative_chapter=22,
                source=DeltaSource(source_type=DeltaSourceType.INFORMATION_SPREAD),
            ),
        )
        provisional = store.record_delta(
            project_id=project_id,
            projection_id="arc2-pressure",
            layer=ProjectionLayer.PROVISIONAL_PROJECTION,
            world_delta=WorldDelta(
                delta_id="provisional_cut_array_ch23",
                project_id=project_id,
                world_line_id="line_homeworld_siege",
                delta_kind=DeltaKind.OFFSCREEN,
                summary="压力测试：敌方切断第三通讯阵列",
                narrative_chapter=23,
                source=DeltaSource(source_type=DeltaSourceType.FACTION_ACTION),
            ),
        )
        snapshot = store.load_projection(project_id, "arc2-pressure")

        assert planned.projection_layer == ProjectionLayer.PLANNED_PROJECTION.value
        assert provisional.projection_layer == ProjectionLayer.PROVISIONAL_PROJECTION.value
        assert snapshot.actual_delta_ids == []
        assert snapshot.planned_delta_ids == ["planned_hint_ch22"]
        assert snapshot.provisional_delta_ids == ["provisional_cut_array_ch23"]
        assert session.scalar(select(func.count()).select_from(WorldDeltaRow)) == 0

        result = store.promote_delta(
            provisional.id,
            compiler=WorldModelCompiler(session),
            approved_by=["provisional-promotion-test"],
            review_verdict_id="review-promote",
        )

        assert result.committed is True
        assert result.world_delta_ids == ["provisional_cut_array_ch23"]
        assert session.scalar(select(func.count()).select_from(WorldDeltaRow)) == 1

        promoted_snapshot = store.load_projection(project_id, "arc2-pressure")
        assert promoted_snapshot.actual_delta_ids == ["provisional_cut_array_ch23"]
        assert promoted_snapshot.provisional_delta_ids == ["provisional_cut_array_ch23"]
