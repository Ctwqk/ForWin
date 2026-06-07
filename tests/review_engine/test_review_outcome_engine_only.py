from __future__ import annotations

from forwin.canon_quality.signals import CanonQualitySignal
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.review_engine.engine import AutoDecisionEngine
from forwin.review_engine.rules.review_outcome import build_review_outcome_rules
from forwin.review_engine.types import Decision, DecisionInput, PlanLayerHealth


def _input(
    *,
    review: ReviewVerdict,
    signals: list[CanonQualitySignal] | None = None,
    current_chapter: int = 10,
    target_total_chapters: int = 20,
) -> DecisionInput:
    return DecisionInput(
        project_id="project-1",
        chapter_number=current_chapter,
        review=review,
        signals=list(signals or []),
        open_obligations=[],
        operation_mode="blackbox",
        attempts_completed=0,
        prior_scope_history=[],
        budget=None,
        target_total_chapters=target_total_chapters,
        plan_layer_health=PlanLayerHealth(),
    )


def _decision(input_payload: DecisionInput) -> Decision:
    return AutoDecisionEngine(build_review_outcome_rules()).decide(input_payload)


def test_engine_routes_clean_pass_to_auto_approve_commit_clean() -> None:
    decision = _decision(_input(review=ReviewVerdict(verdict="pass")))

    assert decision.outcome == "auto_approve"
    assert decision.sub_action["review_action"] == "commit_clean"
    assert decision.sub_action["minimum_scope"] == "draft"
    assert decision.routed_from == "review_engine"


def test_engine_routes_placeholder_failure_to_local_repair() -> None:
    decision = _decision(
        _input(
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
            )
        )
    )

    assert decision.outcome == "local_repair"
    assert decision.sub_action["review_action"] == "local_rewrite"
    assert decision.sub_action["minimum_scope"] == "draft"
    assert decision.sub_action["primary_issue_class"] == "placeholder_leakage"


def test_engine_routes_motivation_gap_to_chapter_patch() -> None:
    signal = CanonQualitySignal(
        signal_id="sig-motive",
        project_id="project-1",
        chapter_number=10,
        signal_type="motivation_gap",
        severity="warning",
        target_scope="character",
        description="动机需要后续解释。",
    )

    decision = _decision(_input(review=ReviewVerdict(verdict="warn"), signals=[signal]))

    assert decision.outcome == "chapter_patch"
    assert decision.sub_action["review_action"] == "defer_with_chapter_plan_patch"
    assert decision.sub_action["minimum_scope"] == "chapter_plan"
    assert decision.sub_action["blocking_signal_ids"] == []


def test_engine_routes_form_countdown_inconsistency_to_chapter_plan_patch() -> None:
    signal = CanonQualitySignal(
        signal_id="sig-countdown",
        project_id="project-1",
        chapter_number=10,
        signal_type="form_countdown_inconsistency",
        severity="error",
        target_scope="chapter",
        description="countdown contradicted prior canon",
    )

    decision = _decision(_input(review=ReviewVerdict(verdict="warn"), signals=[signal]))

    assert decision.outcome == "chapter_patch"
    assert decision.sub_action["review_action"] == "defer_with_chapter_plan_patch"
    assert decision.sub_action["minimum_scope"] == "chapter_plan"
    assert decision.sub_action["blocking_signal_ids"] == ["sig-countdown"]


def test_engine_routes_form_invariant_drift_to_chapter_plan_patch() -> None:
    signal = CanonQualitySignal(
        signal_id="sig-invariant",
        project_id="project-1",
        chapter_number=10,
        signal_type="form_invariant_drift",
        severity="error",
        target_scope="chapter",
        description="strong state contradicted prior canon",
    )

    decision = _decision(_input(review=ReviewVerdict(verdict="warn"), signals=[signal]))

    assert decision.outcome == "chapter_patch"
    assert decision.sub_action["review_action"] == "defer_with_chapter_plan_patch"
    assert decision.sub_action["minimum_scope"] == "chapter_plan"
    assert decision.sub_action["blocking_signal_ids"] == ["sig-invariant"]


def test_engine_routes_identity_failure_to_arc_patch() -> None:
    decision = _decision(
        _input(
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
            current_chapter=37,
            target_total_chapters=60,
        )
    )

    assert decision.outcome == "arc_patch"
    assert decision.sub_action["review_action"] == "arc_replan_then_rewrite"
    assert decision.sub_action["minimum_scope"] == "arc"


def test_engine_blocks_final_p1_book_signal() -> None:
    signal = CanonQualitySignal(
        signal_id="sig-final",
        project_id="project-1",
        chapter_number=60,
        signal_type="final_hook_closure",
        severity="warning",
        target_scope="book",
        description="终章仍有 P1 主线义务。",
    )

    decision = _decision(
        _input(
            review=ReviewVerdict(verdict="warn"),
            signals=[signal],
            current_chapter=60,
            target_total_chapters=60,
        )
    )

    assert decision.outcome == "system_block"
    assert decision.sub_action["review_action"] == "block"
    assert decision.sub_action["minimum_scope"] == "book"
    assert decision.sub_action["blocking_signal_ids"] == ["sig-final"]
