from __future__ import annotations

from forwin.canon_quality.signals import CanonQualitySignal
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.review.decision.engine import AutoDecisionEngine
from forwin.review.decision.rules.review_outcome import build_review_outcome_rules
from forwin.review.decision.types import Decision, DecisionInput, PlanLayerHealth


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
        attempts_completed=0,
        prior_scope_history=[],
        budget=None,
        target_total_chapters=target_total_chapters,
        plan_layer_health=PlanLayerHealth(),
    )


def _engine_decision(input_payload: DecisionInput) -> Decision:
    return AutoDecisionEngine(build_review_outcome_rules()).decide(input_payload)


def test_clean_pass_matches_review_outcome_router() -> None:
    input_payload = _input(review=ReviewVerdict(verdict="pass"))

    decision = _engine_decision(input_payload)

    assert decision.sub_action["review_action"] == "commit_clean"
    assert decision.outcome == "accept"
    assert decision.routed_from == "ReviewOutcomeRouter"


def test_placeholder_failure_matches_review_outcome_router() -> None:
    input_payload = _input(
        review=ReviewVerdict(
            verdict="fail",
            issues=[
                ContinuityIssue(
                    rule_name="placeholder_leakage",
                    severity="error",
                    description="正文包含占位符。",
                    issue_type="placeholder_leakage",
                )
            ],
        )
    )

    decision = _engine_decision(input_payload)

    assert decision.sub_action["review_action"] == "local_rewrite"
    assert decision.outcome == "local_repair"
    assert decision.routed_from == "ReviewOutcomeRouter"


def test_plan_defer_matches_review_outcome_router() -> None:
    signal = CanonQualitySignal(
        signal_id="sig-motive",
        project_id="project-1",
        chapter_number=10,
        signal_type="motivation_gap",
        severity="warning",
        target_scope="character",
        description="动机需要后续解释。",
    )
    input_payload = _input(review=ReviewVerdict(verdict="warn"), signals=[signal])

    decision = _engine_decision(input_payload)

    assert decision.sub_action["review_action"] == "defer_with_chapter_plan_patch"
    assert decision.outcome == "chapter_patch"
    assert decision.routed_from == "ReviewOutcomeRouter"


def test_final_block_matches_review_outcome_router() -> None:
    signal = CanonQualitySignal(
        signal_id="sig-final",
        project_id="project-1",
        chapter_number=20,
        signal_type="final_hook_closure",
        severity="warning",
        target_scope="book",
        description="终章仍有主线义务。",
    )
    input_payload = _input(
        review=ReviewVerdict(verdict="warn"),
        signals=[signal],
        current_chapter=20,
        target_total_chapters=20,
    )

    decision = _engine_decision(input_payload)

    assert decision.sub_action["review_action"] == "block"
    assert decision.outcome == "system_block"
    assert decision.routed_from == "ReviewOutcomeRouter"
