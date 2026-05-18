from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from forwin.config import DEFAULT_MINIMAX_BASE_URL, DEFAULT_MINIMAX_MODEL
from forwin.governance import (
    BandCheckpointDetail,
    BlockingReasonInfo,
    DecisionEventInfo,
    NarrativeConstraintInfo,
    PlanTaskItem,
    ProjectGovernanceSettings,
)
from forwin.protocol.subworld import SubWorldSummary


class GenerateRequest(BaseModel):
    premise: str
    genre: str = "玄幻"
    num_chapters: int = 3
    project_id: str | None = None
    model_profile_id: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    operation_mode: str | None = None
    freeze_failed_candidates: bool | None = None
    min_chapter_chars: int | None = None
    review_interval_chapters: int | None = None
    progression_mode: str | None = None
    auto_band_checkpoint: bool | None = None
    band_warn_action: str | None = None
    manual_checkpoints_enabled: bool | None = None
    future_constraints_enabled: bool | None = None
    generation_audit_interval_chapters: int | None = Field(default=None, ge=0)
    generation_audit_pause_enabled: bool | None = None


class LLMSettingsRequest(BaseModel):
    api_key: str = ""
    base_url: str = DEFAULT_MINIMAX_BASE_URL
    model: str = DEFAULT_MINIMAX_MODEL
    operation_mode: str = "blackbox"
    freeze_failed_candidates: bool = True
    min_chapter_chars: int = 2500
    review_interval_chapters: int = 0
    progression_mode: str = "serial_canon_band_guard"
    auto_band_checkpoint: bool = True
    band_warn_action: str = "pause"
    manual_checkpoints_enabled: bool = True
    future_constraints_enabled: bool = True
    generation_audit_interval_chapters: int = 0
    generation_audit_pause_enabled: bool = False


class ModelProfile(BaseModel):
    id: str
    name: str
    has_api_key: bool
    base_url: str
    model: str


class LLMProfileUpsertRequest(BaseModel):
    profile_id: str | None = None
    name: str
    api_key: str = ""
    base_url: str = DEFAULT_MINIMAX_BASE_URL
    model: str = DEFAULT_MINIMAX_MODEL
    set_as_default: bool = False


class LLMDefaultProfileRequest(BaseModel):
    profile_id: str


class LLMPreferencesRequest(BaseModel):
    operation_mode: str = "blackbox"
    freeze_failed_candidates: bool = True
    min_chapter_chars: int = 2500
    review_interval_chapters: int = 0
    progression_mode: str = "serial_canon_band_guard"
    auto_band_checkpoint: bool = True
    band_warn_action: str = "pause"
    manual_checkpoints_enabled: bool = True
    future_constraints_enabled: bool = True
    generation_audit_interval_chapters: int = 0
    generation_audit_pause_enabled: bool = False


class LLMSettingsResponse(BaseModel):
    has_api_key: bool
    base_url: str
    model: str
    profiles: list[ModelProfile] = Field(default_factory=list)
    default_profile_id: str = ""
    operation_mode: str = "blackbox"
    freeze_failed_candidates: bool = True
    min_chapter_chars: int = 2500
    review_interval_chapters: int = 0
    progression_mode: str = "serial_canon_band_guard"
    auto_band_checkpoint: bool = True
    band_warn_action: str = "pause"
    manual_checkpoints_enabled: bool = True
    future_constraints_enabled: bool = True
    generation_audit_interval_chapters: int = 0
    generation_audit_pause_enabled: bool = False
    message: str = ""


__all__ = [
    'GenerateRequest',
    'LLMSettingsRequest',
    'ModelProfile',
    'LLMProfileUpsertRequest',
    'LLMDefaultProfileRequest',
    'LLMPreferencesRequest',
    'LLMSettingsResponse',
]
