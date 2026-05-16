from __future__ import annotations

from forwin.narrative_obligations.types import ReviewOutcome
from forwin.reviewer.outcome import repair_scope_for_outcome, merge_repair_scope


def test_repair_scope_uses_outcome_minimum_scope_before_attempt_number() -> None:
    outcome = ReviewOutcome(
        action="arc_replan_then_rewrite",
        reason="identity conflict",
        minimum_scope="arc",
        primary_issue_class="identity_ambiguity",
    )

    assert repair_scope_for_outcome(outcome, attempt_no=1) == "arc"


def test_repair_scope_keeps_local_pollution_at_draft_scope_on_later_attempts() -> None:
    outcome = ReviewOutcome(
        action="local_rewrite",
        reason="placeholder",
        minimum_scope="draft",
        primary_issue_class="placeholder_leakage",
    )

    assert repair_scope_for_outcome(outcome, attempt_no=3) == "draft"


def test_merge_repair_scope_does_not_downgrade_deterministic_arc() -> None:
    final_scope, downgrade_reason = merge_repair_scope(
        deterministic_scope="arc",
        requested_scope="band",
        allow_arc=True,
    )

    assert final_scope == "arc"
    assert downgrade_reason == ""


def test_merge_repair_scope_records_arc_downgrade_when_policy_disallows_arc() -> None:
    final_scope, downgrade_reason = merge_repair_scope(
        deterministic_scope="band",
        requested_scope="arc",
        allow_arc=False,
    )

    assert final_scope == "band"
    assert "arc requested" in downgrade_reason
