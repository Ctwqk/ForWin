from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from forwin.personality.models import PersonalityAssignmentReport


class CharacterCreationRequest(BaseModel):
    project_id: str
    source: str
    source_ref: str = ""
    character_id: str = ""
    legacy_entity_id: str = ""
    roster_item_id: str = ""
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    summary: str = ""
    importance: int = 5
    created_at_chapter: int = 0
    profile: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
    relation_seed: dict[str, Any] = Field(default_factory=dict)
    roster_context: dict[str, Any] = Field(default_factory=dict)
    creation_context: dict[str, Any] = Field(default_factory=dict)
    personality_loadout: dict[str, Any] | None = None
    personality_tags: list[str] = Field(default_factory=list)
    personality_policy: str = "auto"
    existing_resolution: str = "get_or_create"
    generic_character_policy: str = "reject_or_group"
    audit_reason: str = ""
    create_legacy_entity: bool = True


class CharacterCreationResult(BaseModel):
    project_id: str
    character_id: str = ""
    character_name: str = ""
    created: bool = False
    merged_existing: bool = False
    world_node: dict[str, Any] = Field(default_factory=dict)
    legacy_entity_id: str = ""
    roster_item_id: str = ""
    personality_loadout: dict[str, Any] = Field(default_factory=dict)
    personality_assignment: PersonalityAssignmentReport | dict[str, Any] = Field(default_factory=dict)
    integrity_report: Any = None
    decision_event_ids: list[str] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)


RosterMaterializationRequest = CharacterCreationRequest
LegacyCharacterImportRequest = CharacterCreationRequest
BookStateCharacterPatchRequest = CharacterCreationRequest
