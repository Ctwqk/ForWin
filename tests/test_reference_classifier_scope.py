from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.book_state.writer_contract import WriterContractDeltaBuilder
from forwin.checker.reference_classifier import (
    LANGUAGE_GENERIC_CHARACTER_REFERENCES,
    classify_reference,
    looks_like_generic_character_reference,
    looks_like_non_character_reference,
    reference_rule_catalog,
)
from forwin.models import Project
from forwin.models.base import Base
from forwin.naming import EntityRegistrar
from forwin.naming.entity_registrar import LLMEntityAdmissionClassifier
from forwin.protocol import EntityMention, WriterOutput
from forwin.protocol.state_change import EventCandidate


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def test_global_reference_short_circuit_is_language_only() -> None:
    for name in ("路人", "陆明的手下", "遗体", "不明追踪者"):
        assert looks_like_generic_character_reference(name) is True
        assert classify_reference(name).scope == "language_generic"

    candidates = {
        "馆员": "project",
        "基金会代理人": "genre_candidate",
        "无脸人": "project",
        "AI-7": "genre_candidate",
        "锚点037": "project",
        "003号分割体": "project",
        "若槐宗邦": "project",
        "张三-已故": "genre_candidate",
    }
    for name, scope in candidates.items():
        assert looks_like_generic_character_reference(name) is False
        assert looks_like_non_character_reference(name) is False
        assert classify_reference(name).scope == scope

    assert "馆员" not in LANGUAGE_GENERIC_CHARACTER_REFERENCES
    catalog = reference_rule_catalog()
    assert {item.scope for item in catalog} == {"global", "genre_candidate"}
    assert all("story" not in item.rule_key for item in catalog)


def test_story_scar_terms_and_dead_normalizer_are_absent_from_production() -> None:
    root = Path(__file__).parents[1]
    classifier_source = (
        root / "forwin/checker/reference_classifier.py"
    ).read_text(encoding="utf-8")
    for term in (
        "馆员",
        "无脸人",
        "权限买家",
        "记忆馆",
        "旧港",
        "宗邦",
        "档案署",
        "镜像审计员",
        "分割体",
        "锚点",
        "蘅照夜",
    ):
        assert term not in classifier_source
    review_autofix_source = (
        root / "forwin/generation/pipeline_core/review_autofix.py"
    ).read_text(encoding="utf-8")
    assert "_project_character_names" not in review_autofix_source
    assert "normalize_character_reference" not in classifier_source


def test_genre_and_story_candidates_reach_project_entity_classifier() -> None:
    class CapturingClassifier:
        def __init__(self) -> None:
            self.names: list[str] = []

        def classify(self, *, names: list[str], **_kwargs):
            self.names = list(names)
            return [
                {
                    "name": name,
                    "decision": "background_generic",
                    "reason": "project-scoped decision",
                }
                for name in names
            ]

    engine, session = _session()
    try:
        project = Project(title="候选准入", premise="p", genre="g")
        session.add(project)
        session.flush()
        names = ["馆员", "基金会代理人", "AI-7", "锚点037"]
        classifier = CapturingClassifier()

        EntityRegistrar(session=session, classifier=classifier).plan_writer_output(
            project_id=project.id,
            chapter_number=1,
            writer_output=WriterOutput(
                project_id=project.id,
                chapter_number=1,
                title="第一章",
                body="馆员与基金会代理人检查 AI-7 和锚点037。",
                end_of_chapter_summary="馆员完成检查。",
                entity_mentions=[
                    EntityMention(
                        entity_name=name,
                        entity_kind="character",
                        is_named=True,
                    )
                    for name in names
                ],
            ),
        )

        assert classifier.names == names
    finally:
        session.close()
        engine.dispose()


def test_llm_classifier_receives_nonbinding_genre_candidate_features() -> None:
    class CapturingClient:
        def __init__(self) -> None:
            self.messages: list[dict] = []

        def chat(self, messages, **_kwargs):
            self.messages = messages
            return json.dumps(
                {
                    "decisions": [
                        {
                            "name": "AI-7",
                            "decision": "background_generic",
                        }
                    ]
                }
            )

    client = CapturingClient()
    LLMEntityAdmissionClassifier(client).classify(
        project_id="project-1",
        chapter_number=1,
        names=["AI-7", "馆员"],
        writer_output=WriterOutput(
            project_id="project-1",
            chapter_number=1,
            title="第一章",
            body="AI-7 与馆员进入现场。",
            end_of_chapter_summary="两者进入现场。",
        ),
        existing_entities=[],
    )

    payload = json.loads(client.messages[1]["content"])
    assert payload["reference_candidates"] == [
        {
            "name": "AI-7",
            "scope": "genre_candidate",
            "features": ["technical_identifier"],
        }
    ]


def test_story_candidate_without_classifier_fails_closed() -> None:
    engine, session = _session()
    try:
        project = Project(title="无分类器", premise="p", genre="g")
        session.add(project)
        session.flush()

        result = EntityRegistrar(session=session).plan_writer_output(
            project_id=project.id,
            chapter_number=1,
            writer_output=WriterOutput(
                project_id=project.id,
                chapter_number=1,
                title="第一章",
                body="基金会代理人进入现场。",
                end_of_chapter_summary="基金会代理人出现。",
                entity_mentions=[
                    EntityMention(
                        entity_name="基金会代理人",
                        entity_kind="character",
                        is_named=True,
                    )
                ],
            ),
        )

        assert result.background_generic_names == []
        assert result.plan_conflicts == ["基金会代理人"]
    finally:
        session.close()
        engine.dispose()


def test_writer_contract_rejects_unadmitted_genre_candidate() -> None:
    engine, session = _session()
    try:
        project = Project(title="事件候选", premise="p", genre="g")
        session.add(project)
        session.flush()
        output = WriterOutput(
            project_id=project.id,
            chapter_number=1,
            title="第一章",
            body="AI-7 覆盖记录。",
            end_of_chapter_summary="记录被覆盖。",
            new_events=[
                EventCandidate(
                    summary="AI-7 覆盖记录",
                    significance="major",
                    involved_entity_names=["AI-7"],
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
    finally:
        session.close()
        engine.dispose()
