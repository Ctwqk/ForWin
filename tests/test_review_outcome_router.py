from __future__ import annotations

from forwin.canon_quality.signals import CanonQualitySignal
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.reviewer.outcome import ReviewOutcomeRouter


def test_review_outcome_routes_clean_pass_to_commit_clean() -> None:
    outcome = ReviewOutcomeRouter().route(
        review=ReviewVerdict(verdict="pass"),
        signals=[],
        current_chapter=10,
        target_total_chapters=20,
    )

    assert outcome.action == "commit_clean"
    assert outcome.minimum_scope == "draft"


def test_review_outcome_routes_placeholder_to_local_rewrite() -> None:
    outcome = ReviewOutcomeRouter().route(
        review=ReviewVerdict(
            verdict="fail",
            issues=[
                ContinuityIssue(
                    rule_name="placeholder_leakage",
                    severity="error",
                    description="正文包含相关人员占位符。",
                    issue_type="placeholder_leakage",
                    target_scope="body",
                )
            ],
        ),
        signals=[],
        current_chapter=10,
        target_total_chapters=20,
    )

    assert outcome.action == "local_rewrite"
    assert outcome.minimum_scope == "draft"
    assert outcome.primary_issue_class == "placeholder_leakage"


def test_review_outcome_routes_motivation_gap_to_plan_backed_defer() -> None:
    signal = CanonQualitySignal(
        signal_id="sig-motive",
        project_id="p1",
        chapter_number=10,
        signal_type="motivation_gap",
        severity="warning",
        target_scope="character",
        description="韩砚动机尚未解释。",
    )

    outcome = ReviewOutcomeRouter().route(
        review=ReviewVerdict(verdict="warn"),
        signals=[signal],
        current_chapter=10,
        target_total_chapters=20,
    )

    assert outcome.action == "defer_with_chapter_plan_patch"
    assert outcome.minimum_scope == "chapter_plan"
    assert outcome.blocking_signal_ids == []


def test_review_outcome_routes_identity_failure_to_arc_replan_then_rewrite() -> None:
    outcome = ReviewOutcomeRouter().route(
        review=ReviewVerdict(
            verdict="fail",
            issues=[
                ContinuityIssue(
                    rule_name="identity_conflict",
                    severity="error",
                    description="核心身份跨章冲突。",
                    issue_type="identity_ambiguity",
                    target_scope="arc",
                )
            ],
        ),
        signals=[],
        current_chapter=37,
        target_total_chapters=60,
    )

    assert outcome.action == "arc_replan_then_rewrite"
    assert outcome.minimum_scope == "arc"


def test_review_outcome_blocks_final_p1_obligation() -> None:
    signal = CanonQualitySignal(
        signal_id="sig-final",
        project_id="p1",
        chapter_number=60,
        signal_type="final_hook_closure",
        severity="warning",
        target_scope="book",
        description="终章仍有 P1 主线义务。",
    )

    outcome = ReviewOutcomeRouter().route(
        review=ReviewVerdict(verdict="warn"),
        signals=[signal],
        current_chapter=60,
        target_total_chapters=60,
    )

    assert outcome.action == "block"
    assert outcome.minimum_scope == "book"
    assert outcome.blocking_signal_ids == ["sig-final"]
