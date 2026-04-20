from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SubWorldScope = Literal["global_core", "arc_local"]
SubWorldStatus = Literal["active", "retired"]
RosterStatus = Literal["seeded_named", "planned_slot", "activated_named", "retired"]


class SubWorldSummary(BaseModel):
    id: str = ""
    name: str = ""
    purpose: str = ""
    scope: SubWorldScope = "arc_local"
    status: SubWorldStatus = "active"
    active_in_current_band: bool = False
    core_cast: list[str] = Field(default_factory=list)
    planned_slot_count: int = 0


class SubWorldCharacterSeed(BaseModel):
    name: str
    description: str = ""
    role_hint: str = ""
    aliases: list[str] = Field(default_factory=list)
    importance: int = 5
    initial_state: dict = Field(default_factory=dict)


class SubWorldSlotPlan(BaseModel):
    slot_key: str
    role_hint: str = ""
    description: str = ""


class SubWorldRegionSeed(BaseModel):
    name: str
    level: int = 1
    kind: str = "local_region"
    parent_region_name: str = ""
    summary: str = ""
    culture_traits: list[str] = Field(default_factory=list)
    climate: str = ""
    terrain: list[str] = Field(default_factory=list)
    controller_factions: list[str] = Field(default_factory=list)


class SubWorldPlanItem(BaseModel):
    subworld_id: str = ""
    parent_subworld_id: str = ""
    name: str
    purpose: str = ""
    scope: SubWorldScope = "arc_local"
    chapter_window_hint: str = ""
    core_named_characters: list[SubWorldCharacterSeed] = Field(default_factory=list)
    planned_slots: list[SubWorldSlotPlan] = Field(default_factory=list)
    region_seeds: list[SubWorldRegionSeed] = Field(default_factory=list)


class SubWorldPlanDelta(BaseModel):
    reuse_subworld_ids: list[str] = Field(default_factory=list)
    retire_subworld_ids: list[str] = Field(default_factory=list)
    new_subworlds: list[SubWorldPlanItem] = Field(default_factory=list)
    initial_active_subworld_ids: list[str] = Field(default_factory=list)


class ChapterEntryTarget(BaseModel):
    chapter_hint: int = 0
    entity_name: str = ""
    subworld_id: str = ""
    role_hint: str = ""


class EntityMention(BaseModel):
    entity_name: str = ""
    entity_kind: str = "character"
    is_named: bool = True
    is_on_stage: bool = True
    evidence_refs: list[str] = Field(default_factory=list)
