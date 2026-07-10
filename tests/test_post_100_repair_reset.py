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


def test_entity_registrar_persists_registration_alias_background_and_conflict_decisions() -> None:
    from forwin.naming.entity_registrar import EntityRegistrar

    class FakeClassifier:
        def classify(self, *, names, **_kwargs):
            assert names == ["猎锚者X（远程声音）", "灰鹞", "网络中介人", "突兀主角"]
            return [
                {
                    "decision": "register_character",
                    "name": "猎锚者X（远程声音）",
                    "canonical_name": "猎锚者X",
                    "aliases": ["猎锚者X（远程声音）"],
                    "role_hint": "远程声音",
                    "gender": "unknown",
                },
                {
                    "decision": "register_alias",
                    "name": "灰鹞",
                    "entity_id": "chen-zhaoning",
                    "aliases": ["灰鹞"],
                    "role_hint": "行动代号",
                },
                {
                    "decision": "background_generic",
                    "name": "网络中介人",
                    "reason": "背景群体，不是命名角色",
                },
                {
                    "decision": "plan_conflict",
                    "name": "突兀主角",
                    "reason": "与本章计划冲突",
                },
            ]

    session = _session()
    try:
        project = Project(title="注册流", premise="主角陆明。", genre="pulp", setting_summary="s")
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
            body="猎锚者X（远程声音）提醒陆明，灰鹞截断了网络中介人的链路，突兀主角却突然出现。",
            end_of_chapter_summary="陆明听见猎锚者X的远程声音。",
            entity_mentions=[
                EntityMention(entity_name="猎锚者X（远程声音）", entity_kind="character", is_named=True),
                EntityMention(entity_name="灰鹞", entity_kind="character", is_named=True),
                EntityMention(entity_name="网络中介人", entity_kind="character", is_named=True),
                EntityMention(entity_name="突兀主角", entity_kind="character", is_named=True),
            ],
        )

        result = EntityRegistrar(session=session, classifier=FakeClassifier()).register_writer_output(
            project_id=project.id,
            chapter_number=6,
            writer_output=output,
        )
        session.commit()

        repo = StateRepository(session)
        resolved = repo.get_entities_by_names(project.id, ["猎锚者X（远程声音）", "灰鹞"])
        events = session.execute(
            select(DecisionEvent).where(DecisionEvent.project_id == project.id)
        ).scalars().all()
        aliases = session.execute(
            select(EntityAlias.alias).where(EntityAlias.project_id == project.id)
        ).scalars().all()

        assert result.plan_conflicts == ["突兀主角"]
        assert resolved["猎锚者X（远程声音）"].name == "猎锚者X"
        assert resolved["灰鹞"].id == "chen-zhaoning"
        assert "网络中介人" not in repo.get_entities_by_names(project.id, ["网络中介人"])
        assert {"猎锚者X（远程声音）", "灰鹞"}.issubset(set(aliases))
        assert {
            "entity_registered",
            "entity_alias_registered",
            "entity_background_generic",
            "entity_plan_conflict",
        }.issubset({event.event_type for event in events})
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


def test_subworld_string_genericization_is_removed_from_production_paths() -> None:
    root = Path(__file__).resolve().parents[1]
    production_files = [
        root / "forwin" / "subworld" / "admission_policy.py",
        root / "forwin" / "subworld" / "admission_patch.py",
        root / "forwin" / "orchestrator_loop_core" / "repair_loop.py",
        root / "forwin" / "orchestrator_loop_core" / "subworld_admission_repair.py",
        root / "forwin" / "ui_assets" / "home" / "app_task_progress.js",
    ]
    forbidden = {
        "_apply_subworld_admission_autofix",
        "_generic_subworld_reference",
        "genericize_background_reference",
        "subworld_admission_replacements",
        "subworld_admission_autofix",
    }

    for path in production_files:
        text = path.read_text(encoding="utf-8")
        assert not any(marker in text for marker in forbidden), path


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
                        rule_name="sub_world_unknown_named_entity",
                        issue_type="subworld_admission",
                        severity="error",
                        description="命名角色未注册。",
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
