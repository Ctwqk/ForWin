from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
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
