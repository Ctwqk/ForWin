from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.book_state import (
    BookStateDirectCommitService,
    BookStateQuery,
    BookStateRepository,
)
from forwin.book_state.writer_contract import WriterContractDeltaBuilder
from forwin.models import Project
from forwin.models.base import Base
from forwin.naming import EntityRegistrar
from forwin.protocol import EntityMention, WriterOutput
from forwin.protocol.book_state import ApprovedGraphDeltaSet, WorldNode
from forwin.protocol.state_change import (
    EventCandidate,
    StateChangeCandidate,
    ThreadBeatCandidate,
    TimeAdvance,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def test_writer_contract_maps_structured_output_into_book_state_deltas() -> None:
    engine, session = _session()
    try:
        project = Project(title="单写方", premise="主角陆明。", genre="pulp")
        session.add(project)
        session.flush()
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(
                id="char-luming",
                project_id=project.id,
                node_type="character",
                name="陆明",
                aliases=[],
                description="主角",
                state={"status": "active", "goal": "调查"},
            )
        )
        repo.append_world_node_state(
            project_id=project.id,
            node_id="char-luming",
            node_type="character",
            as_of_chapter=1,
            state={"status": "active", "goal": "调查"},
        )
        output = WriterOutput(
            project_id=project.id,
            chapter_number=2,
            title="第二章",
            body="陆明确认了新的目标。",
            end_of_chapter_summary="陆明决定进入旧塔。",
            entity_mentions=[EntityMention(entity_name="陆明", entity_kind="character")],
            state_changes=[
                StateChangeCandidate(
                    entity_name="陆明",
                    entity_kind="character",
                    field="goal",
                    old_value="调查",
                    new_value="进入旧塔",
                    reason="线索推进",
                )
            ],
            new_events=[
                EventCandidate(
                    summary="陆明确认旧塔入口",
                    significance="major",
                    involved_entity_names=["陆明"],
                    roles=["protagonist"],
                )
            ],
            thread_beats=[
                ThreadBeatCandidate(
                    thread_name="旧塔之谜",
                    beat_type="escalation",
                    description="入口坐标被确认",
                )
            ],
            time_advance=TimeAdvance(
                new_time_label="第二日清晨",
                duration_description="一夜后",
            ),
        )

        result = WriterContractDeltaBuilder(session).build(
            project_id=project.id,
            chapter_number=2,
            writer_output=output,
            review_verdict_id="review-2",
        )

        assert result.issues == []
        assert len(result.graph_deltas) == 1
        delta = result.graph_deltas[0]
        assert delta.story_time == "第二日清晨"
        assert any(
            patch.node_id == "char-luming"
            and patch.field_path == "state.goal"
            and patch.new_value == "进入旧塔"
            for patch in delta.node_patches
        )
        assert any(patch.node_type == "event" for patch in delta.node_patches)
        assert any(patch.target_ref.startswith("plot_thread:") for patch in delta.narrative_patches)
        assert delta.fact_patches[0].proposition == "陆明确认旧塔入口"

        commit_result = BookStateDirectCommitService(session).commit(
            ApprovedGraphDeltaSet(
                project_id=project.id,
                chapter_number=2,
                graph_deltas=result.graph_deltas,
                approved_by=["test"],
                review_verdict_id="review-2",
            )
        )
        query = BookStateQuery(session)
        entities = query.active_entities(project.id, as_of_chapter=2)
        events = query.recent_events(project.id, before_chapter=3)
        threads = query.active_threads(project.id, as_of_chapter=2)
        timeline = query.current_timeline(project.id, as_of_chapter=2)

        assert commit_result.committed is True
        assert next(item for item in entities if item.name == "陆明").current_state["goal"] == "进入旧塔"
        assert events[0].summary == "陆明确认旧塔入口"
        assert threads[0].name == "旧塔之谜"
        assert threads[0].recent_beats == ["入口坐标被确认"]
        assert timeline is not None and timeline.current_time_label == "第二日清晨"
    finally:
        session.close()
        engine.dispose()


def test_entity_admission_plan_creates_book_state_character_patch_without_legacy_write() -> None:
    class Classifier:
        def classify(self, **_kwargs):
            return [
                {
                    "decision": "register_character",
                    "name": "陈潮白",
                    "canonical_name": "陈潮白",
                    "role_hint": "馆员",
                }
            ]

    engine, session = _session()
    try:
        project = Project(title="实体计划", premise="主角陆明。", genre="pulp")
        session.add(project)
        session.flush()
        planned = EntityRegistrar(
            session=session,
            classifier=Classifier(),
        ).plan_writer_output(
            project_id=project.id,
            chapter_number=3,
            writer_output=WriterOutput(
                project_id=project.id,
                chapter_number=3,
                title="第三章",
                body="陈潮白递出档案。",
                end_of_chapter_summary="馆员交付档案。",
                entity_mentions=[
                    EntityMention(entity_name="陈潮白", entity_kind="character")
                ],
            ),
        )

        result = WriterContractDeltaBuilder(session).build(
            project_id=project.id,
            chapter_number=3,
            writer_output=planned.writer_output,
            review_verdict_id="review-3",
        )

        decision = planned.plan.decisions[0]
        assert result.issues == []
        assert any(
            patch.op == "create"
            and patch.node_id == decision.entity_id
            and patch.node_type == "character"
            for patch in result.graph_deltas[0].node_patches
        )
    finally:
        session.close()
        engine.dispose()


def test_unadmitted_character_state_change_fails_closed() -> None:
    engine, session = _session()
    try:
        project = Project(title="未知角色", premise="主角陆明。", genre="pulp")
        session.add(project)
        session.flush()
        output = WriterOutput(
            project_id=project.id,
            chapter_number=4,
            title="第四章",
            body="突兀角色改变目标。",
            end_of_chapter_summary="目标变化。",
            state_changes=[
                StateChangeCandidate(
                    entity_name="突兀角色",
                    entity_kind="character",
                    field="goal",
                    old_value="",
                    new_value="夺取档案",
                    reason="突然出现",
                )
            ],
        )

        result = WriterContractDeltaBuilder(session).build(
            project_id=project.id,
            chapter_number=4,
            writer_output=output,
            review_verdict_id="review-4",
        )

        assert result.graph_deltas == []
        assert [issue.code for issue in result.issues] == [
            "unresolved_character_reference"
        ]
    finally:
        session.close()
        engine.dispose()
