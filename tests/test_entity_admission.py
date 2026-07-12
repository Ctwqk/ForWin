from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from forwin.models import Entity, Project
from forwin.models.base import Base
from forwin.naming import EntityRegistrar
from forwin.naming.entity_registrar import LLMEntityAdmissionClassifier
from forwin.protocol import EntityMention, SceneOutput, WriterOutput
from forwin.protocol.state_change import EventCandidate, StateChangeCandidate
from forwin.review.draft_service import DraftReviewService


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def test_draft_review_turns_entity_admission_conflict_into_hard_issue() -> None:
    engine, session = _session()
    try:
        project = Project(title="准入冲突", premise="主角陆明。", genre="pulp")
        session.add(project)
        session.flush()
        planned = EntityRegistrar(session=session).plan_writer_output(
            project_id=project.id,
            chapter_number=3,
            writer_output=WriterOutput(
                project_id=project.id,
                chapter_number=3,
                title="第三章",
                body="突兀角色进入现场。",
                end_of_chapter_summary="突兀角色出现。",
                entity_mentions=[
                    EntityMention(
                        entity_name="突兀角色",
                        entity_kind="character",
                        is_named=True,
                    )
                ],
            ),
        )

        issues = DraftReviewService._entity_admission_issues(planned.writer_output)

        assert len(issues) == 1
        assert issues[0].issue_type == "entity_admission_plan_conflict"
        assert issues[0].severity == "error"
        assert issues[0].blocking is True
    finally:
        session.close()
        engine.dispose()


def test_reference_classifier_marks_generic_role_without_llm() -> None:
    class UnexpectedClassifier:
        def classify(self, **_kwargs):
            raise AssertionError("generic role must not reach LLM classifier")

    engine, session = _session()
    try:
        project = Project(title="泛称输入", premise="主角陆明。", genre="pulp")
        session.add(project)
        session.flush()
        result = EntityRegistrar(
            session=session,
            classifier=UnexpectedClassifier(),
        ).plan_writer_output(
            project_id=project.id,
            chapter_number=4,
            writer_output=WriterOutput(
                project_id=project.id,
                chapter_number=4,
                title="第四章",
                body="不明追踪者切断链路。",
                end_of_chapter_summary="链路中断。",
                entity_mentions=[
                    EntityMention(
                        entity_name="不明追踪者",
                        entity_kind="character",
                        is_named=True,
                    )
                ],
            ),
        )

        assert result.background_generic_names == ["不明追踪者"]
        assert result.plan.decisions[0].action == "background_generic"
        assert result.writer_output.entity_mentions == []
        assert session.execute(select(Entity)).scalars().all() == []
    finally:
        session.close()
        engine.dispose()


def test_reference_classifier_drops_named_mention_without_prose_evidence() -> None:
    class UnexpectedClassifier:
        def classify(self, **_kwargs):
            raise AssertionError("unsupported mention must not reach LLM classifier")

    engine, session = _session()
    try:
        project = Project(title="抽取噪声", premise="潮序局负责复核。", genre="pulp")
        session.add(project)
        session.flush()
        result = EntityRegistrar(
            session=session,
            classifier=UnexpectedClassifier(),
        ).plan_writer_output(
            project_id=project.id,
            chapter_number=4,
            writer_output=WriterOutput(
                project_id=project.id,
                chapter_number=4,
                title="第四章",
                body="潮序局关闭了复核窗口。",
                end_of_chapter_summary="复核窗口关闭。",
                entity_mentions=[
                    EntityMention(
                        entity_name="蔡序",
                        entity_kind="character",
                        is_named=True,
                    )
                ],
            ),
        )

        assert result.background_generic_names == ["蔡序"]
        assert result.writer_output.entity_mentions == []
        assert result.plan_conflicts == []
    finally:
        session.close()
        engine.dispose()


def test_admission_discovers_character_references_outside_entity_mentions() -> None:
    class Classifier:
        def __init__(self) -> None:
            self.names: list[str] = []

        def classify(self, **kwargs):
            self.names = list(kwargs["names"])
            return [
                {
                    "name": name,
                    "decision": "register_character",
                    "canonical_name": name,
                    "role_hint": "本章在场角色",
                }
                for name in self.names
            ]

    classifier = Classifier()
    engine, session = _session()
    try:
        project = Project(title="结构化角色", premise="三人联合复核。", genre="pulp")
        session.add(project)
        session.flush()
        result = EntityRegistrar(
            session=session,
            classifier=classifier,
        ).plan_writer_output(
            project_id=project.id,
            chapter_number=1,
            writer_output=WriterOutput(
                project_id=project.id,
                chapter_number=1,
                title="第一章",
                body="季澈与白铭交出证据，梁祚批准联合窗口。",
                end_of_chapter_summary="梁祚批准三人联合追查。",
                entity_mentions=[
                    EntityMention(entity_name="季澈", entity_kind="character"),
                    EntityMention(entity_name="白铭", entity_kind="character"),
                ],
                state_changes=[
                    StateChangeCandidate(
                        entity_name="梁祚",
                        entity_kind="character",
                        field="role_state",
                        old_value="常规调度",
                        new_value="批准联合窗口",
                        reason="偏移证据成立",
                    )
                ],
                new_events=[
                    EventCandidate(
                        summary="梁祚批准联合窗口",
                        significance="major",
                        involved_entity_names=["季澈", "白铭", "梁祚"],
                        roles=["protagonist", "support", "admin"],
                    )
                ],
            ),
        )

        assert classifier.names == ["季澈", "白铭", "梁祚"]
        assert result.registered_names == ["季澈", "白铭", "梁祚"]
        assert result.plan_conflicts == []
    finally:
        session.close()
        engine.dispose()


def test_admission_does_not_reclassify_typed_non_character_participants() -> None:
    class Classifier:
        def __init__(self) -> None:
            self.names: list[str] = []

        def classify(self, **kwargs):
            self.names = list(kwargs["names"])
            return [
                {
                    "name": name,
                    "decision": "register_character",
                    "canonical_name": name,
                }
                for name in self.names
            ]

    classifier = Classifier()
    engine, session = _session()
    try:
        project = Project(title="类型保留", premise="季澈复核样本。", genre="pulp")
        session.add(project)
        session.flush()
        result = EntityRegistrar(
            session=session,
            classifier=classifier,
        ).plan_writer_output(
            project_id=project.id,
            chapter_number=1,
            writer_output=WriterOutput(
                project_id=project.id,
                chapter_number=1,
                title="第一章",
                body="季澈将样本-17转入证据链。",
                end_of_chapter_summary="样本-17完成复核。",
                entity_mentions=[
                    EntityMention(entity_name="季澈", entity_kind="character")
                ],
                state_changes=[
                    StateChangeCandidate(
                        entity_name="样本-17",
                        entity_kind="item",
                        field="status",
                        old_value="待复核",
                        new_value="已复核",
                        reason="偏移确认",
                    )
                ],
                new_events=[
                    EventCandidate(
                        summary="季澈完成样本复核",
                        involved_entity_names=["季澈", "样本-17"],
                        roles=["protagonist", "evidence"],
                    )
                ],
            ),
        )

        assert classifier.names == ["季澈"]
        assert result.writer_output.new_events[0].involved_entity_names == [
            "季澈",
            "样本-17",
        ]
    finally:
        session.close()
        engine.dispose()


def test_admission_drops_generic_refs_from_structured_character_surfaces() -> None:
    class UnexpectedClassifier:
        def classify(self, **_kwargs):
            raise AssertionError("generic refs must not reach the LLM classifier")

    engine, session = _session()
    try:
        project = Project(title="结构泛称", premise="系统发出告警。", genre="pulp")
        session.add(project)
        session.flush()
        result = EntityRegistrar(
            session=session,
            classifier=UnexpectedClassifier(),
        ).plan_writer_output(
            project_id=project.id,
            chapter_number=1,
            writer_output=WriterOutput(
                project_id=project.id,
                chapter_number=1,
                title="第一章",
                body="系统提示不明追踪者正在覆盖记录。",
                end_of_chapter_summary="覆盖请求被拦截。",
                state_changes=[
                    StateChangeCandidate(
                        entity_name="不明追踪者",
                        entity_kind="character",
                        field="status",
                        old_value="未知",
                        new_value="正在覆盖记录",
                        reason="系统告警",
                    )
                ],
                new_events=[
                    EventCandidate(
                        summary="系统报告覆盖请求",
                        involved_entity_names=["系统", "不明追踪者"],
                        roles=["observer", "antagonist"],
                    )
                ],
            ),
        )

        assert result.background_generic_names == ["不明追踪者", "系统"]
        assert result.writer_output.state_changes == []
        assert result.writer_output.new_events[0].involved_entity_names == []
        assert result.writer_output.new_events[0].roles == []
    finally:
        session.close()
        engine.dispose()


def test_classifier_accepts_unknown_name_as_backward_compatible_identity_key() -> None:
    class UnknownNameClassifier:
        def classify(self, **_kwargs):
            return [
                {
                    "unknown_name": "蔡序",
                    "decision": "register_character",
                    "canonical_name": "蔡序",
                    "aliases": [],
                    "role_hint": "复核员",
                }
            ]

    engine, session = _session()
    try:
        project = Project(title="分类契约", premise="蔡序负责复核。", genre="pulp")
        session.add(project)
        session.flush()
        result = EntityRegistrar(
            session=session,
            classifier=UnknownNameClassifier(),
        ).plan_writer_output(
            project_id=project.id,
            chapter_number=4,
            writer_output=WriterOutput(
                project_id=project.id,
                chapter_number=4,
                title="第四章",
                body="蔡序在窗口后核对签名。",
                end_of_chapter_summary="蔡序完成核对。",
                entity_mentions=[
                    EntityMention(
                        entity_name="蔡序",
                        entity_kind="character",
                        is_named=True,
                    )
                ],
            ),
        )

        assert result.registered_names == ["蔡序"]
        assert result.plan_conflicts == []
        assert result.plan.decisions[0].mention_name == "蔡序"
    finally:
        session.close()
        engine.dispose()


def test_llm_classifier_includes_evidence_for_mentions_after_body_excerpt() -> None:
    class CapturingClient:
        def __init__(self) -> None:
            self.messages = []

        def chat(self, messages, **_kwargs):
            self.messages = messages
            return json.dumps(
                {
                    "decisions": [
                        {
                            "name": "蔡序",
                            "decision": "register_character",
                            "canonical_name": "蔡序",
                            "aliases": [],
                            "role_hint": "复核员",
                        }
                    ]
                },
                ensure_ascii=False,
            )

    client = CapturingClient()
    classifier = LLMEntityAdmissionClassifier(client)
    body = ("前置场景。" * 500) + "蔡序在复核窗口后核对签名。"

    classifier.classify(
        project_id="project-1",
        chapter_number=1,
        names=["蔡序"],
        writer_output=WriterOutput(
            project_id="project-1",
            chapter_number=1,
            title="第一章",
            body=body,
            end_of_chapter_summary="签名完成核对。",
        ),
        existing_entities=[],
    )

    payload = json.loads(client.messages[1]["content"])
    assert "蔡序" not in payload["body_excerpt"]
    assert "蔡序在复核窗口后核对签名" in payload["mention_evidence"][0]["quotes"][0]
    assert "必须包含 name 字段" in client.messages[0]["content"]


def test_canon_verifier_rejects_admission_conflict_without_reclassification() -> None:
    engine, session = _session()
    try:
        project = Project(title="准入验证", premise="主角陆明。", genre="pulp")
        session.add(project)
        session.flush()
        registrar = EntityRegistrar(session=session)
        planned = registrar.plan_writer_output(
            project_id=project.id,
            chapter_number=5,
            writer_output=WriterOutput(
                project_id=project.id,
                chapter_number=5,
                title="第五章",
                body="突兀角色进入现场。",
                end_of_chapter_summary="突兀角色出现。",
                entity_mentions=[
                    EntityMention(
                        entity_name="突兀角色",
                        entity_kind="character",
                        is_named=True,
                    )
                ],
            ),
        )

        with pytest.raises(ValueError, match="突兀角色"):
            registrar.verify_writer_output_admission(
                project_id=project.id,
                writer_output=planned.writer_output,
            )
    finally:
        session.close()
        engine.dispose()


def test_admission_plan_is_invalidated_when_candidate_changes() -> None:
    engine, session = _session()
    try:
        project = Project(title="准入指纹", premise="主角陆明。", genre="pulp")
        session.add(project)
        session.flush()
        registrar = EntityRegistrar(session=session)
        planned = registrar.plan_writer_output(
            project_id=project.id,
            chapter_number=6,
            writer_output=WriterOutput(
                project_id=project.id,
                chapter_number=6,
                title="第六章",
                body="陆明进入现场。",
                end_of_chapter_summary="陆明抵达。",
            ),
        )
        changed = planned.writer_output.model_copy(update={"body": "陆明离开现场。"})

        issues = DraftReviewService._entity_admission_issues(changed)

        assert len(issues) == 1
        assert issues[0].issue_type == "entity_admission_plan_invalid"
        assert "stale" in issues[0].description
    finally:
        session.close()
        engine.dispose()


def test_admission_plan_ignores_artifact_storage_paths() -> None:
    engine, session = _session()
    try:
        project = Project(title="准入存储路径", premise="主角陆明。", genre="pulp")
        session.add(project)
        session.flush()
        registrar = EntityRegistrar(session=session)
        planned = registrar.plan_writer_output(
            project_id=project.id,
            chapter_number=7,
            writer_output=WriterOutput(
                project_id=project.id,
                chapter_number=7,
                title="第七章",
                body="陆明进入现场。",
                end_of_chapter_summary="陆明抵达。",
                scene_outputs=[
                    SceneOutput(
                        scene_no=1,
                        scene_objective="抵达现场",
                        text="陆明进入现场。",
                    )
                ],
            ),
        )
        persisted = planned.writer_output.model_copy(
            update={
                "draft_blob_path": "minio://drafts/chapter-7.txt",
                "scene_outputs": [
                    planned.writer_output.scene_outputs[0].model_copy(
                        update={"text_blob_path": "minio://scenes/chapter-7-scene-1.txt"}
                    )
                ],
            }
        )

        verified = registrar.verify_writer_output_admission(
            project_id=project.id,
            writer_output=persisted,
        )

        assert verified == planned.plan
    finally:
        session.close()
        engine.dispose()


def test_admission_plan_still_rejects_semantic_scene_changes() -> None:
    engine, session = _session()
    try:
        project = Project(title="准入场景语义", premise="主角陆明。", genre="pulp")
        session.add(project)
        session.flush()
        registrar = EntityRegistrar(session=session)
        planned = registrar.plan_writer_output(
            project_id=project.id,
            chapter_number=8,
            writer_output=WriterOutput(
                project_id=project.id,
                chapter_number=8,
                title="第八章",
                body="陆明进入现场。",
                end_of_chapter_summary="陆明抵达。",
                scene_outputs=[
                    SceneOutput(
                        scene_no=1,
                        scene_objective="抵达现场",
                        text="陆明进入现场。",
                    )
                ],
            ),
        )
        changed = planned.writer_output.model_copy(
            update={
                "scene_outputs": [
                    planned.writer_output.scene_outputs[0].model_copy(
                        update={"text": "陆明离开现场。"}
                    )
                ]
            }
        )

        with pytest.raises(ValueError, match="stale"):
            registrar.verify_writer_output_admission(
                project_id=project.id,
                writer_output=changed,
            )
    finally:
        session.close()
        engine.dispose()
