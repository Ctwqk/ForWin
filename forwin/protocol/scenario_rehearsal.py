from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ScenarioRehearsalRecommendation(str, Enum):
    PASS = "pass"
    PATCH = "patch"
    REPLAN = "replan"
    BLOCK = "block"


class ScenarioRiskFinding(BaseModel):
    risk_type: str
    severity: Literal["info", "warn", "fail"] = "warn"
    message: str
    evidence_refs: list[str] = Field(default_factory=list)
    affected_chapters: list[int] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScenarioPlanPatch(BaseModel):
    patch_type: str
    target: str = ""
    message: str
    evidence_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScenarioAppliedPatch(BaseModel):
    patch_type: str
    target: str = ""
    status: str = "applied"
    message: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScenarioRehearsalReport(BaseModel):
    project_id: str
    arc_id: str = ""
    band_id: str = ""
    rehearsal_scope: Literal["arc", "band", "chapter"] = "band"
    chapter_numbers: list[int] = Field(default_factory=list)
    trigger_reasons: list[str] = Field(default_factory=list)
    world_state_deltas: list[str] = Field(default_factory=list)
    character_knowledge_deltas: list[str] = Field(default_factory=list)
    reader_cognition_deltas: list[str] = Field(default_factory=list)
    visibility_deltas: list[str] = Field(default_factory=list)
    planned_rewards: list[str] = Field(default_factory=list)
    risk_findings: list[ScenarioRiskFinding] = Field(default_factory=list)
    future_conflicts: list[str] = Field(default_factory=list)
    required_plan_patches: list[ScenarioPlanPatch] = Field(default_factory=list)
    recommendation: ScenarioRehearsalRecommendation = ScenarioRehearsalRecommendation.PASS
    resolution_status: str = ""
    applied_patches: list[ScenarioAppliedPatch] = Field(default_factory=list)
    patch_attempt_count: int = 0
    checkpoint_id: str = ""
    replan_event_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
