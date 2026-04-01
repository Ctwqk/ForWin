from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Literal


class ContinuityIssue(BaseModel):
    """A single continuity or quality issue."""
    rule_name: str
    severity: Literal["error", "warning", "info"] = "warning"
    description: str          # In Chinese
    entity_names: list[str] = Field(default_factory=list)


class ReviewVerdict(BaseModel):
    """Result of continuity checking."""
    verdict: Literal["pass", "warn", "fail"]
    issues: list[ContinuityIssue] = Field(default_factory=list)
