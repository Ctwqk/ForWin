from __future__ import annotations

import json

from forwin.protocol.review import ContinuityIssue, RepairInstruction, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.review.repair.verification import RepairVerifier


class ContradictingLLMClient:
    def chat(self, messages, **_kwargs) -> str:
        return json.dumps(
            {
                "fixed_all_must_fix": False,
                "preserved_all_must_preserve": False,
                "unfixed": ["母亲的 canon 姓名是「林若」，本章写成了「林若林若」。"],
                "broken_preserve_constraints": ["第六日·协议真相"],
                "new_risks": ["LLM verifier disagreed without deterministic evidence."],
            },
            ensure_ascii=False,
        )


class CapturingLLMClient:
    def __init__(self) -> None:
        self.messages = []

    def chat(self, messages, **_kwargs) -> str:
        self.messages = list(messages)
        return json.dumps(
            {
                "fixed_all_must_fix": True,
                "preserved_all_must_preserve": True,
                "unfixed": [],
                "broken_preserve_constraints": [],
                "new_risks": [],
            },
            ensure_ascii=False,
        )


def test_repair_verifier_does_not_turn_rule_clean_repair_into_hard_failure() -> None:
    instruction = RepairInstruction(
        repair_scope="draft",
        failure_type="continuity",
        must_fix=["母亲的 canon 姓名是「林若」，本章写成了「林若林若」。"],
        must_preserve=["第六日·协议真相"],
    )
    before_review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="canon_name_drift",
                severity="error",
                description="母亲的 canon 姓名是「林若」，本章写成了「林若林若」。",
                issue_type="continuity",
                target_scope="chapter",
            )
        ],
    )
    after_review = ReviewVerdict(verdict="warn", issues=[])

    result = RepairVerifier(
        llm_client=ContradictingLLMClient(),
        llm_enabled=True,
    ).verify(
        original_output=WriterOutput(
            chapter_number=7,
            title="第六日·协议真相",
            body="旧稿误写母亲林若林若。",
            end_of_chapter_summary="第六日·协议真相",
        ),
        repaired_output=WriterOutput(
            chapter_number=7,
            title="第六日·协议真相",
            body="新稿只保留母亲林若。",
            end_of_chapter_summary="第六日·协议真相",
        ),
        before_review=before_review,
        after_review=after_review,
        repair_instruction=instruction,
    )

    assert result.fixed_all_must_fix is None
    assert result.preserved_all_must_preserve is True
    assert result.unfixed == []
    assert result.broken_preserve_constraints == []
    assert result.verifier_mode == "rule+llm"


def test_repair_verifier_does_not_send_partial_oversized_context() -> None:
    huge_payload = "巨大上下文" * 30_000
    instruction = RepairInstruction(
        repair_scope="chapter_plan",
        failure_type="continuity",
        must_fix=["修复周隐越界直接发声的问题。"],
        must_preserve=["保留倒计时已暂停。"],
        evidence_refs=["chapter=27", huge_payload],
        design_patch={"raw_context": huge_payload},
    )
    before_review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="entity_admission_plan_conflict",
                severity="error",
                description="EntityRegistrar 无法为周隐（声音）作出注册决定。",
                entity_names=["周隐（声音）"],
                issue_type="entity_admission_plan_conflict",
                target_scope="chapter",
                evidence_refs=["chapter=27", huge_payload],
                suggested_fix=huge_payload,
                original_result={"raw_prompt": huge_payload},
            )
        ],
        prompt_trace={"raw_prompt": huge_payload},
        extracted_actuals={"raw_form": huge_payload},
    )
    after_review = ReviewVerdict(
        verdict="warn",
        issues=[
            ContinuityIssue(
                rule_name="plan_task_unfulfilled",
                severity="warning",
                description="计划任务还可以更明确。",
                issue_type="plan_task_fulfillment",
                target_scope="chapter",
                original_result={"raw_prompt": huge_payload},
            )
        ],
        prompt_trace={"raw_prompt": huge_payload},
    )
    client = CapturingLLMClient()

    result = RepairVerifier(llm_client=client, llm_enabled=True).verify(
        original_output=WriterOutput(
            chapter_number=27,
            title="旧稿",
            body="旧稿正文" * 20_000,
            end_of_chapter_summary="旧稿总结",
            generation_meta={"raw_prompt": huge_payload},
        ),
        repaired_output=WriterOutput(
            chapter_number=27,
            title="新稿",
            body="新稿正文" * 20_000,
            end_of_chapter_summary="新稿总结",
            generation_meta={"raw_prompt": huge_payload},
        ),
        before_review=before_review,
        after_review=after_review,
        repair_instruction=instruction,
    )

    assert client.messages == []
    assert result.fixed_all_must_fix is None
    assert result.preserved_all_must_preserve is None
    assert all(check.reason == "input_budget_exceeded" for check in result.checks)


def test_repair_verifier_does_not_treat_different_entities_as_same_unfixed_issue() -> None:
    instruction = RepairInstruction(
        repair_scope="chapter",
        failure_type="continuity",
        must_fix=["EntityRegistrar 无法为命名角色「陈副总」作出注册决定。"],
        must_preserve=[],
    )
    before_review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="entity_admission_plan_conflict",
                severity="error",
                description="EntityRegistrar 无法为命名角色「陈副总」作出注册决定。",
                entity_names=["陈副总"],
                issue_type="entity_admission_plan_conflict",
                target_scope="chapter",
            )
        ],
    )
    after_review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="entity_admission_plan_conflict",
                severity="error",
                description="EntityRegistrar 无法为命名角色「方敏」作出注册决定。",
                entity_names=["方敏"],
                issue_type="entity_admission_plan_conflict",
                target_scope="chapter",
            )
        ],
    )

    result = RepairVerifier().verify(
        original_output=WriterOutput(
            chapter_number=15,
            title="旧稿",
            body="陈副总出现。",
            end_of_chapter_summary="旧稿",
        ),
        repaired_output=WriterOutput(
            chapter_number=15,
            title="新稿",
            body="方敏出现。",
            end_of_chapter_summary="新稿",
        ),
        before_review=before_review,
        after_review=after_review,
        repair_instruction=instruction,
    )

    assert result.fixed_all_must_fix is None
    assert result.unfixed == []
    assert result.new_risks == ["EntityRegistrar 无法为命名角色「方敏」作出注册决定。"]


def test_repair_verifier_treats_same_countdown_rule_as_unfixed_when_numbers_change() -> None:
    instruction = RepairInstruction(
        repair_scope="draft",
        failure_type="continuity",
        must_fix=["倒计时从 84 分钟回升到 89分钟，但正文没有明确 reset。"],
        must_preserve=[],
    )
    before_review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="countdown_non_monotonic",
                severity="error",
                description="倒计时从 84 分钟回升到 89分钟，但正文没有明确 reset。",
                issue_type="countdown_non_monotonic",
                target_scope="ledger",
            )
        ],
    )
    after_review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="countdown_non_monotonic",
                severity="error",
                description="倒计时从 82 分钟回升到 83分钟，但正文没有明确 reset。",
                issue_type="countdown_non_monotonic",
                target_scope="ledger",
            )
        ],
    )

    result = RepairVerifier().verify(
        original_output=WriterOutput(
            chapter_number=1,
            title="旧稿",
            body="84分钟后又写89分钟。",
            end_of_chapter_summary="旧稿",
        ),
        repaired_output=WriterOutput(
            chapter_number=1,
            title="新稿",
            body="82分钟后又写83分钟。",
            end_of_chapter_summary="新稿",
        ),
        before_review=before_review,
        after_review=after_review,
        repair_instruction=instruction,
    )

    assert result.fixed_all_must_fix is False
    assert result.unfixed == ["倒计时从 82 分钟回升到 83分钟，但正文没有明确 reset。"]
    assert result.new_risks == []


def test_repair_verifier_ignores_single_character_preserve_fragments() -> None:
    instruction = RepairInstruction(
        repair_scope="chapter",
        failure_type="mixed",
        must_fix=[],
        must_preserve=["示"],
    )

    result = RepairVerifier().verify(
        original_output=WriterOutput(
            chapter_number=16,
            title="旧稿",
            body="原稿包含一个无意义的单字片段：示。",
            end_of_chapter_summary="旧稿",
        ),
        repaired_output=WriterOutput(
            chapter_number=16,
            title="新稿",
            body="修复稿删除了这个低信号片段。",
            end_of_chapter_summary="新稿",
        ),
        before_review=ReviewVerdict(verdict="warn", issues=[]),
        after_review=ReviewVerdict(verdict="warn", issues=[]),
        repair_instruction=instruction,
    )

    assert result.preserved_all_must_preserve is None
    assert result.broken_preserve_constraints == []


def test_repair_verifier_does_not_treat_rewritten_body_wording_as_broken_preserve() -> None:
    instruction = RepairInstruction(
        repair_scope="chapter",
        failure_type="continuity",
        must_fix=["统一三个场景里的规则定义。"],
        must_preserve=["旧稿第二场写下的临时规则措辞"],
    )

    result = RepairVerifier().verify(
        original_output=WriterOutput(
            chapter_number=9,
            title="校正规则",
            body="旧稿第二场写下的临时规则措辞，随后又给出另一套定义。",
            end_of_chapter_summary="三处定义互相冲突。",
        ),
        repaired_output=WriterOutput(
            chapter_number=9,
            title="校正规则",
            body="修复稿只保留已经进入 canon 的唯一规则定义。",
            end_of_chapter_summary="规则定义已经统一。",
        ),
        before_review=ReviewVerdict(
            verdict="fail",
            issues=[
                ContinuityIssue(
                    rule_name="canon_rule_conflict",
                    severity="error",
                    description="统一三个场景里的规则定义。",
                    issue_type="continuity",
                    target_scope="chapter",
                )
            ],
        ),
        after_review=ReviewVerdict(verdict="pass", issues=[]),
        repair_instruction=instruction,
    )

    assert result.fixed_all_must_fix is None
    assert result.preserved_all_must_preserve is None
    assert result.broken_preserve_constraints == []
