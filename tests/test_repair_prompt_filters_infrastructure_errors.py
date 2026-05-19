from __future__ import annotations

from forwin.protocol.context import ChapterContextPack
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.reviewer.hub import HistoricalReviewHub
from forwin.reviewer.infrastructure_errors import (
    filter_writer_fixable_issues,
    infrastructure_issues,
)


def _context() -> ChapterContextPack:
    return ChapterContextPack(
        project_id="p1",
        project_title="测试书",
        premise="测试",
        genre="悬疑",
        setting_summary="",
        project_target_total_chapters=30,
        chapter_number=18,
        chapter_plan_title="第十八章",
        chapter_plan_one_line="中段转场。",
        chapter_goals=["推进主线"],
    )


def test_infrastructure_errors_are_removed_from_writer_must_fix() -> None:
    schema_issue = ContinuityIssue(
        rule_name="chapter_review_form_schema",
        severity="error",
        description=(
            "countdowns.0.consistent_with_prior.value Input should be a valid string "
            "[type=string_type, input_value=False]"
        ),
        reviewer="canon_quality",
        issue_type="form_schema_invalid",
        source_layer="chapter_review_form",
        source_analyzer="pydantic",
        source_mode="chapter_review_form",
        blocking=True,
    )
    prose_issue = ContinuityIssue(
        rule_name="personality_drift",
        severity="error",
        description="角色动机偏离既有性格。",
        reviewer="personality",
        issue_type="personality_drift",
        blocking=True,
    )

    instruction = HistoricalReviewHub._continuity_repair_instruction(
        continuity_issues=[schema_issue, prose_issue],
        context=_context(),
    )

    assert instruction.must_fix == ["角色动机偏离既有性格。"]
    assert "form_schema_invalid" in instruction.design_patch["infrastructure_filtered_issue_types"]


def test_infrastructure_issue_helpers_keep_exact_operator_triage_payload() -> None:
    review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="schema",
                severity="error",
                description="ValidationError: Input should be a valid string",
                issue_type="form_schema_invalid",
                source_analyzer="ChapterReviewAnswers",
            ),
            ContinuityIssue(
                rule_name="hook",
                severity="error",
                description="章末钩子偏弱。",
                issue_type="hook_failure",
            ),
        ],
    )

    infra = infrastructure_issues(review.issues)
    writer_fixable = filter_writer_fixable_issues(review.issues)

    assert [issue.issue_type for issue in infra] == ["form_schema_invalid"]
    assert [issue.issue_type for issue in writer_fixable] == ["hook_failure"]
