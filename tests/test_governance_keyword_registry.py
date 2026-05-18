from __future__ import annotations

from forwin.governance import NarrativeConstraintInfo
from forwin.governance_checks import evaluate_constraint_issues
from forwin.governance_keywords import constraint_keywords


def test_constraint_keywords_are_available_from_single_registry() -> None:
    registry = constraint_keywords()

    assert "死亡" in registry.death
    assert "揭露" in registry.reveal


def test_prefix_negated_death_keyword_does_not_hard_trigger() -> None:
    constraint = NarrativeConstraintInfo(
        id="c1",
        constraint_type="character_availability",
        subject_name="韩青",
        description="韩青不能死亡",
    )

    issues = evaluate_constraint_issues(
        [constraint],
        combined_text="本章计划明确要求韩青不要死亡，只写她撤离后继续提供支援。",
        state_changes=[],
        events=[],
        thread_beats=[],
        reviewer="test",
        issue_type="test",
        target_scope="chapter",
    )

    assert issues == []


def test_positive_death_keyword_still_triggers() -> None:
    constraint = NarrativeConstraintInfo(
        id="c1",
        constraint_type="character_availability",
        subject_name="韩青",
        description="韩青不能死亡",
    )

    issues = evaluate_constraint_issues(
        [constraint],
        combined_text="韩青在爆炸中死亡，通讯频道只剩静电。",
        state_changes=[],
        events=[],
        thread_beats=[],
        reviewer="test",
        issue_type="test",
        target_scope="chapter",
    )

    assert [issue.rule_name for issue in issues] == ["future_constraint_violation"]


def test_mixed_negated_and_positive_death_occurrences_still_trigger() -> None:
    constraint = NarrativeConstraintInfo(
        id="c1",
        constraint_type="character_availability",
        subject_name="韩青",
        description="韩青不能死亡",
    )

    issues = evaluate_constraint_issues(
        [constraint],
        combined_text="本章韩青死亡。要避免死亡披露过早。",
        state_changes=[],
        events=[],
        thread_beats=[],
        reviewer="test",
        issue_type="test",
        target_scope="chapter",
    )

    assert [issue.rule_name for issue in issues] == ["future_constraint_violation"]
