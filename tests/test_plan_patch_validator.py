from __future__ import annotations

from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch
from forwin.planning.plan_patch_validator import PlanPatchValidator


def _obligation() -> NarrativeObligation:
    return NarrativeObligation(
        id="obl-1",
        project_id="project-1",
        origin_chapter_number=10,
        obligation_type="motivation_gap",
        priority="P1",
        status="proposed",
        summary="韩砚动机尚未解释。",
        hardness="design_debt",
        deadline_chapter=12,
        payoff_test="第12章前给出明确动机证据。",
        evidence_refs=["review:1"],
    )


def test_plan_patch_validator_accepts_future_patch_covering_obligation() -> None:
    obligation = _obligation()
    patch = NarrativePlanPatch(
        id="patch-1",
        project_id="project-1",
        patch_type="defer_acceptance",
        target_scope="chapter",
        affected_chapters=[11, 12],
        source_obligation_ids=["obl-1"],
        new_contract={"payoff_test": obligation.payoff_test},
        writer_context_injections=[{"obligation_id": "obl-1", "instruction": "偿还动机缺口"}],
        reviewer_context_injections=[{"obligation_id": "obl-1", "payoff_test": obligation.payoff_test}],
        expected_resolution_tests=[obligation.payoff_test],
        validation_status="pending",
    )

    result = PlanPatchValidator().validate(
        patch=patch,
        obligations=[obligation],
        current_chapter=10,
        target_total_chapters=12,
    )

    assert result.passed is True
    assert result.errors == []


def test_plan_patch_validator_rejects_missing_patch_binding_and_past_chapter() -> None:
    obligation = _obligation()
    patch = NarrativePlanPatch(
        id="patch-1",
        project_id="project-1",
        patch_type="defer_acceptance",
        target_scope="chapter",
        affected_chapters=[10],
        source_obligation_ids=[],
        new_contract={"payoff_test": obligation.payoff_test},
        writer_context_injections=[],
        reviewer_context_injections=[],
        expected_resolution_tests=[],
    )

    result = PlanPatchValidator().validate(
        patch=patch,
        obligations=[obligation],
        current_chapter=10,
        target_total_chapters=12,
    )

    assert result.passed is False
    assert "missing_source_obligation:obl-1" in result.errors
    assert "affected_chapter_not_future:10" in result.errors
    assert "missing_writer_context_injections" in result.errors
    assert "missing_reviewer_context_injections" in result.errors


def test_plan_patch_validator_allows_current_unaccepted_canon_plan_staleness_patch() -> None:
    patch = NarrativePlanPatch(
        id="patch-current",
        project_id="project-1",
        patch_type="canon_plan_staleness",
        target_scope="chapter",
        affected_chapters=[23],
        target_plan_id="plan-23",
        new_contract={"countdown_key": "memory_reset", "latest_remaining_minutes": 90},
        writer_context_injections=[{"countdown_key": "memory_reset", "latest_remaining_minutes": 90}],
        reviewer_context_injections=[{"countdown_key": "memory_reset", "payoff_test": "不得超过90分钟"}],
        expected_resolution_tests=["不得超过90分钟"],
    )

    result = PlanPatchValidator().validate(
        patch=patch,
        obligations=[],
        current_chapter=23,
        target_total_chapters=60,
        accepted_chapters=[],
    )

    assert result.passed is True
    assert result.errors == []


def test_plan_patch_validator_rejects_future_plan_audit_patch_for_accepted_chapter() -> None:
    patch = NarrativePlanPatch(
        id="patch-accepted",
        project_id="project-1",
        patch_type="future_plan_audit",
        target_scope="chapter",
        affected_chapters=[24],
        target_plan_id="plan-24",
        new_contract={"countdown_key": "memory_reset", "latest_remaining_minutes": 90},
        writer_context_injections=[{"countdown_key": "memory_reset", "latest_remaining_minutes": 90}],
        reviewer_context_injections=[{"countdown_key": "memory_reset", "payoff_test": "不得超过90分钟"}],
        expected_resolution_tests=["不得超过90分钟"],
    )

    result = PlanPatchValidator().validate(
        patch=patch,
        obligations=[],
        current_chapter=23,
        target_total_chapters=60,
        accepted_chapters=[24],
    )

    assert result.passed is False
    assert "affected_chapter_already_accepted:24" in result.errors


def test_plan_patch_validator_rejects_removing_unresolved_obligation() -> None:
    patch = NarrativePlanPatch(
        id="patch-remove",
        project_id="project-1",
        patch_type="future_plan_audit",
        target_scope="chapter",
        affected_chapters=[24],
        target_plan_id="plan-24",
        new_contract={"remove_obligation_ids": ["obl-open"]},
        writer_context_injections=[{"instruction": "patch"}],
        reviewer_context_injections=[{"payoff_test": "check"}],
        expected_resolution_tests=["check"],
        metadata={"removed_obligation_ids": ["obl-open"]},
    )

    result = PlanPatchValidator().validate(
        patch=patch,
        obligations=[],
        current_chapter=23,
        target_total_chapters=60,
        unresolved_obligation_ids=["obl-open"],
    )

    assert result.passed is False
    assert "removes_unresolved_obligation:obl-open" in result.errors


def test_plan_patch_validator_accepts_band_patch_for_future_band_chapters() -> None:
    obligation = _obligation().model_copy(
        update={
            "id": "obl-band",
            "obligation_type": "reader_promise_payoff",
            "deadline_chapter": 14,
            "metadata": {"minimum_scope": "band"},
        }
    )
    patch = NarrativePlanPatch(
        id="patch-band",
        project_id="project-1",
        patch_type="band_defer_acceptance",
        target_scope="band",
        target_band_id="arc-1:band:2",
        affected_chapters=[11, 12, 13, 14],
        source_obligation_ids=["obl-band"],
        new_contract={
            "band_obligation_contract": {
                "open_obligations": ["obl-band"],
                "must_resolve_by_band_end": ["obl-band"],
                "allowed_carry_forward": [],
                "payoff_tests": {"obl-band": obligation.payoff_test},
            }
        },
        writer_context_injections=[{"obligation_id": "obl-band", "instruction": "band 偿还"}],
        reviewer_context_injections=[{"obligation_id": "obl-band", "payoff_test": obligation.payoff_test}],
        expected_resolution_tests=[obligation.payoff_test],
    )

    result = PlanPatchValidator().validate(
        patch=patch,
        obligations=[obligation],
        current_chapter=10,
        target_total_chapters=20,
        band_plan_bounds={"arc-1:band:2": (11, 14)},
        minimum_scope_by_obligation={"obl-band": "band"},
    )

    assert result.passed is True
    assert result.errors == []


def test_plan_patch_validator_rejects_chapter_patch_for_band_obligation() -> None:
    obligation = _obligation().model_copy(
        update={
            "id": "obl-band",
            "obligation_type": "reader_promise_payoff",
            "deadline_chapter": 14,
            "metadata": {"minimum_scope": "band"},
        }
    )
    patch = NarrativePlanPatch(
        id="patch-chapter",
        project_id="project-1",
        patch_type="defer_acceptance",
        target_scope="chapter",
        target_plan_id="plan-11",
        affected_chapters=[11],
        source_obligation_ids=["obl-band"],
        new_contract={"payoff_test": obligation.payoff_test},
        writer_context_injections=[{"obligation_id": "obl-band", "instruction": "单章补一句"}],
        reviewer_context_injections=[{"obligation_id": "obl-band", "payoff_test": obligation.payoff_test}],
        expected_resolution_tests=[obligation.payoff_test],
    )

    result = PlanPatchValidator().validate(
        patch=patch,
        obligations=[obligation],
        current_chapter=10,
        target_total_chapters=20,
        minimum_scope_by_obligation={"obl-band": "band"},
    )

    assert result.passed is False
    assert "patch_scope_below_obligation_minimum:obl-band:chapter<band" in result.errors


def test_plan_patch_validator_rejects_p1_band_carry_forward() -> None:
    obligation = _obligation().model_copy(
        update={
            "id": "obl-band",
            "priority": "P1",
            "deadline_chapter": 14,
            "metadata": {"minimum_scope": "band"},
        }
    )
    patch = NarrativePlanPatch(
        id="patch-band",
        project_id="project-1",
        patch_type="band_defer_acceptance",
        target_scope="band",
        target_band_id="arc-1:band:2",
        affected_chapters=[11, 12, 13, 14],
        source_obligation_ids=["obl-band"],
        new_contract={
            "band_obligation_contract": {
                "open_obligations": ["obl-band"],
                "must_resolve_by_band_end": [],
                "allowed_carry_forward": ["obl-band"],
                "payoff_tests": {"obl-band": obligation.payoff_test},
            }
        },
        writer_context_injections=[{"obligation_id": "obl-band", "instruction": "band 偿还"}],
        reviewer_context_injections=[{"obligation_id": "obl-band", "payoff_test": obligation.payoff_test}],
        expected_resolution_tests=[obligation.payoff_test],
    )

    result = PlanPatchValidator().validate(
        patch=patch,
        obligations=[obligation],
        current_chapter=10,
        target_total_chapters=20,
        band_plan_bounds={"arc-1:band:2": (11, 14)},
        minimum_scope_by_obligation={"obl-band": "band"},
    )

    assert result.passed is False
    assert "p0_p1_obligation_cannot_carry_forward:obl-band" in result.errors
