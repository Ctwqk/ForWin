from __future__ import annotations

from pydantic import BaseModel, Field

from forwin.protocol.review import RepairInstruction


class BookStateExtractionGateIssue(BaseModel):
    reviewer: str
    severity: str = "fail"
    failure_type: str
    message: str
    evidence_refs: list[str] = Field(default_factory=list)
    repair_patch: dict[str, object] = Field(default_factory=dict)


class BookStateExtractionGateVerdict(BaseModel):
    passed: bool
    issues: list[BookStateExtractionGateIssue] = Field(default_factory=list)
    approved_changes: object | None = None
    repair_instruction: RepairInstruction | None = None
