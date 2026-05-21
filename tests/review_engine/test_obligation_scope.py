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


def test_decide_obligation_scope_uses_next_band_when_current_band_has_no_future_chapters() -> None:
    scope = decide_obligation_scope(
        issue_type="reveal_escalation_needed",
        priority="P1",
        current_chapter=14,
        target_total_chapters=20,
        bands=[
            BandScopeCandidate(
                band_id="arc-1:band:2",
                arc_id="arc-1",
                chapter_start=9,
                chapter_end=14,
                planned_chapters=[],
            ),
            BandScopeCandidate(
                band_id="arc-1:band:3",
                arc_id="arc-1",
                chapter_start=15,
                chapter_end=18,
                planned_chapters=[15, 16, 17, 18],
            ),
        ],
    )

    assert scope.action == "defer_with_band_plan_patch"
    assert scope.target_scope == "band"
    assert scope.target_band_id == "arc-1:band:3"
    assert scope.affected_chapters == [15, 16, 17, 18]


def test_decide_obligation_scope_blocks_band_defer_when_no_future_band_is_available() -> None:
    scope = decide_obligation_scope(
        issue_type="reader_promise_payoff",
        priority="P1",
        current_chapter=20,
        target_total_chapters=20,
        bands=[],
    )

    assert scope.action == "manual_review_required"
    assert scope.target_scope == "band"
    assert scope.reason == "no future band plan available for band-level obligation"
