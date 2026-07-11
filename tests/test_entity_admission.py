from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from forwin.models import Entity, Project
from forwin.models.base import Base
from forwin.naming import EntityRegistrar
from forwin.protocol import EntityMention, WriterOutput
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
