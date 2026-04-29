from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _PersonalityModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PersonalitySkillRef(_PersonalityModel):
    skill: str
    weight: float = Field(default=1.0, ge=0.0, le=1.0)
    active_when: list[str] = Field(default_factory=list)
    trigger: list[str] = Field(default_factory=list)
    target: str = ""

    @field_validator("skill")
    @classmethod
    def _skill_required(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("skill is required")
        return normalized

    @field_validator("active_when", "trigger", mode="before")
    @classmethod
    def _string_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()] if str(value).strip() else []


class PersonalityLoadout(_PersonalityModel):
    dominant: PersonalitySkillRef | None = None
    secondary: list[PersonalitySkillRef] = Field(default_factory=list)
    social_mask: list[PersonalitySkillRef] = Field(default_factory=list)
    stress_modes: list[PersonalitySkillRef] = Field(default_factory=list)
    relationship_patterns: list[PersonalitySkillRef] = Field(default_factory=list)
    overrides: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        if "stress_modes" not in payload and "stress_mode" in payload:
            payload["stress_modes"] = payload.get("stress_mode")
        return payload

    def active_skill_ids(self) -> set[str]:
        ids: set[str] = set()
        if self.dominant is not None:
            ids.add(self.dominant.skill)
        for group in (
            self.secondary,
            self.social_mask,
            self.stress_modes,
            self.relationship_patterns,
        ):
            ids.update(item.skill for item in group)
        return ids


class ActivePersonalitySkills(_PersonalityModel):
    dominant: list[str] = Field(default_factory=list)
    secondary: list[str] = Field(default_factory=list)
    social_mask: list[str] = Field(default_factory=list)
    stress_mode: list[str] = Field(default_factory=list)
    relationship_pattern: list[str] = Field(default_factory=list)


class PersonalityBehaviorBias(_PersonalityModel):
    perception: list[str] = Field(default_factory=list)
    decision: list[str] = Field(default_factory=list)
    dialogue: list[str] = Field(default_factory=list)
    body_language: list[str] = Field(default_factory=list)
    relationship_behavior: list[str] = Field(default_factory=list)
    stress_behavior: list[str] = Field(default_factory=list)


class ActivePersonalityContext(_PersonalityModel):
    character_id: str
    character_name: str = ""
    active_skills: ActivePersonalitySkills = Field(default_factory=ActivePersonalitySkills)
    current_behavior_bias: PersonalityBehaviorBias = Field(default_factory=PersonalityBehaviorBias)
    constraints: list[str] = Field(default_factory=list)


class PersonalitySkillInfo(_PersonalityModel):
    name: str
    version: str = "1.0.0"
    description: str = ""
    skill_type: str = ""
    path: str = ""
    skill_hash: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    body: str = ""
    incomplete: bool = False

    def catalog_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "skill_type": self.skill_type,
            "path": self.path,
            "hash": self.skill_hash,
            "metadata": self.metadata,
            "incomplete": self.incomplete,
        }


class CharacterPersonalityPolicy(_PersonalityModel):
    policy_version: str = "character_personality_assignment.v1"
    require_named_loadout: bool = True
    strict_integrity: bool = False
    low_confidence_blocks_core: bool = False
    allow_background_minimal_loadout: bool = True
    preserve_manual_override: bool = True
    fallback_character_class: str = "named_supporting_character"
    allow_llm_assisted_suggestion: bool = False
    cast_diversity_enabled: bool = True
    cast_diversity_adjustment: float = 8.0
    relationship_enrichment_enabled: bool = True


class PersonalityAssignmentRequest(_PersonalityModel):
    project_id: str
    character_id: str = ""
    character_name: str = ""
    source: str = ""
    source_ref: str = ""
    description: str = ""
    summary: str = ""
    role_hint: str = ""
    narrative_role: str = ""
    public_identity: str = ""
    role_archetype: str = ""
    culture_tag: str = ""
    origin_faction_id: str = ""
    faction_id: str = ""
    goal: str = ""
    long_term_goal: str = ""
    relationship_summary: str = ""
    personality_tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    genre: str = ""
    importance: int = 5
    character_class: str = "supporting_named_character"
    explicit_loadout: dict[str, Any] | None = None
    existing_loadout: dict[str, Any] | None = None
    existing_assignment: dict[str, Any] | None = None
    existing_cast_loadouts: list[dict[str, Any]] = Field(default_factory=list)
    policy: CharacterPersonalityPolicy = Field(default_factory=CharacterPersonalityPolicy)


class SelectedPersonalitySkillReport(_PersonalityModel):
    skill: str
    slot: str
    weight: float = 0.0
    score: float = 0.0
    reason_tags: list[str] = Field(default_factory=list)


class CandidatePersonalitySkillReport(_PersonalityModel):
    skill: str
    skill_type: str = ""
    score: float = 0.0
    eligible_slots: list[str] = Field(default_factory=list)
    reason_tags: list[str] = Field(default_factory=list)


class RejectedPersonalitySkillReport(_PersonalityModel):
    skill: str
    reason: str
    score: float = 0.0


class PersonalityAssignmentReport(_PersonalityModel):
    assignment_id: str
    policy_version: str = "character_personality_assignment.v1"
    assignment_mode: str = "auto_rule"
    source: str = ""
    source_ref: str = ""
    assigned_at: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: str = "valid"
    manual_override: bool = False
    preserved_existing_loadout: bool = False
    selected_skills: list[SelectedPersonalitySkillReport] = Field(default_factory=list)
    candidate_skills: list[CandidatePersonalitySkillReport] = Field(default_factory=list)
    rejected_skills: list[RejectedPersonalitySkillReport] = Field(default_factory=list)
    signal_summary: dict[str, Any] = Field(default_factory=dict)
    reason_tags: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PersonalityAssignmentValidationReport(_PersonalityModel):
    ok: bool = True
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    unknown_skill_ids: list[str] = Field(default_factory=list)


class PersonalityAssignmentResult(_PersonalityModel):
    loadout: PersonalityLoadout
    report: PersonalityAssignmentReport
    validation: PersonalityAssignmentValidationReport


class PersonalityAssignmentPreview(PersonalityAssignmentResult):
    pass
