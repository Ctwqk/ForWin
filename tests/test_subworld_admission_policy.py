from __future__ import annotations

from forwin.protocol.experience import ChapterExperiencePlan
from forwin.protocol.review import ContinuityIssue
from forwin.protocol.state_change import EventCandidate
from forwin.protocol.subworld import EntityMention
from forwin.protocol.writer import WriterOutput
from forwin.subworld.admission_policy import SubworldAdmissionPolicy


def _issue(name: str) -> ContinuityIssue:
    return ContinuityIssue(
        rule_name="sub_world_unknown_named_entity",
        issue_type="subworld_admission_unauthorized_new_entity",
        severity="error",
        description=f"命名角色「{name}」未在当前 chapter 的 subworld 准入名单中。",
        entity_names=[name],
        evidence_refs=[f"body:{name}"],
    )


def _output(name: str, *, event_role: str = "") -> WriterOutput:
    return WriterOutput(
        chapter_number=89,
        title="裂隙前夜",
        body=f"{name}把密钥交给陆明，并承诺在午夜前打开侧门。",
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
        new_events=[
            EventCandidate(
                summary=f"{name}交出密钥",
                significance="major",
                involved_entity_names=["陆明", name],
                roles=["protagonist", event_role or "key_holder"],
            )
        ],
    )


def test_policy_registers_planned_or_stateful_entity() -> None:
    decision = SubworldAdmissionPolicy().classify(
        issue=_issue("赵衍"),
        writer_output=_output("赵衍"),
        chapter_goals=["赵衍必须交付旧港密钥"],
        chapter_task_contract=["让赵衍成为侧门行动的临时同盟"],
        chapter_experience_plan=ChapterExperiencePlan(),
        existing_entities=[],
        book_state_snapshot={},
    )

    assert decision.action == "register_entity"
    assert decision.entity_name == "赵衍"
    assert decision.entity_kind == "character"
    assert decision.evidence_refs == ["body:赵衍"]


def test_policy_genericizes_background_reference() -> None:
    decision = SubworldAdmissionPolicy().classify(
        issue=_issue("老孙"),
        writer_output=WriterOutput(
            chapter_number=89,
            title="地下分馆",
            body="老孙站在地下分馆门口，没有留下真名，只说馆内已经熄灯。",
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
        chapter_goals=[],
        chapter_task_contract=[],
        chapter_experience_plan=ChapterExperiencePlan(),
        existing_entities=[],
        book_state_snapshot={},
    )

    assert decision.action == "genericize_background_reference"
    assert decision.entity_name == "老孙"
    assert decision.replacement in {"馆员", "集团高管"}


def test_policy_genericizes_non_cast_corpse_reference_even_with_state_events() -> None:
    decision = SubworldAdmissionPolicy().classify(
        issue=_issue("尸体"),
        writer_output=WriterOutput(
            chapter_number=89,
            title="冷却层密室",
            body="尸体在冷却层密室，许澄从瞳孔反光里提取坐标。",
            end_of_chapter_summary="许澄提取坐标。",
            entity_mentions=[
                EntityMention(
                    entity_name="尸体",
                    entity_kind="character",
                    is_named=True,
                    is_on_stage=True,
                    evidence_refs=["body:尸体"],
                )
            ],
            new_events=[
                EventCandidate(
                    summary="许澄提取坐标",
                    significance="major",
                    involved_entity_names=["许澄", "尸体"],
                    roles=["protagonist", "evidence"],
                )
            ],
        ),
        chapter_goals=[],
        chapter_task_contract=[],
        chapter_experience_plan=ChapterExperiencePlan(),
        existing_entities=[],
        book_state_snapshot={},
    )

    assert decision.action == "genericize_background_reference"
    assert decision.entity_name == "尸体"
    assert decision.replacement == "遗体"


def test_policy_genericizes_role_and_status_labels_even_with_state_events() -> None:
    for entity_name, replacement in [
        ("Ω级权限买家", "匿名买家"),
        ("馆员-活跃", "状态记录"),
    ]:
        decision = SubworldAdmissionPolicy().classify(
            issue=_issue(entity_name),
            writer_output=WriterOutput(
                chapter_number=98,
                title="黑市账本",
                body=f"账本里出现{entity_name}，许澄将它标记为异常交易线索。",
                end_of_chapter_summary=f"许澄记录{entity_name}。",
                entity_mentions=[
                    EntityMention(
                        entity_name=entity_name,
                        entity_kind="character",
                        is_named=True,
                        is_on_stage=True,
                        evidence_refs=[f"body:{entity_name}"],
                    )
                ],
                new_events=[
                    EventCandidate(
                        summary="许澄记录异常交易线索",
                        significance="major",
                        involved_entity_names=["许澄", entity_name],
                        roles=["protagonist", "record_label"],
                    )
                ],
            ),
            chapter_goals=[],
            chapter_task_contract=[],
            chapter_experience_plan=ChapterExperiencePlan(),
            existing_entities=[],
            book_state_snapshot={},
        )

        assert decision.action == "genericize_background_reference"
        assert decision.entity_name == entity_name
        assert decision.replacement == replacement


def test_policy_returns_manual_action_for_ambiguous_unplanned_story_entity() -> None:
    decision = SubworldAdmissionPolicy().classify(
        issue=_issue("沈墨"),
        writer_output=_output("沈墨", event_role="unknown_operator"),
        chapter_goals=[],
        chapter_task_contract=[],
        chapter_experience_plan=ChapterExperiencePlan(),
        existing_entities=[],
        book_state_snapshot={},
    )

    assert decision.action == "manual_review_required"
    assert decision.entity_name == "沈墨"
    assert "register_entity" in decision.manual_actions
    assert "genericize_background_reference" in decision.manual_actions
