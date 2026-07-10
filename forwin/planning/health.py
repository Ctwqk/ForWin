from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from forwin.protocol.scenario_rehearsal import ScenarioRehearsalReport

    from .future_plan_audit import FuturePlanAuditRun


PlanHealthSeverity = Literal["pass", "warn", "fail"]
PlanHealthScope = Literal["chapter", "band", "arc", "book", "operator"]


class PlanHealth(BaseModel):
    severity: PlanHealthSeverity = "pass"
    scope: PlanHealthScope = "chapter"
    evidence: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    blocking: bool = False


class PlanHealthService:
    @staticmethod
    def from_future_audit(
        run: FuturePlanAuditRun,
        *,
        scope: PlanHealthScope = "book",
    ) -> PlanHealth:
        evidence = [
            ref
            for issue in run.issues
            for ref in issue.evidence_refs
            if str(ref or "").strip()
        ]
        reasons = [
            *[str(reason) for reason in run.blocking_reasons if str(reason).strip()],
            *[
                issue.description
                for issue in run.issues
                if issue.severity == "error" and issue.description
            ],
        ]
        blocking = bool(run.blocking_reasons) or any(
            issue.blocking and issue.severity == "error" for issue in run.issues
        )
        severity: PlanHealthSeverity = (
            "fail" if blocking or run.status == "fail" else "warn" if run.status == "warn" else "pass"
        )
        return PlanHealth(
            severity=severity,
            scope=scope,
            evidence=_dedupe(evidence),
            reasons=_dedupe(reasons),
            blocking=blocking,
        )

    @staticmethod
    def from_patch_validation(
        result: object,
        *,
        scope: PlanHealthScope,
        evidence: list[str] | None = None,
    ) -> PlanHealth:
        errors = [
            str(error)
            for error in getattr(result, "errors", []) or []
            if str(error).strip()
        ]
        passed = bool(getattr(result, "passed", False))
        return PlanHealth(
            severity="pass" if passed else "fail",
            scope=scope,
            evidence=_dedupe(evidence or []),
            reasons=_dedupe(errors),
            blocking=not passed,
        )

    @staticmethod
    def from_scenario_rehearsal(report: ScenarioRehearsalReport) -> PlanHealth:
        from forwin.protocol.scenario_rehearsal import ScenarioRehearsalRecommendation

        recommendation = report.recommendation
        blocking = recommendation in {
            ScenarioRehearsalRecommendation.REPLAN,
            ScenarioRehearsalRecommendation.BLOCK,
        } or any(finding.severity == "fail" for finding in report.risk_findings)
        severity: PlanHealthSeverity = (
            "fail"
            if blocking
            else "warn"
            if recommendation == ScenarioRehearsalRecommendation.PATCH
            else "pass"
        )
        evidence = [
            ref
            for finding in report.risk_findings
            for ref in finding.evidence_refs
            if str(ref or "").strip()
        ]
        reasons = [
            finding.message
            for finding in report.risk_findings
            if finding.message
        ]
        return PlanHealth(
            severity=severity,
            scope=report.rehearsal_scope,
            evidence=_dedupe(evidence),
            reasons=_dedupe(reasons),
            blocking=blocking,
        )

    @staticmethod
    def combine(*health: PlanHealth) -> PlanHealth:
        if not health:
            return PlanHealth()
        severity_rank = {"pass": 0, "warn": 1, "fail": 2}
        scope_rank = {"chapter": 0, "band": 1, "arc": 2, "book": 3, "operator": 4}
        return PlanHealth(
            severity=max(health, key=lambda item: severity_rank[item.severity]).severity,
            scope=max(health, key=lambda item: scope_rank[item.scope]).scope,
            evidence=_dedupe([ref for item in health for ref in item.evidence]),
            reasons=_dedupe([reason for item in health for reason in item.reasons]),
            blocking=any(item.blocking for item in health),
        )


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip()
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


__all__ = [
    "PlanHealth",
    "PlanHealthScope",
    "PlanHealthService",
    "PlanHealthSeverity",
]
