from __future__ import annotations

from forwin.planning.obligation_scope_router import BandScopeCandidate, ObligationScopeRouter


def test_scope_router_routes_reader_promise_to_current_band_future_chapters() -> None:
    decision = ObligationScopeRouter().route(
        issue_type="reader_promise_payoff",
        priority="P1",
        current_chapter=10,
        target_total_chapters=20,
        bands=[
            BandScopeCandidate(
                band_id="arc-1:band:2",
                arc_id="arc-1",
                chapter_start=9,
                chapter_end=14,
                planned_chapters=[11, 12, 13, 14],
            )
        ],
    )

    assert decision.action == "defer_with_band_plan_patch"
    assert decision.target_scope == "band"
    assert decision.target_band_id == "arc-1:band:2"
    assert decision.affected_chapters == [11, 12, 13, 14]
    assert decision.deadline_chapter == 14


def test_scope_router_routes_motivation_gap_to_chapter() -> None:
    decision = ObligationScopeRouter().route(
        issue_type="motivation_gap",
        priority="P2",
        current_chapter=10,
        target_total_chapters=20,
        bands=[
            BandScopeCandidate(
                band_id="arc-1:band:2",
                arc_id="arc-1",
                chapter_start=9,
                chapter_end=14,
                planned_chapters=[11, 12, 13, 14],
            )
        ],
    )

    assert decision.action == "defer_with_chapter_plan_patch"
    assert decision.target_scope == "chapter"
    assert decision.affected_chapters == [11]
    assert decision.deadline_chapter == 11


def test_scope_router_routes_structural_issues_to_arc_manual() -> None:
    decision = ObligationScopeRouter().route(
        issue_type="identity_ambiguity",
        priority="P1",
        current_chapter=37,
        target_total_chapters=60,
        bands=[],
    )

    assert decision.action == "manual_review_required"
    assert decision.target_scope == "arc"
    assert decision.reason == "identity_ambiguity requires arc-level planning"


def test_scope_router_uses_next_band_when_current_band_has_no_future_chapters() -> None:
    decision = ObligationScopeRouter().route(
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

    assert decision.target_scope == "band"
    assert decision.target_band_id == "arc-1:band:3"
    assert decision.affected_chapters == [15, 16, 17, 18]


def test_scope_router_blocks_band_defer_when_no_future_band_is_available() -> None:
    decision = ObligationScopeRouter().route(
        issue_type="reader_promise_payoff",
        priority="P1",
        current_chapter=20,
        target_total_chapters=20,
        bands=[],
    )

    assert decision.action == "manual_review_required"
    assert decision.target_scope == "band"
    assert decision.reason == "no future band plan available for band-level obligation"
