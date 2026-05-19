from __future__ import annotations

from forwin.review_engine.rules.obligation_scope import (
    BandScopeCandidate,
    decide_obligation_scope,
    decision_from_obligation_scope,
)


def test_decide_obligation_scope_routes_chapter_issue_without_legacy_router() -> None:
    scope = decide_obligation_scope(
        issue_type="motivation_gap",
        priority="P1",
        current_chapter=3,
        target_total_chapters=10,
        bands=[],
    )
    decision = decision_from_obligation_scope(scope)

    assert scope.action == "defer_with_chapter_plan_patch"
    assert scope.affected_chapters == [4]
    assert decision.outcome == "chapter_patch"
    assert decision.routed_from == "review_engine"
    assert "legacy_action" not in decision.sub_action


def test_decide_obligation_scope_routes_band_issue_without_legacy_router() -> None:
    scope = decide_obligation_scope(
        issue_type="reader_promise_payoff",
        priority="P1",
        current_chapter=5,
        target_total_chapters=20,
        bands=[
            BandScopeCandidate(
                band_id="band-1",
                arc_id="arc-1",
                chapter_start=4,
                chapter_end=8,
                planned_chapters=[6, 7, 8],
            )
        ],
    )
    decision = decision_from_obligation_scope(scope)

    assert scope.action == "defer_with_band_plan_patch"
    assert scope.target_band_id == "band-1"
    assert decision.outcome == "band_patch"
    assert decision.routed_from == "review_engine"
