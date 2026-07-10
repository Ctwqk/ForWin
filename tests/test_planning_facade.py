from __future__ import annotations

from pathlib import Path

from forwin.planning import PlanHealthService, PlanningQuery, PlanningService
from forwin.planning.future_plan_audit import FuturePlanAuditIssue, FuturePlanAuditRun
from forwin.planning.structural_patch_validator import PatchValidationResult
from forwin.protocol.scenario_rehearsal import (
    ScenarioRehearsalRecommendation,
    ScenarioRehearsalReport,
    ScenarioRiskFinding,
)


ROOT = Path(__file__).resolve().parents[1]


def test_future_plan_audit_maps_to_blocking_plan_health() -> None:
    health = PlanHealthService.from_future_audit(
        FuturePlanAuditRun(
            project_id="project-1",
            current_chapter=8,
            trigger_stage="pre_write",
            status="fail",
            issues=[
                FuturePlanAuditIssue(
                    issue_type="deadline_conflict",
                    target_chapter=9,
                    description="deadline cannot be met",
                    evidence_refs=["chapter:9"],
                )
            ],
            blocking_reasons=["deadline_conflict"],
        ),
        scope="chapter",
    )

    assert health.severity == "fail"
    assert health.scope == "chapter"
    assert health.blocking is True
    assert health.evidence == ["chapter:9"]


def test_patch_and_rehearsal_health_share_one_contract() -> None:
    patch_health = PlanHealthService.from_patch_validation(
        PatchValidationResult(passed=False, errors=["missing_source_evidence"]),
        scope="arc",
    )
    rehearsal_health = PlanHealthService.from_scenario_rehearsal(
        ScenarioRehearsalReport(
            project_id="project-1",
            rehearsal_scope="band",
            recommendation=ScenarioRehearsalRecommendation.BLOCK,
            risk_findings=[
                ScenarioRiskFinding(
                    risk_type="canon_conflict",
                    severity="fail",
                    message="canon conflict",
                    evidence_refs=["canon:fact-1"],
                )
            ],
        )
    )

    combined = PlanHealthService.combine(patch_health, rehearsal_health)
    assert combined.severity == "fail"
    assert combined.scope == "arc"
    assert combined.blocking is True
    assert combined.reasons == ["missing_source_evidence", "canon conflict"]


def test_planning_facade_replaces_dynamic_forwarding_shims() -> None:
    assert PlanningService.__name__ == "PlanningService"
    assert PlanningQuery.__name__ == "PlanningQuery"
    assert not (ROOT / "forwin" / "planning" / "future_plan_auditor.py").exists()
    phase24 = (ROOT / "forwin" / "orchestrator" / "phase24.py").read_text()
    assert "class PlanningServices" not in phase24
    assert "self.services" not in phase24
