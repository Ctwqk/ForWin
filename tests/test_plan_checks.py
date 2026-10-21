from __future__ import annotations

from forwin.narrative_obligations.types import NarrativeObligation
from forwin.protocol.experience import BandDelightSchedule, BandObligationContract
from forwin.review.plan_checks import evaluate_band_obligation_contract, evaluate_director_imbalance


def test_director_ignores_setup_words_in_free_text_notes() -> None:
    issues = evaluate_director_imbalance(
        review_metas=[
            {"chapter_number": number, "issue_types": ["experience"], "review_notes": ["setup 伏笔"]}
            for number in (1, 2)
        ],
        band_stall_guard=0, reviewer="test", target_scope="band",
    )

    assert issues == []


def test_director_keeps_structured_reward_checks() -> None:
    issues = evaluate_director_imbalance(
        review_metas=[
            {"chapter_number": number, "issue_types": ["payoff_miss"], "planned_reward_tags": ["mystery"]}
            for number in (1, 2)
        ],
        band_stall_guard=0, reviewer="test", target_scope="band",
    )

    assert {issue.rule_name for issue in issues} == {
        "director_payoff_consecutive_missing", "director_mystery_without_clarification",
    }


def test_band_obligation_contract_still_requires_resolution() -> None:
    schedule = BandDelightSchedule(
        band_id="band-1", chapter_start=1, chapter_end=2,
        band_obligation_contract=BandObligationContract(
            open_obligations=["ob-1"], must_resolve_by_band_end=["ob-1"],
        ),
    )
    obligation = NarrativeObligation(
        id="ob-1", project_id="test", origin_chapter_number=1,
        obligation_type="plan_task_fulfillment", summary="Deliver the packet",
        deadline_chapter=2, payoff_test="The recipient has the packet", status="active",
    )

    issues = evaluate_band_obligation_contract(
        schedule, obligations=[obligation], band_end_chapter=2,
        reviewer="test", target_scope="band",
    )
    assert [(issue.rule_name, issue.severity) for issue in issues] == [
        ("band_obligation_unresolved", "error"),
    ]
    assert evaluate_band_obligation_contract(
        schedule, obligations=[obligation.model_copy(update={"status": "resolved"})],
        band_end_chapter=2, reviewer="test", target_scope="band",
    ) == []
