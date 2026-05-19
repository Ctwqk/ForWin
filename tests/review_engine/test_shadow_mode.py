from __future__ import annotations

from forwin.review_engine.parity import compare_shadow_decisions
from forwin.review_engine.types import Decision


def test_shadow_comparison_marks_mismatch() -> None:
    result = compare_shadow_decisions(
        live=Decision("manual_review", "old", "old_rule", [], "old", {}),
        shadow=Decision("system_block", "new", "new_rule", [], "engine", {}),
    )

    assert result.shadow_mismatch is True


def test_shadow_comparison_matches_same_outcome_and_action() -> None:
    result = compare_shadow_decisions(
        live=Decision("manual_review", "old", "old_rule", [], "old", {"legacy_action": "manual"}),
        shadow=Decision("manual_review", "new", "new_rule", [], "engine", {"legacy_action": "manual"}),
    )

    assert result.shadow_mismatch is False
