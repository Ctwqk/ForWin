from __future__ import annotations

import json

from forwin.protocol.review import ContinuityIssue, RepairInstruction, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.reviser.verification import RepairVerifier


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

    assert result.fixed_all_must_fix is True
    assert result.preserved_all_must_preserve is True
    assert result.unfixed == []
    assert result.broken_preserve_constraints == []
    assert result.verifier_mode == "rule_preferred_llm_disagreed"


def test_repair_verifier_does_not_treat_different_entities_as_same_unfixed_issue() -> None:
    instruction = RepairInstruction(
        repair_scope="chapter",
        failure_type="continuity",
        must_fix=["命名角色「陈副总」未在当前 chapter 的 subworld 准入名单中。"],
        must_preserve=[],
    )
    before_review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="sub_world_unknown_named_entity",
                severity="error",
                description="命名角色「陈副总」未在当前 chapter 的 subworld 准入名单中。",
                entity_names=["陈副总"],
                issue_type="subworld_admission",
                target_scope="chapter",
            )
        ],
    )
    after_review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="sub_world_unknown_named_entity",
                severity="error",
                description="命名角色「方敏」未在当前 chapter 的 subworld 准入名单中。",
                entity_names=["方敏"],
                issue_type="subworld_admission",
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

    assert result.fixed_all_must_fix is True
    assert result.unfixed == []
    assert result.new_risks == ["命名角色「方敏」未在当前 chapter 的 subworld 准入名单中。"]


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

    assert result.preserved_all_must_preserve is True
    assert result.broken_preserve_constraints == []
