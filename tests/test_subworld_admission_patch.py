from __future__ import annotations

import json
from types import SimpleNamespace

from forwin.models.project import ChapterPlan
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.protocol.review import ContinuityIssue, RepairInstruction
from forwin.protocol.subworld import EntityMention
from forwin.protocol.writer import WriterOutput
from forwin.subworld.admission_patch import apply_subworld_admission_patch


def _chapter_plan() -> ChapterPlan:
    return ChapterPlan(
        id="plan-1",
        project_id="project-1",
        arc_plan_id="arc-1",
        chapter_number=89,
        title="裂隙前夜",
        one_line="陆明确认午夜侧门计划。",
        goals_json=json.dumps(["赵衍必须交付旧港密钥"], ensure_ascii=False),
        task_contract_json=json.dumps(["让赵衍成为侧门行动的临时同盟"], ensure_ascii=False),
        experience_plan_json="{}",
    )


def _writer_output(name: str) -> WriterOutput:
    return WriterOutput(
        chapter_number=89,
        title="裂隙前夜",
        body=f"{name}把密钥交给陆明。",
        end_of_chapter_summary=f"{name}参与了午夜侧门计划。",
        entity_mentions=[
            EntityMention(
                entity_name=name,
                entity_kind="character",
                is_named=True,
                is_on_stage=True,
                evidence_refs=[f"body:{name}"],
            )
        ],
    )


def _instruction(name: str) -> RepairInstruction:
    return RepairInstruction(
        repair_scope="subworld",
        failure_type="mixed",
        must_fix=[f"命名角色「{name}」未准入"],
        must_preserve=["裂隙前夜"],
        design_patch={
            "subworld_admission_issue": ContinuityIssue(
                rule_name="sub_world_unknown_named_entity",
                issue_type="subworld_admission_unauthorized_new_entity",
                severity="error",
                description=f"命名角色「{name}」未准入。",
                entity_names=[name],
                evidence_refs=[f"body:{name}"],
            ).model_dump(mode="json")
        },
    )


def test_register_patch_adds_chapter_entry_target_without_writer_rewrite() -> None:
    plan = _chapter_plan()

    result = apply_subworld_admission_patch(
        session=None,
        project_id="project-1",
        chapter_plan=plan,
        writer_output=_writer_output("赵衍"),
        repair_instruction=_instruction("赵衍"),
        current_plan=ChapterExperiencePlan(),
        active_subworld_ids=["global-core"],
        context=SimpleNamespace(chapter_experience_plan=ChapterExperiencePlan()),
        protected_names=set(),
    )

    assert result.failure_reason == ""
    assert result.decision.action == "register_entity"
    assert result.requires_writer_rewrite is False
    stored = ChapterExperiencePlan.model_validate(json.loads(plan.experience_plan_json))
    assert [target.entity_name for target in stored.chapter_entry_targets] == ["赵衍"]
    assert stored.chapter_entry_targets[0].subworld_id == "global-core"


def test_background_reference_patch_requires_operator_decision() -> None:
    plan = _chapter_plan()
    plan.goals_json = "[]"
    plan.task_contract_json = "[]"

    result = apply_subworld_admission_patch(
        session=None,
        project_id="project-1",
        chapter_plan=plan,
        writer_output=WriterOutput(
            chapter_number=89,
            title="地下分馆",
            body="老孙站在地下分馆门口，没有留下真名。",
            end_of_chapter_summary="地下分馆门口的馆员提供了线索。",
            entity_mentions=[
                EntityMention(
                    entity_name="老孙",
                    entity_kind="character",
                    is_named=True,
                    is_on_stage=True,
                    evidence_refs=["body:老孙"],
                )
            ],
        ),
        repair_instruction=_instruction("老孙"),
        current_plan=ChapterExperiencePlan(),
        active_subworld_ids=[],
        context=SimpleNamespace(chapter_experience_plan=ChapterExperiencePlan()),
        protected_names=set(),
    )

    assert result.failure_reason == "unplanned stateful named entity requires operator choice"
    assert result.decision.action == "manual_review_required"
    assert result.requires_writer_rewrite is False
    assert result.design_patch["subworld_admission_action"] == "manual_review_required"
