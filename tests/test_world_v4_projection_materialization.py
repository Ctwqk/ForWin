from __future__ import annotations

import json

from sqlalchemy import select

from forwin.models import Entity, EntityState, Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.world_v4 import CognitionSnapshotRow
from forwin.protocol.world_v4 import (
    ApprovedWorldChangeSet,
    DeltaKind,
    DeltaSource,
    DeltaSourceType,
    ExtractedWorldChangeSet,
    GapObserverState,
    GapStatus,
    KnowledgeGap,
    KnowledgeUpdateEvent,
    KnowledgeUpdateType,
    ObserverType,
    VisibilityState,
    WorldCompileRequest,
    WorldDelta,
    WorldLine,
)
from forwin.world_model_v4.compiler import WorldModelCompiler
from forwin.world_model_v4.repository import WorldModelRepository


def test_projection_rebuilds_reader_and_character_cognition_snapshots() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(title="Projection", premise="母星危机", genre="科幻", setting_summary="")
        session.add(project)
        session.flush()
        project_id = project.id
        WorldModelRepository(session).create_world_line(
            WorldLine(
                world_line_id="line_homeworld_siege",
                project_id=project_id,
                line_type="hidden_parallel_line",
            )
        )
        extracted = ExtractedWorldChangeSet(
            project_id=project_id,
            chapter_number=25,
            world_deltas=[
                WorldDelta(
                    delta_id="delta_ch25_distress_call",
                    project_id=project_id,
                    world_line_id="line_homeworld_siege",
                    delta_kind=DeltaKind.REVEAL,
                    summary="残缺求援显示母星遭遇围困",
                    narrative_chapter=25,
                    source=DeltaSource(source_type=DeltaSourceType.INFORMATION_SPREAD),
                )
            ],
            knowledge_gap_updates=[
                KnowledgeGap(
                    gap_id="gap_homeworld_siege",
                    project_id=project_id,
                    objective_truth="父亲在母星被围",
                    related_world_line_id="line_homeworld_siege",
                    status=GapStatus.PARTIALLY_CLOSED,
                    observer_states={
                        "reader": GapObserverState(
                            observer_type=ObserverType.READER,
                            observer_id="reader",
                            visibility=VisibilityState.PARTIALLY_REVEALED,
                        ),
                        "protagonist": GapObserverState(
                            observer_type=ObserverType.CHARACTER,
                            observer_id="protagonist",
                            visibility=VisibilityState.SUSPECTED,
                        ),
                    },
                )
            ],
            knowledge_update_events=[
                KnowledgeUpdateEvent(
                    update_event_id="knowledge_reader_ch25",
                    project_id=project_id,
                    update_type=KnowledgeUpdateType.REVEAL,
                    observer_type=ObserverType.READER,
                    observer_id="reader",
                    related_gap_id="gap_homeworld_siege",
                    related_delta_id="delta_ch25_distress_call",
                    from_state=VisibilityState.HINTED,
                    to_state=VisibilityState.PARTIALLY_REVEALED,
                    chapter_number=25,
                ),
                KnowledgeUpdateEvent(
                    update_event_id="knowledge_protagonist_ch25",
                    project_id=project_id,
                    update_type=KnowledgeUpdateType.HINT,
                    observer_type=ObserverType.CHARACTER,
                    observer_id="protagonist",
                    related_gap_id="gap_homeworld_siege",
                    related_delta_id="delta_ch25_distress_call",
                    from_state=VisibilityState.UNKNOWN,
                    to_state=VisibilityState.SUSPECTED,
                    chapter_number=25,
                ),
            ],
        )
        WorldModelCompiler(session).compile(
            WorldCompileRequest(
                project_id=project_id,
                chapter_number=25,
                approved_changes=ApprovedWorldChangeSet.from_extracted(extracted),
                compiler_run_id="compile-25",
            )
        )

    with Session() as session:
        rows = session.execute(
            select(CognitionSnapshotRow).where(CognitionSnapshotRow.project_id == project_id)
        ).scalars().all()
        snapshots = {row.observer_id: row for row in rows}

    reader_visibility = json.loads(snapshots["reader"].visibility_by_delta_json)
    protagonist_gaps = json.loads(snapshots["protagonist"].suspected_gap_ids_json)
    assert reader_visibility["delta_ch25_distress_call"] == "partially_revealed"
    assert protagonist_gaps == ["gap_homeworld_siege"]


def test_projection_materializes_entity_state_without_overwriting_hidden_truth() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(title="Projection entity", premise="母星危机", genre="科幻", setting_summary="")
        session.add(project)
        session.flush()
        project_id = project.id
        entity = Entity(project_id=project_id, kind="location", name="homeworld")
        session.add(entity)
        session.flush()
        WorldModelRepository(session).create_world_line(
            WorldLine(
                world_line_id="line_homeworld_siege",
                project_id=project_id,
                line_type="hidden_parallel_line",
            )
        )
        extracted = ExtractedWorldChangeSet(
            project_id=project_id,
            chapter_number=21,
            world_deltas=[
                WorldDelta(
                    delta_id="delta_ch21_siege_begins",
                    project_id=project_id,
                    world_line_id="line_homeworld_siege",
                    delta_kind=DeltaKind.OFFSCREEN,
                    summary="敌军开始围困母星",
                    narrative_chapter=21,
                    source=DeltaSource(source_type=DeltaSourceType.FACTION_ACTION),
                    affected_locations=["homeworld"],
                )
            ],
        )
        WorldModelCompiler(session).compile(
            WorldCompileRequest(
                project_id=project_id,
                chapter_number=21,
                approved_changes=ApprovedWorldChangeSet.from_extracted(extracted),
                compiler_run_id="compile-21",
            )
        )

    with Session() as session:
        row = session.execute(
            select(EntityState).where(EntityState.entity_id == entity.id)
        ).scalar_one()
        state = json.loads(row.state_json)

    assert state["visible_to_reader"] != "father_sieged_confirmed"
    assert state["objective_layer"]["siege_status"] == "under_siege"
