from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from forwin.models import DecisionEvent, Entity, EntityAlias, Project
from forwin.models.base import Base
from forwin.models.draft import ChapterDraft
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.protocol import EntityMention, WriterOutput
from forwin.state.repo import StateRepository


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_entity_registrar_builds_four_outcome_plan_without_mutating_entities() -> None:
    from forwin.naming import EntityRegistrar

    class FakeClassifier:
        def classify(self, *, names, **_kwargs):
            assert names == ["猎锚者X（远程声音）", "灰鹞", "宁网", "突兀主角"]
            return [
                {
                    "decision": "register_character",
                    "name": "猎锚者X（远程声音）",
                    "canonical_name": "猎锚者X",
                    "aliases": ["猎锚者X（远程声音）"],
                    "role_hint": "远程声音",
                },
                {
                    "decision": "register_alias",
                    "name": "灰鹞",
                    "entity_id": "chen-zhaoning",
                    "aliases": ["灰鹞"],
                },
                {
                    "decision": "background_generic",
                    "name": "宁网",
                    "reason": "背景泛指",
                },
                {
                    "decision": "plan_conflict",
                    "name": "突兀主角",
                    "reason": "与本章计划冲突",
                },
            ]

    session = _session()
    try:
        project = Project(title="注册流", premise="主角陆明。", genre="pulp")
        session.add(project)
        session.flush()
        session.add(
            Entity(
                id="chen-zhaoning",
                project_id=project.id,
                kind="character",
                name="陈昭宁",
                aliases_json="[]",
                description="既有角色",
                created_at_chapter=1,
                is_active=True,
            )
        )
        session.flush()
        output = WriterOutput(
            project_id=project.id,
            chapter_number=6,
            title="第6章",
            body="猎锚者X提醒陆明，灰鹞截断了宁网的链路，突兀主角却突然出现。",
            end_of_chapter_summary="陆明听见猎锚者X的远程声音。",
            entity_mentions=[
                EntityMention(entity_name="猎锚者X（远程声音）", entity_kind="character"),
                EntityMention(entity_name="灰鹞", entity_kind="character"),
                EntityMention(entity_name="宁网", entity_kind="character"),
                EntityMention(entity_name="突兀主角", entity_kind="character"),
            ],
        )

        result = EntityRegistrar(
            session=session,
            classifier=FakeClassifier(),
        ).plan_writer_output(
            project_id=project.id,
            chapter_number=6,
            writer_output=output,
        )

        assert result.plan_conflicts == ["突兀主角"]
        assert {decision.action for decision in result.plan.decisions} == {
            "register_character",
            "register_alias",
            "background_generic",
            "plan_conflict",
        }
        assert [entity.name for entity in session.execute(select(Entity)).scalars()] == ["陈昭宁"]
        assert session.execute(select(EntityAlias)).scalars().all() == []
        assert session.execute(select(DecisionEvent)).scalars().all() == []
    finally:
        session.close()


def test_entity_admission_plan_applies_only_at_canon_boundary() -> None:
    from forwin.canon import EntityAdmissionCommitter
    from forwin.naming import EntityRegistrar

    class FakeClassifier:
        def classify(self, *, names, **_kwargs):
            assert names == ["猎锚者X（远程声音）", "灰鹞"]
            return [
                {
                    "decision": "register_character",
                    "name": "猎锚者X（远程声音）",
                    "canonical_name": "猎锚者X",
                    "aliases": ["猎锚者X（远程声音）"],
                },
                {
                    "decision": "register_alias",
                    "name": "灰鹞",
                    "entity_id": "chen-zhaoning",
                    "aliases": ["灰鹞"],
                },
            ]

    session = _session()
    try:
        project = Project(title="Canon 注册", premise="主角陆明。", genre="pulp")
        session.add(project)
        session.flush()
        session.add(
            Entity(
                id="chen-zhaoning",
                project_id=project.id,
                kind="character",
                name="陈昭宁",
                aliases_json="[]",
                description="既有角色",
                created_at_chapter=1,
                is_active=True,
            )
        )
        session.flush()
        registrar = EntityRegistrar(session=session, classifier=FakeClassifier())
        result = registrar.plan_writer_output(
            project_id=project.id,
            chapter_number=6,
            writer_output=WriterOutput(
                project_id=project.id,
                chapter_number=6,
                title="第6章",
                body="猎锚者X提醒灰鹞切断链路。",
                end_of_chapter_summary="远程协作建立。",
                entity_mentions=[
                    EntityMention(entity_name="猎锚者X（远程声音）", entity_kind="character"),
                    EntityMention(entity_name="灰鹞", entity_kind="character"),
                ],
            ),
        )

        assert [entity.name for entity in session.execute(select(Entity)).scalars()] == ["陈昭宁"]
        verified_plan = registrar.verify_writer_output_admission(
            project_id=project.id,
            writer_output=result.writer_output,
        )
        EntityAdmissionCommitter(session).apply(
            project_id=project.id,
            plan=verified_plan,
        )
        session.flush()

        repo = StateRepository(session)
        resolved = repo.get_entities_by_names(
            project.id,
            ["猎锚者X（远程声音）", "灰鹞"],
        )
        events = session.execute(
            select(DecisionEvent).where(DecisionEvent.project_id == project.id)
        ).scalars().all()
        assert resolved["猎锚者X（远程声音）"].name == "猎锚者X"
        assert resolved["灰鹞"].id == "chen-zhaoning"
        assert {event.event_type for event in events} >= {
            "entity_registered",
            "entity_alias_registered",
        }
    finally:
        session.close()


def test_entity_registrar_classifier_failure_is_recorded_as_plan_conflict() -> None:
    from forwin.naming.entity_registrar import EntityRegistrar

    class FailingClassifier:
        def classify(self, **_kwargs):
            raise RuntimeError("classifier unavailable")

    session = _session()
    try:
        project = Project(title="注册失败", premise="主角陆明。", genre="pulp")
        session.add(project)
        session.flush()
        output = WriterOutput(
            project_id=project.id,
            chapter_number=2,
            title="第2章",
            body="突兀角色进入现场。",
            end_of_chapter_summary="突兀角色出现。",
            entity_mentions=[
                EntityMention(
                    entity_name="突兀角色",
                    entity_kind="character",
                    is_named=True,
                )
            ],
        )

        result = EntityRegistrar(
            session=session,
            classifier=FailingClassifier(),
        ).plan_writer_output(
            project_id=project.id,
            chapter_number=2,
            writer_output=output,
        )

        assert result.plan_conflicts == ["突兀角色"]
        assert result.plan.plan_conflicts == ["突兀角色"]
        assert result.plan.decisions[0].reason == "classifier_error:RuntimeError"
        assert session.execute(select(Entity)).scalars().all() == []
    finally:
        session.close()


def test_subworld_string_genericization_autofix_is_removed_from_orchestrator_boundary() -> None:
    forbidden = {
        "_apply_subworld_admission_autofix",
        "_generic_subworld_reference",
        "_subworld_role_titles",
        "_looks_like_genericizable_unknown_reference",
        "_placeholder_role_replacement",
    }

    assert not any(hasattr(WritingOrchestrator, name) for name in forbidden)


def test_legacy_subworld_admission_modules_and_tokens_are_removed() -> None:
    root = Path(__file__).resolve().parents[1]
    deleted_files = [
        root / "forwin" / "subworld" / "admission_policy.py",
        root / "forwin" / "subworld" / "admission_patch.py",
        root / "forwin" / "orchestrator_loop_core" / "subworld_admission_repair.py",
        root / "forwin" / "planning" / "subworld_admission.py",
        root / "forwin" / "review" / "repair_handlers" / "subworld.py",
    ]
    forbidden = {
        "_apply_subworld_admission_autofix",
        "_generic_subworld_reference",
        "genericize_background_reference",
        "subworld_admission_replacements",
        "subworld_admission_autofix",
        "subworld_admission_patch",
        "sub_world_unknown_named_entity",
    }

    assert not any(path.exists() for path in deleted_files)
    production_files = [
        path
        for path in (root / "forwin").rglob("*")
        if path.is_file() and path.suffix in {".py", ".js"}
    ]
    for path in production_files:
        text = path.read_text(encoding="utf-8")
        assert not any(marker in text for marker in forbidden), path

    registrar_source = (root / "forwin" / "naming" / "entity_registrar.py").read_text()
    canon_source = (root / "forwin" / "canon" / "admission.py").read_text()
    checker_source = (root / "forwin" / "checker" / "rules.py").read_text()
    assert "self.session.add(" not in registrar_source
    assert "def _check_subworld_admission" not in checker_source
    verify_index = canon_source.index("verify_writer_output_admission")
    book_state_index = canon_source.index("_commit_book_state_canon")
    apply_index = canon_source.index("EntityAdmissionCommitter(session).apply")
    assert verify_index < book_state_index < apply_index


def test_allowed_entity_names_do_not_scrape_recent_accepted_summaries() -> None:
    session = _session()
    try:
        project = Project(title="摘要不准入", premise="主角陆明。", genre="pulp", setting_summary="s")
        session.add(project)
        session.flush()
        arc = ArcPlanVersion(project_id=project.id, arc_synopsis="arc", chapter_start=1, chapter_end=10)
        session.add(arc)
        session.flush()
        chapter_5 = ChapterPlan(
            project_id=project.id,
            arc_plan_id=arc.id,
            chapter_number=5,
            title="第5章",
            one_line="陆明听见猎锚者X的远程声音。",
            status="accepted",
        )
        session.add(chapter_5)
        session.flush()
        session.add(
            ChapterDraft(
                chapter_plan_id=chapter_5.id,
                version=1,
                body_text="正文",
                summary="猎锚者X要求陆明记住灰鹞。",
                char_count=2,
            )
        )
        session.flush()

        # If this test ever starts depending on ChapterPlan/ChapterDraft summary text,
        # the old alias-hotfix path has crept back in.
        repo = StateRepository(session)
        assert "猎锚者X" not in repo.get_allowed_entity_names(project.id, 6)
    finally:
        session.close()


def test_repair_v2_no_longer_has_subworld_repair_scope() -> None:
    from forwin.protocol.review import ContinuityIssue, ReviewVerdict
    from forwin.review.decision.rules.repair_v2 import (
        MAX_ATTEMPTS_PER_SCOPE,
        _SCOPE_TO_OUTCOME,
        decide_repair_v2,
    )
    from forwin.review.decision.types import DecisionInput, PlanLayerHealth

    decision = decide_repair_v2(
        DecisionInput(
            project_id="project-1",
            chapter_number=8,
            review=ReviewVerdict(
                verdict="fail",
                issues=[
                    ContinuityIssue(
                        rule_name="entity_admission_plan_conflict",
                        issue_type="entity_admission_plan_conflict",
                        severity="error",
                        description="EntityRegistrar 无法完成命名角色注册。",
                        evidence_refs=["chapter=8", "entity=灰鹞"],
                    )
                ],
            ),
            signals=[],
            open_obligations=[],
            attempts_completed=0,
            prior_scope_history=[],
            budget=None,
            target_total_chapters=200,
            plan_layer_health=PlanLayerHealth(),
        )
    )

    assert "subworld" not in _SCOPE_TO_OUTCOME
    assert "subworld" not in MAX_ATTEMPTS_PER_SCOPE
    assert decision.outcome == "manual_review"
    assert decision.sub_action["scope"] == "operator"


def test_band_plan_entry_targets_do_not_infer_names_from_plan_text() -> None:
    from forwin.planning.band_plan_service import _chapter_entry_targets_for_plan

    plan = ChapterPlan(
        project_id="project-1",
        arc_plan_id="arc-1",
        chapter_number=8,
        title="灰鹞的条件",
        one_line="陆明发现馆员陈潮白持有锚点。",
        goals_json='["引入猎锚者X作为远程压力"]',
    )
    schedule = type(
        "Schedule",
        (),
        {
            "chapter_entry_targets": [],
            "active_subworld_ids": ["global-core"],
        },
    )()

    assert _chapter_entry_targets_for_plan(schedule=schedule, plan=plan) == []
