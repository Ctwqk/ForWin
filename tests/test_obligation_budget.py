from __future__ import annotations

from forwin.narrative_obligations.budget import ObligationBudgetPolicy, evaluate_obligation_budget
from forwin.narrative_obligations.types import NarrativeObligation


def _obligation(
    obligation_id: str,
    *,
    origin_chapter: int = 10,
    obligation_type: str = "motivation_gap",
    priority: str = "P1",
) -> NarrativeObligation:
    return NarrativeObligation(
        id=obligation_id,
        project_id="p1",
        origin_chapter_number=origin_chapter,
        obligation_type=obligation_type,
        priority=priority,  # type: ignore[arg-type]
        status="active",
        summary="待偿还缺口",
        hardness="design_debt",
        deadline_chapter=origin_chapter + 2,
        payoff_test="必须给出明确偿还证据。",
    )


def test_obligation_budget_blocks_too_many_new_p1_p2_in_one_chapter() -> None:
    new_items = [_obligation(f"obl-{index}", priority="P1") for index in range(3)]

    result = evaluate_obligation_budget(
        open_obligations=[],
        new_obligations=new_items,
        current_chapter=10,
        band_start=9,
        band_end=14,
        arc_start=1,
        arc_end=20,
        policy=ObligationBudgetPolicy(max_new_p1_p2_per_chapter=2),
    )

    assert result.allowed is False
    assert result.over_budget is True
    assert "chapter_new_p1_p2_budget_exceeded:3>2" in result.reasons


def test_obligation_budget_blocks_band_and_arc_structural_overflow() -> None:
    open_items = [
        _obligation(f"band-{index}", origin_chapter=8 + index, priority="P1")
        for index in range(5)
    ]
    open_items.extend(
        [
            _obligation("identity-1", obligation_type="identity_ambiguity", priority="P1"),
            _obligation("countdown-1", obligation_type="countdown_explanation", priority="P1"),
        ]
    )
    new_items = [_obligation("identity-2", obligation_type="identity_ambiguity", priority="P1")]

    result = evaluate_obligation_budget(
        open_obligations=open_items,
        new_obligations=new_items,
        current_chapter=10,
        band_start=8,
        band_end=14,
        arc_start=1,
        arc_end=20,
        policy=ObligationBudgetPolicy(
            max_open_p1_p2_per_band=5,
            max_open_arc_structural_p1=2,
        ),
    )

    assert result.allowed is False
    assert "band_open_p1_p2_budget_exceeded:8>5" in result.reasons
    assert "arc_structural_p1_budget_exceeded:3>2" in result.reasons


def test_obligation_budget_blocks_p0_at_final_band_start() -> None:
    result = evaluate_obligation_budget(
        open_obligations=[_obligation("p0-main", priority="P0")],
        new_obligations=[],
        current_chapter=51,
        band_start=51,
        band_end=60,
        arc_start=1,
        arc_end=60,
        final_band_start_chapter=51,
    )

    assert result.allowed is False
    assert "final_band_open_p0_obligation:p0-main" in result.reasons
