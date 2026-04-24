from __future__ import annotations

import json

from sqlalchemy import func, select

from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.event import CanonEvent
from forwin.models.world_v4 import (
    ReaderExperienceDeltaRow,
    RevealEventRow,
    WorldCompileRunV4Row,
    WorldDeltaRow,
)
from forwin.protocol.world_v4 import (
    ApprovedWorldChangeSet,
    DeltaKind,
    DeltaSource,
    DeltaSourceType,
    ExtractedWorldChangeSet,
    KnowledgeUpdateEvent,
    KnowledgeUpdateType,
    ObserverType,
    ReaderExperienceDelta,
    RevealEvent,
    VisibilityState,
    WorldCompileRequest,
    WorldDelta,
    WorldLine,
)
from forwin.reviewer_v4 import V4ReviewGateVerdict, V4ReviewIssue
from forwin.world_model_v4.compiler import WorldModelCompiler
from forwin.world_model_v4.repository import WorldModelRepository


def _approved_changes(project_id: str) -> ApprovedWorldChangeSet:
    extracted = ExtractedWorldChangeSet(
        project_id=project_id,
        chapter_number=23,
        world_deltas=[
            WorldDelta(
                delta_id="delta_hint_callsign",
                project_id=project_id,
                world_line_id="line_homeworld_siege",
                delta_kind=DeltaKind.HINT,
                summary="乱码通讯与父亲旧部呼号",
                narrative_chapter=23,
                source=DeltaSource(source_type=DeltaSourceType.INFORMATION_SPREAD),
            )
        ],
        reveal_events=[
            RevealEvent(
                reveal_event_id="reveal_ch23_hint",
                project_id=project_id,
                related_gap_id="gap_homeworld_siege",
                reveal_to_reader=True,
                reveal_method="hint",
                from_state=VisibilityState.HIDDEN,
                to_state=VisibilityState.HINTED,
            )
        ],
        knowledge_update_events=[
            KnowledgeUpdateEvent(
                update_event_id="knowledge_ch23_reader_hint",
                project_id=project_id,
                update_type=KnowledgeUpdateType.HINT,
                observer_type=ObserverType.READER,
                observer_id="reader",
                related_gap_id="gap_homeworld_siege",
                from_state=VisibilityState.HIDDEN,
                to_state=VisibilityState.HINTED,
                chapter_number=23,
            )
        ],
        reader_experience_deltas=[
            ReaderExperienceDelta(
                reader_experience_delta_id="reader_exp_ch23_hint",
                project_id=project_id,
                chapter_number=23,
                cognition_transition="hidden -> hinted",
                payoff_type="short_term_hint",
                reward_tags=["mystery"],
            )
        ],
    )
    return ApprovedWorldChangeSet.from_extracted(
        extracted,
        approved_by=["WorldDeltaReviewer", "RevealReviewer", "ReaderCognitionReviewer"],
        review_verdict_id="review-23",
    )


def test_compiler_writes_approved_ledgers_snapshot_and_derived_canon_event() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(
            title="V4 compiler",
            premise="母星危机",
            genre="科幻",
            setting_summary="母星与殖民地",
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
        result = WorldModelCompiler(session).compile(
            WorldCompileRequest(
                project_id=project_id,
                chapter_number=23,
                approved_changes=_approved_changes(project_id),
                review_verdict_id="review-23",
                compiler_run_id="compile-23",
            )
        )

    with Session() as session:
        world_delta_count = session.scalar(select(func.count()).select_from(WorldDeltaRow))
        reveal_count = session.scalar(select(func.count()).select_from(RevealEventRow))
        reader_exp_count = session.scalar(
            select(func.count()).select_from(ReaderExperienceDeltaRow)
        )
        compile_run = session.execute(select(WorldCompileRunV4Row)).scalar_one()
        canon_event = session.execute(select(CanonEvent)).scalar_one()
        snapshot = WorldModelRepository(session).get_snapshot_as_of_chapter(project_id, 23)

    assert result.committed is True
    assert result.world_delta_ids == ["delta_hint_callsign"]
    assert result.reveal_event_ids == ["reveal_ch23_hint"]
    assert result.reader_experience_delta_ids == ["reader_exp_ch23_hint"]
    assert result.snapshot_id == snapshot.snapshot_id
    assert result.derived_canon_event_ids == [canon_event.id]
    assert world_delta_count == 1
    assert reveal_count == 1
    assert reader_exp_count == 1
    assert compile_run.committed is True
    assert json.loads(compile_run.retrieval_pack_json) == {}
    assert json.loads(compile_run.projection_refresh_json)["snapshot_id"] == result.snapshot_id
    assert "乱码通讯与父亲旧部呼号" in canon_event.summary


def test_compiler_blocks_failed_review_without_writing_ledgers() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(
            title="V4 compiler block",
            premise="母星危机",
            genre="科幻",
            setting_summary="母星与殖民地",
        )
        session.add(project)
        session.flush()
        project_id = project.id
        failed_verdict = V4ReviewGateVerdict(
            passed=False,
            issues=[
                V4ReviewIssue(
                    reviewer="RevealReviewer",
                    severity="fail",
                    failure_type="early_reveal",
                    message="提前 reveal",
                )
            ],
        )
        result = WorldModelCompiler(session).compile_gate_verdict(
            project_id=project_id,
            chapter_number=23,
            verdict=failed_verdict,
            compiler_run_id="compile-blocked-23",
        )

    with Session() as session:
        world_delta_count = session.scalar(select(func.count()).select_from(WorldDeltaRow))
        compile_run = session.execute(select(WorldCompileRunV4Row)).scalar_one()

    assert result.committed is False
    assert result.blocked_reasons == ["early_reveal: 提前 reveal"]
    assert world_delta_count == 0
    assert compile_run.committed is False
