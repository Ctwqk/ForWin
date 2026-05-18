from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from forwin.narrative_obligations.types import NarrativePlanPatch


AuditStatus = Literal["pass", "warn", "fail"]


class FuturePlanAuditIssue(BaseModel):
    issue_type: str
    severity: Literal["warning", "error"] = "error"
    target_chapter: int
    target_plan_id: str = ""
    description: str
    evidence_refs: list[str] = Field(default_factory=list)
    patch_type: str = "future_plan_audit"
    blocking: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class FuturePlanAuditRun(BaseModel):
    id: str = ""
    project_id: str
    current_chapter: int
    trigger_stage: str
    inspected_chapters: list[int] = Field(default_factory=list)
    status: AuditStatus = "pass"
    issues: list[FuturePlanAuditIssue] = Field(default_factory=list)
    plan_patches: list[NarrativePlanPatch] = Field(default_factory=list)
    applied_plan_patch_ids: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


FuturePlanAuditIssue.model_rebuild()
FuturePlanAuditRun.model_rebuild()

__all__ = ["AuditStatus", "FuturePlanAuditIssue", "FuturePlanAuditRun"]
