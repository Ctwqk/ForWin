from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


EntityAdmissionAction = Literal[
    "register_character",
    "register_alias",
    "background_generic",
    "plan_conflict",
]


class EntityAdmissionDecision(BaseModel):
    mention_name: str
    action: EntityAdmissionAction
    entity_id: str = ""
    canonical_name: str = ""
    aliases: list[str] = Field(default_factory=list)
    role_hint: str = ""
    importance: int = 5
    reason: str = ""


class EntityAdmissionPlan(BaseModel):
    schema_version: Literal["v1"] = "v1"
    project_id: str
    chapter_number: int
    candidate_fingerprint: str
    decisions: list[EntityAdmissionDecision] = Field(default_factory=list)
    plan_conflicts: list[str] = Field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.plan_conflicts)


__all__ = [
    "EntityAdmissionAction",
    "EntityAdmissionDecision",
    "EntityAdmissionPlan",
]
