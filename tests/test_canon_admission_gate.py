from __future__ import annotations

import pytest

from forwin.canon_quality.gate import evaluate_canon_admission
from forwin.canon_quality.signals import CanonQualitySignal


def test_canon_admission_blocks_error_signal_in_strict_mode() -> None:
    result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=3,
        draft_id="d1",
        review_id="r1",
        review_verdict="warn",
        signals=[
            CanonQualitySignal(
                signal_id="sig-placeholder",
                project_id="p1",
                chapter_number=3,
                signal_type="placeholder_leakage",
                severity="error",
                target_scope="body",
                subject_key="placeholder:相关人员",
                description="正文包含占位符。",
                evidence_refs=["body:0-4"],
            )
        ],
        mode="strict",
    )

    assert result.commit_allowed is False
    assert result.verdict == "fail"
    assert result.blocking_issue_count == 1
    assert result.deterministic_issue_refs == ["sig-placeholder"]


def test_canon_admission_shadow_records_but_allows_commit() -> None:
    result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=3,
        draft_id="d1",
        review_id="r1",
        review_verdict="warn",
        signals=[
            CanonQualitySignal(
                signal_id="sig-terminal",
                project_id="p1",
                chapter_number=3,
                signal_type="terminal_state_active_conflict",
                severity="error",
                target_scope="character",
                subject_key="character:韩砚",
                description="终止态后继续活跃。",
                evidence_refs=["body:10-20"],
            )
        ],
        mode="shadow",
    )

    assert result.commit_allowed is True
    assert result.verdict == "warn"
    assert result.blocking_issue_count == 1
    assert "shadow" in result.gate_summary


def test_canon_admission_fatal_only_blocks_form_invariant_drift_with_evidence() -> None:
    result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=26,
        draft_id="d1",
        review_id="r1",
        review_verdict="warn",
        signals=[
            CanonQualitySignal(
                signal_id="sig-invariant",
                project_id="p1",
                chapter_number=26,
                signal_type="form_invariant_drift",
                severity="error",
                target_scope="chapter",
                subject_key="city_renovation_deadline",
                description="deadline contradicted prior canon",
                evidence_refs=["quote:1"],
            )
        ],
        mode="fatal_only",
    )

    assert result.commit_allowed is False
    assert result.verdict == "fail"
    assert result.deterministic_issue_refs == ["sig-invariant"]
    assert result.required_repair_scope == "chapter_plan"


@pytest.mark.parametrize(
    "signal_type",
    [
        "dead_character_resurrection",
        "already_dead_character_resurrected",
        "character_resurrection",
        "level_rollback",
        "power_level_rollback",
        "duplicate_artifact",
        "duplicate_resource",
        "duplicate_artifact_resource",
        "faction_relation_reversal",
        "protagonist_resource_debt_mismatch",
        "location_teleport",
        "impossible_location_teleport",
    ],
)
def test_pulp_fatal_profile_blocks_expanded_hard_canon_signal(signal_type: str) -> None:
    result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=12,
        signals=[
            CanonQualitySignal(
                signal_id=f"sig-{signal_type}",
                project_id="p1",
                chapter_number=12,
                signal_type=signal_type,
                severity="error",
                evidence_refs=["chapter:12"],
                description=f"hard canon blocker: {signal_type}",
            )
        ],
        mode="pulp_fatal",
    )

    assert result.commit_allowed is False
    assert result.verdict == "fail"
    assert result.deterministic_issue_refs == [f"sig-{signal_type}"]


def test_pulp_fatal_profile_warns_for_p1_obligation_but_blocks_p0() -> None:
    from forwin.narrative_obligations.types import NarrativeObligation

    p1 = NarrativeObligation(
        id="obl-p1",
        project_id="p1",
        origin_chapter_number=10,
        obligation_type="reader_promise_payoff",
        priority="P1",
        status="active",
        summary="补偿读者期待。",
        deadline_chapter=12,
        payoff_test="必须给出回报。",
    )
    p2 = p1.model_copy(update={"id": "obl-p2", "priority": "P2"})
    p0 = p1.model_copy(update={"id": "obl-p0", "priority": "P0"})

    p1_result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=12,
        obligations=[p1],
        mode="pulp_fatal",
    )
    p0_result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=12,
        obligations=[p0],
        mode="pulp_fatal",
    )
    p2_result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=12,
        obligations=[p2],
        mode="pulp_fatal",
    )

    assert p1_result.commit_allowed is True
    assert p1_result.verdict == "warn"
    assert p1_result.admission_mode == "with_obligation"
    assert p1_result.blocking_reasons == []
    assert p2_result.commit_allowed is True
    assert p2_result.verdict == "warn"
    assert p2_result.blocking_reasons == []
    assert p0_result.commit_allowed is False
    assert "obligation_due_unresolved:obl-p0" in p0_result.blocking_reasons
