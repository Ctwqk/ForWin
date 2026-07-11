from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ConstraintType = Literal[
    "character_availability",
    "secret_withhold",
    "relationship_preserve",
    "thread_keep_open",
    "location_availability",
    "rule_preserve",
]


ConstraintLevel = Literal["hard", "soft", "hint"]


ConstraintStatus = Literal["active", "inactive", "archived"]


CONSTRAINT_TYPES = {
    "character_availability",
    "secret_withhold",
    "relationship_preserve",
    "thread_keep_open",
    "location_availability",
    "rule_preserve",
}


CONSTRAINT_LEVELS = {"hard", "soft", "hint"}


CONSTRAINT_STATUSES = {"active", "inactive", "archived"}


class NarrativeConstraintInfo(BaseModel):
    id: str = ""
    project_id: str = ""
    arc_id: str = ""
    band_id: str = ""
    constraint_type: ConstraintType = "character_availability"
    level: ConstraintLevel = "hard"
    subject_name: str = ""
    description: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    effective_from_chapter: int = 1
    protect_until_chapter: int = 0
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""


__all__ = [
    "CONSTRAINT_LEVELS",
    "CONSTRAINT_STATUSES",
    "CONSTRAINT_TYPES",
    "ConstraintLevel",
    "ConstraintStatus",
    "ConstraintType",
    "NarrativeConstraintInfo",
]
