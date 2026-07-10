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
)
from forwin.protocol.subworld import SubWorldSummary


class GenerateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    premise: str
    genre: str = "玄幻"
    num_chapters: int = 3
    project_id: str | None = None
    model_profile_id: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


class LLMSettingsRequest(BaseModel):
    api_key: str = ""
    base_url: str = DEFAULT_MINIMAX_BASE_URL
    model: str = DEFAULT_MINIMAX_MODEL


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
    pass


class LLMSettingsResponse(BaseModel):
    has_api_key: bool
    base_url: str
    model: str
    profiles: list[ModelProfile] = Field(default_factory=list)
    default_profile_id: str = ""
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
