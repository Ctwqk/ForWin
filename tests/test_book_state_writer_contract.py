from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.audit.events import DecisionEventType
from forwin.audit.gate_outcome import parse_gate_outcome
from forwin.book_state import (
    BookStateCompiler,
    BookStateQuery,
    BookStateRepository,
    BookStateReviewGate,
)
from forwin.book_state.extraction.contract import (
    BookStateExtractionIssue,
    BookStateExtractionResult,
)
from forwin.book_state.writer_contract import WriterContractDeltaBuilder
from forwin.canon.preparation import BookStateCanonPreparer, CanonPreparationContext
from forwin.models import Project
from forwin.models.base import Base
from forwin.naming import EntityRegistrar
from forwin.protocol import EntityMention, LoreCandidate, WriterOutput
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet,
    GraphDelta,
    MapEdge,
    MapNode,
    NodePatch,
    WorldNode,
)
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.state_change import (
    EventCandidate,
    StateChangeCandidate,
    ThreadBeatCandidate,
    TimeAdvance,
)
from forwin.runtime.policy import RuntimePolicy


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def test_review_gate_blocks_writer_rewrite_of_canonical_rule_definition() -> None:
    engine, session = _session()
    try:
        project = Project(title="规则门禁", premise="测试", genre="科幻")
        session.add(project)
        session.flush()
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(
                id="rule-transit-protocol",
                project_id=project.id,
                node_type="rule",
                name="通行协议",
                profile={"public_version": "三印同亮，门右移一格。"},
                created_at_chapter=5,
            )
        )

        verdict = BookStateReviewGate(session).review(
            ApprovedGraphDeltaSet(
                project_id=project.id,
                chapter_number=6,
                graph_deltas=[
                    GraphDelta(
                        id="delta-rule-rewrite",
                        project_id=project.id,
                        chapter_number=6,
                        source_type="writer_output",
                        operation="apply_writer_contract",
                        node_patches=[
                            NodePatch(
                                node_id="rule-transit-protocol",
                                node_type="rule",
                                op="set",
                                field_path="profile.public_version",
                                old_value="三印同亮，门右移一格。",
                                new_value="三印同亮，门右移两格。",
                            )
                        ],
                    )
                ],
            )
        )

        assert verdict.accepted is False
        issue = next(
            item
            for item in verdict.issues
            if item.code == "immutable_rule_definition_conflict"
        )
        assert issue.target_ref == "node:rule-transit-protocol:profile.public_version"
    finally:
        session.close()
        engine.dispose()


def test_book_state_preparer_records_review_block_reason() -> None:
    engine, session = _session()
    recorded_events: list[dict[str, object]] = []

    def record_decision_event(**payload):
        recorded_events.append(payload)
        return SimpleNamespace(id=f"event-{len(recorded_events)}")

    try:
        project = Project(title="规则审计", premise="测试", genre="科幻")
        session.add(project)
        session.flush()
        BookStateRepository(session).create_world_node(
            WorldNode(
                id="rule-transit-protocol",
                project_id=project.id,
                node_type="rule",
                name="通行协议",
                profile={"public_version": "三印同亮，门右移一格。"},
                created_at_chapter=5,
            )
        )
        output = WriterOutput(
            project_id=project.id,
            chapter_number=6,
            title="第六章",
            body="众人误称门会右移两格。",
            end_of_chapter_summary="正文改写了既有规则。",
            state_changes=[
                StateChangeCandidate(
                    entity_name="通行协议",
                    entity_kind="rule",
                    field="public_version",
                    old_value="三印同亮，门右移一格。",
                    new_value="三印同亮，门右移两格。",
                    reason="writer explicitly rewrote the rule definition",
                )
            ],
        )

        outcome = BookStateCanonPreparer().prepare(
            context=CanonPreparationContext(
                policy=RuntimePolicy.for_profile("standard"),
                llm_client=object(),  # type: ignore[arg-type]
                artifact_store=object(),  # type: ignore[arg-type]
                _record_decision_event=record_decision_event,  # type: ignore[arg-type]
                _record_rule_decision_event=lambda **_kwargs: None,  # type: ignore[arg-type]
            ),
            session=session,
            candidate_id="candidate-review-6",
            project_id=project.id,
            chapter_number=6,
            writer_output=output,
            verdict=ReviewVerdict(verdict="pass"),
        )

        assert outcome.blocked is True
        assert recorded_events[-1]["event_type"] == DecisionEventType.CANON_COMMIT_BLOCKED
        payload = recorded_events[-1]["payload"]
        assert isinstance(payload, dict)
        assert payload["issues"][0]["code"] == "immutable_rule_definition_conflict"
        gate_outcome = parse_gate_outcome(payload)
        assert gate_outcome is not None
        assert gate_outcome.gate_id == "book_state_review"
        assert gate_outcome.candidate_id == "candidate-review-6"
        assert gate_outcome.decision == "block"
        assert gate_outcome.blocked is True
        assert gate_outcome.issue_keys == ["immutable_rule_definition_conflict"]
    finally:
        session.close()
        engine.dispose()


def test_book_state_preparer_records_extraction_block_reason(monkeypatch) -> None:
    engine, session = _session()
    recorded_events: list[dict[str, object]] = []

    def record_decision_event(**payload):
        recorded_events.append(payload)
        return SimpleNamespace(id=f"event-{len(recorded_events)}")

    class RejectedExtractor:
        def __init__(self, **_kwargs) -> None:
            pass

        def extract(self, request):
            return BookStateExtractionResult(
                project_id=request.project_id,
                chapter_number=request.chapter_number,
                accepted=False,
                issues=[
                    BookStateExtractionIssue(
                        code="writer_contract_invalid",
                        message="writer contract extraction failed",
                        evidence_refs=["chapter:1"],
                    )
                ],
            )

    monkeypatch.setattr(
        "forwin.canon.preparation.BookStateGraphDeltaExtractor",
        RejectedExtractor,
    )
    try:
        project = Project(title="提取审计", premise="测试", genre="科幻")
        session.add(project)
        session.flush()
        output = WriterOutput(
            project_id=project.id,
            chapter_number=1,
            title="第一章",
            body="结构化提取失败。",
            end_of_chapter_summary="提取失败。",
        )

        outcome = BookStateCanonPreparer().prepare(
            context=CanonPreparationContext(
                policy=RuntimePolicy.for_profile("standard"),
                llm_client=object(),  # type: ignore[arg-type]
                artifact_store=object(),  # type: ignore[arg-type]
                _record_decision_event=record_decision_event,  # type: ignore[arg-type]
                _record_rule_decision_event=lambda **_kwargs: None,  # type: ignore[arg-type]
            ),
            session=session,
            candidate_id="candidate-extraction-1",
            project_id=project.id,
            chapter_number=1,
            writer_output=output,
            verdict=ReviewVerdict(verdict="pass"),
        )

        assert outcome.blocked is True
        payload = recorded_events[-1]["payload"]
        assert isinstance(payload, dict)
        assert payload["issues"][0]["code"] == "writer_contract_invalid"
        gate_outcome = parse_gate_outcome(payload)
        assert gate_outcome is not None
        assert gate_outcome.gate_id == "book_state_extraction"
        assert gate_outcome.candidate_id == "candidate-extraction-1"
        assert gate_outcome.decision == "block"
        assert gate_outcome.blocked is True
        assert gate_outcome.issue_keys == ["writer_contract_invalid"]
    finally:
        session.close()
        engine.dispose()


def test_writer_contract_persists_rule_lore_as_canonical_definition() -> None:
    engine, session = _session()
    try:
        project = Project(title="规则登记", premise="测试", genre="科幻")
        session.add(project)
        session.flush()
        output = WriterOutput(
            project_id=project.id,
            chapter_number=1,
            title="第一章",
            body="众人验证了通行协议。",
            end_of_chapter_summary="通行协议首次被公开验证。",
            entity_mentions=[
                EntityMention(entity_name="通行协议", entity_kind="rule")
            ],
            lore_candidates=[
                LoreCandidate(
                    subject_name="通行协议",
                    subject_type="rule",
                    description="三印同亮，门右移一格。",
                    evidence_refs=["chapter:1:scene:2"],
                    confidence=0.98,
                )
            ],
        )

        result = WriterContractDeltaBuilder(session).build(
            project_id=project.id,
            chapter_number=1,
            writer_output=output,
            review_verdict_id="review-1",
        )

        assert result.issues == []
        definition_patch = next(
            patch
            for patch in result.graph_deltas[0].node_patches
            if patch.node_type == "rule"
            and patch.field_path == "profile.public_version"
        )
        assert definition_patch.new_value == "三印同亮，门右移一格。"
        assert "chapter:1:scene:2" in result.graph_deltas[0].evidence_refs

        proposed = ApprovedGraphDeltaSet(
            project_id=project.id,
            chapter_number=1,
            graph_deltas=result.graph_deltas,
            approved_by=["test"],
            review_verdict_id="review-1",
        )
        review = BookStateReviewGate(session).review(proposed)
        assert review.accepted is True
        assert review.approved_changes is not None
        BookStateCompiler(session).compile(review.approved_changes)
        rule = BookStateRepository(session).get_world_node(
            definition_patch.node_id
        )
        assert rule is not None
        assert rule.profile["public_version"] == "三印同亮，门右移一格。"
    finally:
        session.close()
        engine.dispose()


def test_writer_rule_lore_rewrite_is_rejected_end_to_end() -> None:
    engine, session = _session()
    try:
        project = Project(title="规则改写", premise="测试", genre="科幻")
        session.add(project)
        session.flush()
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(
                id="rule-transit-protocol",
                project_id=project.id,
                node_type="rule",
                name="通行协议",
                profile={"public_version": "三印同亮，门右移一格。"},
                created_at_chapter=5,
            )
        )
        output = WriterOutput(
            project_id=project.id,
            chapter_number=6,
            title="第六章",
            body="众人误称门会右移两格。",
            end_of_chapter_summary="正文改写了既有规则。",
            state_changes=[
                StateChangeCandidate(
                    entity_name="通行协议",
                    entity_kind="rule",
                    field="public_version",
                    old_value="三印同亮，门右移一格。",
                    new_value="三印同亮，门右移两格。",
                    reason="writer explicitly rewrote the rule definition",
                )
            ],
        )

        result = WriterContractDeltaBuilder(session).build(
            project_id=project.id,
            chapter_number=6,
            writer_output=output,
            review_verdict_id="review-6",
        )
        assert result.issues == []

        proposed = ApprovedGraphDeltaSet(
            project_id=project.id,
            chapter_number=6,
            graph_deltas=result.graph_deltas,
            approved_by=["test"],
            review_verdict_id="review-6",
        )
        review = BookStateReviewGate(session).review(proposed)

        assert review.accepted is False
        assert any(
            issue.code == "immutable_rule_definition_conflict"
            for issue in review.issues
        )
    finally:
        session.close()
        engine.dispose()


def test_writer_contract_preserves_existing_rule_definition_when_lore_paraphrases_it() -> None:
    engine, session = _session()
    try:
        project = Project(title="规则复述", premise="测试", genre="科幻")
        session.add(project)
        session.flush()
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(
                id="rule-transit-protocol",
                project_id=project.id,
                node_type="rule",
                name="通行协议",
                aliases=["三印协议"],
                profile={"public_version": "三印同亮，门右移一格。"},
                created_at_chapter=5,
            )
        )
        output = WriterOutput(
            project_id=project.id,
            chapter_number=6,
            title="第六章",
            body="三枚印记同时亮起，门便向右移动一格。",
            end_of_chapter_summary="本章再次验证了既有通行协议。",
            lore_candidates=[
                LoreCandidate(
                    subject_name="三印协议",
                    subject_type="rule",
                    description="三枚印记同时发光后，门向右挪动一格。",
                    evidence_refs=["chapter:6:scene:1"],
                )
            ],
        )

        result = WriterContractDeltaBuilder(session).build(
            project_id=project.id,
            chapter_number=6,
            writer_output=output,
            review_verdict_id="review-6",
        )

        assert result.issues == []
        assert not any(
            patch.node_id == "rule-transit-protocol"
            and patch.field_path == "profile.public_version"
            for delta in result.graph_deltas
            for patch in delta.node_patches
        )
        review = BookStateReviewGate(session).review(
            ApprovedGraphDeltaSet(
                project_id=project.id,
                chapter_number=6,
                graph_deltas=result.graph_deltas,
                approved_by=["test"],
                review_verdict_id="review-6",
            )
        )
        assert review.accepted is True
    finally:
        session.close()
        engine.dispose()


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

        proposed = ApprovedGraphDeltaSet(
            project_id=project.id,
            chapter_number=2,
            graph_deltas=result.graph_deltas,
            approved_by=["test"],
            review_verdict_id="review-2",
        )
        review_gate = BookStateReviewGate(session).review(proposed)
        assert review_gate.approved_changes is not None
        commit_result = BookStateCompiler(session).compile(
            review_gate.approved_changes
        )
        query = BookStateQuery(session)
        entities = query.active_entities(project.id, as_of_chapter=2)
        events = query.recent_events(project.id, before_chapter=3)
        threads = query.active_threads(project.id, as_of_chapter=2)
        timeline = query.current_timeline(project.id, as_of_chapter=2)

        assert review_gate.accepted is True
        assert commit_result.committed is True
        assert next(item for item in entities if item.name == "陆明").current_state["goal"] == "进入旧塔"
        assert events[0].summary == "陆明确认旧塔入口"
        assert threads[0].name == "旧塔之谜"
        assert threads[0].recent_beats == ["入口坐标被确认"]
        assert timeline is not None and timeline.current_time_label == "第二日清晨"
    finally:
        session.close()
        engine.dispose()


def test_location_change_resolves_embedded_map_names_to_canonical_ids() -> None:
    engine, session = _session()
    try:
        project = Project(title="地点归一", premise="季澈转移档案。", genre="pulp")
        session.add(project)
        session.flush()
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(
                id="char-jiche",
                project_id=project.id,
                node_type="character",
                name="季澈",
                state={"location_id": "loc-repair"},
            )
        )
        repo.append_world_node_state(
            project_id=project.id,
            node_id="char-jiche",
            node_type="character",
            as_of_chapter=0,
            state={"location_id": "loc-repair"},
        )
        repo.create_map_node(
            MapNode(
                id="loc-repair",
                project_id=project.id,
                node_type="site",
                name="档案修复仓",
            )
        )
        repo.create_map_node(
            MapNode(
                id="loc-memory",
                project_id=project.id,
                node_type="site",
                name="身份记忆馆",
            )
        )
        repo.create_map_edge(
            MapEdge(
                id="edge-repair-memory",
                project_id=project.id,
                from_node_id="loc-repair",
                to_node_id="loc-memory",
                edge_type="path",
                travel_time=1,
            )
        )
        output = WriterOutput(
            project_id=project.id,
            chapter_number=1,
            title="第一章",
            body="季澈从档案修复仓进入身份记忆馆。",
            end_of_chapter_summary="季澈抵达身份记忆馆。",
            entity_mentions=[
                EntityMention(entity_name="季澈", entity_kind="character")
            ],
            state_changes=[
                StateChangeCandidate(
                    entity_name="季澈",
                    entity_kind="character",
                    field="location",
                    old_value="档案修复仓主控椅前",
                    new_value="中控枢纽接驳台及身份记忆馆通道",
                    reason="前往核验身份记录",
                )
            ],
        )

        result = WriterContractDeltaBuilder(session).build(
            project_id=project.id,
            chapter_number=1,
            writer_output=output,
            review_verdict_id="review-location",
        )

        location_patch = next(
            patch
            for patch in result.graph_deltas[0].node_patches
            if patch.field_path == "state.location_id"
        )
        assert location_patch.old_value == "loc-repair"
        assert location_patch.new_value == "loc-memory"
        review = BookStateReviewGate(session).review(
            ApprovedGraphDeltaSet(
                project_id=project.id,
                chapter_number=1,
                graph_deltas=result.graph_deltas,
            )
        )
        assert review.accepted is True
        assert review.issues == []
    finally:
        session.close()
        engine.dispose()


def test_unresolved_location_description_does_not_enter_location_id() -> None:
    engine, session = _session()
    try:
        project = Project(title="地点描述", premise="季澈移动。", genre="pulp")
        session.add(project)
        session.flush()
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(
                id="char-jiche",
                project_id=project.id,
                node_type="character",
                name="季澈",
            )
        )
        output = WriterOutput(
            project_id=project.id,
            chapter_number=1,
            title="第一章",
            body="季澈走到临时接驳台。",
            end_of_chapter_summary="季澈停在接驳台。",
            state_changes=[
                StateChangeCandidate(
                    entity_name="季澈",
                    entity_kind="character",
                    field="location",
                    old_value="修复仓主控椅前",
                    new_value="临时中控接驳台",
                    reason="避开封锁",
                )
            ],
        )

        result = WriterContractDeltaBuilder(session).build(
            project_id=project.id,
            chapter_number=1,
            writer_output=output,
            review_verdict_id="review-description",
        )

        assert result.issues == []
        patches = result.graph_deltas[0].node_patches
        assert not any(patch.field_path == "state.location_id" for patch in patches)
        description_patch = next(
            patch
            for patch in patches
            if patch.field_path == "metadata.writer_location"
        )
        assert description_patch.new_value == {
            "reported_old": "修复仓主控椅前",
            "reported_new": "临时中控接驳台",
        }
        review = BookStateReviewGate(session).review(
            ApprovedGraphDeltaSet(
                project_id=project.id,
                chapter_number=1,
                graph_deltas=result.graph_deltas,
            )
        )
        assert review.accepted is True
        assert review.issues == []
    finally:
        session.close()
        engine.dispose()


def test_canonical_unreachable_location_change_remains_blocked() -> None:
    engine, session = _session()
    try:
        project = Project(title="地点阻断", premise="季澈无法越界。", genre="pulp")
        session.add(project)
        session.flush()
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(
                id="char-jiche",
                project_id=project.id,
                node_type="character",
                name="季澈",
                state={"location_id": "loc-a"},
            )
        )
        repo.append_world_node_state(
            project_id=project.id,
            node_id="char-jiche",
            node_type="character",
            as_of_chapter=0,
            state={"location_id": "loc-a"},
        )
        repo.create_map_node(
            MapNode(
                id="loc-a",
                project_id=project.id,
                node_type="site",
                name="甲区",
            )
        )
        repo.create_map_node(
            MapNode(
                id="loc-b",
                project_id=project.id,
                node_type="site",
                name="乙区",
            )
        )
        output = WriterOutput(
            project_id=project.id,
            chapter_number=1,
            title="第一章",
            body="季澈试图从甲区进入乙区。",
            end_of_chapter_summary="通路仍然封闭。",
            state_changes=[
                StateChangeCandidate(
                    entity_name="季澈",
                    entity_kind="character",
                    field="location",
                    old_value="甲区",
                    new_value="乙区",
                    reason="尝试越界",
                )
            ],
        )

        result = WriterContractDeltaBuilder(session).build(
            project_id=project.id,
            chapter_number=1,
            writer_output=output,
            review_verdict_id="review-blocked",
        )
        review = BookStateReviewGate(session).review(
            ApprovedGraphDeltaSet(
                project_id=project.id,
                chapter_number=1,
                graph_deltas=result.graph_deltas,
            )
        )

        assert review.accepted is False
        assert [issue.code for issue in review.issues] == ["movement_unreachable"]
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


def test_newly_admitted_character_location_has_no_fabricated_old_value() -> None:
    class Classifier:
        def classify(self, **_kwargs):
            return [
                {
                    "decision": "register_character",
                    "name": "季澈",
                    "canonical_name": "季澈",
                    "role_hint": "复核员",
                }
            ]

    engine, session = _session()
    try:
        project = Project(title="新角色地点", premise="季澈进入场景。", genre="pulp")
        session.add(project)
        session.flush()
        BookStateRepository(session).create_map_node(
            MapNode(
                id="loc-memory",
                project_id=project.id,
                node_type="site",
                name="身份记忆馆",
            )
        )
        planned = EntityRegistrar(
            session=session,
            classifier=Classifier(),
        ).plan_writer_output(
            project_id=project.id,
            chapter_number=1,
            writer_output=WriterOutput(
                project_id=project.id,
                chapter_number=1,
                title="第一章",
                body="季澈从修复仓来到身份记忆馆。",
                end_of_chapter_summary="季澈抵达身份记忆馆。",
                entity_mentions=[
                    EntityMention(entity_name="季澈", entity_kind="character")
                ],
                state_changes=[
                    StateChangeCandidate(
                        entity_name="季澈",
                        entity_kind="character",
                        field="location",
                        old_value="档案修复仓主控椅前",
                        new_value="身份记忆馆通道",
                        reason="进入主舞台",
                    )
                ],
            ),
        )

        result = WriterContractDeltaBuilder(session).build(
            project_id=project.id,
            chapter_number=1,
            writer_output=planned.writer_output,
            review_verdict_id="review-new-character",
        )

        location_patch = next(
            patch
            for patch in result.graph_deltas[0].node_patches
            if patch.field_path == "state.location_id"
        )
        assert location_patch.old_value is None
        assert location_patch.new_value == "loc-memory"
        review = BookStateReviewGate(session).review(
            ApprovedGraphDeltaSet(
                project_id=project.id,
                chapter_number=1,
                graph_deltas=result.graph_deltas,
            )
        )
        assert review.accepted is True
        assert review.approved_changes is not None
        compile_result = BookStateCompiler(session).compile(
            review.approved_changes
        )
        assert compile_result.committed is True
    finally:
        session.close()
        engine.dispose()


def test_named_non_character_nodes_use_only_canonical_fields() -> None:
    engine, session = _session()
    try:
        project = Project(title="非角色节点", premise="规则约束物件。", genre="pulp")
        session.add(project)
        session.flush()
        output = WriterOutput(
            project_id=project.id,
            chapter_number=1,
            title="第一章",
            body="归档窗口层锁住了偏差锁定单。",
            end_of_chapter_summary="锁定单进入归档窗口。",
            entity_mentions=[
                EntityMention(entity_name="归档窗口层", entity_kind="rule"),
                EntityMention(entity_name="偏差锁定单", entity_kind="item"),
            ],
        )

        result = WriterContractDeltaBuilder(session).build(
            project_id=project.id,
            chapter_number=1,
            writer_output=output,
            review_verdict_id="review-non-character",
        )
        review = BookStateReviewGate(session).review(
            ApprovedGraphDeltaSet(
                project_id=project.id,
                chapter_number=1,
                graph_deltas=result.graph_deltas,
            )
        )

        created = [
            patch
            for patch in result.graph_deltas[0].node_patches
            if str(patch.op) == "create"
        ]
        assert {patch.node_type for patch in created} == {"item", "rule"}
        assert all(patch.new_value["profile"] == {} for patch in created)
        assert review.accepted is True
        assert review.issues == []
    finally:
        session.close()
        engine.dispose()


def test_noncanonical_writer_fields_do_not_pollute_canonical_state() -> None:
    engine, session = _session()
    try:
        project = Project(title="字段归档", premise="物件进入证据链。", genre="pulp")
        session.add(project)
        session.flush()
        output = WriterOutput(
            project_id=project.id,
            chapter_number=1,
            title="第一章",
            body="样本-17被装入证据盒，核验流程留下审计注记。",
            end_of_chapter_summary="证据链完成登记。",
            state_changes=[
                StateChangeCandidate(
                    entity_name="样本-17",
                    entity_kind="item",
                    field="custody_state",
                    old_value="留在队列",
                    new_value="被装入证据盒",
                    reason="保全证据",
                ),
                StateChangeCandidate(
                    entity_name="核验流程",
                    entity_kind="rule",
                    field="audit_note",
                    old_value="",
                    new_value="仅允许最小账本复核",
                    reason="限制记忆债",
                ),
            ],
        )

        result = WriterContractDeltaBuilder(session).build(
            project_id=project.id,
            chapter_number=1,
            writer_output=output,
            review_verdict_id="review-writer-fields",
        )
        patches = result.graph_deltas[0].node_patches
        assert any(
            patch.node_type == "item"
            and patch.field_path == "state.state_summary"
            and patch.new_value == "被装入证据盒"
            for patch in patches
        )
        assert any(
            patch.node_type == "rule"
            and patch.field_path == "metadata.writer_state.audit_note"
            and patch.new_value == "仅允许最小账本复核"
            for patch in patches
        )
        assert not any(
            patch.field_path.startswith("state.metadata.")
            for patch in patches
        )
        review = BookStateReviewGate(session).review(
            ApprovedGraphDeltaSet(
                project_id=project.id,
                chapter_number=1,
                graph_deltas=result.graph_deltas,
            )
        )
        assert review.accepted is True
        assert review.issues == []
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


def test_event_contract_ignores_generic_and_non_character_participants() -> None:
    class Classifier:
        def classify(self, **_kwargs):
            return [
                {
                    "decision": "register_character",
                    "name": "季澈",
                    "canonical_name": "季澈",
                    "role_hint": "复核员",
                },
                {
                    "decision": "background_generic",
                    "name": "系统",
                    "reason": "project classifier decision",
                },
            ]

    engine, session = _session()
    try:
        project = Project(title="事件泛称", premise="季澈核对签名。", genre="pulp")
        session.add(project)
        session.flush()
        planned = EntityRegistrar(
            session=session,
            classifier=Classifier(),
        ).plan_writer_output(
            project_id=project.id,
            chapter_number=1,
            writer_output=WriterOutput(
                project_id=project.id,
                chapter_number=1,
                title="第一章",
                body="季澈发现系统记录了另一身份的覆盖请求。",
                end_of_chapter_summary="季澈封存覆盖请求。",
                entity_mentions=[
                    EntityMention(entity_name="季澈", entity_kind="character")
                ],
                new_events=[
                    EventCandidate(
                        summary="另一身份通过系统尝试覆盖记录",
                        significance="major",
                        involved_entity_names=["季澈", "系统", "另一身份"],
                        roles=["protagonist", "observer"],
                    )
                ],
            ),
        )

        result = WriterContractDeltaBuilder(session).build(
            project_id=project.id,
            chapter_number=1,
            writer_output=planned.writer_output,
            review_verdict_id="review-1",
        )

        assert result.issues == []
        assert len(result.graph_deltas) == 1
        event_patch = next(
            patch
            for patch in result.graph_deltas[0].node_patches
            if patch.node_type == "event"
        )
        admitted_character = next(
            decision
            for decision in planned.plan.decisions
            if decision.mention_name == "季澈"
        )
        assert event_patch.new_value["state"]["participant_ids"] == [
            admitted_character.entity_id
        ]
    finally:
        session.close()
        engine.dispose()


def test_event_contract_still_rejects_unadmitted_named_participant() -> None:
    engine, session = _session()
    try:
        project = Project(title="事件真名", premise="季澈核对签名。", genre="pulp")
        session.add(project)
        session.flush()
        output = WriterOutput(
            project_id=project.id,
            chapter_number=1,
            title="第一章",
            body="周隐覆盖了记录。",
            end_of_chapter_summary="记录被覆盖。",
            new_events=[
                EventCandidate(
                    summary="周隐覆盖记录",
                    significance="major",
                    involved_entity_names=["周隐"],
                    roles=["antagonist"],
                )
            ],
        )

        result = WriterContractDeltaBuilder(session).build(
            project_id=project.id,
            chapter_number=1,
            writer_output=output,
            review_verdict_id="review-1",
        )

        assert result.graph_deltas == []
        assert [issue.code for issue in result.issues] == [
            "unresolved_event_entity"
        ]
        assert "周隐" in result.issues[0].message
    finally:
        session.close()
        engine.dispose()
