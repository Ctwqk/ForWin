from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from forwin.book_state import BookStateRepository
from forwin.book_state.extraction.translator import ChapterDeltaTranslator
from forwin.models import Entity, Project
from forwin.models.base import Base
from forwin.naming import EntityRegistrar
from forwin.naming.entity_registrar import LLMEntityAdmissionClassifier
from forwin.protocol import EntitySnapshot, SceneOutput, WriterOutput
from forwin.protocol.book_state import WorldNode
from forwin.review.draft_service import DraftReviewService
from tests.chapter_delta_helpers import chapter_delta, mention


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def _output(project_id: str, chapter_number: int, body: str, summary: str, **delta: Any) -> WriterOutput:
    """WriterOutput whose chapter delta carries ``delta``; admission reads names only from it."""
    return WriterOutput(
        project_id=project_id,
        chapter_number=chapter_number,
        title=f"第{chapter_number}章",
        body=body,
        end_of_chapter_summary=summary,
        chapter_delta=chapter_delta(body, **delta),
    )


def test_draft_review_turns_entity_admission_conflict_into_hard_issue() -> None:
    engine, session = _session()
    try:
        project = Project(title="准入冲突", premise="主角陆明。", genre="pulp")
        session.add(project)
        session.flush()
        planned = EntityRegistrar(session=session).plan_writer_output(
            project_id=project.id,
            chapter_number=3,
            writer_output=_output(
                project.id, 3, "突兀角色进入现场。", "突兀角色出现。",
                entity_mentions=[mention("突兀角色")],
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


def test_legacy_writer_fields_no_longer_drive_admission() -> None:
    class UnexpectedClassifier:
        def classify(self, **_kwargs):
            raise AssertionError("legacy writer fields must not reach admission")

    engine, session = _session()
    try:
        project = Project(title="旧字段", premise="主角陆明。", genre="pulp")
        session.add(project)
        session.flush()
        result = EntityRegistrar(session=session, classifier=UnexpectedClassifier()).plan_writer_output(
            project_id=project.id,
            chapter_number=3,
            # A candidate stored before the chapter delta still carries the writer's fields.
            writer_output=WriterOutput.model_validate({
                "project_id": project.id,
                "chapter_number": 3,
                "title": "第三章",
                "body": "突兀角色进入现场。",
                "end_of_chapter_summary": "突兀角色出现。",
                "state_changes": [{
                    "entity_name": "突兀角色", "entity_kind": "character", "field": "status",
                    "old_value": "", "new_value": "在场", "reason": "出场",
                }],
                "new_events": [{"summary": "突兀角色出场", "involved_entity_names": ["突兀角色"]}],
                "entity_mentions": [{"entity_name": "突兀角色", "entity_kind": "character"}],
            }),
        )

        assert result.plan.decisions == []
        assert result.plan_conflicts == []
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
        output = _output(
            project.id, 4, "不明追踪者切断链路。", "链路中断。",
            entity_mentions=[mention("不明追踪者")],
        )
        result = EntityRegistrar(
            session=session,
            classifier=UnexpectedClassifier(),
        ).plan_writer_output(
            project_id=project.id,
            chapter_number=4,
            writer_output=output,
        )

        assert result.background_generic_names == ["不明追踪者"]
        assert result.plan.decisions[0].action == "background_generic"
        # The translator ignores background refs, so the chapter delta stays as extracted.
        assert result.writer_output.chapter_delta == output.chapter_delta
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
            writer_output=_output(
                project.id, 4, "潮序局关闭了复核窗口。", "复核窗口关闭。",
                entity_mentions=[mention("蔡序")],
            ),
        )

        assert result.background_generic_names == ["蔡序"]
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
        project = Project(title="结构化角色", premise="四人联合复核。", genre="pulp")
        session.add(project)
        session.flush()
        result = EntityRegistrar(
            session=session,
            classifier=classifier,
        ).plan_writer_output(
            project_id=project.id,
            chapter_number=1,
            writer_output=_output(
                project.id, 1,
                "季澈与白铭交出证据，梁祚批准联合窗口，韩溯赶到档案室，严汀得知偏移。",
                "梁祚批准联合追查。",
                entity_mentions=[mention("季澈")],
                entity_changes=[{
                    "new_entity": {"name": "白铭", "kind": "character"},
                    "field": "status", "new_value": "交出证据", "evidence": "白铭交出证据",
                }],
                events=[{
                    "summary": "梁祚批准联合窗口", "participant_refs": ["new:季澈", "new:梁祚"],
                    "significance": "major", "visibility": "public", "evidence": "梁祚批准联合窗口",
                }],
                movements=[{
                    "entity_ref": "new:韩溯", "from_ref": "", "to_ref": "archive", "evidence": "韩溯赶到档案室",
                }],
                knowledge_events=[{
                    "observer_ref": "new:严汀", "new_fact": {
                        "proposition": "偏移成立", "truth_value": True, "sensitivity": "public",
                    }, "state": "knows", "channel": "witnessed", "evidence": "严汀得知偏移",
                }],
            ),
        )

        assert classifier.names == ["季澈", "白铭", "梁祚", "韩溯", "严汀"]
        assert result.registered_names == ["季澈", "白铭", "梁祚", "韩溯", "严汀"]
        assert result.plan_conflicts == []
    finally:
        session.close()
        engine.dispose()


class _CapturingAdmitAll:
    def __init__(self) -> None:
        self.names: list[str] = []

    def classify(self, **kwargs):
        self.names = list(kwargs["names"])
        return [
            {"name": name, "decision": "register_character", "canonical_name": name}
            for name in self.names
        ]


def test_admission_collects_new_refs_from_every_entity_ref_field() -> None:
    classifier = _CapturingAdmitAll()
    engine, session = _session()
    try:
        project = Project(title="引用全集", premise="p", genre="g")
        session.add(project)
        session.flush()
        body = "甲一与乙二结盟，丁四告诉丙三真相，戊五把铜令交给己六，庚七受伤，玉牌落到辛八手里，众人抵达渡口。"
        result = EntityRegistrar(session=session, classifier=classifier).plan_writer_output(
            project_id=project.id,
            chapter_number=1,
            writer_output=_output(
                project.id, 1, body, "众人抵达渡口。",
                relation_changes=[{
                    "source_ref": "new:甲一", "target_ref": "new:乙二", "edge_type": "ally_of",
                    "op": "create", "evidence": "甲一与乙二结盟",
                }],
                knowledge_events=[{
                    "observer_ref": "new:丙三", "new_fact": {
                        "proposition": "真相", "truth_value": True, "sensitivity": "public",
                    }, "state": "knows", "channel": "told", "source_ref": "new:丁四",
                    "evidence": "丁四告诉丙三真相",
                }],
                item_transfers=[
                    {
                        "new_item": {"name": "铜令", "kind": "item"}, "from_ref": "new:戊五",
                        "to_ref": "new:己六", "evidence": "戊五把铜令交给己六",
                    },
                    {
                        "item_ref": "item-ledger", "from_ref": "new:己六", "to_ref": "new:渡口",
                        "evidence": "众人抵达渡口",
                    },
                ],
                entity_changes=[
                    {"ref": "new:庚七", "field": "status", "new_value": "受伤", "evidence": "庚七受伤"},
                    {
                        "new_entity": {"name": "玉牌", "kind": "item"}, "field": "holder_id",
                        "new_value": "new:辛八", "evidence": "玉牌落到辛八手里",
                    },
                ],
                new_sites=[{"name": "渡口", "near_ref": "loc-1", "evidence": "众人抵达渡口"}],
            ),
        )

        assert sorted(classifier.names) == sorted(["甲一", "乙二", "丙三", "丁四", "戊五", "己六", "庚七", "辛八"])
        assert len(classifier.names) == len(set(classifier.names))
        assert result.plan_conflicts == []
    finally:
        session.close()
        engine.dispose()


def test_untyped_mention_kind_does_not_hide_a_new_character_ref() -> None:
    classifier = _CapturingAdmitAll()
    engine, session = _session()
    try:
        project = Project(title="自由类别", premise="p", genre="g")
        session.add(project)
        session.flush()
        EntityRegistrar(session=session, classifier=classifier).plan_writer_output(
            project_id=project.id,
            chapter_number=1,
            writer_output=_output(
                project.id, 1, "陈七递来铜令。", "陈七出现。",
                entity_mentions=[mention("陈七", kind="npc")],
                events=[{
                    "summary": "陈七递来铜令", "participant_refs": ["new:陈七"], "significance": "minor",
                    "visibility": "public", "evidence": "陈七递来铜令",
                }],
            ),
        )

        assert classifier.names == ["陈七"]
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
            writer_output=_output(
                project.id, 1, "季澈将样本-17转入证据链。", "样本-17完成复核。",
                entity_mentions=[mention("季澈")],
                entity_changes=[{
                    "new_entity": {"name": "样本-17", "kind": "item"},
                    "field": "status", "new_value": "已复核", "evidence": "样本-17转入证据链",
                }],
                events=[{
                    "summary": "季澈完成样本复核", "participant_refs": ["new:季澈", "new:样本-17"],
                    "significance": "minor", "visibility": "public", "evidence": "季澈将样本-17转入证据链",
                }],
            ),
        )

        assert classifier.names == ["季澈"]
        assert result.writer_output.chapter_delta.events[0].participant_refs == ["new:季澈", "new:样本-17"]
    finally:
        session.close()
        engine.dispose()


def test_admission_reuses_known_non_character_canon_entity() -> None:
    class UnexpectedClassifier:
        def classify(self, **_kwargs):
            raise AssertionError("known Canon entity must not reach character admission")

    engine, session = _session()
    try:
        project = Project(title="既有规则", premise="R11 是归档规则。", genre="pulp")
        session.add(project)
        session.flush()
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(
                id="rule-r11",
                project_id=project.id,
                node_type="rule",
                name="R11",
                state={"status": "active"},
            ),
            version_chapter=2,
            version_state={"status": "active"},
        )

        result = EntityRegistrar(
            session=session,
            classifier=UnexpectedClassifier(),
        ).plan_writer_output(
            project_id=project.id,
            chapter_number=3,
            writer_output=_output(
                project.id, 3, "R11 改变了归档路径。", "R11 的路径发生变化。",
                entity_mentions=[mention("R11")],
                entity_changes=[{
                    "ref": "rule-r11", "field": "status", "new_value": "rerouted",
                    "evidence": "R11 改变了归档路径",
                }],
            ),
        )

        assert result.plan.decisions == []
        assert result.plan_conflicts == []
        assert DraftReviewService._entity_admission_issues(result.writer_output) == []
        translation = ChapterDeltaTranslator(session).translate(
            project_id=project.id,
            chapter_number=3,
            extraction=result.writer_output.chapter_delta,
            admission_plan=result.plan,
            chapter_summary=result.writer_output.end_of_chapter_summary,
        )
        assert translation.issues == ()
        assert any(
            patch.node_id == "rule-r11"
            and patch.field_path == "state.status"
            and patch.new_value == "rerouted"
            for patch in translation.delta.node_patches
        )
        assert not any(
            patch.op == "create" and patch.node_type == "character"
            for patch in translation.delta.node_patches
        )
    finally:
        session.close()
        engine.dispose()


def test_admission_keeps_generic_refs_on_structured_character_surfaces_as_background() -> None:
    class UnexpectedClassifier:
        def classify(self, **_kwargs):
            raise AssertionError("generic refs must not reach the LLM classifier")

    engine, session = _session()
    try:
        project = Project(title="结构泛称", premise="系统发出告警。", genre="pulp")
        session.add(project)
        session.flush()
        output = _output(
            project.id, 1, "系统提示不明追踪者正在覆盖记录。", "覆盖请求被拦截。",
            entity_changes=[{
                "new_entity": {"name": "不明追踪者", "kind": "character"},
                "field": "status", "new_value": "正在覆盖记录", "evidence": "不明追踪者正在覆盖记录",
            }],
            events=[{
                "summary": "不明追踪者发起覆盖请求", "participant_refs": ["new:不明追踪者"],
                "significance": "minor", "visibility": "public", "evidence": "不明追踪者正在覆盖记录",
            }],
        )
        result = EntityRegistrar(
            session=session,
            classifier=UnexpectedClassifier(),
        ).plan_writer_output(
            project_id=project.id,
            chapter_number=1,
            writer_output=output,
        )

        assert result.background_generic_names == ["不明追踪者"]
        assert result.writer_output.chapter_delta == output.chapter_delta
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
            writer_output=_output(
                project.id, 4, "蔡序在窗口后核对签名。", "蔡序完成核对。",
                entity_mentions=[mention("蔡序")],
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


def test_llm_classifier_repairs_schema_invalid_json_once() -> None:
    class SequencedClient:
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.responses = [
                json.dumps(
                    {
                        "entities": [
                            {
                                "name": "蔡序",
                                "decision": "register_character",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
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
                ),
            ]

        def chat(self, messages, **kwargs):
            self.calls.append({"messages": messages, **kwargs})
            return self.responses[len(self.calls) - 1]

    client = SequencedClient()
    decisions = LLMEntityAdmissionClassifier(client).classify(
        project_id="project-1",
        chapter_number=1,
        names=["蔡序"],
        writer_output=WriterOutput(
            project_id="project-1",
            chapter_number=1,
            title="第一章",
            body="蔡序在复核窗口后核对签名。",
            end_of_chapter_summary="签名完成核对。",
        ),
        existing_entities=[],
    )

    assert decisions[0]["name"] == "蔡序"
    assert len(client.calls) == 2
    assert client.calls[0]["output_schema"]["required"] == ["decisions"]
    assert client.calls[0]["stage_key"] == "entity_registrar"
    assert client.calls[1]["stage_key"] == "entity_registrar_json_repair"
    assert "entities" in client.calls[1]["messages"][-1]["content"]


def test_llm_classifier_repairs_unresolvable_alias_target() -> None:
    class SequencedClient:
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.responses = [
                json.dumps(
                    {
                        "decisions": [
                            {
                                "name": "刀疤男",
                                "decision": "register_alias",
                                "entity_id": "missing-entity",
                                "aliases": ["刀疤男"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decisions": [
                            {
                                "name": "刀疤男",
                                "decision": "background_generic",
                                "reason": "generic descriptive reference",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
            ]

        def chat(self, messages, **kwargs):
            self.calls.append({"messages": messages, **kwargs})
            return self.responses[len(self.calls) - 1]

    client = SequencedClient()
    decisions = LLMEntityAdmissionClassifier(client).classify(
        project_id="project-1",
        chapter_number=15,
        names=["刀疤男"],
        writer_output=WriterOutput(
            project_id="project-1",
            chapter_number=15,
            title="第十五章",
            body="刀疤男递来一份地图。",
            end_of_chapter_summary="主角取得地图。",
        ),
        existing_entities=[
            EntitySnapshot(
                entity_id="known-entity",
                kind="character",
                name="岑昀行",
                aliases=[],
                description="既有角色",
                current_state={},
            )
        ],
    )

    assert len(client.calls) == 2
    assert decisions[0]["decision"] == "background_generic"
    assert "missing-entity" in client.calls[1]["messages"][-1]["content"]


def test_llm_classifier_scopes_exhausted_coverage_failure_to_omitted_name() -> None:
    class PartialClient:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def chat(self, messages, **kwargs):
            self.calls.append({"messages": messages, **kwargs})
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

    client = PartialClient()
    decisions = LLMEntityAdmissionClassifier(client).classify(
        project_id="project-1",
        chapter_number=1,
        names=["蔡序", "遮蔽名_"],
        writer_output=WriterOutput(
            project_id="project-1",
            chapter_number=1,
            title="第一章",
            body="蔡序看见档案上只剩遮蔽名_。",
            end_of_chapter_summary="档案中的名字遭到遮蔽。",
        ),
        existing_entities=[],
    )

    assert len(client.calls) == 2
    assert decisions == [
        {
            "name": "蔡序",
            "decision": "register_character",
            "canonical_name": "蔡序",
            "aliases": [],
            "role_hint": "复核员",
        }
    ]


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
            writer_output=_output(
                project.id, 5, "突兀角色进入现场。", "突兀角色出现。",
                entity_mentions=[mention("突兀角色")],
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
