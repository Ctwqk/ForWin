from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, field_validator

from forwin.book_state.schema import IMPACT_LEVELS


ConflictSource = Literal[
    "world_tick", "historical_rewrite", "chapter_extraction", "plan_feasibility", "band_planning"
]
ConflictViolation = Literal[
    "protection", "milestone", "reveal_schedule", "feasibility", "onstage_slip",
    "impact_review", "replay_cas",
]
ConflictOption = Literal[
    "veto_world", "rewrite_chapter", "amend_plan", "plot_responds", "accept_with_obligation"
]
ConflictDecider = Literal["auto", "codex", "human"]


class ConflictDraft(BaseModel):
    project_id: str
    source: ConflictSource
    origin_key: str = ""
    detected_after_chapter: int
    story_time: int
    involved_refs: list[str]
    violated: ConflictViolation
    impact_level: str
    summary: str
    proposed_change: dict[str, Any]
    intent_ref: str = ""
    actor_ref: str = ""
    blocks_from_chapter: int

    @field_validator("impact_level")
    @classmethod
    def validate_impact_level(cls, value: str) -> str:
        if value not in IMPACT_LEVELS:
            raise ValueError(f"impact_level must be one of {IMPACT_LEVELS}")
        return value


class ConflictDecision(BaseModel):
    option: ConflictOption
    decided_by: ConflictDecider
    actor_id: str = ""
    rationale: str
    # Why the decider chose it, as the operator states it; empty for codex and auto decisions.
    reason: str = ""
    decided_at: datetime
