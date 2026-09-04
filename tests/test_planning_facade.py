from __future__ import annotations

from pathlib import Path

from forwin.planning import PlanHealthService, PlanningQuery, PlanningService
from forwin.planning.future_plan_audit import FuturePlanAuditIssue, FuturePlanAuditRun


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


def test_planning_facade_replaces_dynamic_forwarding_shims() -> None:
    assert PlanningService.__name__ == "PlanningService"
    assert PlanningQuery.__name__ == "PlanningQuery"
    assert not (ROOT / "forwin" / "planning" / "future_plan_auditor.py").exists()
    arc_envelope = (ROOT / "forwin" / "planning" / "arc_envelope.py").read_text()
    assert "class PlanningServices" not in arc_envelope
    assert "self.services" not in arc_envelope
