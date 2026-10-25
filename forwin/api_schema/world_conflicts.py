from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class WorldConflictInfo(BaseModel):
    id: str
    project_id: str
    source: str
    origin_key: str = ""
    detected_after_chapter: int
    story_time: int
    involved_refs: list[str] = Field(default_factory=list)
    violated: str
    proposed_change: dict[str, Any] = Field(default_factory=dict)
    impact_level: str
    options: list[str] = Field(default_factory=list)
    required_decider: str
    blocks_from_chapter: int
    status: str
    summary: str
    decision: dict[str, Any] = Field(default_factory=dict)
    result_refs: list[str] = Field(default_factory=list)
    intent_ref: str = ""
    actor_ref: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Set on a resolve response only: what the standing resolution left undone, {code, message}, and the plan
    # conflict to amend through when a world-tick amend_plan could not plan the chapters again.
    warnings: list[dict[str, str]] = Field(default_factory=list)
    followup_conflict_id: str = ""


class WorldConflictResolveRequest(BaseModel):
    option: str
    rationale: str
    reason: str

    @field_validator("reason")
    @classmethod
    def require_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason is required")
        return value


__all__ = ["WorldConflictInfo", "WorldConflictResolveRequest"]
